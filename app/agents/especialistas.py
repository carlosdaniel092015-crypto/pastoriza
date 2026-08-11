"""Registro de especialistas. Cada agente vive en su propio módulo:
`ventas.py`, `pedido.py`, `soporte.py`.
"""
from __future__ import annotations

from app.agents.pedido import pedido
from app.agents.soporte import soporte
from app.agents.ventas import ventas

ESPECIALISTAS = {"ventas": ventas, "pedido": pedido, "soporte": soporte}
