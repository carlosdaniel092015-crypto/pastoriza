"""Cuánto cuesta en DÓLARES lo que gastó el bot.

Es una PROYECCIÓN, no una factura: se calcula con los tokens que reportó el SDK por la
tarifa del modelo. La factura real de OpenAI puede diferir — sobre todo porque el
descuento por prompt caching (la parte repetida del prompt sale más barata) acá no se
descuenta, así que este número es un TECHO, nunca una sorpresa hacia arriba.

OJO: los precios los pone OpenAI y CAMBIAN. Están acá, en un solo lugar y con la fecha
en que se anotaron, para que actualizarlos sea editar esta tabla y nada más. Si un
modelo no está en la tabla, su coste se informa como desconocido en vez de inventar un
número (un cero mentiría diciendo que ese agente es gratis).
"""
from __future__ import annotations

import re

# USD por 1.000.000 de tokens: (entrada, salida). Anotados 2026-08.
# Verificalos en la página de precios de OpenAI antes de tomar decisiones de plata.
PRECIOS: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o4-mini": (1.10, 4.40),
}

# Los ids de modelo suelen venir con la fecha de la versión ("gpt-4o-2024-08-06"): se
# recorta para no tener que anotar una fila por cada snapshot.
_RE_FECHA = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{4})$")


def normalizar(modelo: str) -> str:
    return _RE_FECHA.sub("", str(modelo or "").strip().lower())


def tarifa(modelo: str) -> tuple[float, float] | None:
    """(USD/1M entrada, USD/1M salida) o None si ese modelo no está en la tabla."""
    m = normalizar(modelo)
    if m in PRECIOS:
        return PRECIOS[m]
    # Un snapshot desconocido de una familia conocida ("gpt-4o-mini-algo-nuevo") es
    # mejor cobrarlo con la tarifa de la familia que declararlo desconocido.
    for nombre in sorted(PRECIOS, key=len, reverse=True):
        if m.startswith(nombre):
            return PRECIOS[nombre]
    return None


def costo(modelo: str, entrada: int, salida: int) -> float | None:
    """USD de esos tokens con ese modelo. None si no se conoce la tarifa."""
    t = tarifa(modelo)
    if t is None:
        return None
    p_in, p_out = t
    return (int(entrada or 0) * p_in + int(salida or 0) * p_out) / 1_000_000


def fmt(usd: float | None) -> str:
    """Para mostrar: los costes por mensaje son fracciones de centavo, así que un
    redondeo a 2 decimales los aplastaría todos a $0.00."""
    if usd is None:
        return "—"
    if usd == 0:
        return "$0.00"
    if usd < 0.01:
        return f"${usd:.5f}"
    if usd < 1:
        return f"${usd:.4f}"
    return f"${usd:,.2f}"


__all__ = ["PRECIOS", "costo", "fmt", "normalizar", "tarifa"]
