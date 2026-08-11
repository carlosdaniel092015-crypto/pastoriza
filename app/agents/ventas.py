"""Agente VENTAS / CATÁLOGO (modelo mini).

Descubrimiento de productos (texto y foto) + cotización. NO cierra pedidos.
Prompt: prompts/ventas.md (+ base_comun.md). Editable/subible desde el panel.
"""
from __future__ import annotations

from app.agents.base import crear_especialista
from app.settings import settings
from app.tools.catalogo_tools import (
    buscar_por_foto,
    buscar_producto,
    detalle_producto,
    link_tienda,
    listar_catalogo,
)
from app.tools.cotizar_tools import cotizar
from app.tools.odoo_tools import escalar_a_humano

TOOLS = [
    buscar_producto,
    listar_catalogo,
    detalle_producto,
    buscar_por_foto,
    link_tienda,
    cotizar,
    escalar_a_humano,
]

ventas = crear_especialista("ventas", TOOLS, settings.model_mini)
