"""Aprobación del PAGO: las acciones con efecto (avisar, aprobar, rechazar).

El bot no da un pago por bueno. Cuando llega un comprobante crea el pedido, le dice
al cliente que se está verificando y le manda al SUPERVISOR una plantilla de WhatsApp
con la foto del comprobante, el cliente, la dirección, los productos y los montos, con
dos botones. Recién cuando el supervisor toca "aprobar", el cliente recibe el número
de pedido.

Este módulo existe aparte del panel porque la misma acción entra por DOS puertas:
  - el botón de la plantilla de WhatsApp (webhook, `app/main.py`), y
  - el panel (`app/panel/router.py`).
Si la lógica viviera en el endpoint, la puerta de WhatsApp sería una segunda
implementación —y la única forma de que las dos divergan es que existan dos.

La parte PURA (armar el texto, los montos y parsear el botón) está en
`app/aprobacion.py`; acá está lo que toca Redis, YCloud y Odoo.
"""
from __future__ import annotations

from typing import Any

from app import aprobacion, media_publica
from app.business_config import load_config
from app.estado import encolar_revision
from app.logging_conf import get_logger
from app.media import descargar, mime_de_url
from app.panel import events
from app.session import RedisSession
from app.settings import settings
from app.ycloud import ycloud

log = get_logger(__name__)


# ----------------------------------------------------------------- aviso ---
async def _publicar_comprobante(imagen_url: str) -> str:
    """Republica el comprobante en NUESTRO dominio, sin token.

    Las URLs de media de YCloud exigen `X-API-Key` (ver `media.descargar`), así que
    Meta no puede descargarlas para la cabecera de la plantilla: le llegaría un 401 y
    la plantilla saldría sin foto (o no saldría). Se baja una vez y se sirve en
    /panel/media/<token>, que es público y transitorio.
    """
    if not imagen_url or not settings.base_url:
        return ""
    try:
        data = await descargar(imagen_url)
    except Exception as exc:  # noqa: BLE001
        log.warning("comprobante_no_republicado", error=str(exc))
        return ""
    ctype = mime_de_url(imagen_url) or "image/jpeg"
    token = media_publica.guardar(data, ctype)
    return f"{settings.base_url}/panel/media/{token}.{media_publica.extension(ctype)}"


async def avisar_supervisor(
    *,
    chat_id: str,
    emisor: str,
    order_id: int,
    modalidad: str,
    cliente: str,
    telefono: str,
    direccion: str,
    lineas: list[dict] | None,
    envio: float = 0.0,
    imagen_url: str = "",
) -> bool:
    """Manda la plantilla de aprobación al supervisor. True si YCloud la aceptó.

    NO lanza: que falle el aviso no puede tumbar el turno del cliente (ya se le
    contestó). Si vuelve False, el pedido igual queda "pendiente" en el panel, que es
    la otra puerta para aprobarlo.
    """
    try:
        params = aprobacion.parametros(
            order_id=order_id,
            modalidad=modalidad,
            cliente=cliente,
            telefono=telefono,
            direccion=direccion,
            lineas=lineas,
            envio=envio,
        )
        botones = [
            aprobacion.payload(aprobacion.ACCION_APROBAR, chat_id, order_id),
            aprobacion.payload(aprobacion.ACCION_RECHAZAR, chat_id, order_id),
        ]
        # Dos plantillas porque una con encabezado de imagen EXIGE una imagen en cada
        # envío: en retiro no hay comprobante, así que va la que no tiene encabezado.
        # Ver PLANTILLA_META.md.
        plantilla = (
            settings.template_aprobacion_pago if imagen_url
            else settings.template_aprobacion_retiro
        )

        async def _mandar(foto_url: str) -> bool:
            return await ycloud.enviar_plantilla_botones(
                settings.admin_phone,
                emisor,
                plantilla,
                params,
                imagen_url=foto_url,
                botones=botones,
            )

        foto = await _publicar_comprobante(imagen_url)
        ok = await _mandar(foto)

        if not ok and foto:
            # La plantilla puede estar dada de alta SIN encabezado de imagen. Mandarle
            # un componente de header que la plantilla no declara hace que Meta rechace
            # el mensaje ENTERO: el supervisor se quedaría sin aviso y sin botones por
            # una foto. Vale más el aviso sin foto que ningún aviso, así que se
            # reintenta sin ella y el comprobante se manda aparte.
            log.warning(
                "aviso_aprobacion_sin_encabezado",
                chat_id=chat_id, order_id=order_id,
                detalle=(
                    "la plantilla no acepta la foto en el encabezado; se reintenta sin "
                    "ella (ver PLANTILLA_META.md: Encabezado = Imagen)"
                ),
            )
            ok = await _mandar("")
            if ok:
                await _mandar_comprobante_aparte(emisor, foto, order_id)
    except Exception as exc:  # noqa: BLE001
        log.error("aviso_aprobacion_fallo", chat_id=chat_id, error=str(exc))
        return False
    if ok:
        log.info("aviso_aprobacion_enviado", chat_id=chat_id, order_id=order_id)
    else:
        # Lo más probable: Meta todavía no aprobó la plantilla (ver PLANTILLA_META.md).
        log.error(
            "aviso_aprobacion_rechazado",
            chat_id=chat_id, order_id=order_id, plantilla=plantilla,
        )
    return ok


