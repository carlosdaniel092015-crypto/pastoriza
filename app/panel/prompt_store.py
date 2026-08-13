"""Prompts POR AGENTE, editables desde el panel.

Cada agente (base_comun, ventas, pedido, soporte, enrutador) tiene:
  - un `.md` base versionado en `prompts/{agente}.md`, y
  - opcionalmente un override en Redis (`pastoriza:panel:prompt:{agente}`) que el panel
    puede subir/pegar y se aplica al instante.

El prompt EFECTIVO de un agente = override (si existe) o el `.md` base.

El SDK arma las instrucciones de forma SÍNCRONA, así que mantenemos ambos (base y override)
en cache de memoria de proceso, refrescado al arrancar (`cargar`) y en cada `guardar`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.logging_conf import get_logger
from app.redis_client import run_write, with_reconnect
from app.settings import settings

log = get_logger(__name__)

AGENTES = ("base_comun", "ventas", "pedido", "soporte", "enrutador")
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def agentes() -> tuple[str, ...]:
    """Agentes editables: los base + los PERSONALIZADOS creados desde el panel.

    Los personalizados no tienen `.md` base (su prompt vive sólo como override),
    así que `get_base` devuelve "" para ellos y el panel muestra el editor vacío.
    """
    from app.panel import agentes_custom

    return AGENTES + tuple(n for n in agentes_custom.nombres() if n not in AGENTES)

# Caches de proceso.
_base: dict[str, str] = {}
_override: dict[str, str | None] = {}
_cargado = False


def _key(agente: str) -> str:
    return settings.key("panel", "prompt", agente)


def _leer_md(agente: str) -> str:
    ruta = PROMPTS_DIR / f"{agente}.md"
    try:
        return ruta.read_text(encoding="utf-8").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("prompt_md_no_leido", agente=agente, error=str(exc))
        return ""


def get_prompt(agente: str) -> str:
    """Prompt efectivo del agente (SÍNCRONO): override si existe, si no el .md base."""
    ov = _override.get(agente)
    if ov:
        return ov
    return _base.get(agente, "")


def get_base(agente: str) -> str:
    return _base.get(agente, "")


def usando_override(agente: str) -> bool:
    return bool(_override.get(agente))


async def cargar() -> None:
    """Carga los .md base y los overrides de Redis. Llamar al arrancar."""
    global _cargado
    for agente in agentes():
        # Los personalizados no tienen .md: su prompt es sólo el override.
        _base[agente] = _leer_md(agente) if agente in AGENTES else ""

        async def _op(r: Any, a: str = agente) -> str | None:
            return await r.get(_key(a))

        try:
            _override[agente] = await with_reconnect(_op)
        except Exception as exc:  # noqa: BLE001
            log.warning("prompt_override_load_fallo", agente=agente, error=str(exc))
            _override[agente] = None
    _cargado = True


async def guardar(agente: str, texto: str) -> None:
    """Guarda/borra el override de un agente y actualiza el cache al instante."""
    if agente not in agentes():
        raise ValueError(f"agente inválido: {agente}")
    texto = (texto or "").strip()

    async def _op(r: Any) -> None:
        if texto:
            await r.set(_key(agente), texto)
        else:
            await r.delete(_key(agente))

    await run_write(_op)
    _override[agente] = texto or None
    log.info("prompt_guardado", agente=agente, vacio=not bool(texto), largo=len(texto))
