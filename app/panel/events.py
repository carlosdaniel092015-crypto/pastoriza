"""Feed de eventos del panel: turnos, pedidos, escalamientos y errores.

Todo va a una lista Redis capada (`pastoriza:panel:events`), con un id
incremental para que el navegador pida sólo lo nuevo (polling). Los eventos de
tipo alerta/error además disparan notificación por Telegram.

Metadatos por chat (`pastoriza:panel:chatmeta`) guardan lo necesario para que el
supervisor pueda responder manualmente (emisor/destino de YCloud) sin depender
de tener a mano el último webhook.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.logging_conf import get_logger
from app.panel import telegram
from app.redis_client import run_write, with_reconnect
from app.settings import settings

log = get_logger(__name__)

EVENTS_KEY = settings.key("panel", "events")
SEQ_KEY = settings.key("panel", "seq")
CHATMETA_KEY = settings.key("panel", "chatmeta")
MAX_EVENTS = 1000

# Tipos que se consideran alerta (badge en el panel + Telegram).
KINDS_ALERTA = {"error", "handoff", "revision", "comprobante_sin_pedido"}


async def publicar(kind: str, chat_id: str, **data: Any) -> dict:
    """Registra un evento y, si es alerta, notifica por Telegram."""
    eid = await run_write(lambda r: r.incr(SEQ_KEY))
    if eid is None:
        eid = int(time.time() * 1000)

    evt: dict[str, Any] = {
        "id": eid,
        "ts": time.time(),
        "kind": kind,
        "chat_id": chat_id,
        **data,
    }
    payload = json.dumps(evt, ensure_ascii=False, default=str)

    async def _push(r: Any) -> None:
        pipe = r.pipeline()
        pipe.lpush(EVENTS_KEY, payload)
        pipe.ltrim(EVENTS_KEY, 0, MAX_EVENTS - 1)
        await pipe.execute()

    await run_write(_push)  # 1 intento: no duplicar eventos

    if kind in KINDS_ALERTA:
        await _notificar(evt)
    return evt


async def _notificar(evt: dict) -> None:
    if not telegram.configurado():
        return
    icono = {"error": "🔴", "handoff": "🟠", "revision": "🟡"}.get(evt["kind"], "⚠️")
    motivos = evt.get("motivos") or evt.get("detalle") or ""
    if isinstance(motivos, list):
        motivos = ", ".join(motivos)
    texto = (
        f"{icono} <b>{evt['kind'].upper()}</b>\n"
        f"Cliente: {evt.get('user_name') or evt.get('chat_id')}\n"
        f"Chat: {evt.get('chat_id')}\n"
        f"{motivos}"
    )
    await telegram.enviar(texto)


async def listar(after: int = 0, limite: int = 200) -> list[dict]:
    """Eventos con id > after (más nuevos primero se re-ordenan a cronológico)."""
    async def _op(r: Any) -> list[str]:
        return await r.lrange(EVENTS_KEY, 0, limite - 1)

    try:
        raw = await with_reconnect(_op)
    except Exception:  # noqa: BLE001
        return []
    out: dict[int, dict] = {}
    for item in raw:
        try:
            e = json.loads(item)
        except json.JSONDecodeError:
            continue
        if e.get("id", 0) > after:
            out[e.get("id", 0)] = e  # dedupe por id (red de seguridad)
    return sorted(out.values(), key=lambda e: e.get("id", 0))


# ------------------------------------------------------------- chatmeta ---
async def tocar_chatmeta(
    chat_id: str,
    *,
    emisor: str = "",
    destino: dict | None = None,
    user_name: str = "",
    telefono: str = "",
    ultimo: str = "",
) -> None:
    meta = {
        "chat_id": chat_id,
        "emisor": emisor,
        "destino": destino or {"to": chat_id},
        "user_name": user_name,
        "telefono": telefono,
        "ultimo": ultimo[:200],
        "ultimo_ts": time.time(),
    }

    async def _op(r: Any) -> None:
        await r.hset(CHATMETA_KEY, chat_id, json.dumps(meta, ensure_ascii=False))

    try:
        await with_reconnect(_op)
    except Exception as exc:  # noqa: BLE001
        log.warning("chatmeta_no_guardado", error=str(exc))


async def leer_chatmeta(chat_id: str) -> dict:
    async def _op(r: Any) -> str | None:
        return await r.hget(CHATMETA_KEY, chat_id)

    try:
        raw = await with_reconnect(_op)
    except Exception:  # noqa: BLE001
        raw = None
    if not raw:
        return {"chat_id": chat_id, "destino": {"to": chat_id}, "emisor": ""}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"chat_id": chat_id, "destino": {"to": chat_id}, "emisor": ""}


async def todos_chatmeta() -> dict[str, dict]:
    async def _op(r: Any) -> dict:
        return await r.hgetall(CHATMETA_KEY)

    try:
        raw = await with_reconnect(_op)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict] = {}
    for k, v in (raw or {}).items():
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            continue
    return out