async def _mandar_comprobante_aparte(emisor: str, foto: str, order_id: int) -> None:
    """El comprobante como imagen suelta, cuando no pudo ir en la plantilla.

    Best-effort: fuera de la ventana de 24 h de WhatsApp esto no sale, y está bien —
    el aviso con los botones ya salió y la foto también está en el panel y adjunta al
    pedido en Odoo. Nunca lanza: es un extra, no el aviso.
    """
    try:
        ok = await ycloud.enviar_imagen(
            {"to": settings.admin_phone}, emisor, foto,
            caption=f"Comprobante del pedido {order_id}",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("comprobante_aparte_fallo", order_id=order_id, error=str(exc))
        return
    log.info("comprobante_aparte", order_id=order_id, enviado=bool(ok))


# ------------------------------------------------------------- acciones ---
async def buscar_por_pedido(order_id: int) -> str:
    """chat_id del pago pendiente de ese pedido (para cuando el supervisor escribe
    "aprobar 160" a mano y no viene el chat_id del botón). "" si no aparece."""
    try:
        metas = await events.todos_chatmeta(estricto=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("busqueda_pedido_fallo", order_id=order_id, error=str(exc))
        return ""
    for cid, meta in (metas or {}).items():
        apro = (meta or {}).get("aprobacion") or {}
        try:
            mismo = int(apro.get("order_id") or 0) == int(order_id)
        except (TypeError, ValueError):
            mismo = False
        if mismo:
            return str(cid)
    return ""


async def aprobar(chat_id: str, via: str = "panel") -> dict:
    """El supervisor da el pago por bueno: recién ahí el cliente recibe el número.

    Devuelve {"ok": bool, ...}. Con ok=False trae "error" (texto para mostrar) y
    "status" (código HTTP que le corresponde). No lanza: los dos que la llaman —el
    panel y el webhook— necesitan responder algo, no un stacktrace.

    NO pausa el bot (a diferencia de responder a mano): este mensaje es del bot, no
    una toma de control del chat.
    """
    meta = await events.leer_chatmeta(chat_id)
    apro = meta.get("aprobacion") or {}
    order_id = apro.get("order_id")
    if not order_id:
        return {
            "ok": False,
            "status": 400,
            "error": "esta conversación no tiene un pago pendiente de aprobación",
        }
    if apro.get("estado") == "aprobado":
        return {"ok": True, "ya_estaba": True, "order_id": order_id}

    emisor = meta.get("emisor") or settings.ycloud_from
    destino = meta.get("destino") or {"to": chat_id}
    cfg = await load_config(emisor)
    # "Tu pago fue verificado y aceptado" sólo si hubo un pago: al de RETIRO se le
    # confirma el pedido y se le recuerda que paga al retirar. Si la marca vieja no
    # trae `con_pago` (pendientes de antes de esto), se asume que sí: era el único caso.
    con_pago = apro.get("con_pago")
    con_pago = True if con_pago is None else bool(con_pago)
    plantilla = (cfg.msg_pago_aprobado if con_pago else cfg.msg_retiro_aprobado) or ""
    try:
        texto = plantilla.format(numero=order_id)
    except (KeyError, IndexError, ValueError):
        # Si alguien dejó una llave rara en el mensaje editable, no se rompe el envío.
        texto = f"{plantilla} (pedido {order_id})".strip()

    enviado = False
    try:
        enviado = await ycloud.enviar_texto(destino, emisor, texto, simular_tipeo=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("aprobar_pago_envio_fallo", chat_id=chat_id, error=str(exc))
    if not enviado:
        # No se marca aprobado si el cliente no recibió nada: quedaría creyendo que
        # sigue en verificación y nadie volvería a mirarlo.
        log.error("aprobar_pago_no_enviado", chat_id=chat_id, order_id=order_id)
        return {
            "ok": False,
            "status": 502,
            "order_id": order_id,
            "error": "no se pudo enviar el mensaje al cliente; el pago sigue pendiente",
        }

    await events.guardar_aprobacion(chat_id, "aprobado", order_id)
    await RedisSession(chat_id).add_items([{"role": "assistant", "content": texto}])
    await events.publicar(
        "order", chat_id, emisor=emisor, user_name=meta.get("user_name", ""),
        detalle=(
            f"{'Pago' if con_pago else 'Pedido'} APROBADO por el supervisor · "
            f"pedido {order_id}"
        ),
        order_id=order_id,
    )
    await events.tocar_chatmeta(
        chat_id, emisor=emisor, destino=destino,
        user_name=meta.get("user_name", ""), telefono=meta.get("telefono", ""),
        ultimo=texto, ultimo_de="bot",
    )
    log.info(
        "aprobacion_aplicada",
        chat_id=chat_id, order_id=order_id, via=via, con_pago=con_pago,
    )
    return {"ok": True, "order_id": order_id, "enviado": True}


async def rechazar(chat_id: str, motivo: str = "", via: str = "panel") -> dict:
    """Marca el pago como NO aprobado. A propósito NO le escribe nada al cliente.

    Decirle a alguien que su pago no sirve es una conversación que tiene que tener una
    persona, con el motivo real (monto distinto, comprobante ilegible, transferencia no
    acreditada). Queda el chat marcado y el supervisor escribe.
    """
    meta = await events.leer_chatmeta(chat_id)
    apro = meta.get("aprobacion") or {}
    if not apro:
        return {"ok": False, "status": 400, "error": "no hay un pago pendiente"}

    order_id = apro.get("order_id")
    await events.guardar_aprobacion(chat_id, "rechazado", order_id, motivo)
    await encolar_revision(
        chat_id, ["pago_rechazado"], motivo or "(sin motivo)",
        order_id, meta.get("user_name", ""),
    )
    await events.publicar(
        "revision", chat_id, emisor=meta.get("emisor", ""),
        user_name=meta.get("user_name", ""), motivos=["pago_rechazado"],
        resumen=motivo[:200] or "Pago no aprobado por el supervisor",
    )
    log.info("pago_rechazado", chat_id=chat_id, order_id=order_id, via=via)
    return {"ok": True, "order_id": order_id}


# ------------------------------------------- respuesta del supervisor ---
async def procesar_respuesta_supervisor(texto: str) -> dict[str, Any] | None:
    """Aplica el botón (o el mensaje) con el que el supervisor responde la plantilla.

    Devuelve None si el texto NO es una respuesta de aprobación — ahí el mensaje sigue
    su curso normal (el supervisor también es un contacto de WhatsApp cualquiera).
    """
    parsed = aprobacion.parsear_respuesta(texto)
    if not parsed:
        return None
    accion, chat_id, order_id = parsed
    if not chat_id:
        # Lo escribió a mano ("aprobar 160"): hay que ubicar de qué chat es ese pedido.
        chat_id = await buscar_por_pedido(order_id)
    if not chat_id:
        log.warning("aprobacion_sin_chat", order_id=order_id, accion=accion)
        return {"ok": False, "error": f"no encontré el pedido {order_id}", "accion": accion}

    if accion == aprobacion.ACCION_APROBAR:
        res = await aprobar(chat_id, via="whatsapp")
    else:
        res = await rechazar(chat_id, "No aprobado por el supervisor", via="whatsapp")
    return {**res, "accion": accion, "chat_id": chat_id, "order_id": order_id}


__all__ = [
    "aprobar",
    "avisar_supervisor",
    "buscar_por_pedido",
    "procesar_respuesta_supervisor",
    "rechazar",
]
