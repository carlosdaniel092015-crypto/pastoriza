"""Cotización determinista. El modelo NO calcula: sólo pasa datos y lee el resultado."""
from __future__ import annotations

from dataclasses import dataclass

from agents import RunContextWrapper, function_tool

from app.context import ConversationContext


@dataclass
class Cotizacion:
    cantidad: int
    precio_unitario: float
    subtotal_sin_itbis: float
    itbis: float
    total_productos: float
    envio: float
    total_final: float
    modalidad: str


def calcular(
    precio_unitario: float,
    cantidad: int,
    modalidad: str = "",
    precio_envio: float = 550.0,
) -> Cotizacion:
    """Función pura: testeable sin Odoo ni OpenAI (ver tests/test_cotizar.py)."""
    if precio_unitario <= 0:
        raise ValueError("precio_unitario debe ser mayor que 0")
    if cantidad <= 0 or cantidad > 100_000 or int(cantidad) != cantidad:
        raise ValueError("cantidad inválida")

    r = lambda x: round(x + 1e-9, 2)  # noqa: E731
    sub_con = r(precio_unitario * cantidad)
    sub_sin = r(sub_con / 1.18)
    itbis = r(sub_con - sub_sin)

    m = modalidad.lower()
    m = "envio" if m.startswith("env") else ("retiro" if m.startswith("ret") else "")
    envio = precio_envio if m == "envio" else 0.0

    return Cotizacion(
        cantidad=int(cantidad),
        precio_unitario=r(precio_unitario),
        subtotal_sin_itbis=sub_sin,
        itbis=itbis,
        total_productos=sub_con,
        envio=envio,
        total_final=r(sub_con + envio),
        modalidad=m,
    )


@function_tool
async def cotizar(
    ctx: RunContextWrapper[ConversationContext],
    precio_unitario: float,
    cantidad: int,
    modalidad: str = "",
) -> str:
    """Calcula la cotización con ITBIS y envío. Úsala SIEMPRE; nunca calcules a mano.

    Args:
        precio_unitario: Precio unitario CON ITBIS, tal como lo devolvió el catálogo.
        cantidad: Cantidad de unidades, entero positivo.
        modalidad: "envio", "retiro" o vacío si el cliente aún no eligió.
    """
    cfg = ctx.context.cfg
    try:
        c = calcular(precio_unitario, cantidad, modalidad, cfg.precio_envio_num)
    except ValueError as exc:
        if "cantidad" in str(exc):
            ctx.context.marcar_revision("cantidad_invalida")
        return f"ERROR: {exc}. Confirma el dato con el cliente antes de seguir."

    if c.cantidad > 5000:
        ctx.context.marcar_revision("cantidad_muy_alta")

    lineas = [
        "COTIZACION (para mostrar al cliente):",
        f"Cantidad: {c.cantidad}",
        f"Precio unitario (ITBIS incluido): RD${c.precio_unitario:.2f}",
        f"Subtotal (sin ITBIS): RD${c.subtotal_sin_itbis:.2f}",
        f"ITBIS 18%: RD${c.itbis:.2f}",
    ]
    if c.modalidad == "envio":
        lineas += [f"Envio: RD${c.envio:.2f}", f"TOTAL: RD${c.total_final:.2f}"]
    elif c.modalidad == "retiro":
        lineas += [
            "Retiro en tienda (sin costo de envio)",
            f"TOTAL: RD${c.total_final:.2f}",
        ]
    else:
        con_envio = round(c.total_productos + cfg.precio_envio_num, 2)
        lineas += [
            f"TOTAL productos: RD${c.total_productos:.2f}",
            f"(Si es envio a domicilio, suma RD${cfg.precio_envio_num:.2f} "
            f"-> TOTAL con envio: RD${con_envio:.2f})",
        ]
    lineas += ["", f"price_unit para la línea del pedido: {c.precio_unitario:.2f}"]
    return "\n".join(lineas)


COTIZAR_TOOLS = [cotizar]
