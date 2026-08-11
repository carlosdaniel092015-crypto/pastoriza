"""Debounce: agrupa la ráfaga de mensajes cortos del cliente en un solo turno.

Reemplaza los 6 nodos de n8n (`Leer Buffer` -> `Acumular` -> `Guardar` ->
`Push last_id` -> `Wait 6s` -> `Switch dedup`). Diferencia importante: en n8n
cada mensaje mantenía una EJECUCIÓN abierta 6 segundos; acá el request se
responde en milisegundos y la espera ocurre en una task de asyncio.

Multi-worker: el chequeo de `last_id` se hace contra Redis, así que si otro
worker recibió un mensaje más nuevo, esta task se descarta sola.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict

from app.logging_conf import get_logger
from app.models import InboundMessage
from app.redis_client import get_redis
from app.settings import settings

log = get_logger(__name__)


def _k_buffer(chat_id: str) -> str:
    return settings.key("buffer", chat_id)


def _k_last(chat_id: str) -> str:
    return settings.key("last_id", chat_id)


def _k_first_ts(chat_id: str) -> str:
    return settings.key("first_ts", chat_id)


async def acumular(msg: InboundMessage) -> None:
    """Guarda el mensaje en el buffer y marca este message_id como el más reciente."""
    r = get_redis()
    ttl = int(settings.debounce_max_wait + settings.debounce_seconds + 30)
    pipe = r.pipeline()
    pipe.rpush(_k_buffer(msg.chat_id), json.dumps(asdict(msg), default=str))
    pipe.expire(_k_buffer(msg.chat_id), ttl)
    pipe.set(_k_last(msg.chat_id), msg.message_id, ex=ttl)
    pipe.set(_k_first_ts(msg.chat_id), str(time.time()), ex=ttl, nx=True)
    await pipe.execute()


async def esperar_turno(msg: InboundMessage) -> bool:
    """Espera la ventana de debounce. True si este mensaje es el que dispara el turno.

    Si mientras esperábamos llegó otro mensaje, este mensaje "pierde" y se
    descarta: el mensaje nuevo se encargará de procesar todo el bloque.
    """
    r = get_redis()
    await asyncio.sleep(settings.debounce_seconds)

    ultimo = await r.get(_k_last(msg.chat_id))
    if ultimo == msg.message_id:
        return True

    # Techo duro: si el cliente escribe sin parar, procesamos igual pasado el máximo.
    try:
        primero = float(await r.get(_k_first_ts(msg.chat_id)) or 0)
    except (TypeError, ValueError):
        primero = 0.0
    if primero and (time.time() - primero) > settings.debounce_max_wait:
        log.info("debounce_techo_alcanzado", chat_id=msg.chat_id)
        return True

    return False


async def drenar(chat_id: str) -> list[InboundMessage]:
    """Saca todos los mensajes acumulados y limpia el buffer (atómico)."""
    r = get_redis()
    pipe = r.pipeline()
    pipe.lrange(_k_buffer(chat_id), 0, -1)
    pipe.delete(_k_buffer(chat_id))
    pipe.delete(_k_first_ts(chat_id))
    resultados = await pipe.execute()

    msgs: list[InboundMessage] = []
    for raw in resultados[0] or []:
        try:
            msgs.append(InboundMessage(**json.loads(raw)))
        except Exception as exc:  # noqa: BLE001
            log.warning("buffer_item_corrupto", error=str(exc))
    return msgs
