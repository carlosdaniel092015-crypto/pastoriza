"""Estado operativo en Redis: pausa manual, ventana de 24h y cola de revisión.

La cola de revisión es la pieza que te saca de estar mirando todas las
conversaciones: sólo entra acá lo que necesita ojos humanos.
"""
from __future__ import annotations

import json
import time

from app.logging_conf import get_logger
from app.redis_client import get_redis, run_write, with_reconnect
from app.settings import settings

log = get_logger(__name__)

TTL_PAUSA = 1800  # 30 min, igual que en n8n
TTL_VENTANA = 90_000  # ~25 h
COLA_REVISION = "revision"
MAX_REVISION = 500


BOT_GLOBAL_KEY = settings.key("bot_global")  # presente = bot apagado globalmente


async def _escritura_idempotente(op, que: str, **ctx) -> bool:
    """SET/DELETE idempotentes: reintenta (with_reconnect), no como run_write.

    run_write hace UN intento y se TRAGA el error (devuelve None): con un blip de
    Redis, pausar el bot o el kill-switch fallaban en silencio y el bot seguía
    respondiéndole a un cliente que el supervisor ya había tomado. Reintentar es
    seguro porque estas escrituras son idempotentes (a diferencia de lpush).
    """
    try:
        await with_reconnect(op)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error(que + "_fallo", error=str(exc), **ctx)
        return False


async def set_bot_global(encendido: bool) -> bool:
    """Kill-switch global: enciende/apaga el bot para TODOS los clientes."""
    if encendido:
        ok = await _escritura_idempotente(
            lambda r: r.delete(BOT_GLOBAL_KEY), "bot_global"
        )
    else:
        ok = await _escritura_idempotente(
            lambda r: r.set(BOT_GLOBAL_KEY, "off"), "bot_global"
        )
    log.info("bot_global", encendido=encendido, ok=ok)
    return ok


async def bot_global_apagado() -> bool:
    try:
        return bool(await with_reconnect(lambda r: r.get(BOT_GLOBAL_KEY)))
    except Exception:  # noqa: BLE001
        return False


async def pausar_bot(chat_id: str) -> bool:
    ok = await _escritura_idempotente(
        lambda r: r.set(
            settings.key("bot_disabled", chat_id), "disabled-by-manager", ex=TTL_PAUSA
        ),
        "bot_pausar",
        chat_id=chat_id,
    )
    log.info("bot_pausado", chat_id=chat_id, ok=ok)
    return ok


async def reactivar_bot(chat_id: str) -> bool:
    ok = await _escritura_idempotente(
        lambda r: r.delete(settings.key("bot_disabled", chat_id)),
        "bot_reactivar",
        chat_id=chat_id,
    )
    log.info("bot_reactivado", chat_id=chat_id, ok=ok)
    return ok


async def bot_pausado(chat_id: str) -> bool:
    # Ante un blip de Redis, NO tumbar el turno: asumimos "no pausado" y seguimos.
    try:
        return bool(await with_reconnect(lambda r: r.get(settings.key("bot_disabled", chat_id))))
    except Exception:  # noqa: BLE001
        log.warning("bot_pausado_check_fallo", chat_id=chat_id)
        return False


async def pausados(chat_ids: list[str]) -> set[str]:
    """Cuáles de esos chats están en control humano, en UNA sola ida a Redis.

    El panel necesita el estado de TODAS las conversaciones en cada refresco: con
    un `get` por chat eran cientos de idas y vueltas por poll (lento para quien
    opera y carga inútil sobre el mismo Redis que usa el bot para atender).
    """
    ids = [c for c in chat_ids if c]
    if not ids:
        return set()
    claves = [settings.key("bot_disabled", c) for c in ids]
    try:
        valores = await with_reconnect(lambda r: r.mget(claves))
    except Exception:  # noqa: BLE001
        log.warning("pausados_check_fallo", chats=len(ids))
        return set()
    return {c for c, v in zip(ids, valores or []) if v}


TTL_MSG_BOT = 7200  # 2h: ventana para reconocer un mensaje como "enviado por el bot"


async def registrar_msg_bot(msg_id: str) -> None:
    """Marca un id de mensaje como enviado por el BOT (no por un humano)."""
    if not msg_id:
        return
    await _escritura_idempotente(
        lambda r: r.set(settings.key("bot_msg", msg_id), "1", ex=TTL_MSG_BOT),
        "registrar_msg_bot",
    )


async def es_msg_bot(msg_id: str) -> bool:
    """True si ese id lo envió el bot (para NO tratarlo como intervención humana)."""
    if not msg_id:
        return False
    try:
        return bool(await with_reconnect(lambda r: r.get(settings.key("bot_msg", msg_id))))
    except Exception:  # noqa: BLE001
        # Ante duda (blip de Redis), asumir que ES del bot: NO pausar por error.
        return True


# Lo cotizado sobrevive al turno porque el comprobante llega DESPUÉS: cuando el cliente
# manda la foto, la cotización ya pasó y `ConversationContext` arranca en cero. Sin esto
# no habría contra qué comparar el monto transferido. Mismo TTL que la sesión.
async def guardar_cotizacion(chat_id: str, total: float) -> None:
    """Guarda el total cotizado más ALTO del chat (el que hay que cubrir)."""
    if not chat_id or not total or total <= 0:
        return
    previo = await leer_cotizacion(chat_id)
    if total <= previo:
        return
    await _escritura_idempotente(
        lambda r: r.set(
            settings.key("cotizado", chat_id),
            f"{float(total):.2f}",
            ex=settings.session_ttl_seconds,
        ),
        "guardar_cotizacion",
        chat_id=chat_id,
    )


