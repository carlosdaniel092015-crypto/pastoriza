"""Mejora continua supervisada: reglas/FAQ, correcciones y sugerencias del bot.

Filosofía: el modelo NO se re-entrena solo. En su lugar, el supervisor (o el
propio bot proponiendo) acumula conocimiento que se INYECTA en las instrucciones
del agente en cada turno. Efecto inmediato, auditable y reversible.

- reglas:       hechos/reglas de negocio que el bot aplica siempre.
- correcciones: "en esta situación, responde así" (aprendido de un caso real).
- sugerencias:  propuestas (del bot o del análisis) que esperan tu aprobación.

Las reglas y correcciones se cachean en memoria para poder inyectarlas de forma
SÍNCRONA (lo exige construir_instrucciones del SDK). El cache se refresca al
arrancar y en cada cambio hecho desde el panel.

POR CANAL: cada número de YCloud tiene sus propias reglas y correcciones
(`pastoriza:panel:reglas:c:<canal>`) ADEMÁS de las comunes. Al bot de un canal se
le inyectan las comunes + las de su canal; una regla cargada dentro del 6701 no
llega al 1092 salvo que se guarde "en ambos". Ver `app/canales.py`.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.canales import COMUN, canal_id, key_canal
from app.logging_conf import get_logger
from app.redis_client import run_write, with_reconnect
from app.settings import settings

log = get_logger(__name__)

K_REGLAS = settings.key("panel", "reglas")
K_CORREC = settings.key("panel", "correcciones")
K_SUGER = settings.key("panel", "sugerencias")
K_SEQ = settings.key("panel", "conocimiento_seq")

MAX_INYECTAR_REGLAS = 40
MAX_INYECTAR_CORREC = 25

# Cache en memoria para inyección síncrona: canal -> items ("" = común).
_reglas: dict[str, list[dict]] = {COMUN: []}
_correc: dict[str, list[dict]] = {COMUN: []}
_cargado = False


async def _canales() -> tuple[str, ...]:
    from app.business_config import canales_configurados

    return tuple({*await canales_configurados(), *(c for c in _reglas if c)})


# ------------------------------------------------------------- helpers ---
async def _next_id() -> int:
    eid = await run_write(lambda r: r.incr(K_SEQ))
    return int(eid) if eid is not None else int(time.time() * 1000)


async def _leer_lista(key: str) -> list[dict]:
    async def _op(r: Any) -> list[str]:
        return await r.lrange(key, 0, -1)

    try:
        raw = await with_reconnect(_op)
    except Exception:  # noqa: BLE001
        return []
    vistos: set = set()
    out: list[dict] = []
    for item in raw:
        try:
            obj = json.loads(item)
        except json.JSONDecodeError:
            continue
        oid = obj.get("id")
        if oid is not None and oid in vistos:  # dedupe por id (red de seguridad)
            continue
        if oid is not None:
            vistos.add(oid)
        out.append(obj)
    return out


async def _push(key: str, obj: dict) -> None:
    payload = json.dumps(obj, ensure_ascii=False)
    await run_write(lambda r: r.lpush(key, payload))  # 1 intento: no duplicar


async def _reescribir(key: str, items: list[dict]) -> None:
    async def _op(r: Any) -> None:
        pipe = r.pipeline()
        pipe.delete(key)
        if items:
            pipe.rpush(key, *[json.dumps(i, ensure_ascii=False) for i in items])
        await pipe.execute()

    await run_write(_op)


# ------------------------------------------------------------- cache ---
async def cargar() -> None:
    """Refresca el cache de reglas y correcciones (común y por canal) desde Redis."""
    global _cargado
    canales = (COMUN, *await _canales())
    reglas: dict[str, list[dict]] = {}
    correc: dict[str, list[dict]] = {}
    for c in canales:
        reglas[c] = await _leer_lista(key_canal(K_REGLAS, c))
        correc[c] = await _leer_lista(key_canal(K_CORREC, c))
    _reglas.clear()
    _reglas.update(reglas)
    _correc.clear()
    _correc.update(correc)
    _cargado = True


def get_bloque_inyeccion(canal: str = COMUN) -> str:
    """Bloque a inyectar en el prompt del agente (SÍNCRONO): común + el del canal."""
    if not _cargado:
        return ""
    c = canal_id(canal)
    # Primero las del canal: si hay tope, gana lo específico de ese número.
    todas_reglas = (_reglas.get(c, []) if c else []) + _reglas.get(COMUN, [])
    todas_correc = (_correc.get(c, []) if c else []) + _correc.get(COMUN, [])

    partes: list[str] = []
    reglas = [r for r in todas_reglas if r.get("activa", True)][:MAX_INYECTAR_REGLAS]
    if reglas:
        partes.append("# CONOCIMIENTO DEL NEGOCIO (cargado por el supervisor)")
        partes.extend(f"- {r.get('texto', '').strip()}" for r in reglas if r.get("texto"))
    correc = todas_correc[:MAX_INYECTAR_CORREC]
    if correc:
        partes.append("\n# CORRECCIONES APRENDIDAS (cómo responder en estos casos)")
        for c_ in correc:
            sit = (c_.get("situacion") or "").strip()
            resp = (c_.get("respuesta_correcta") or "").strip()
            if sit and resp:
                partes.append(f'- Si el cliente {sit} -> responde así: "{resp}"')
    return "\n".join(partes)


async def _destinos(canal: str, ambos: bool) -> list[str]:
    """A qué canales escribir: el común (y sólo él) o el canal indicado."""
    c = canal_id(canal)
    return [COMUN] if (ambos or not c) else [c]


# ------------------------------------------------------------- reglas ---
async def add_regla(
    texto: str, origen: str = "supervisor", canal: str = COMUN, ambos: bool = False
) -> dict:
    destino = (await _destinos(canal, ambos))[0]
    obj = {
        "id": await _next_id(),
        "texto": texto.strip(),
        "activa": True,
        "origen": origen,
        "canal": destino,
        "ts": time.time(),
    }
    await _push(key_canal(K_REGLAS, destino), obj)
    await cargar()
    return obj


async def list_reglas(canal: str = COMUN) -> list[dict]:
    """Las del canal + las comunes (marcadas con `canal`, para que el panel lo muestre)."""
    c = canal_id(canal)
    propias = [
        {**r, "canal": c} for r in (await _leer_lista(key_canal(K_REGLAS, c)) if c else [])
    ]
    comunes = [{**r, "canal": COMUN} for r in await _leer_lista(K_REGLAS)]
    return propias + comunes


async def del_regla(rid: int, canal: str = COMUN) -> None:
    """Borra la regla donde esté: en el canal o en la común."""
    for key in _keys_de(K_REGLAS, canal):
        items = await _leer_lista(key)
        quedan = [r for r in items if r.get("id") != rid]
        if len(quedan) != len(items):
            await _reescribir(key, quedan)
            break
    await cargar()


def _keys_de(base: str, canal: str) -> list[str]:
    c = canal_id(canal)
    return ([key_canal(base, c)] if c else []) + [base]


# --------------------------------------------------------- correcciones ---
async def add_correccion(
    situacion: str,
    respuesta_correcta: str,
    motivo: str = "",
    chat_id: str = "",
    canal: str = COMUN,
    ambos: bool = False,
) -> dict:
    destino = (await _destinos(canal, ambos))[0]
    obj = {
        "id": await _next_id(),
        "situacion": situacion.strip(),
        "respuesta_correcta": respuesta_correcta.strip(),
        "motivo": motivo.strip(),
        "chat_id": chat_id,
        "canal": destino,
        "ts": time.time(),
    }
    await _push(key_canal(K_CORREC, destino), obj)
    await cargar()
    return obj


async def list_correcciones(canal: str = COMUN) -> list[dict]:
    c = canal_id(canal)
    propias = [
        {**x, "canal": c} for x in (await _leer_lista(key_canal(K_CORREC, c)) if c else [])
    ]
    comunes = [{**x, "canal": COMUN} for x in await _leer_lista(K_CORREC)]
    return propias + comunes


async def del_correccion(cid: int, canal: str = COMUN) -> None:
    for key in _keys_de(K_CORREC, canal):
        items = await _leer_lista(key)
        quedan = [c for c in items if c.get("id") != cid]
        if len(quedan) != len(items):
            await _reescribir(key, quedan)
            break
    await cargar()


# --------------------------------------------------------- sugerencias ---
async def add_sugerencia(
    tipo: str,
    contenido: str,
    riesgo: str = "bajo",
    origen: str = "analisis",
    origen_chats: list[str] | None = None,
    canal: str = COMUN,
) -> dict:
    """tipo: 'regla' | 'correccion' | 'prompt'. riesgo: 'bajo' | 'alto'.

    origen_chats: chat_ids de las conversaciones que motivaron la sugerencia (para
    que el supervisor pueda abrir el hilo desde el panel).
    canal: de qué número salieron esas conversaciones; al aprobarla, la regla se
    aplica a ESE canal (vacío = a los dos)."""
    obj = {
        "id": await _next_id(),
        "tipo": tipo,
        "contenido": contenido.strip(),
        "riesgo": riesgo,
        "origen": origen,
        "origen_chats": list(origen_chats or []),
        "canal": canal_id(canal),
        "estado": "pendiente",
        "ts": time.time(),
    }
    await _push(K_SUGER, obj)
    return obj


async def list_sugerencias(
    solo_pendientes: bool = False, canal: str = COMUN
) -> list[dict]:
    """Las del canal + las que no son de ningún canal (comunes)."""
    items = await _leer_lista(K_SUGER)
    if solo_pendientes:
        items = [s for s in items if s.get("estado") == "pendiente"]
    c = canal_id(canal)
    if c:
        items = [s for s in items if str(s.get("canal") or "") in (c, COMUN)]
    return items


async def _set_estado_sugerencia(sid: int, estado: str) -> dict | None:
    items = await _leer_lista(K_SUGER)
    encontrada = None
    for s in items:
        if s.get("id") == sid:
            s["estado"] = estado
            encontrada = s
    await _reescribir(K_SUGER, items)
    return encontrada


async def aprobar_sugerencia(sid: int) -> dict | None:
    """Aprueba: si es regla, la agrega a las reglas activas DEL CANAL de la sugerencia.

    La sugerencia sale de conversaciones de un número concreto, así que la mejora se
    aplica a ese número; si no tiene canal (análisis general), va a las comunes.
    """
    s = await _set_estado_sugerencia(sid, "aprobada")
    if s and s.get("tipo") == "regla":
        await add_regla(
            s.get("contenido", ""), origen="sugerencia", canal=str(s.get("canal") or "")
        )
    return s


async def rechazar_sugerencia(sid: int) -> dict | None:
    return await _set_estado_sugerencia(sid, "rechazada")
