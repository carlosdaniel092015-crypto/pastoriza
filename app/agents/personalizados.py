"""Construye los Agent de los agentes PERSONALIZADOS creados desde el panel.

El registro (nombre, herramientas, palabras, modelo) vive en
`app/panel/agentes_custom.py`; el prompt en `prompt_store`. Aquí se arma el Agent
del SDK con los packs de herramientas elegidos, y se cachea hasta que el registro
cambie (para no reconstruirlo en cada turno).
"""
from __future__ import annotations

from agents import Agent

from app.agents.base import crear_especialista
from app.context import ConversationContext
from app.logging_conf import get_logger
from app.panel import agentes_custom
from app.settings import settings
from app.tools.catalogo_tools import CATALOGO_TOOLS
from app.tools.cotizar_tools import COTIZAR_TOOLS
from app.tools.odoo_tools import ODOO_TOOLS, buscar_pedidos_cliente, escalar_a_humano

log = get_logger(__name__)

PACK_TOOLS = {
    "catalogo": CATALOGO_TOOLS,
    "cotizar": COTIZAR_TOOLS,
    "pedido": ODOO_TOOLS,
    "pedidos_cliente": [buscar_pedidos_cliente],
}

# nombre -> (version_del_registro, Agent)
_construidos: dict[str, tuple[int, Agent]] = {}


def _tools_de(cfg: dict) -> list:
    tools: list = []
    vistos: set[int] = set()
    for pack in cfg.get("herramientas", []):
        for t in PACK_TOOLS.get(pack, []):
            if id(t) not in vistos:
                vistos.add(id(t))
                tools.append(t)
    # escalar_a_humano SIEMPRE disponible: está blindada por el determinador
    # (ver odoo_tools.escalar_a_humano), así que no puede escalar de más.
    if id(escalar_a_humano) not in vistos:
        tools.append(escalar_a_humano)
    return tools


def obtener(nombre: str) -> Agent[ConversationContext] | None:
    """Agent del especialista personalizado `nombre`, o None si no existe/está off."""
    cfg = agentes_custom.get(nombre)
    if not cfg or not cfg.get("activo", True):
        return None

    ver = agentes_custom.version()
    cacheado = _construidos.get(nombre)
    if cacheado and cacheado[0] == ver:
        return cacheado[1]

    modelo = settings.model_agente if cfg.get("modelo") == "agente" else settings.model_mini
    agente = crear_especialista(nombre, _tools_de(cfg), modelo)
    _construidos[nombre] = (ver, agente)
    log.info(
        "agente_custom_construido",
        agente=nombre, modelo=modelo, tools=len(_tools_de(cfg)),
    )
    return agente
