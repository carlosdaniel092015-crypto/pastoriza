"""Canario de producción: el bot se vigila a sí mismo y avisa cuando algo se rompe.

Por qué existe: las fallas graves de este bot son SILENCIOSAS para quien opera. El
bot sigue "contestando", pero le dice a todos los clientes "no tengo productos
disponibles" (catálogo vacío), o escala todo al supervisor, o Odoo dejó de
responder. Sin esto, el operador se entera cuando ve una captura de un cliente que
ya se fue.

Sólo LEE y avisa por Telegram: no toca el flujo de venta. Si el propio canario
falla, se loguea y se sigue: nunca puede tumbar el bot.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.logging_conf import get_logger
from app.settings import settings

log = get_logger(__name__)

# Motivos de la cola de revisión que cuentan como "pasó a un humano".
MOTIVOS_ESCALADA = {"handoff", "repeticion_3x", "takeover_supervisor"}


@dataclass
class Estado:
    """Memoria entre corridas: sólo avisamos cuando CAMBIA algo, no en cada vuelta."""

    fallos: set[str] = field(default_factory=set)
    ultimo_aviso: float = 0.0


_estado = Estado()
# Si sigue roto, recordarlo cada 2 h (que no se olvide, pero sin spamear).
_RECORDATORIO = 7200.0


async def _revisar_catalogo() -> tuple[str, str]:
    """El check más importante: sin catálogo, el bot no vende nada."""
    try:
        from app.catalogo import catalogo

        productos = await catalogo.todos()
        if not productos:
            return ("catalogo", "El catálogo está VACÍO: el bot le dice a todos los "
                                "clientes que no hay productos. Revisa Odoo (activos, "
                                "publicados y con foto).")
        return ("", f"{len(productos)} productos")
    except Exception as exc:  # noqa: BLE001
        return ("catalogo", f"No se pudo leer el catálogo desde Odoo: {exc}")


async def _revisar_redis() -> tuple[str, str]:
    """Sin Redis se pierde el historial, las pausas y el debounce."""
    try:
        from app.redis_client import with_reconnect

        await with_reconnect(lambda r: r.ping())
        return ("", "ok")
    except Exception as exc:  # noqa: BLE001
        return ("redis", f"Redis no responde: {exc}")


async def _revisar_escaladas() -> tuple[str, str]:
    """Un pico de escaladas suele significar que algo se rompió río arriba."""
    try:
        from app.estado import listar_revision

        items = await listar_revision(200)
        corte = time.time() - 3600
        recientes = [
            i for i in items
            if float(i.get("ts") or 0) >= corte
            and any(
                m in MOTIVOS_ESCALADA or str(m).startswith("handoff")
                for m in (i.get("motivos") or [])
            )
        ]
        n = len(recientes)
        if n >= settings.canario_max_escaladas_hora:
            return ("escaladas", f"{n} conversaciones pasaron a un humano en la última "
                                 "hora. Puede que el bot esté fallando en algo que "
                                 "debería resolver solo.")
        return ("", f"{n} en la última hora")
    except Exception as exc:  # noqa: BLE001
        log.warning("canario_escaladas_fallo", error=str(exc))
        return ("", "no medido")


async def revisar() -> dict:
    """Corre todos los chequeos. Devuelve {ok, fallos:{clave: motivo}, detalle:{}}."""
    fallos: dict[str, str] = {}
    detalle: dict[str, str] = {}
    for nombre, check in (
        ("catalogo", _revisar_catalogo),
        ("redis", _revisar_redis),
        ("escaladas", _revisar_escaladas),
    ):
        try:
            clave, texto = await check()
        except Exception as exc:  # noqa: BLE001
            clave, texto = nombre, f"el chequeo falló: {exc}"
        if clave:
            fallos[clave] = texto
        else:
            detalle[nombre] = texto
    return {"ok": not fallos, "fallos": fallos, "detalle": detalle}


async def revisar_y_avisar(arranque: bool = False) -> dict:
    """Chequea y avisa por Telegram SÓLO cuando el estado cambia (o sigue roto)."""
    res = await revisar()
    fallos = set(res["fallos"])
    ahora = time.time()
    nuevos = fallos - _estado.fallos
    resueltos = _estado.fallos - fallos

    try:
        from app.panel import telegram

        if nuevos or (fallos and ahora - _estado.ultimo_aviso > _RECORDATORIO):
            cuerpo = "\n".join(f"• <b>{k}</b>: {v}" for k, v in res["fallos"].items())
            await telegram.enviar(f"🚨 <b>El bot tiene un problema</b>\n{cuerpo}")
            _estado.ultimo_aviso = ahora
        elif resueltos and not fallos:
            await telegram.enviar(
                "✅ <b>El bot volvió a la normalidad</b>\n"
                + " · ".join(f"{k}: {v}" for k, v in res["detalle"].items())
            )
        elif arranque and not fallos:
            log.info("canario_arranque_ok", **res["detalle"])
    except Exception as exc:  # noqa: BLE001
        log.warning("canario_aviso_fallo", error=str(exc))

    _estado.fallos = fallos
    log.info("canario", ok=res["ok"], fallos=list(fallos), **res["detalle"])
    return res
