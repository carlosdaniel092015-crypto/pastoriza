"""Tools de catálogo: buscar, detalle, buscar por foto, link de tienda."""
from __future__ import annotations

import json

from agents import RunContextWrapper, function_tool

from app.catalogo import catalogo
from app.context import ConversationContext
from app.logging_conf import get_logger
from app.matching import score_ficha
from app.media import ficha_visual_de_url
from app.odoo import odoo
from app.settings import settings

log = get_logger(__name__)

PARAM_FICHAS = "pastoriza.imgsigs"


@function_tool
async def buscar_producto(
    ctx: RunContextWrapper[ConversationContext], busqueda: str
) -> str:
    """Busca productos en el catálogo de Pastoriza Plastics.

    Úsala SIEMPRE antes de nombrar un producto o dar un precio. Nunca inventes
    productos ni precios. Los precios devueltos ya incluyen ITBIS.

    Args:
        busqueda: Lo que pidió el cliente, en sus palabras. Ej: "botella lisa 8 oz",
            "galones", "botellon sin tapa".
    """
    c = ctx.context
    # limite alto: si el cliente pide una MEDIDA ("las de 12 oz"), queremos TODOS
    # los publicados de esa medida, no solo los primeros 5.
    veredicto, productos = await catalogo.buscar(
        busqueda, limite=settings.max_imagenes_por_mensaje
    )
    c.ofrecer(productos)

    if veredicto == "match_fuerte":
        p = productos[0]
        return (
            "MATCH_FUERTE: el producto existe y coincide claramente.\n"
            f"- id={p.tmpl_id} | {p.nombre} | RD${p.precio_con_itbis:.2f} (ITBIS incl.)\n"
            "Confírmalo POR SU NOMBRE y ponlo en mostrar_productos. No pidas más datos."
        )

    if veredicto == "muy_general":
        c.marcar_revision("busqueda_ambigua")
        return (
            f'BUSQUEDA_MUY_GENERAL: "{busqueda}" no alcanza para identificar un '
            "producto. Pide UN dato más (capacidad en oz o galón, color, tipo de "
            "tapa). NO muestres productos al azar."
        )

    if veredicto == "vacio" or not productos:
        return "No hay productos disponibles en este momento."

    lineas = "\n".join(
        f"- id={p.tmpl_id} | {p.nombre} | RD${p.precio_con_itbis:.2f}"
        for p in productos
    )
    return (
        f"CANDIDATOS ({len(productos)}):\n{lineas}\n"
        "Muéstralos con sus ids en mostrar_productos y pregunta cuál es el que busca."
    )


@function_tool
async def listar_catalogo(ctx: RunContextWrapper[ConversationContext]) -> str:
    """Devuelve TODO el catálogo como lista de texto (nombre + precio con ITBIS).

    Úsala cuando el cliente pide ver el catálogo completo, "todo lo que venden",
    "la lista", "qué productos tienen" o "muéstrame todo". Es la forma correcta de
    responder a eso: NO uses buscar_producto (que solo trae coincidencias).
    """
    productos = await catalogo.todos()
    if not productos:
        return "No hay productos disponibles en este momento."
    lineas = "\n".join(
        f"{i}. {p.nombre} - RD${p.precio_con_itbis:.2f}"
        for i, p in enumerate(productos, 1)
    )
    return (
        f"CATALOGO_COMPLETO ({len(productos)} productos, precios CON ITBIS):\n"
        f"{lineas}\n\n"
        "Preséntalo al cliente TAL CUAL, como lista de texto numerada, con una "
        "línea inicial cordial. NO pongas ids en mostrar_productos: NO se mandan "
        "las fotos de todo el catálogo (sería spam). Cierra ofreciendo mostrar la "
        "foto o cotizar el/los producto(s) que el cliente elija."
    )


