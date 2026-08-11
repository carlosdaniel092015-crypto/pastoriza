"""Detección de mensajes repetidos: si el cliente pregunta lo mismo 3 veces,
se pasa al supervisor (señal de frustración o de intento de abuso).

El estado vive en Redis por chat (`repeticion:{chat_id}`) con TTL corto, para
contar repeticiones consecutivas dentro de una misma sesión de conversación.
"""
from __future__ import annotations

import difflib
import json
from typing import Any

from app.logging_conf import get_logger
from app.matching import quitar_tildes
from app.redis_client import run_write, with_reconnect
from app.settings import settings

log = get_logger(__name__)

TTL = 3600  # 1h: repeticiones "consecutivas" dentro de la misma conversación
UMBRAL = 0.82  # similitud (0-1) para considerar dos mensajes "lo mismo"
MIN_LARGO = 3  # ignora mensajes triviales/vacíos
LIMITE = 3  # a la 3ra repetición, al supervisor


def normalizar(texto: str) -> str:
    return " ".join(quitar_tildes(str(texto or "")).lower().split())


def son_lo_mismo(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= UMBRAL


async def contar_repeticion(chat_id: str, texto: str) -> int:
    """Devuelve cuántas veces seguidas el cliente ha mandado ~lo mismo (incluye esta)."""
    norm = normalizar(texto)
    if len(norm) < MIN_LARGO:
        return 1
    key = settings.key("repeticion", chat_id)

    prev: dict | None = None
    try:
        raw = await with_reconnect(lambda r: r.get(key))
        prev = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        prev = None

    count = 1
    if prev and son_lo_mismo(norm, str(prev.get("norm", ""))):
        count = int(prev.get("count", 1)) + 1

    async def _op(r: Any) -> None:
        await r.set(key, json.dumps({"norm": norm, "count": count}), ex=TTL)

    await run_write(_op)
    return count


async def reset(chat_id: str) -> None:
    await run_write(lambda r: r.delete(settings.key("repeticion", chat_id)))
