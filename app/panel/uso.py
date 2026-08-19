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
from app.panel import precios
from app.redis_client import run_write, with_reconnect
from app.settings import settings

log = get_logger(__name__)

# 45 días de margen: de sobra para ver "últimos 7/30 días" sin que la clave crezca
# para siempre. Cada día es su propia key, así que expirar una vieja no afecta a hoy.
TTL_SEGUNDOS = 45 * 86_400

_CAMPOS = ("tokens_entrada", "tokens_salida", "tokens_total", "requests", "turnos", "duracion_ms")


# El gasto POR CONVERSACIÓN vive lo mismo que el historial (SESSION_TTL_SECONDS): sirve
# para explicar una conversación que está a la vista, no para contabilidad histórica.
TTL_POR_CHAT = 7 * 86_400
# Cuántas conversaciones se rankean. Es un ZSET: sin tope crecería con cada cliente.
MAX_CHATS_RANKEADOS = 300


def _key(fecha: str) -> str:
    return settings.key("panel", f"uso:{fecha}")


def _key_chat(chat_id: str) -> str:
    return settings.key("panel", f"uso:chat:{chat_id}")


def _key_ranking() -> str:
    return settings.key("panel", "uso:chats")


def _hoy() -> str:
    return datetime.now().strftime("%Y-%m-%d")


async def registrar(
    agente: str, usage: Any, duracion_ms: float, chat_id: str = "", modelo: str = ""
) -> None:
    """Se llama después de cada turno del agente. Nunca debe romper el turno del
    cliente: si Redis falla acá, sólo se pierde una métrica, no la respuesta."""
    agente = agente or "desconocido"
    key = _key(_hoy())
    entrada = int(getattr(usage, "input_tokens", 0) or 0)
    salida = int(getattr(usage, "output_tokens", 0) or 0)
    total = int(getattr(usage, "total_tokens", 0) or 0)
    requests = int(getattr(usage, "requests", 0) or 0)

    async def _op(r):
        pipe = r.pipeline()
        pipe.hincrby(key, f"{agente}:tokens_entrada", entrada)
        pipe.hincrby(key, f"{agente}:tokens_salida", salida)
        pipe.hincrby(key, f"{agente}:tokens_total", total)
        pipe.hincrby(key, f"{agente}:requests", requests)
        pipe.hincrby(key, f"{agente}:turnos", 1)
        pipe.hincrby(key, f"{agente}:duracion_ms", int(duracion_ms))
        # El modelo se guarda (no se suma) porque de él depende la TARIFA: los mismos
        # tokens en gpt-4o cuestan ~17x más que en mini, así que sin esto el coste en
        # dólares sería inventado.
        if modelo:
            pipe.hset(key, f"{agente}:modelo", modelo)
        pipe.expire(key, TTL_SEGUNDOS)
        if chat_id:
            # Mismo desglose pero de ESA conversación: cuál gastó más y con qué agente.
            kc = _key_chat(chat_id)
            pipe.hincrby(kc, f"{agente}:tokens_entrada", entrada)
            pipe.hincrby(kc, f"{agente}:tokens_salida", salida)
            pipe.hincrby(kc, f"{agente}:tokens_total", total)
            pipe.hincrby(kc, f"{agente}:requests", requests)
            pipe.hincrby(kc, f"{agente}:turnos", 1)
            pipe.hincrby(kc, f"{agente}:duracion_ms", int(duracion_ms))
            if modelo:
                pipe.hset(kc, f"{agente}:modelo", modelo)
            pipe.expire(kc, TTL_POR_CHAT)
            # Ranking, para no tener que escanear todas las conversaciones.
            pipe.zincrby(_key_ranking(), total, chat_id)
            pipe.zremrangebyrank(_key_ranking(), 0, -(MAX_CHATS_RANKEADOS + 1))
            pipe.expire(_key_ranking(), TTL_POR_CHAT)
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
    totales_por_agente: dict[str, dict] = {}
    for fecha, crudo in zip(fechas, crudos):
        agentes_del_dia, total_dia = _desglosar(crudo)
        for agente, fila in agentes_del_dia.items():
            tot = totales_por_agente.setdefault(agente, _fila_vacia())
            for c in _CAMPOS:
                tot[c] += int(fila.get(c, 0))
            if fila.get("modelo"):
                tot["modelo"] = fila["modelo"]
        por_dia.append({"fecha": fecha, "agentes": agentes_del_dia, "total": total_dia})
    por_dia.reverse()  # más antiguo primero, para leer la tendencia de izquierda a derecha

    total_general = _con_costo(totales_por_agente)

    return {
        "dias": por_dia,
        "por_agente": totales_por_agente,
        "total": total_general,
        "chats": await top_chats(),
        # Para que el panel pueda decir con qué tarifas se hizo la cuenta: es una
        # proyección, y el operador tiene que poder verificarla.
        "tarifas": {m: {"entrada": e, "salida": s} for m, (e, s) in precios.PRECIOS.items()},
    }


