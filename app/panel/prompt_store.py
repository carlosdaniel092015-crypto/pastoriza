"""Prompts POR AGENTE y POR CANAL, editables desde el panel.

Cada agente (base_comun, ventas, pedido, soporte, enrutador) tiene:
  - un `.md` base versionado en `prompts/{agente}.md`,
  - opcionalmente un override COMÚN en Redis (`pastoriza:panel:prompt:{agente}`), y
  - opcionalmente un override PROPIO DE UN CANAL
    (`pastoriza:panel:prompt:{agente}:c:{canal}`).

El prompt EFECTIVO de un agente en un canal = override del canal, si no el override
común, si no el `.md` base. Así el 6701 puede tener su propia instrucción sin tocar
al 1092, y "aplicar a ambos" escribe el común y borra los propios.

El SDK arma las instrucciones de forma SÍNCRONA, así que mantenemos base y overrides
(de todos los canales) en cache de memoria de proceso, refrescado al arrancar
(`cargar`) y en cada `guardar`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.canales import COMUN, canal_id, key_canal
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

    return AGENTES + tuple(n for n in agentes_custom.nombres_todos() if n not in AGENTES)

# Caches de proceso.
_base: dict[str, str] = {}
# canal -> agente -> texto ("" = canal común)
_override: dict[str, dict[str, str]] = {COMUN: {}}
_cargado = False


def _key(agente: str, canal: str = COMUN) -> str:
    return key_canal(settings.key("panel", "prompt", agente), canal)


def _leer_md(agente: str) -> str:
    ruta = PROMPTS_DIR / f"{agente}.md"
    try:
        return ruta.read_text(encoding="utf-8").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("prompt_md_no_leido", agente=agente, error=str(exc))
        return ""


def get_prompt(agente: str, canal: str = COMUN) -> str:
    """Prompt efectivo (SÍNCRONO): override del canal -> override común -> .md base."""
    c = canal_id(canal)
    if c:
        propio = _override.get(c, {}).get(agente)
        if propio:
            return propio
    comun = _override.get(COMUN, {}).get(agente)
    if comun:
        return comun
    return _base.get(agente, "")


def get_base(agente: str) -> str:
    return _base.get(agente, "")


def origen(agente: str, canal: str = COMUN) -> str:
    """'canal' | 'comun' | 'base': de dónde sale el prompt que se está usando."""
    c = canal_id(canal)
    if c and _override.get(c, {}).get(agente):
        return "canal"
    if _override.get(COMUN, {}).get(agente):
        return "comun"
    return "base"


def usando_override(agente: str, canal: str = COMUN) -> bool:
    return origen(agente, canal) != "base"


def canales_cargados() -> tuple[str, ...]:
    return tuple(c for c in _override if c)


async def _canales() -> tuple[str, ...]:
    """Canales cuyos overrides hay que tener en memoria (los configurados + vistos)."""
    from app.business_config import canales_configurados

    return tuple({*await canales_configurados(), *canales_cargados()})


async def cargar() -> None:
    """Carga los .md base y los overrides (común y por canal). Llamar al arrancar."""
    global _cargado
    canales = (COMUN, *await _canales())
    nuevo: dict[str, dict[str, str]] = {c: {} for c in canales}
    for agente in agentes():
        # Los personalizados no tienen .md: su prompt es sólo el override.
        _base[agente] = _leer_md(agente) if agente in AGENTES else ""
        for canal in canales:
            async def _op(r: Any, a: str = agente, c: str = canal) -> str | None:
                return await r.get(_key(a, c))

            try:
                texto = await with_reconnect(_op)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "prompt_override_load_fallo",
                    agente=agente, canal=canal or "comun", error=str(exc),
                )
                # No borrar de memoria lo que ya teníamos por un blip de Redis.
                texto = _override.get(canal, {}).get(agente)
            if texto:
                nuevo[canal][agente] = texto
    _override.clear()
    _override.update(nuevo)
    _cargado = True
    log.info(
        "prompts_cargados",
        canales=[c or "comun" for c in canales],
        overrides={c or "comun": sorted(v) for c, v in nuevo.items() if v},
    )


async def guardar(
    agente: str, texto: str, canal: str = COMUN, ambos: bool = False
) -> None:
    """Guarda/borra un override y actualiza el cache al instante.

    Sin `canal` (o con `ambos`) toca el COMÚN y borra los propios de cada canal, así
    los dos números quedan con el mismo prompt. Con `canal` sólo cambia ese número.
    """
    if agente not in agentes():
        raise ValueError(f"agente inválido: {agente}")
    texto = (texto or "").strip()
    c = canal_id(canal)
    canales = await _canales()

    if ambos or not c:
        async def _op_comun(r: Any) -> None:
            if texto:
                await r.set(_key(agente, COMUN), texto)
            else:
                await r.delete(_key(agente, COMUN))
            for otro in canales:
                await r.delete(_key(agente, otro))

        await run_write(_op_comun)
        _override.setdefault(COMUN, {})
        if texto:
            _override[COMUN][agente] = texto
        else:
            _override[COMUN].pop(agente, None)
        for otro in canales:
            _override.setdefault(otro, {}).pop(agente, None)
        log.info("prompt_guardado", agente=agente, canal="ambos", largo=len(texto))
        return

    async def _op(r: Any) -> None:
        if texto:
            await r.set(_key(agente, c), texto)
        else:
            await r.delete(_key(agente, c))

    await run_write(_op)
    _override.setdefault(c, {})
    if texto:
        _override[c][agente] = texto
    else:
        _override[c].pop(agente, None)
    log.info("prompt_guardado", agente=agente, canal=c, largo=len(texto))
