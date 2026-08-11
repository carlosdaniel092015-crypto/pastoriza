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


async def set_bot_global(encendido: bool) -> None:
    """Kill-switch global: enciende/apaga el bot para TODOS los clientes."""
    if encendido:
        await run_write(lambda r: r.delete(BOT_GLOBAL_KEY))
    else:
        await run_write(lambda r: r.set(BOT_GLOBAL_KEY, "off"))
    log.info("bot_global", encendido=encendido)


async def bot_global_apagado() -> bool:
    try:
        return bool(await with_reconnect(lambda r: r.get(BOT_GLOBAL_KEY)))
    except Exception:  # noqa: BLE001
        return False


async def pausar_bot(chat_id: str) -> None:
    await run_write(
        lambda r: r.set(
            settings.key("bot_disabled", chat_id), "disabled-by-manager", ex=TTL_PAUSA
        )
    )
    log.info("bot_pausado", chat_id=chat_id)


async def reactivar_bot(chat_id: str) -> None:
    await run_write(lambda r: r.delete(settings.key("bot_disabled", chat_id)))
    log.info("bot_reactivado", chat_id=chat_id)


async def bot_pausado(chat_id: str) -> bool:
    # Ante un blip de Redis, NO tumbar el turno: asumimos "no pausado" y seguimos.
    try:
        return bool(await with_reconnect(lambda r: r.get(settings.key("bot_disabled", chat_id))))
    except Exception:  # noqa: BLE001
        log.warning("bot_pausado_check_fallo", chat_id=chat_id)
        return False


TTL_MSG_BOT = 7200  # 2h: ventana para reconocer un mensaje como "enviado por el bot"


async def registrar_msg_bot(msg_id: str) -> None:
    """Marca un id de mensaje como enviado por el BOT (no por un humano)."""
    if not msg_id:
        return
    await run_write(
        lambda r: r.set(settings.key("bot_msg", msg_id), "1", ex=TTL_MSG_BOT)
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


async def tocar_ventana_24h(chat_id: str) -> None:
    await run_write(
        lambda r: r.set(
            settings.key("window_24h", chat_id), str(time.time()), ex=TTL_VENTANA
        )
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
