"""Tools de CRM/ventas sobre Odoo: contactos y pedidos.

Regla dura implementada en CÓDIGO, no en el prompt:
  - `crear_pedido` exige partner_id ya verificado.
  - `agregar_linea_pedido` exige un order_id creado en ESTE turno.
  - El mensaje de confirmación con el número de pedido lo devuelve la tool,
    no lo redacta el modelo.
"""
from __future__ import annotations

import re

from agents import RunContextWrapper, function_tool

from app import comprobante
from app.catalogo import catalogo
from app.context import ConversationContext
from app.estado import leer_cotizacion
from app.logging_conf import get_logger
from app.odoo import odoo
from app.settings import settings

log = get_logger(__name__)

RE_SOLO_DIGITOS = re.compile(r"[^0-9]")


def _telefono_sanitizado(telefono: str | None) -> str:
    if not telefono:
        return ""
    return RE_SOLO_DIGITOS.sub("", telefono)[-10:]


def precio_blindado(precio_unitario: float, precio_catalogo: float) -> tuple[float, bool]:
    """Regla dura (ADR-006): el precio de una línea DEBE ser el del catálogo.

    Devuelve (precio_a_usar, fue_corregido). Tolerancia de 1 centavo para no
    corregir por redondeo. Es pura para poder testearla sin Odoo ni el SDK.
    """
    if abs(precio_unitario - precio_catalogo) > 0.01:
        return precio_catalogo, True
    return precio_unitario, False


