"""Agentes PERSONALIZADOS creados desde el panel.

Un agente de verdad no es sólo un prompt: necesita herramientas y que el enrutador
sepa mandarle conversaciones. Por eso aquí se guarda, por agente:

  - nombre        identificador corto (a-z, 0-9, _)
  - descripcion   para qué sirve; se la damos al determinador para que sepa enrutar
  - herramientas  qué packs puede usar (catalogo, cotizar, pedido, pedidos_cliente)
  - palabras      palabras/frases del cliente que lo activan (enrutado 0 tokens)
  - modelo        "mini" (barato) o "agente" (gpt-4o, para lo delicado)

El PROMPT no vive aquí: se guarda con `prompt_store` (misma key que los agentes
base), así `armar_instrucciones` funciona sin cambios y se edita/sube igual.

POR CANAL: un agente puede ser COMÚN a los dos números o PROPIO de uno
(`pastoriza:panel:agentes_custom:c:<canal>`). Un agente creado dentro del 6701 no
atiende conversaciones del 1092. Ver `app/canales.py`.

Este módulo es a propósito LIVIANO (sólo Redis): lo importa `prompt_store` y el
enrutador. La construcción del Agent vive en `app/agents/personalizados.py`.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from app.canales import COMUN, canal_id, key_canal
from app.logging_conf import get_logger
from app.redis_client import run_write, with_reconnect
from app.settings import settings

log = get_logger(__name__)

K_AGENTES = settings.key("panel", "agentes_custom")

# Nombres reservados: los agentes base y las piezas del prompt.
RESERVADOS = {"base_comun", "ventas", "pedido", "soporte", "enrutador"}
RE_NOMBRE = re.compile(r"^[a-z][a-z0-9_]{2,23}$")

# Packs de herramientas que el panel puede asignar (etiqueta -> para qué sirve).
PACKS = {
    "catalogo": "Buscar productos, ver catálogo, fotos y enlace de tienda",
    "cotizar": "Calcular cotizaciones con ITBIS y envío",
    "pedido": "Crear cliente y pedido en Odoo, agregar líneas",
    "pedidos_cliente": "Consultar los pedidos del cliente",
}
MODELOS = {"mini", "agente"}
MAX_AGENTES = 10

# Cache de proceso (el enrutado y el armado de instrucciones son SÍNCRONOS):
# canal -> nombre -> agente ("" = agentes comunes a los dos números).
_cache: dict[str, dict[str, dict]] = {COMUN: {}}
_version = 0


def version() -> int:
    """Sube en cada cambio; sirve para invalidar los Agent ya construidos."""
    return _version


def nombres(canal: str = COMUN) -> tuple[str, ...]:
    """Agentes que atienden en ese canal: los propios + los comunes."""
    c = canal_id(canal)
    propios = set(_cache.get(c, {})) if c else set()
    return tuple(sorted(propios | set(_cache.get(COMUN, {}))))


def nombres_todos() -> tuple[str, ...]:
    """Todos los agentes creados, de cualquier canal (para el editor de prompts)."""
    out: set[str] = set()
    for agentes in _cache.values():
        out |= set(agentes)
    return tuple(sorted(out))


def listar(canal: str = COMUN) -> list[dict]:
    """Los que atienden en ese canal (propios primero, marcados con `canal`)."""
    c = canal_id(canal)
    propios = [{**v, "canal": c} for _, v in sorted(_cache.get(c, {}).items())] if c else []
    nombres_propios = {a["nombre"] for a in propios}
    comunes = [
        {**v, "canal": COMUN}
        for k, v in sorted(_cache.get(COMUN, {}).items())
        if k not in nombres_propios  # el propio del canal gana sobre el común
    ]
    return propios + comunes


def listar_todos() -> list[dict]:
    """Todos, de todos los canales (para la pantalla de administración)."""
    out: list[dict] = []
    for canal, agentes in sorted(_cache.items()):
        out += [{**v, "canal": canal} for _, v in sorted(agentes.items())]
    return out


def get(nombre: str, canal: str = COMUN) -> dict | None:
    """Definición vigente en ese canal: la propia del canal gana sobre la común."""
    n = str(nombre or "").strip().lower()
    c = canal_id(canal)
    a = (_cache.get(c, {}).get(n) if c else None) or _cache.get(COMUN, {}).get(n)
    return dict(a) if a else None


def canales_cargados() -> tuple[str, ...]:
    return tuple(c for c in _cache if c)


async def _canales() -> tuple[str, ...]:
    from app.business_config import canales_configurados

    return tuple({*await canales_configurados(), *canales_cargados()})


def validar(nombre: str, herramientas: list[str], modelo: str) -> str:
    """Devuelve '' si es válido, o el motivo del rechazo."""
    n = str(nombre or "").strip().lower()
    if not RE_NOMBRE.match(n):
        return ("El nombre debe ser de 3 a 24 caracteres: minúsculas, números y _ "
                "(ej: mayorista, post_venta).")
    if n in RESERVADOS:
        return f"'{n}' es un agente base: elegí otro nombre."
    malas = [h for h in (herramientas or []) if h not in PACKS]
    if malas:
        return f"Herramientas desconocidas: {', '.join(malas)}."
    if modelo not in MODELOS:
        return "Modelo inválido (usá 'mini' o 'agente')."
    return ""


async def _leer_hash(key: str) -> dict[str, dict] | None:
    try:
        raw = await with_reconnect(lambda r: r.hgetall(key))
    except Exception as exc:  # noqa: BLE001
        log.warning("agentes_custom_load_fallo", key=key, error=str(exc))
        return None
    out: dict[str, dict] = {}
    for _, v in (raw or {}).items():
        try:
            obj = json.loads(v)
            if isinstance(obj, dict) and obj.get("nombre"):
                out[str(obj["nombre"])] = obj
        except json.JSONDecodeError:
            continue
    return out


async def cargar() -> None:
    """Refresca el cache (común y por canal). Llamar al arrancar y tras cada cambio."""
    global _version
    nuevo: dict[str, dict[str, dict]] = {}
    for c in (COMUN, *await _canales()):
        leido = await _leer_hash(key_canal(K_AGENTES, c))
        # Un blip de Redis no debe VACIAR los agentes en memoria.
        nuevo[c] = leido if leido is not None else _cache.get(c, {})
    _cache.clear()
    _cache.update(nuevo)
    _version += 1
    log.info(
        "agentes_custom_cargados",
        por_canal={(c or "comun"): sorted(v) for c, v in nuevo.items() if v},
    )


async def guardar(
    nombre: str,
    descripcion: str,
    herramientas: list[str],
    palabras: list[str],
    modelo: str = "mini",
    activo: bool = True,
    canal: str = COMUN,
    ambos: bool = False,
) -> dict:
    """Crea o actualiza un agente. El prompt se guarda aparte (prompt_store).

    Sin `canal` (o con `ambos`) el agente atiende en los DOS números; con `canal`
    sólo en ese, y no aparece en el otro.
    """
    n = str(nombre).strip().lower()
    motivo = validar(n, herramientas, modelo)
    if motivo:
        raise ValueError(motivo)
    c = COMUN if ambos else canal_id(canal)
    existentes = _cache.get(c, {})
    if n not in existentes and len(existentes) >= MAX_AGENTES:
        raise ValueError(f"Máximo {MAX_AGENTES} agentes personalizados por canal.")

    obj = {
        "nombre": n,
        "descripcion": str(descripcion or "").strip()[:300],
        "herramientas": [h for h in herramientas if h in PACKS],
        "palabras": [
            p.strip().lower() for p in (palabras or []) if str(p).strip()
        ][:40],
        "modelo": modelo,
        "activo": bool(activo),
        "canal": c,
        "ts": time.time(),
    }

    async def _op(r: Any) -> None:
        await r.hset(key_canal(K_AGENTES, c), n, json.dumps(obj, ensure_ascii=False))

    await run_write(_op)
    await cargar()
    log.info(
        "agente_custom_guardado",
        agente=n, canal=c or "ambos", herramientas=obj["herramientas"],
    )
    return obj


async def borrar(nombre: str, canal: str = COMUN) -> bool:
    """Borra el agente del canal indicado; si no está ahí, el común."""
    n = str(nombre or "").strip().lower()
    c = canal_id(canal)
    destino = c if (c and n in _cache.get(c, {})) else COMUN
    if n not in _cache.get(destino, {}):
        return False
    await run_write(lambda r: r.hdel(key_canal(K_AGENTES, destino), n))
    # También el prompt, para no dejar basura en Redis. Si el agente era propio de un
    # canal, sólo se borra el prompt de ese canal.
    try:
        from app.panel import prompt_store

        await prompt_store.guardar(n, "", canal=destino, ambos=not destino)
    except Exception as exc:  # noqa: BLE001
        log.warning("agente_custom_prompt_no_borrado", agente=n, error=str(exc))
    await cargar()
    log.info("agente_custom_borrado", agente=n, canal=destino or "comun")
    return True
