"""Historial de conversación en Redis.

El Agents SDK trae `SQLiteSession` de fábrica y, según versión, un
`RedisSession` en `agents.extensions.memory`. Para no depender de la versión
implementamos el protocolo `Session` a mano (son cuatro métodos).

Esto reemplaza `Memoria Conversacion1` (memoryBufferWindow) de n8n, que perdía
el hilo del pedido a medio hacer.
"""
from __future__ import annotations

import json
from typing import Any

from app.logging_conf import get_logger
from app.redis_client import run_write, with_reconnect
from app.settings import settings

log = get_logger(__name__)


class RedisSession:
    """Lista Redis con los items del historial (JSON por elemento).

    Cumple el protocolo `agents.memory.Session`.
    """

    def __init__(
        self,
        session_id: str,
        *,
        ttl: int | None = None,
        max_items: int | None = None,
    ) -> None:
        self.session_id = session_id
        self._ttl = ttl if ttl is not None else settings.session_ttl_seconds
        self._max = max_items if max_items is not None else settings.session_max_items
        self._key = settings.key("session", session_id)

    async def get_items(self, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is None:
            raw = await with_reconnect(lambda r: r.lrange(self._key, 0, -1))
        else:
            raw = await with_reconnect(lambda r: r.lrange(self._key, -limit, -1))
        out: list[dict[str, Any]] = []
        for item in raw:
            try:
                out.append(json.loads(item))
            except json.JSONDecodeError:
                continue
        return out

    async def add_items(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        payload = [json.dumps(i, ensure_ascii=False, default=str) for i in items]

        async def _op(r: Any) -> None:
            pipe = r.pipeline()
            pipe.rpush(self._key, *payload)
            pipe.ltrim(self._key, -self._max, -1)
            pipe.expire(self._key, self._ttl)
            await pipe.execute()

        # Escritura NO idempotente: un solo intento para no duplicar el historial.
        await run_write(_op)

    async def pop_item(self) -> dict[str, Any] | None:
        raw = await run_write(lambda r: r.rpop(self._key))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def clear_session(self) -> None:
        await with_reconnect(lambda r: r.delete(self._key))
