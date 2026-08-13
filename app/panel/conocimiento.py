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
"""
from __future__ import annotations

import json
import time
from typing import Any

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

# Cache en memoria para inyección síncrona.
_reglas: list[dict] = []
_correc: list[dict] = []
_cargado = False


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
    """Refresca el cache de reglas y correcciones desde Redis."""
    global _reglas, _correc, _cargado
    _reglas = await _leer_lista(K_REGLAS)
    _correc = await _leer_lista(K_CORREC)
    _cargado = True


def get_bloque_inyeccion() -> str:
    """Bloque de texto a inyectar en el prompt del agente (SÍNCRONO). '' si no hay."""
    if not _cargado:
        return ""
    partes: list[str] = []
    reglas = [r for r in _reglas if r.get("activa", True)][:MAX_INYECTAR_REGLAS]
    if reglas:
        partes.append("# CONOCIMIENTO DEL NEGOCIO (cargado por el supervisor)")
        partes.extend(f"- {r.get('texto', '').strip()}" for r in reglas if r.get("texto"))
    correc = _correc[:MAX_INYECTAR_CORREC]
    if correc:
        partes.append("\n# CORRECCIONES APRENDIDAS (cómo responder en estos casos)")
        for c in correc:
            sit = (c.get("situacion") or "").strip()
            resp = (c.get("respuesta_correcta") or "").strip()
            if sit and resp:
                partes.append(f'- Si el cliente {sit} -> responde así: "{resp}"')
    return "\n".join(partes)


# ------------------------------------------------------------- reglas ---
async def add_regla(texto: str, origen: str = "supervisor") -> dict:
    obj = {
        "id": await _next_id(),
        "texto": texto.strip(),
        "activa": True,
        "origen": origen,
        "ts": time.time(),
    }
    await _push(K_REGLAS, obj)
    await cargar()
    return obj


async def list_reglas() -> list[dict]:
    return await _leer_lista(K_REGLAS)


async def del_regla(rid: int) -> None:
    items = [r for r in await _leer_lista(K_REGLAS) if r.get("id") != rid]
    await _reescribir(K_REGLAS, items)
    await cargar()


# --------------------------------------------------------- correcciones ---
async def add_correccion(
    situacion: str, respuesta_correcta: str, motivo: str = "", chat_id: str = ""
) -> dict:
    obj = {
        "id": await _next_id(),
        "situacion": situacion.strip(),
        "respuesta_correcta": respuesta_correcta.strip(),
        "motivo": motivo.strip(),
        "chat_id": chat_id,
        "ts": time.time(),
    }
    await _push(K_CORREC, obj)
    await cargar()
    return obj


async def list_correcciones() -> list[dict]:
    return await _leer_lista(K_CORREC)


async def del_correccion(cid: int) -> None:
    items = [c for c in await _leer_lista(K_CORREC) if c.get("id") != cid]
    await _reescribir(K_CORREC, items)
    await cargar()


# --------------------------------------------------------- sugerencias ---
async def add_sugerencia(
    tipo: str,
    contenido: str,
    riesgo: str = "bajo",
    origen: str = "analisis",
    origen_chats: list[str] | None = None,
) -> dict:
    """tipo: 'regla' | 'correccion' | 'prompt'. riesgo: 'bajo' | 'alto'.

    origen_chats: chat_ids de las conversaciones que motivaron la sugerencia (para
    que el supervisor pueda abrir el hilo desde el panel)."""
    obj = {
        "id": await _next_id(),
        "tipo": tipo,
        "contenido": contenido.strip(),
        "riesgo": riesgo,
        "origen": origen,
        "origen_chats": list(origen_chats or []),
        "estado": "pendiente",
        "ts": time.time(),
    }
    await _push(K_SUGER, obj)
    return obj


async def list_sugerencias(solo_pendientes: bool = False) -> list[dict]:
    items = await _leer_lista(K_SUGER)
    if solo_pendientes:
        items = [s for s in items if s.get("estado") == "pendiente"]
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
    """Aprueba: si es regla, la agrega a reglas activas."""
    s = await _set_estado_sugerencia(sid, "aprobada")
    if s and s.get("tipo") == "regla":
        await add_regla(s.get("contenido", ""), origen="sugerencia")
    return s


async def rechazar_sugerencia(sid: int) -> dict | None:
    return await _set_estado_sugerencia(sid, "rechazada")
