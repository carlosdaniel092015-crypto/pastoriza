"""Compatibilidad. El agente único evolucionó a un sistema multi-agente en
`app/agents/` (enrutador → ventas / pedido / soporte). Se re-exporta `RespuestaBot`
para no romper imports antiguos.
"""
from app.agents import ESPECIALISTAS, RespuestaBot, elegir_agente

__all__ = ["RespuestaBot", "ESPECIALISTAS", "elegir_agente"]
