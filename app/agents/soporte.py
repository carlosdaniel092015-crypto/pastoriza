"""Agente SOPORTE / RECLAMOS (modelo mini).

Quejas, cancelaciones (escala, NO cancela) y dudas fuera de venta.
Prompt: prompts/soporte.md (+ base_comun.md). Editable/subible desde el panel.
"""
from __future__ import annotations

from app.agents.base import crear_especialista
from app.settings import settings
from app.tools.odoo_tools import buscar_pedidos_cliente, escalar_a_humano

TOOLS = [escalar_a_humano, buscar_pedidos_cliente]

soporte = crear_especialista("soporte", TOOLS, settings.model_mini)
