"""Lo que el bot le manda al SUPERVISOR por WhatsApp (ADMIN_PHONE).

POR QUÉ EXISTE: el panel muestra las conversaciones con CLIENTES, así que los mensajes
que el bot le manda al supervisor (la plantilla de aprobación con los botones, los
avisos de escalamiento, los errores) no se veían en ninguna parte. Quien opera no tenía
forma de saber si al 6701 le llegó el aviso, qué decía, ni de qué pedido era — y si la
plantilla la rechaza Meta, el síntoma es justamente que NO llega nada.

Va a una lista Redis capada, igual que el feed de eventos: son unos pocos cientos de
bytes por entrada y se leen en orden.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.logging_conf import get_logger
from app.redis_client import run_write, with_reconnect
from app.settings import settings

log = get_logger(__name__)

KEY = settings.key("panel", "supervisor")
MAX = 400
TTL_SEGUNDOS = 30 * 86_400


async def registrar(
    tipo: str,
    *,
    chat_id: str = "",
    cliente: str = "",
    emisor: str = "",
    texto: str = "",
    plantilla: str = "",
    order_id: int | None = None,
    enviado: bool = True,
    detalle: str = "",
    **extra: Any,
) -> None:
    """Anota un mensaje al supervisor. NUNCA propaga: si esto falla sólo se pierde el
    registro, y perder el registro no vale perder el aviso ni el turno del cliente."""
    entrada = {
        "ts": int(time.time()),
        "tipo": tipo,  # "aprobacion" | "aviso" | "comprobante" | "rechazo_motivo"
        "chat_id": chat_id,
        "cliente": cliente,
        "emisor": emisor,  # por qué número NUESTRO salió
        "destino": settings.admin_phone,
        "texto": (texto or "")[:1200],
        "plantilla": plantilla,
        "order_id": order_id,
        "enviado": bool(enviado),
        "detalle": detalle[:300],
        **{k: v for k, v in extra.items() if v not in (None, "")},
    }

    async def _op(r):
        pipe = r.pipeline()
        pipe.rpush(KEY, json.dumps(entrada, ensure_ascii=False))
        pipe.ltrim(KEY, -MAX, -1)
        pipe.expire(KEY, TTL_SEGUNDOS)
        return await pipe.execute()

    try:
        await run_write(_op)
    except Exception as exc:  # noqa: BLE001
        log.warning("supervisor_log_fallo", tipo=tipo, error=str(exc))


async def listar(limite: int = 100) -> list[dict]:
    """Del más NUEVO al más viejo: lo último que se le mandó es lo que importa."""
    limite = max(1, min(limite, MAX))
    try:
        crudo = await with_reconnect(lambda r: r.lrange(KEY, -limite, -1))
    except Exception as exc:  # noqa: BLE001
        log.warning("supervisor_log_listar_fallo", error=str(exc))
        return []
    out: list[dict] = []
    for item in crudo or []:
        try:
            out.append(json.loads(item))
        except (json.JSONDecodeError, TypeError):
            continue
    out.reverse()
    return out


async def sin_entregar() -> int:
    """Cuántos NO se pudieron entregar. Es el número que importa vigilar: un aviso que
    no llegó es un pedido esperando aprobación que el supervisor no sabe que existe."""
    return sum(1 for e in await listar(MAX) if not e.get("enviado"))
