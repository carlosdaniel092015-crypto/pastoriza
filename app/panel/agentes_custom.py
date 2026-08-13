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

Este módulo es a propósito LIVIANO (sólo Redis): lo importa `prompt_store` y el
enrutador. La construcción del Agent vive en `app/agents/personalizados.py`.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

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

# Cache de proceso: el enrutado y el armado de instrucciones son SÍNCRONOS.
_cache: dict[str, dict] = {}
_version = 0


def version() -> int:
    """Sube en cada cambio; sirve para invalidar los Agent ya construidos."""
    return _version


def nombres() -> tuple[str, ...]:
    return tuple(sorted(_cache))


def listar() -> list[dict]:
    return [dict(v) for _, v in sorted(_cache.items())]


def get(nombre: str) -> dict | None:
    a = _cache.get(str(nombre or "").strip().lower())
    return dict(a) if a else None


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


async def cargar() -> None:
    """Refresca el cache desde Redis. Llamar al arrancar y tras cada cambio."""
    global _cache, _version
    try:
        raw = await with_reconnect(lambda r: r.hgetall(K_AGENTES))
    except Exception as exc:  # noqa: BLE001
        log.warning("agentes_custom_load_fallo", error=str(exc))
        return
    out: dict[str, dict] = {}
    for k, v in (raw or {}).items():
        try:
            obj = json.loads(v)
            if isinstance(obj, dict) and obj.get("nombre"):
                out[str(obj["nombre"])] = obj
        except json.JSONDecodeError:
            continue
    _cache = out
    _version += 1
    log.info("agentes_custom_cargados", total=len(out), nombres=list(out))


async def guardar(
    nombre: str,
    descripcion: str,
    herramientas: list[str],
    palabras: list[str],
    modelo: str = "mini",
    activo: bool = True,
) -> dict:
    """Crea o actualiza un agente. El prompt se guarda aparte (prompt_store)."""
    n = str(nombre).strip().lower()
    motivo = validar(n, herramientas, modelo)
    if motivo:
        raise ValueError(motivo)
    if n not in _cache and len(_cache) >= MAX_AGENTES:
        raise ValueError(f"Máximo {MAX_AGENTES} agentes personalizados.")

    obj = {
        "nombre": n,
        "descripcion": str(descripcion or "").strip()[:300],
        "herramientas": [h for h in herramientas if h in PACKS],
        "palabras": [
            p.strip().lower() for p in (palabras or []) if str(p).strip()
        ][:40],
        "modelo": modelo,
        "activo": bool(activo),
        "ts": time.time(),
    }

    async def _op(r: Any) -> None:
        await r.hset(K_AGENTES, n, json.dumps(obj, ensure_ascii=False))

    await run_write(_op)
    await cargar()
    log.info("agente_custom_guardado", agente=n, herramientas=obj["herramientas"])
    return obj


async def borrar(nombre: str) -> bool:
    n = str(nombre or "").strip().lower()
    if n not in _cache:
        return False
    await run_write(lambda r: r.hdel(K_AGENTES, n))
    # También el prompt, para no dejar basura en Redis.
    try:
        from app.panel import prompt_store

        await prompt_store.guardar(n, "")
    except Exception as exc:  # noqa: BLE001
        log.warning("agente_custom_prompt_no_borrado", agente=n, error=str(exc))
    await cargar()
    log.info("agente_custom_borrado", agente=n)
    return True
