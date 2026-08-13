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


def _emparejar_tools(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Quita tool-calls huérfanos que rompen la API de OpenAI.

    El recorte del historial (ltrim a max_items) puede cortar entre un
    `function_call` y su `function_call_output`, dejando uno sin el otro. Si ese
    huérfano llega al modelo, la API responde 400 "No tool call found for function
    call output with call_id ...". Aquí conservamos SOLO los pares completos (la
    call y su output ambos presentes); los mensajes normales no se tocan. Sanea
    tanto historiales nuevos como los que ya quedaron corruptos en Redis.
    """
    calls: set[str] = set()
    outs: set[str] = set()
    for it in items:
        cid = it.get("call_id")
        if not cid:
            continue
        tipo = it.get("type")
        if tipo == "function_call":
            calls.add(cid)
        elif tipo == "function_call_output":
            outs.add(cid)
    completos = calls & outs
    limpio: list[dict[str, Any]] = []
    for it in items:
        if it.get("type") in ("function_call", "function_call_output"):
            if it.get("call_id") in completos:
                limpio.append(it)
            # huérfano -> se descarta
        else:
            limpio.append(it)
    return limpio


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
        return _emparejar_tools(out)

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

    async def items_con_indice(self) -> list[dict[str, Any]]:
        """Items CRUDOS con su índice real en Redis (`_idx`), para poder editarlos.

        `get_items` devuelve la lista SANEADA (sin tool-calls huérfanos), así que sus
        posiciones no coinciden con las de Redis: editar por esa posición tocaría el
        mensaje equivocado. El panel usa ésta.
        """
        raw = await with_reconnect(lambda r: r.lrange(self._key, 0, -1))
        out: list[dict[str, Any]] = []
        for i, item in enumerate(raw):
            try:
                obj = json.loads(item)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                obj["_idx"] = i
                out.append(obj)
        return out

    async def editar_item(self, indice: int, contenido: str) -> bool:
        """Reescribe el contenido del mensaje `indice`. True si se pudo."""
        raw = await with_reconnect(lambda r: r.lindex(self._key, indice))
        if not raw:
            return False
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return False
        obj["content"] = contenido
        obj["editado"] = True
        payload = json.dumps(obj, ensure_ascii=False, default=str)
        await with_reconnect(lambda r: r.lset(self._key, indice, payload))
        return True

    async def borrar_item(self, indice: int) -> bool:
        """Borra el mensaje `indice`. Redis no borra por posición: se marca con un
        centinela y se elimina por valor (patrón estándar LSET + LREM)."""
        centinela = "__BORRADO_PANEL__"
        try:
            await with_reconnect(lambda r: r.lset(self._key, indice, centinela))
        except Exception:  # noqa: BLE001  (índice fuera de rango)
            return False
        await with_reconnect(lambda r: r.lrem(self._key, 1, centinela))
        return True

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
