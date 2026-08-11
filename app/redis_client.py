"""Conexión Redis única para todo el servicio.

Redis cumple CUATRO roles acá (todos sobre la misma instancia, distinto prefijo):
  1. Historial de conversación (sesión del agente)  -> session:{chat_id}
  2. Debounce / buffer de mensajes en ráfaga        -> buffer:{chat_id}, last_id:{chat_id}
  3. Estado operativo (bot pausado, ventana 24h)    -> bot_disabled:{chat_id}, window_24h:{chat_id}
  4. Config de negocio editable + mapa de anuncios  -> config, ads_map
"""
from __future__ import annotations

import contextlib
import uuid
from typing import AsyncIterator

import redis.asyncio as aioredis
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry

from app.settings import settings

_pool: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _pool
    if _pool is None:
        # Redis remoto (p.ej. Redis Cloud) puede cortar conexiones ociosas y
        # tirar el TCP a mitad de una operación. keepalive + reintentos con
        # backoff hacen que reconecte solo en vez de reventar la conversación.
        _pool = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            health_check_interval=30,
            socket_keepalive=True,
            socket_timeout=15,
            socket_connect_timeout=15,
            retry=Retry(ExponentialBackoff(base=0.2, cap=3.0), retries=3),
            retry_on_error=[RedisConnectionError, RedisTimeoutError],
        )
    return _pool


async def close_redis() -> None:
    global _pool
    if _pool is not None:
        with contextlib.suppress(Exception):
            await _pool.aclose()
        _pool = None


async def with_reconnect(op, attempts: int = 3):
    """Ejecuta `op(redis)` y, si el remoto cortó la conexión, descarta el pool
    envenenado y reintenta con un cliente fresco.

    Necesario con Redis remoto (Redis Cloud): corta conexiones ociosas durante
    los turnos lentos del agente y el reintento a nivel de socket no siempre
    reconecta; recrear el pool sí (un cliente nuevo siempre reconecta).
    """
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return await op(get_redis())
        except (RedisConnectionError, RedisTimeoutError, OSError) as exc:
            last = exc
            await close_redis()  # el próximo get_redis() crea un pool nuevo
    assert last is not None
    raise last


async def run_write(op):
    """Escritura NO idempotente (lpush, rpush, incr…): UN solo intento.

    Si el remoto corta al leer la respuesta, el comando ya se ejecutó en Redis;
    reintentar duplicaría datos (historial, eventos). Por eso no reintentamos:
    solo descartamos el pool envenenado para que la próxima operación reconecte.
    Devuelve el resultado, o None si falló (la próxima ya reconecta).
    """
    try:
        return await op(get_redis())
    except (RedisConnectionError, RedisTimeoutError, OSError):
        await close_redis()
        return None


@contextlib.asynccontextmanager
async def conversation_lock(
    chat_id: str, ttl: int = 180
) -> AsyncIterator[bool]:
    """Evita que dos turnos de la MISMA conversación corran el agente a la vez.

    Devuelve True si se obtuvo el lock, False si ya había otro turno corriendo.
    (En n8n esto no existía: dos ejecuciones podían pisarse el buffer.)

    El TTL debe superar el peor turno posible (timeout del agente + envío con
    delay de tipeo) para que el lock nunca expire a mitad de un turno vivo y
    deje entrar un segundo procesamiento en paralelo.
    """
    key = settings.key("lock", chat_id)
    token = uuid.uuid4().hex
    degradado = False  # True = blip de Redis: no hay lock real que soltar
    try:
        got = await get_redis().set(key, token, nx=True, ex=ttl)
    except (RedisConnectionError, RedisTimeoutError, OSError):
        # Blip de Redis al tomar el lock: mejor procesar el turno (degradar) que
        # perderlo. El riesgo de doble-proceso es menor que dejar al cliente sin
        # respuesta.
        await close_redis()
        got = True
        degradado = True
    try:
        yield bool(got)
    finally:
        # Liberar solo si de verdad tomamos el lock (no en el caso degradado) y
        # solo si el token sigue siendo el nuestro. Antes esto usaba una variable
        # `r` inexistente: el NameError quedaba tragado por suppress y el lock
        # NUNCA se liberaba, sobreviviendo hasta expirar por TTL.
        if got and not degradado:
            script = (
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end"
            )
            with contextlib.suppress(Exception):
                await get_redis().eval(script, 1, key, token)
