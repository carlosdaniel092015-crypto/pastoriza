"""Registro de especialistas. Cada agente vive en su propio módulo:
`ventas.py`, `pedido.py`, `soporte.py`.
"""
from __future__ import annotations

from app.agents.pedido import pedido
from app.agents.soporte import soporte
from app.agents.ventas import ventas

ESPECIALISTAS = {"ventas": ventas, "pedido": pedido, "soporte": soporte}


def obtener(nombre: str):
    """Agent para `nombre`: base o PERSONALIZADO (creado desde el panel).

    Cae a `ventas` si el nombre no existe (agente borrado a mitad de un turno,
    o un enrutado raro): mejor atender con ventas que romper el turno.
    """
    base = ESPECIALISTAS.get(nombre)
    if base is not None:
        return base
    # Import perezoso: personalizados importa las tools, que importan el catálogo.
    from app.agents.personalizados import obtener as obtener_custom

    return obtener_custom(nombre) or ESPECIALISTAS["ventas"]
