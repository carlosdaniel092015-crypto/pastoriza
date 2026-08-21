"""Cotización determinista. El modelo NO calcula: sólo pasa datos y lee el resultado."""
from __future__ import annotations

from dataclasses import dataclass

from agents import RunContextWrapper, function_tool

from app.context import ConversationContext
from app.estado import guardar_cotizacion, guardar_cotizacion_subtotal
from app.logging_conf import get_logger

log = get_logger(__name__)


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


async def cotizar_impl(
    ctx: ConversationContext,
    precio_unitario: float,
    cantidad: int,
    modalidad: str = "",
    producto_id: int = 0,
) -> str:
    """La lógica de `cotizar`, sin el envoltorio del SDK.

    Separada como `crear_pedido_impl` (ver odoo_tools.py) para poder testear el AVISO
    de pedido mínimo llamándola directo, sin montar un Runner.
    """
    cfg = ctx.cfg
    # Blindaje de precio (ADR-006), igual que agregar_linea_pedido: la línea del
    # pedido SIEMPRE usa el precio del catálogo, así que si aquí cotizáramos con
    # otro, el cliente vería un total y pagaría uno distinto (la corrección era muda).
    if producto_id:
        try:
            from app.catalogo import catalogo

            p = await catalogo.por_tmpl_id(int(producto_id))
        except Exception:  # noqa: BLE001
            p = None
        if p:
            corregido = abs(float(precio_unitario) - p.precio_con_itbis) > 0.01
            if corregido:
                log.warning(
                    "cotizar_precio_corregido",
                    chat_id=ctx.chat_id,
                    producto=p.nombre,
                    enviado=precio_unitario,
                    real=p.precio_con_itbis,
                )
                ctx.marcar_revision("cotizar_precio_corregido")
                precio_unitario = p.precio_con_itbis
    try:
        c = calcular(precio_unitario, cantidad, modalidad, cfg.precio_envio_num)
    except ValueError as exc:
        if "cantidad" in str(exc):
            ctx.marcar_revision("cantidad_invalida")
        return f"ERROR: {exc}. Confirma el dato con el cliente antes de seguir."

    if c.cantidad > 5000:
        ctx.marcar_revision("cantidad_muy_alta")

    # Queda registrado como EFECTO de la tool (no lo declara el modelo): es lo que
    # alimenta el semáforo de cierre del panel. Se guarda la cotización más grande del
    # turno, que es la que mejor representa la intención.
    if c.total_final >= ctx.cotizado_total:
        ctx.cotizado_unidades = c.cantidad
        ctx.cotizado_total = c.total_final
        ctx.cotizado_subtotal = c.subtotal_sin_itbis
    if c.modalidad:
        ctx.cotizado_modalidad = c.modalidad
    # Además se persiste: el comprobante llega en un turno POSTERIOR y hay que tener
    # contra qué comparar el monto transferido (ver estado.guardar_cotizacion).
    await guardar_cotizacion(ctx.chat_id, c.total_final)
    await guardar_cotizacion_subtotal(ctx.chat_id, c.subtotal_sin_itbis)

    minimo = cfg.monto_minimo_num
    bajo_minimo = minimo > 0 and c.subtotal_sin_itbis < minimo

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
    if bajo_minimo:
        # Regla de negocio: informarlo SIEMPRE que el subtotal no llegue, sin que el
        # cliente lo pregunte. No se deja como sugerencia del prompt nada más: va en la
        # propia cotización que el modelo tiene que mostrar (ver `crear_pedido_impl`,
        # que además bloquea el pedido si esto no se corrigió).
        lineas += [
            "",
            f"AVISO (decíselo al cliente, con amabilidad): el pedido minimo es "
            f"RD${minimo:,.2f} de subtotal (sin ITBIS ni envio) y esta cotizacion solo "
            f"llega a RD${c.subtotal_sin_itbis:,.2f}. Ofrecele sumar mas unidades o "
            "productos hasta alcanzarlo.",
        ]
    lineas += ["", f"price_unit para la línea del pedido: {c.precio_unitario:.2f}"]
    return "\n".join(lineas)


@function_tool
async def cotizar(
    ctx: RunContextWrapper[ConversationContext],
    precio_unitario: float,
    cantidad: int,
    modalidad: str = "",
    producto_id: int = 0,
) -> str:
    """Calcula la cotización con ITBIS y envío. Úsala SIEMPRE; nunca calcules a mano.

    Args:
        precio_unitario: Precio unitario CON ITBIS, tal como lo devolvió el catálogo.
        cantidad: Cantidad de unidades, entero positivo.
        modalidad: "envio", "retiro" o vacío si el cliente aún no eligió.
        producto_id: id del producto que estás cotizando (el `id=` que devolvió la
            búsqueda). PÁSALO SIEMPRE: con él se verifica el precio contra el catálogo.
    """
    return await cotizar_impl(ctx.context, precio_unitario, cantidad, modalidad, producto_id)


COTIZAR_TOOLS = [cotizar]