@function_tool
async def detalle_producto(
    ctx: RunContextWrapper[ConversationContext], nombre: str
) -> str:
    """Devuelve el precio exacto y el id de un producto concreto, para cotizar.

    Args:
        nombre: Nombre del producto tal como apareció en la búsqueda.
    """
    p = await catalogo.por_nombre(nombre)
    if not p:
        return f'No encontré "{nombre}". Vuelve a buscar con buscar_producto.'
    ctx.context.ofrecer([p])
    return (
        f"id={p.tmpl_id} | {p.nombre}\n"
        f"precio_unitario (CON ITBIS, el que se le dice al cliente y el que va "
        f"en la línea del pedido): {p.precio_con_itbis:.2f}"
    )


@function_tool
async def buscar_por_foto(ctx: RunContextWrapper[ConversationContext]) -> str:
    """Identifica el envase de la foto que acaba de enviar el cliente.

    Compara la foto contra las fichas visuales del catálogo. Úsala cuando el
    cliente mande la FOTO de un envase, en vez de adivinar por el texto.
    """
    c = ctx.context
    if not c.imagen_url:
        return "No llegó ninguna foto en este turno. Pide la capacidad (oz o galón)."

    try:
        raw = await odoo.get_param(PARAM_FICHAS)
    except Exception as exc:  # noqa: BLE001
        log.error("fichas_lectura_fallo", error=str(exc))
        raw = None

    if not raw:
        return (
            "El índice de fotos no está generado (corré scripts/indexar_fichas.py). "
            "Pide la capacidad al cliente y usa buscar_producto."
        )

    try:
        store: dict = json.loads(raw)
    except json.JSONDecodeError:
        return "El índice de fotos está corrupto. Pide la capacidad al cliente."

    ficha = await ficha_visual_de_url(c.imagen_url)
    if not ficha or not ficha.get("tipo"):
        c.marcar_revision("foto_ilegible")
        return "No pude leer bien la foto. Pide al cliente la capacidad (oz o galón)."

    ranked = sorted(
        (
            (int(tmpl_id), entry, score_ficha(ficha, entry.get("ficha", {})))
            for tmpl_id, entry in store.items()
        ),
        key=lambda x: x[2],
        reverse=True,
    )
    if not ranked:
        return "No encontré un envase parecido. Pide la capacidad (oz o galón)."

    mejor_id, mejor, mejor_sc = ranked[0]
    segundo_sc = ranked[1][2] if len(ranked) > 1 else 0

    if mejor_sc >= 11 and mejor_sc >= segundo_sc + 4:
        p = await catalogo.por_tmpl_id(mejor_id)
        if p:
            c.ofrecer([p])
            return (
                "MATCH_FOTO (confianza alta):\n"
                f"- id={p.tmpl_id} | {p.nombre} | RD${p.precio_con_itbis:.2f}\n"
                "Confírmalo por su nombre y ponlo en mostrar_productos."
            )

    c.marcar_revision("foto_sin_match_claro")
    top = [x for x in ranked if x[2] > 0][:5]
    lineas = []
    for tmpl_id, entry, _sc in top:
        p = await catalogo.por_tmpl_id(tmpl_id)
        if p:
            c.ofrecer([p])
            lineas.append(f"- id={p.tmpl_id} | {p.nombre} | RD${p.precio_con_itbis:.2f}")
    if not lineas:
        return "No encontré un envase parecido. Pide la capacidad (oz o galón)."
    return (
        "SIN_MATCH_CLARO. Candidatos más parecidos:\n"
        + "\n".join(lineas)
        + "\nMuéstralos y pregunta cuál es, o pide la capacidad."
    )


@function_tool
async def link_tienda(ctx: RunContextWrapper[ConversationContext], nombre: str) -> str:
    """Devuelve el enlace de la tienda online para un producto.

    Args:
        nombre: Nombre del producto.
    """
    p = await catalogo.por_nombre(nombre)
    if not p:
        return f'No encontré "{nombre}".'
    return p.shop_url or f"https://{ctx.context.cfg.website}"


CATALOGO_TOOLS = [
    buscar_producto,
    listar_catalogo,
    detalle_producto,
    buscar_por_foto,
    link_tienda,
]
