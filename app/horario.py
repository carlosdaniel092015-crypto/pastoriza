"""Ventana horaria en la que el bot responde solo, por canal.

Pura: sin Redis, sin HTTP, se puede testear entera. `desde`/`hasta` en "HH:MM" (24h).
Vacío en cualquiera de los dos = SIN restricción, el bot siempre está activo — es el
default, así que no cambia nada para un canal que nunca configuró esto.

Fuera de la ventana el bot no responde (mismo tratamiento que `bot_pausado`): el
mensaje queda visible en el panel para que alguien lo atienda a mano.
"""
from __future__ import annotations

from datetime import datetime, time

from app.logging_conf import get_logger

log = get_logger(__name__)


def _parsear(hhmm: str) -> time | None:
    hhmm = (hhmm or "").strip()
    if not hhmm:
        return None
    try:
        h, m = hhmm.split(":", 1)
        return time(int(h), int(m))
    except (ValueError, TypeError):
        log.warning("horario_invalido", valor=hhmm)
        return None


def dentro_de_horario(desde: str, hasta: str, ahora: datetime | None = None) -> bool:
    """True si el bot debe responder AHORA.

    Falla ABIERTO (True) si `desde`/`hasta` está vacío o mal escrito: un typo en el
    horario no puede dejar al bot mudo todo el día sin que nadie sepa por qué.
    """
    t_desde = _parsear(desde)
    t_hasta = _parsear(hasta)
    if t_desde is None or t_hasta is None:
        return True
    hora = (ahora or datetime.now()).time()
    if t_desde <= t_hasta:
        return t_desde <= hora < t_hasta
    # Cruza la medianoche (ej: 19:00 a 05:00): activo en la tarde-noche O la madrugada.
    return hora >= t_desde or hora < t_hasta
