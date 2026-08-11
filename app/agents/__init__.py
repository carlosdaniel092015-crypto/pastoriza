"""Sistema multi-agente: enrutador → especialista (ventas / pedido / soporte).

Reemplaza al agente único anterior. `RespuestaBot` sigue siendo la salida estructurada
común; `ESPECIALISTAS` mapea nombre→Agent; `elegir_agente` es el enrutado determinista.
"""
from app.agents.base import RespuestaBot
from app.agents.enrutador import elegir_agente
from app.agents.especialistas import ESPECIALISTAS

__all__ = ["RespuestaBot", "ESPECIALISTAS", "elegir_agente"]