def _desglosar(crudo: dict) -> tuple[dict[str, dict], dict[str, Any]]:
    """Pasa de los campos planos de Redis (`agente:campo`) a {agente: {campo: n}} más
    el total. Es el mismo formato que devuelve `resumen`, así la UI no aprende dos."""
    por_agente: dict[str, dict] = {}
    for campo, valor in (crudo or {}).items():
        agente, _, sufijo = campo.rpartition(":")
        if not agente:
            continue
        if sufijo == "modelo":
            por_agente.setdefault(agente, _fila_vacia())["modelo"] = str(valor)
            continue
        if sufijo not in _CAMPOS:
            continue
        fila = por_agente.setdefault(agente, _fila_vacia())
        fila[sufijo] = int(fila.get(sufijo, 0)) + int(valor)
    total = _con_costo(por_agente)
    return por_agente, total


def _con_costo(por_agente: dict[str, dict]) -> dict[str, Any]:
    """Agrega `costo_usd` a cada agente y devuelve el total. El coste NO se guarda en
    Redis: se calcula al leer, así un cambio de tarifa se refleja en lo ya registrado
    en vez de quedar congelado con el precio del día en que se gastó."""
    total = _fila_vacia()
    total_usd = 0.0
    # Si algún agente usó un modelo sin tarifa conocida, el total queda marcado como
    # incompleto: mejor decir "faltan datos" que sumar de menos y parecer más barato.
    completo = True
    for fila in por_agente.values():
        for c in _CAMPOS:
            total[c] += int(fila.get(c, 0))
        usd = precios.costo(
            fila.get("modelo", ""), fila.get("tokens_entrada", 0), fila.get("tokens_salida", 0)
        )
        fila["costo_usd"] = usd
        if usd is None:
            completo = False
        else:
            total_usd += usd
    total["costo_usd"] = round(total_usd, 6)
    total["costo_completo"] = completo
    return total


async def por_chat(chat_id: str) -> dict:
    """Lo que gastó UNA conversación, con el desglose por agente. Para responder
    "¿por qué esta conversación salió caples?" sin adivinar."""
    if not chat_id:
        return {"por_agente": {}, "total": _fila_vacia()}
    try:
        crudo = await with_reconnect(lambda r: r.hgetall(_key_chat(chat_id)))
    except Exception as exc:  # noqa: BLE001
        log.warning("uso_por_chat_fallo", chat_id=chat_id, error=str(exc))
        crudo = {}
    por_agente, total = _desglosar(crudo)
    return {"por_agente": por_agente, "total": total}


async def top_chats(limite: int = 15) -> list[dict]:
    """Las conversaciones que más gastaron, de mayor a menor. Sale del ZSET para no
    tener que escanear una key por conversación."""
    limite = max(1, min(limite, MAX_CHATS_RANKEADOS))
    try:
        ids = await with_reconnect(
            lambda r: r.zrevrange(_key_ranking(), 0, limite - 1)
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("uso_top_chats_fallo", error=str(exc))
        return []

    async def _leer(r):
        pipe = r.pipeline()
        for cid in ids or []:
            pipe.hgetall(_key_chat(cid))
        return await pipe.execute()

    if not ids:
        return []
    try:
        crudos = await with_reconnect(_leer)
    except Exception as exc:  # noqa: BLE001
        log.warning("uso_top_chats_detalle_fallo", error=str(exc))
        return []

    out: list[dict] = []
    for cid, crudo in zip(ids, crudos):
        # La entrada del ranking puede sobrevivir al hash (TTL distinto): si ya no hay
        # desglose, la conversación venció y no se muestra un cero engañoso.
        if not crudo:
            continue
        por_agente, total = _desglosar(crudo)
        out.append({
            "chat_id": cid,
            "agentes": sorted(por_agente.keys()),
            "por_agente": por_agente,
            **total,
        })
    return out
