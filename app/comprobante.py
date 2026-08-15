"""Leer el MONTO de un comprobante de transferencia. Función pura, testeable.

Regla del negocio: en ENVÍO el pedido no se crea sin comprobante, y el comprobante
tiene que ser por el total de la factura **o más**. Acá va sólo la lectura del monto;
quién bloquea la creación es `tools/odoo_tools.crear_pedido` (regla dura en código, no
en el prompt).

La entrada es la descripción que devuelve la visión (`media.PROMPT_IMAGEN`), del estilo:

    COMPROBANTE_PAGO: [Banco Popular, monto RD$5,860.00, referencia 004512345, 14/08/2026]

Dos decisiones que importan:

- **Sólo se leen números marcados como dinero** (`RD$`, `DOP`, `$`). Un comprobante trae
  número de referencia, cuenta y fecha; sin esa marca, cualquiera de esos se podría leer
  como el monto y bloquear un pago bueno.
- **Si no se puede leer el monto, no se afirma nada** (`None`). Ante duda no se bloquea:
  el pago igual lo aprueba una persona que ve la foto, y un falso "no coincide" le dice
  a un cliente que pagó bien que no pagó.
"""
from __future__ import annotations

import re

# Tolerancia en pesos: el cliente puede transferir 5,859.99 por un redondeo del banco,
# y eso no es un pago corto.
TOLERANCIA = 1.0

_RE_MONEDA = re.compile(r"(?:RD\s*\$|DOP\s*\$?|\$)\s*([\d][\d.,]*)", re.IGNORECASE)


def _a_float(bruto: str) -> float | None:
    """'5,860.00' -> 5860.0 · '5.860' -> 5860.0 · '5,86' -> 5.86.

    En RD se escribe como en EE.UU. (coma de miles, punto decimal), pero los bancos y el
    OCR mezclan formatos, así que se decide por la POSICIÓN del último separador: si deja
    1 o 2 dígitos al final es decimal; si deja 3, era separador de miles.
    """
    s = str(bruto or "").strip().strip(".,")
    if not re.fullmatch(r"[\d.,]+", s or "") or not re.search(r"\d", s):
        return None

    ultimo = max(s.rfind("."), s.rfind(","))
    if ultimo == -1:
        entero, decimales = s, ""
    else:
        decimales = s[ultimo + 1 :]
        if len(decimales) in (1, 2):
            entero = s[:ultimo]
        else:  # 3 dígitos (o más) = separador de miles, no decimal
            entero, decimales = s, ""

    entero = re.sub(r"[.,]", "", entero)
    if not entero:
        return None
    try:
        return float(f"{entero}.{decimales or '0'}")
    except ValueError:
        return None


def montos_de(texto: str) -> list[float]:
    """Todos los importes con marca de moneda que aparecen en la descripción."""
    out: list[float] = []
    for bruto in _RE_MONEDA.findall(str(texto or "")):
        v = _a_float(bruto)
        if v is not None and v > 0:
            out.append(v)
    return out


def monto_pagado(texto: str) -> float | None:
    """El monto transferido, o None si no se puede leer.

    Se toma el MAYOR de los importes: un comprobante suele mostrar también el balance
    de la cuenta o un cargo por servicio, y quedarse con el menor haría rebotar pagos
    correctos. Quedarse con el mayor puede dejar pasar uno corto — y eso lo ve el
    supervisor en la foto, que es quien aprueba.
    """
    montos = montos_de(texto)
    return max(montos) if montos else None


def faltante(texto: str, total: float) -> float | None:
    """Cuánto falta para cubrir `total`, o None si no aplica bloquear.

    None = o no se pudo leer el monto, o no hay total con qué comparar, o el pago
    alcanza. Un número > 0 = el comprobante es por menos y ESO sí se le dice al cliente.
    """
    try:
        objetivo = float(total or 0)
    except (TypeError, ValueError):
        return None
    if objetivo <= 0:
        return None
    pagado = monto_pagado(texto)
    if pagado is None:
        return None
    if pagado + TOLERANCIA >= objetivo:
        return None
    return round(objetivo - pagado, 2)


__all__ = ["TOLERANCIA", "faltante", "monto_pagado", "montos_de"]
