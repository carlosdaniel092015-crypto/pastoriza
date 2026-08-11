"""Agente PEDIDO / CHECKOUT (gpt-4o, el delicado).

Cierra la venta: verifica/crea contacto, toma la dirección detallada y registra el
pedido. Prompt: prompts/pedido.md (+ base_comun.md). Editable/subible desde el panel.
"""
from __future__ import annotations

from app.agents.base import crear_especialista
from app.settings import settings
from app.tools.catalogo_tools import detalle_producto
from app.tools.cotizar_tools import cotizar
from app.tools.odoo_tools import (
    actualizar_contacto,
    agregar_linea_pedido,
    buscar_pedidos_cliente,
    crear_contacto,
    crear_pedido,
    escalar_a_humano,
    verificar_contacto,
)

TOOLS = [
    detalle_producto,
    cotizar,
    verificar_contacto,
    crear_contacto,
    actualizar_contacto,
    crear_pedido,
    agregar_linea_pedido,
    buscar_pedidos_cliente,
    escalar_a_humano,
]

pedido = crear_especialista("pedido", TOOLS, settings.model_agente)