async def leer_cotizacion(chat_id: str) -> float:
    """Total cotizado del chat. 0.0 si no hay o si Redis falla (no se bloquea a nadie
    por no poder leer: el pago igual lo aprueba una persona que ve el comprobante)."""
    if not chat_id:
        return 0.0
    try:
        raw = await with_reconnect(lambda r: r.get(settings.key("cotizado", chat_id)))
    except Exception:  # noqa: BLE001
        return 0.0
    try:
        return float(raw) if raw else 0.0
    except (TypeError, ValueError):
        return 0.0


# El comprobante también tiene que sobrevivir al turno. Caso real: el cliente manda la
# foto ANTES de dar la dirección; el pedido no se puede crear todavía, y en el turno
# siguiente —cuando por fin da la dirección— el comprobante ya no está en el contexto.
# Sin esto el bot le vuelve a pedir la foto que acaba de mandar, y otra vez, para
# siempre. Se CONSUME al crear el pedido para que un comprobante no respalde dos.
async def guardar_comprobante(chat_id: str, url: str, texto: str) -> None:
    if not chat_id or not (url or texto):
        return
    await _escritura_idempotente(
        lambda r: r.set(
            settings.key("comprobante", chat_id),
            json.dumps({"url": url, "texto": texto, "ts": time.time()}),
            ex=settings.session_ttl_seconds,
        ),
        "guardar_comprobante",
        chat_id=chat_id,
    )


async def leer_comprobante(chat_id: str) -> dict:
    """{'url','texto','ts'} del último comprobante recibido y NO usado todavía. {} si no
    hay o si Redis falla: ante duda se pide la foto, que es el lado seguro."""
    if not chat_id:
        return {}
    try:
        raw = await with_reconnect(lambda r: r.get(settings.key("comprobante", chat_id)))
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def consumir_comprobante(chat_id: str) -> None:
    """Lo borra: ya respaldó un pedido y no puede respaldar otro."""
    if not chat_id:
        return
    await _escritura_idempotente(
        lambda r: r.delete(settings.key("comprobante", chat_id)),
        "consumir_comprobante",
        chat_id=chat_id,
    )


# El pedido ABIERTO del chat: creado y todavía sin decidir. Vive 7 días porque el
# cliente cotiza un viernes y transfiere el lunes — más que la sesión (24 h), a
# propósito. Sin esto, el comprobante que llega días después hace que el modelo llame
# `crear_pedido` otra vez y quede un pedido DUPLICADO y VACÍO en Odoo, con el
# comprobante adjunto al vacío (pasó de verdad: S00163 con las líneas, S00166 en 0.00).
TTL_PEDIDO_ABIERTO = 604_800  # 7 días


async def guardar_pedido_abierto(
    chat_id: str, order_id: int, modalidad: str = "", direccion: str = ""
) -> None:
    if not chat_id or not order_id:
        return
    await _escritura_idempotente(
        lambda r: r.set(
            settings.key("pedido_abierto", chat_id),
            json.dumps({
                "order_id": int(order_id),
                "modalidad": modalidad,
                "direccion": direccion,
                "ts": time.time(),
            }),
            ex=TTL_PEDIDO_ABIERTO,
        ),
        "guardar_pedido_abierto",
        chat_id=chat_id,
    )


async def leer_pedido_abierto(chat_id: str) -> dict:
    """{'order_id','modalidad','direccion','ts'} o {} si no hay (o si Redis falla).

    Ante duda devuelve {}: no adoptar un pedido que no se pudo confirmar es preferible
    a adoptar uno equivocado.
    """
    if not chat_id:
        return {}
    try:
        raw = await with_reconnect(
            lambda r: r.get(settings.key("pedido_abierto", chat_id))
        )
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) and data.get("order_id") else {}


async def cerrar_pedido_abierto(chat_id: str) -> None:
    """Ya se decidió (aprobado o rechazado): el próximo pedido es uno nuevo."""
    if not chat_id:
        return
    await _escritura_idempotente(
        lambda r: r.delete(settings.key("pedido_abierto", chat_id)),
        "cerrar_pedido_abierto",
        chat_id=chat_id,
    )


async def tocar_ventana_24h(chat_id: str) -> None:
    await _escritura_idempotente(
        lambda r: r.set(
            settings.key("window_24h", chat_id), str(time.time()), ex=TTL_VENTANA
        ),
        "tocar_ventana_24h",
        chat_id=chat_id,
    )


async def encolar_revision(
    chat_id: str,
    motivos: list[str],
    resumen: str,
    order_id: int | None = None,
    user_name: str = "",
) -> None:
    """Revisión por excepción: sólo lo que el bot marcó como dudoso."""
    if not motivos:
        return
    item = {
        "ts": time.time(),
        "chat_id": chat_id,
        "user_name": user_name,
        "motivos": motivos,
        "resumen": resumen[:500],
        "order_id": order_id,
    }
    async def _op(r):
        pipe = r.pipeline()
        pipe.lpush(settings.key(COLA_REVISION), json.dumps(item, ensure_ascii=False))
        pipe.ltrim(settings.key(COLA_REVISION), 0, MAX_REVISION - 1)
        await pipe.execute()

    await run_write(_op)
    log.info("revision_encolada", chat_id=chat_id, motivos=motivos)


async def listar_revision(limite: int = 50) -> list[dict]:
    try:
        raw = await with_reconnect(
            lambda r: r.lrange(settings.key(COLA_REVISION), 0, limite - 1)
        )
    except Exception:  # noqa: BLE001
        return []
    out = []
    for item in raw:
        try:
            out.append(json.loads(item))
        except json.JSONDecodeError:
            continue
    return out


async def limpiar_revision() -> None:
    await run_write(lambda r: r.delete(settings.key(COLA_REVISION)))
