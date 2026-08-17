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

# Qué eventos disparan una notificación por TELEGRAM. Solo FALLOS técnicos.
# Las alertas de MEJORA (sugerencias del bot) las notifica el analista aparte
# (app/panel/analista.py). El resto (handoff, revision, etc.) sigue en el panel
# pero NO satura Telegram.
KINDS_TELEGRAM = {"error"}


async def publicar(kind: str, chat_id: str, **data: Any) -> dict:
    """Registra un evento y, si es alerta, notifica por Telegram."""
    eid = await run_write(lambda r: r.incr(SEQ_KEY))
    if eid is None:
        eid = int(time.time() * 1000)

    # Canal (número nuestro por el que entró la conversación): el panel se divide
    # por canal, así que cada evento debe saber a cuál pertenece. Si quien publica
    # no lo pasó, se toma de la meta del chat en vez de exigirlo en cada llamada.
    if not data.get("emisor") and chat_id and chat_id != "-":
        try:
            data["emisor"] = (await leer_chatmeta(chat_id)).get("emisor", "") or ""
        except Exception:  # noqa: BLE001
            data["emisor"] = ""

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

    if kind in KINDS_TELEGRAM:
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
    ultimo_de: str = "cliente",  # cliente | bot | asesor
    ad_id: str = "",
    ad_headline: str = "",
    ad_producto: str = "",
    score: int | None = None,
    score_sem: str = "",
    score_hitos: list[str] | None = None,
) -> None:
    # SIEMPRE se lee lo previo y se MEZCLA (prev + lo nuevo). Antes el dict se rearmaba
    # de cero y había que acordarse de preservar campo por campo: cada dato nuevo
    # (anuncio, semáforo, aprobación del pago…) era una oportunidad de borrar en
    # silencio algo del cliente. Mezclando, lo que no se pasa sobrevive solo.
    prev = await leer_chatmeta(chat_id)
    ad_id = ad_id or prev.get("ad_id", "") or ""
    ad_headline = ad_headline or prev.get("ad_headline", "") or ""
    ad_producto = ad_producto or prev.get("ad_producto", "") or ""
    user_name = user_name or prev.get("user_name", "") or ""
    telefono = telefono or prev.get("telefono", "") or ""
    emisor = emisor or prev.get("emisor", "") or ""
    destino = destino or prev.get("destino") or None
    if score is None:
        score = prev.get("score")
        score_sem = score_sem or prev.get("score_sem", "") or ""
        if score_hitos is None:
            score_hitos = prev.get("score_hitos") or None

    meta = {
        **prev,
        "chat_id": chat_id,
        "emisor": emisor,
        "destino": destino or {"to": chat_id},
        "user_name": user_name,
        "telefono": telefono,
        # `ultimo` es el ÚLTIMO mensaje de la conversación, venga del cliente, del bot
        # o de un asesor, y `ultimo_de` dice quién lo dijo: la lista de chats mostraba
        # sólo lo del cliente, así que no se veía qué había contestado el bot.
        "ultimo": ultimo[:200],
        "ultimo_de": ultimo_de or "cliente",
        "ultimo_ts": time.time(),
        "ad_id": ad_id,
        "ad_producto": ad_producto,
        "ad_headline": ad_headline,
        # Semáforo de cierre (app/score.py). Se guarda el valor ABSOLUTO recalculado,
        # nunca un incremento: esta escritura usa `with_reconnect`, que reintenta, y un
        # delta se contaría dos veces.
        "score": score,
        "score_sem": score_sem,
        "score_hitos": list(score_hitos or []),
    }

    async def _op(r: Any) -> None:
        await r.hset(CHATMETA_KEY, chat_id, json.dumps(meta, ensure_ascii=False))

    try:
        await with_reconnect(_op)
    except Exception as exc:  # noqa: BLE001
        log.warning("chatmeta_no_guardado", error=str(exc))


async def guardar_aprobacion(
    chat_id: str,
    estado: str,
    order_id: int | None = None,
    motivo: str = "",
    modalidad: str = "",
    con_pago: bool | None = None,
) -> dict:
    """Estado de aprobación del pedido por el supervisor.

    El bot nunca le da el número al cliente por su cuenta: deja el pedido "pendiente" y
    avisa que se está revisando. Sólo una persona lo aprueba, y recién ahí el cliente
    recibe la confirmación con el número.

    `modalidad` y `con_pago` viajan acá porque de ellos depende QUÉ se le escribe al
    cliente al aprobar: "tu pago fue verificado" sólo aplica si hubo un pago.

    estado: "pendiente" | "aprobado" | "rechazado".
    """
    meta = await leer_chatmeta(chat_id)
    previa = meta.get("aprobacion") or {}
    apro = {
        "estado": estado,
        "order_id": order_id if order_id is not None else previa.get("order_id"),
        "motivo": motivo[:200],
        # Al aprobar/rechazar no se vuelven a pasar: se conservan los de la pendiente.
        "modalidad": modalidad or previa.get("modalidad", "") or "",
        "con_pago": previa.get("con_pago") if con_pago is None else bool(con_pago),
        "ts": time.time(),
    }
    meta["aprobacion"] = apro

    async def _op(r: Any) -> None:
        await r.hset(CHATMETA_KEY, chat_id, json.dumps(meta, ensure_ascii=False))

    try:
        await with_reconnect(_op)
    except Exception as exc:  # noqa: BLE001
        log.warning("aprobacion_no_guardada", chat_id=chat_id, error=str(exc))
    return apro


async def guardar_score(
    chat_id: str, score: int | None, sem: str, hitos: list[str]
) -> bool:
    """Escribe SÓLO el semáforo de un chat, sin tocar nada más.

    No se usa `tocar_chatmeta` a propósito: esa función reescribe `ultimo` y pisa
    `ultimo_ts` con la hora actual, así que recalcular el semáforo de las
    conversaciones viejas las haría aparecer todas como "ahora" y reordenaría la lista.
    """
    meta = await leer_chatmeta(chat_id)
    meta.update({"score": score, "score_sem": sem, "score_hitos": list(hitos or [])})

    async def _op(r: Any) -> None:
        await r.hset(CHATMETA_KEY, chat_id, json.dumps(meta, ensure_ascii=False))

    try:
        await with_reconnect(_op)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("score_no_guardado", chat_id=chat_id, error=str(exc))
        return False


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


async def borrar_chatmeta(chat_id: str) -> None:
    """Quita la conversación del índice del panel (no toca Odoo)."""
    async def _op(r: Any) -> None:
        await r.hdel(CHATMETA_KEY, chat_id)

    try:
        await with_reconnect(_op)
    except Exception as exc:  # noqa: BLE001
        log.warning("chatmeta_no_borrado", error=str(exc))


async def todos_chatmeta(estricto: bool = False) -> dict[str, dict]:
    """Índice de conversaciones del panel.

    `estricto=True` propaga el fallo de Redis en vez de devolver {}: un índice vacío
    por error se veía EXACTAMENTE igual que "no hay conversaciones", y quien opera no
    tenía forma de distinguirlo.
    """
    async def _op(r: Any) -> dict:
        return await r.hgetall(CHATMETA_KEY)

    try:
        raw = await with_reconnect(_op)
    except Exception:  # noqa: BLE001
        if estricto:
            raise
        return {}
    out: dict[str, dict] = {}
    for k, v in (raw or {}).items():
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            continue
    return out
