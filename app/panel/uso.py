"""Cuántos tokens gasta el bot y qué tan rápido responde, por agente y por día.

Se acumula en Redis (hash por día, `pastoriza:panel:uso:{YYYY-MM-DD}`) en vez de
guardar un evento por turno: con cientos de turnos diarios, un HINCRBY por campo es
mucho más liviano que una lista que crece sin techo. El panel sólo necesita totales
(hoy, últimos 7 días), no el detalle de cada turno — eso ya queda en los logs.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.logging_conf import get_logger
from app.redis_client import run_write, with_reconnect
from app.settings import settings

log = get_logger(__name__)

# 45 días de margen: de sobra para ver "últimos 7/30 días" sin que la clave crezca
# para siempre. Cada día es su propia key, así que expirar una vieja no afecta a hoy.
TTL_SEGUNDOS = 45 * 86_400

_CAMPOS = ("tokens_entrada", "tokens_salida", "tokens_total", "requests", "turnos", "duracion_ms")


def _key(fecha: str) -> str:
    return settings.key("panel", f"uso:{fecha}")


def _hoy() -> str:
    return datetime.now().strftime("%Y-%m-%d")


async def registrar(agente: str, usage: Any, duracion_ms: float) -> None:
    """Se llama después de cada turno del agente. Nunca debe romper el turno del
    cliente: si Redis falla acá, sólo se pierde una métrica, no la respuesta."""
    agente = agente or "desconocido"
    key = _key(_hoy())

    async def _op(r):
        pipe = r.pipeline()
        pipe.hincrby(key, f"{agente}:tokens_entrada", int(getattr(usage, "input_tokens", 0) or 0))
        pipe.hincrby(key, f"{agente}:tokens_salida", int(getattr(usage, "output_tokens", 0) or 0))
        pipe.hincrby(key, f"{agente}:tokens_total", int(getattr(usage, "total_tokens", 0) or 0))
        pipe.hincrby(key, f"{agente}:requests", int(getattr(usage, "requests", 0) or 0))
        pipe.hincrby(key, f"{agente}:turnos", 1)
        pipe.hincrby(key, f"{agente}:duracion_ms", int(duracion_ms))
        pipe.expire(key, TTL_SEGUNDOS)
        return await pipe.execute()

    try:
        await run_write(_op)
    except Exception as exc:  # noqa: BLE001
        log.warning("uso_no_registrado", agente=agente, error=str(exc))


def _fila_vacia() -> dict[str, int]:
    return {c: 0 for c in _CAMPOS}


async def resumen(dias: int = 7) -> dict:
    """Totales de los últimos `dias` días (incluye hoy), por agente y en general."""
    dias = max(1, min(dias, 45))
    hoy = datetime.now().date()
    fechas = [(hoy - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(dias)]

    async def _leer_todas(r):
        pipe = r.pipeline()
        for f in fechas:
            pipe.hgetall(_key(f))
        return await pipe.execute()

    try:
        crudos = await with_reconnect(_leer_todas)
    except Exception as exc:  # noqa: BLE001
        log.warning("uso_resumen_fallo", error=str(exc))
        crudos = [{} for _ in fechas]

    por_dia: list[dict] = []
    totales_por_agente: dict[str, dict[str, int]] = {}
    for fecha, crudo in zip(fechas, crudos):
        agentes_del_dia: dict[str, dict[str, int]] = {}
        for campo, valor in (crudo or {}).items():
            agente, _, sufijo = campo.rpartition(":")
            if not agente or sufijo not in _CAMPOS:
                continue
            fila = agentes_del_dia.setdefault(agente, _fila_vacia())
            fila[sufijo] += int(valor)
            tot = totales_por_agente.setdefault(agente, _fila_vacia())
            tot[sufijo] += int(valor)
        total_dia = _fila_vacia()
        for fila in agentes_del_dia.values():
            for c in _CAMPOS:
                total_dia[c] += fila[c]
        por_dia.append({"fecha": fecha, "agentes": agentes_del_dia, "total": total_dia})
    por_dia.reverse()  # más antiguo primero, para leer la tendencia de izquierda a derecha

    total_general = _fila_vacia()
    for fila in totales_por_agente.values():
        for c in _CAMPOS:
            total_general[c] += fila[c]

    return {"dias": por_dia, "por_agente": totales_por_agente, "total": total_general}