# Un nombre de cliente real: letras (con tildes/ñ), espacios y . ' -. Nada de
# emojis ni adornos. Sirve para no meter el alias de WhatsApp en Odoo.
RE_NOMBRE_PERSONA = re.compile(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ .'\-]{1,49}$")


def nombre_no_valido(nombre: str, nombre_whatsapp: str = "") -> str:
    """Devuelve el motivo si el nombre NO sirve para un contacto de Odoo, o ''.

    Caso real: el bot creó el contacto y el pedido con el alias de WhatsApp
    ("la patrona RD Hija Rey 🎉") sin preguntarle el nombre al cliente. Es pura
    para poder testearla sin Odoo.
    """
    n = (nombre or "").strip()
    if not n:
        return "falta el nombre del cliente"
    if nombre_whatsapp and n.casefold() == (nombre_whatsapp or "").strip().casefold():
        return "ese es el nombre de WhatsApp, no el que dio el cliente"
    if not RE_NOMBRE_PERSONA.match(n):
        return "el nombre trae emojis o símbolos: no parece un nombre real"
    return ""


def datos_envio_faltantes(
    provincia: str, municipio: str, sector: str, calle: str
) -> list[str]:
    """Campos obligatorios de dirección de envío que faltan (regla dura)."""
    return [
        etiqueta
        for etiqueta, valor in (
            ("provincia", provincia),
            ("municipio/pueblo", municipio),
            ("sector", sector),
            ("calle", calle),
        )
        if not (valor or "").strip()
    ]


def nota_entrega(
    modalidad: str,
    provincia: str = "",
    municipio: str = "",
    sector: str = "",
    calle: str = "",
    numero_casa: str = "",
    tipo_lugar: str = "",
    referencia: str = "",
    ubicacion_mapa: str = "",
) -> str:
    """Arma la nota de entrega del pedido (retiro o envío detallado)."""
    if modalidad.lower().startswith("ret"):
        return "ENTREGA: Retiro en tienda"
    partes = [
        f"Provincia: {provincia.strip()}",
        f"Municipio/Pueblo: {municipio.strip()}",
        f"Sector: {sector.strip()}",
        f"Calle: {calle.strip()}",
    ]
    if numero_casa.strip():
        partes.append(f"No. casa/edificio: {numero_casa.strip()}")
    if tipo_lugar.strip():
        partes.append(f"Tipo: {tipo_lugar.strip()}")
    if referencia.strip():
        partes.append(f"Referencia: {referencia.strip()}")
    if ubicacion_mapa.strip():
        partes.append(f"Ubicación (mapa): {ubicacion_mapa.strip()}")
    return "ENTREGA: Envío a domicilio | " + " | ".join(partes)


@function_tool
async def verificar_contacto(ctx: RunContextWrapper[ConversationContext]) -> str:
    """Busca al cliente en Odoo por su teléfono de WhatsApp.

    Llámala ANTES de crear cualquier contacto o pedido.
    """
    c = ctx.context
    tel = _telefono_sanitizado(c.telefono)
    if not tel:
        return (
            "El cliente escribió desde un anuncio y no tenemos su teléfono real. "
            "Pídele el número para coordinar la entrega antes de crear el pedido."
        )
    res = await odoo.search_read(
        "res.partner",
        [["phone_sanitized", "like", tel]],
        ["id", "name", "phone", "street", "street2", "city", "zip"],
        limit=1,
    )
    if not res:
        return "NO_EXISTE: el cliente no está registrado. Usa crear_contacto."
    p = res[0]
    c.partner_id = int(p["id"])
    return (
        f"EXISTE: partner_id={p['id']} | {p.get('name')} | "
        f"dir: {p.get('street') or '-'} {p.get('street2') or ''} {p.get('city') or ''}"
    )


@function_tool
async def crear_contacto(
    ctx: RunContextWrapper[ConversationContext],
    nombre: str,
    calle: str = "",
    referencia: str = "",
    ciudad: str = "",
    telefono: str = "",
    email: str = "",
) -> str:
    """Crea el contacto del cliente en Odoo.

    Args:
        nombre: Nombre completo tal como lo escribió el cliente (no el de WhatsApp).
        calle: Dirección principal. Obligatoria si es envío a domicilio.
        referencia: Punto de referencia o detalle adicional de la dirección.
        ciudad: Ciudad o sector.
        telefono: Teléfono que dio el cliente. Sólo si no tenemos el de WhatsApp.
        email: Correo, si lo dio.
    """
    c = ctx.context
    # CANDADO: el contacto queda en Odoo para siempre; no se crea con el alias de
    # WhatsApp ni con emojis. Si no se lo preguntaste al cliente, pregúntaselo.
    motivo = nombre_no_valido(nombre, c.user_name)
    if motivo:
        log.warning(
            "contacto_nombre_rechazado",
            chat_id=c.chat_id, nombre=nombre[:40], motivo=motivo,
        )
        c.marcar_revision("nombre_invalido")
        return (
            f"ERROR: {motivo}. PREGÚNTALE al cliente su nombre completo (ej: "
            '"¿A nombre de quién registro el pedido?") y vuelve a intentarlo con '
            "lo que te responda. NO uses el nombre de WhatsApp."
        )
    valores = {
        "name": nombre.strip(),
        "street": calle.strip(),
        "street2": referencia.strip(),
        "city": ciudad.strip(),
        "zip": "",
        "country_id": settings.odoo_country_id,
        "phone": c.telefono or telefono.strip(),
        "email": email.strip(),
    }
    valores = {k: v for k, v in valores.items() if v not in ("", None)}
    partner_id = await odoo.create("res.partner", valores)
    c.partner_id = partner_id
    log.info("contacto_creado", chat_id=c.chat_id, partner_id=partner_id)
    return f"OK: contacto creado, partner_id={partner_id}"


@function_tool
async def actualizar_contacto(
    ctx: RunContextWrapper[ConversationContext],
    nombre: str = "",
    calle: str = "",
    referencia: str = "",
    ciudad: str = "",
    telefono: str = "",
) -> str:
    """Actualiza los datos del contacto ya existente (dirección, nombre, teléfono).

    Args:
        nombre: Nuevo nombre, si cambió.
        calle: Dirección principal.
        referencia: Punto de referencia.
        ciudad: Ciudad o sector.
        telefono: Teléfono de contacto.
    """
    c = ctx.context
    if not c.partner_id:
        return "ERROR: primero llama a verificar_contacto o crear_contacto."
    valores = {
        k: v.strip()
        for k, v in {
            "name": nombre,
            "street": calle,
            "street2": referencia,
            "city": ciudad,
            "phone": telefono,
        }.items()
        if v and v.strip()
    }
    if not valores:
        return "Nada que actualizar."
    await odoo.write("res.partner", c.partner_id, valores)
    return f"OK: contacto {c.partner_id} actualizado ({', '.join(valores)})."


async def _faltante_del_comprobante(c: ConversationContext) -> float | None:
    """Cuánto le falta al comprobante para cubrir lo cotizado (None = no bloquear).

    Lo cotizado se persiste porque la cotización pasó en un turno ANTERIOR (ver
    `estado.guardar_cotizacion`). Si no hay cotización, o no se puede leer el monto del
    comprobante, NO se bloquea: el pago igual lo aprueba una persona que ve la foto, y
    un falso "no coincide" le dice a un cliente que pagó bien que no pagó.
    """
    total = c.cotizado_total or await leer_cotizacion(c.chat_id)
    falta = comprobante.faltante(c.comprobante_texto, total)
    if falta:
        log.warning(
            "comprobante_monto_corto",
            chat_id=c.chat_id, falta=falta, total=total,
            leido=comprobante.monto_pagado(c.comprobante_texto),
        )
    return falta


async def crear_pedido_impl(
    c: ConversationContext,
    modalidad: str,
    provincia: str = "",
    municipio: str = "",
    sector: str = "",
    calle: str = "",
    numero_casa: str = "",
    tipo_lugar: str = "",
    referencia: str = "",
    ubicacion_mapa: str = "",
) -> str:
    """La lógica de `crear_pedido`, sin el envoltorio del SDK.

    Está separada para poder testear las REGLAS DURAS (en envío no hay pedido sin
    comprobante, y el comprobante tiene que cubrir el total) llamándolas directo, sin
    montar un Runner. Lo que decide si se crea un pedido merece test propio.

    Para ENVÍO la dirección debe venir COMPLETA y detallada (regla dura: se valida
    aquí, no en el prompt). Si el cliente compartió su ubicación por el mapa de
    WhatsApp, pásala en `ubicacion_mapa` (el link de Google Maps), pero igual pide
    los datos escritos para que el mensajero no dependa solo del pin.

    Args:
        modalidad: "envio" o "retiro".
        provincia: Provincia (ej. Santo Domingo, Santiago). Obligatoria si es envío.
        municipio: Municipio o pueblo. Obligatorio si es envío.
        sector: Sector o barrio. Obligatorio si es envío.
        calle: Calle y, si aplica, número/esquina. Obligatoria si es envío.
        numero_casa: Número de casa/edificio/apto.
        tipo_lugar: "casa" o "negocio" (si es negocio, incluye el nombre en referencia).
        referencia: Punto de referencia (ej. "frente al colmado X").
        ubicacion_mapa: Link de Google Maps si el cliente compartió su ubicación.
    """
    if not c.partner_id:
        return "ERROR: no hay contacto verificado. Llama a verificar_contacto primero."
    if c.order_id:
        return f"Ya existe un pedido creado en este turno: {c.order_id}."

    m = modalidad.lower()
    if m.startswith("env"):
        # REGLA DURA: en envío no hay pedido sin comprobante, y el comprobante tiene
        # que cubrir el total. Va acá y no en el prompt porque un prompt es una
        # sugerencia: el modelo ya llegó a crear pedidos "confirmando" pagos que
        # nadie mandó. En retiro NO aplica: ahí se paga en el mostrador.
        if not c.es_comprobante:
            return (
                "ERROR: para ENVÍO no se crea el pedido sin comprobante. Pídele al "
                "cliente la FOTO del comprobante de la transferencia por el total "
                "(o más) y crea el pedido recién cuando la mande. Si prefiere pagar "
                "al recibir o retirar en tienda, ofrécele el retiro en tienda."
            )
        falta = await _faltante_del_comprobante(c)
        if falta:
            c.marcar_revision("comprobante_monto_corto")
            c.comprobante_faltante = falta
            return (
                f"ERROR: el comprobante no cubre el total: faltan RD${falta:,.2f}. NO "
                "se creó el pedido. Dile al cliente cuánto falta con amabilidad, por "
                "si transfirió de menos o mandó el comprobante equivocado, y pídele "
                "el comprobante por el monto completo."
            )
        faltan = datos_envio_faltantes(provincia, municipio, sector, calle)
        if faltan:
            return (
                "ERROR: para el envío falta(n): "
                + ", ".join(faltan)
                + ". Pídeselos al cliente antes de crear el pedido "
                "(provincia, municipio/pueblo, sector y calle son obligatorios; "
                "número de casa, si es casa o negocio, y un punto de referencia ayudan al mensajero)."
            )
        nota = nota_entrega(
            "envio", provincia, municipio, sector, calle,
            numero_casa, tipo_lugar, referencia, ubicacion_mapa,
        )
    elif m.startswith("ret"):
        nota = nota_entrega("retiro")
    else:
        return 'ERROR: modalidad debe ser "envio" o "retiro".'

    order_id = await odoo.create(
        "sale.order",
        {
            "partner_id": c.partner_id,
            "partner_invoice_id": c.partner_id,
            "partner_shipping_id": c.partner_id,
            "note": nota,
        },
    )
    c.order_id = int(order_id)
    # Modalidad real del pedido: la usa el panel para saber si hay que esperar la
    # transferencia (envío) o si se paga en el mostrador (retiro, no lleva comprobante).
    c.pedido_modalidad = "envio" if m.startswith("env") else "retiro"
    # La dirección tal como quedó en el pedido (misma nota que se guarda en Odoo).
    c.direccion_entrega = nota.replace("ENTREGA: ", "", 1)
    log.info(
        "pedido_creado", chat_id=c.chat_id, order_id=order_id,
        modalidad=c.pedido_modalidad,
    )
    if c.es_comprobante:
        # Hay pago de por medio: el número de pedido NO se le da al cliente hasta que
        # el supervisor apruebe (ADR-013). `_sanear` lo fuerza igual, pero el modelo
        # tiene que leer acá lo mismo o redacta algo que después se le reemplaza.
        return (
            f"OK: pedido creado con número {order_id} (NO se lo digas al cliente). "
            "Ahora agrega las líneas con agregar_linea_pedido. Al cliente decile SOLO "
            "que su pago quedó en verificación y que el supervisor le confirma en un "
            "momento. El número de pedido se lo damos cuando el supervisor apruebe."
        )
    return (
        f"OK: pedido creado con número {order_id}. Ahora agrega las líneas con "
        "agregar_linea_pedido. Puedes confirmarle al cliente que su pedido quedó "
        f"registrado con el número {order_id}."
    )


@function_tool
async def crear_pedido(
    ctx: RunContextWrapper[ConversationContext],
    modalidad: str,
    provincia: str = "",
    municipio: str = "",
    sector: str = "",
    calle: str = "",
    numero_casa: str = "",
    tipo_lugar: str = "",
    referencia: str = "",
    ubicacion_mapa: str = "",
) -> str:
    """Crea el pedido en Odoo. SÓLO tras comprobante válido (envío) o confirmación (retiro).

    En ENVÍO no se crea nada sin el comprobante de pago, y el comprobante tiene que ser
    por el total o más: pídele la foto ANTES. En RETIRO no se pide comprobante (se paga
    en el mostrador). La dirección de envío debe venir COMPLETA y detallada. Todo eso se
    valida acá, no en el prompt. Si el cliente compartió su ubicación por el mapa de
    WhatsApp, pásala en `ubicacion_mapa` (el link de Google Maps), pero igual pide los
    datos escritos para que el mensajero no dependa solo del pin.

    Args:
        modalidad: "envio" o "retiro".
        provincia: Provincia (ej. Santo Domingo, Santiago). Obligatoria si es envío.
        municipio: Municipio o pueblo. Obligatorio si es envío.
        sector: Sector o barrio. Obligatorio si es envío.
        calle: Calle y, si aplica, número/esquina. Obligatoria si es envío.
        numero_casa: Número de casa/edificio/apto.
        tipo_lugar: "casa" o "negocio" (si es negocio, incluye el nombre en referencia).
        referencia: Punto de referencia (ej. "frente al colmado X").
        ubicacion_mapa: Link de Google Maps si el cliente compartió su ubicación.
    """
    return await crear_pedido_impl(
        ctx.context, modalidad, provincia, municipio, sector, calle,
        numero_casa, tipo_lugar, referencia, ubicacion_mapa,
    )


@function_tool
async def agregar_linea_pedido(
    ctx: RunContextWrapper[ConversationContext],
    producto_id: int,
    cantidad: int,
    precio_unitario: float,
) -> str:
    """Agrega un producto al pedido recién creado.

    Args:
        producto_id: El id que devolvió buscar_producto o detalle_producto.
        cantidad: Unidades.
        precio_unitario: Precio CON ITBIS, igual al cotizado.
    """
    c = ctx.context
    if not c.order_id:
        return "ERROR: no hay pedido creado. Llama a crear_pedido primero."
    if cantidad <= 0:
        return "ERROR: cantidad inválida."

    producto = await catalogo.por_tmpl_id(producto_id)
    if not producto:
        return f"ERROR: el producto id={producto_id} no existe en el catálogo."

    # Blindaje: el precio debe coincidir con el del catálogo (tolerancia 1 centavo).
    precio_original = precio_unitario
    precio_unitario, corregido = precio_blindado(precio_unitario, producto.precio_con_itbis)
    if corregido:
        log.warning(
            "precio_corregido",
            chat_id=c.chat_id,
            pedido=c.order_id,
            enviado=precio_original,
            real=producto.precio_con_itbis,
        )

    line_id = await odoo.create(
        "sale.order.line",
        {
            "order_id": c.order_id,
            "product_id": producto.variant_id,
            "product_uom_qty": cantidad,
            "name": producto.nombre,
            "price_unit": precio_unitario,
        },
    )
    c.lineas_creadas += 1
    c.lineas.append({
        "nombre": producto.nombre,
        "cantidad": int(cantidad),
        "precio": float(precio_unitario),
        "total": round(cantidad * precio_unitario, 2),
    })
    log.info(
        "linea_creada",
        chat_id=c.chat_id,
        order_id=c.order_id,
        line_id=line_id,
        producto=producto.nombre,
        cantidad=cantidad,
    )
    total = round(cantidad * precio_unitario, 2)
    return (
        f"OK: {cantidad} x {producto.nombre} a RD${precio_unitario:.2f} "
        f"= RD${total:.2f} agregado al pedido {c.order_id}."
    )


@function_tool
async def buscar_pedidos_cliente(ctx: RunContextWrapper[ConversationContext]) -> str:
    """Lista los pedidos en borrador del cliente actual."""
    c = ctx.context
    if not c.partner_id:
        return "ERROR: primero verifica el contacto."
    res = await odoo.search_read(
        "sale.order",
        [["partner_id", "=", c.partner_id], ["state", "=", "draft"]],
        ["id", "name", "amount_total", "state"],
        limit=5,
    )
    if not res:
        return "El cliente no tiene pedidos en borrador."
    return "\n".join(
        f"- {o['name']} (id={o['id']}) RD${o.get('amount_total', 0):.2f}" for o in res
    )


@function_tool
async def escalar_a_humano(
    ctx: RunContextWrapper[ConversationContext], motivo: str
) -> str:
    """Pasa la conversación a una persona del equipo.

    ÚSALA SÓLO si el cliente pide explícitamente hablar con alguien, quiere cancelar o
    quitar algo de un pedido, hay una queja seria, o hay insultos/abuso. Para precios,
    medidas, envío, ubicación, pago, disponibilidad, fotos, saludos o "ok": NO la uses,
    eso lo resuelves tú.

    Args:
        motivo: Motivo breve, para el aviso interno.
    """
    c = ctx.context
    # CANDADO (invariante): la escalada la habilita el DETERMINADOR, no el modelo.
    # Sin visto bueno no se escala, aunque el modelo insista: llegó a pasarle al
    # supervisor un simple SALUDO ("Hola, buenas tardes, cómo estás").
    if not c.permite_escalar:
        log.warning(
            "escalada_bloqueada",
            chat_id=c.chat_id,
            motivo=motivo,
            determinador=c.motivo_determinador,
        )
        c.marcar_revision("escalada_bloqueada")
        return (
            "NO_ESCALAR: esto no amerita un asesor; lo resuelves tú. Si no tienes el "
            "dato exacto, pregúntale con amabilidad qué necesita (medida, tipo, "
            "cantidad) u ofrécele el catálogo. NO le digas que avisaste a un "
            "supervisor ni que alguien lo va a contactar."
        )
    c.escalar = True
    c.marcar_revision(f"handoff: {motivo}")
    return (
        "OK: el equipo fue notificado. Dile al cliente que un asesor se comunica "
        "en breve. No sigas intentando resolverlo tú."
    )


ODOO_TOOLS = [
    verificar_contacto,
    crear_contacto,
    actualizar_contacto,
    crear_pedido,
    agregar_linea_pedido,
    buscar_pedidos_cliente,
    escalar_a_humano,
]
