"""Orquestación de un turno completo.

Flujo:
  webhook -> acumular en buffer -> (debounce) -> drenar -> combinar ->
  resolver media -> fast-path o agente -> validar salida -> enviar ->
  efectos (adjuntar comprobante, avisar admin, cola de revisión)
"""
from __future__ import annotations

import asyncio
import base64
import re
import traceback

from agents import MaxTurnsExceeded, Runner

from app.agents import ESPECIALISTAS, RespuestaBot, elegir_agente
from app.business_config import get_producto_de_anuncio, load_config
from app.catalogo import catalogo
from app.context import ConversationContext
from app.debounce import acumular, drenar, esperar_turno
from app.estado import (
    bot_global_apagado,
    bot_pausado,
    encolar_revision,
    es_msg_bot,
    pausar_bot,
    registrar_msg_bot,
    tocar_ventana_24h,
)
from app.logging_conf import get_logger
from app.media import analizar_imagen, descargar, mime_de_url, transcribir_audio
from app.models import InboundMessage, parse_message_updated, ubicacion_a_texto
from app.odoo import odoo
from app.panel import events as panel_events
from app.redis_client import conversation_lock
from app.repeticion import contar_repeticion
from app.repeticion import reset as reset_repeticion
from app.router import respuesta_directa
from app.session import RedisSession
from app.settings import settings
from app.ycloud import ycloud

log = get_logger(__name__)

MSG_TIPO_NO_SOPORTADO = (
    "Por ahora solo proceso texto, audio e imagenes. Escribeme tu consulta por "
    "texto y con gusto te ayudo."
)

# asyncio sólo guarda una referencia DÉBIL a las tasks: si no las retenemos acá,
# el recolector puede matar un turno a mitad de la ventana de debounce y el
# cliente se queda esperando una respuesta que nunca llega.
_TAREAS_VIVAS: set[asyncio.Task] = set()

# Red de seguridad mínima (10 líneas, no las 100 del nodo `Clasificar Respuesta1`):
# el modelo ya no PUEDE crear un pedido inventado, pero sí podría redactar una
# frase que lo insinúe. Si lo hace sin order_id real, la reemplazamos.
RE_CLAIM_PEDIDO = re.compile(
    r"(pedido[^.\n]{0,40}(registrad|cread|confirmad|procesad)"
    r"|(registrad|cread|confirmad)[^.\n]{0,40}pedido"
    r"|recib[ií][^.\n]{0,40}comprobante)",
    re.IGNORECASE,
)


# --------------------------------------------------------------- entrada ---
async def manejar_entrante(msg: InboundMessage) -> None:
    """Se llama desde el webhook. No bloquea: acumula y programa el turno."""
    if settings.allowlist and msg.chat_id not in settings.allowlist:
        log.info("fuera_de_allowlist", chat_id=msg.chat_id)
        return

    if await bot_global_apagado():
        log.info("bot_global_off_ignorando", chat_id=msg.chat_id)
        return

    if await bot_pausado(msg.chat_id):
        log.info("bot_pausado_ignorando", chat_id=msg.chat_id)
        return

    await acumular(msg)
    tarea = asyncio.create_task(_turno_diferido(msg))
    _TAREAS_VIVAS.add(tarea)
    tarea.add_done_callback(_TAREAS_VIVAS.discard)


async def manejar_saliente(body: dict) -> None:
    """whatsapp.message.updated: si un mensaje saliente a un cliente NO lo envió
    el bot, lo escribió el supervisor desde YCloud -> tomar control (pausar 30 min).

    Seguro por diseño: NO envía nada; ante duda, no pausa.
    """
    if not settings.pausar_por_agente_humano:
        return
    info = parse_message_updated(body)
    if not info:
        return
    log.info("saliente_recibido", to=info["to"], status=info["status"], mid=info["id"])

    if await es_msg_bot(info["id"]):
        return  # lo envió el bot (o duda): no es intervención humana

    chat_id = info["to"]
    # Marcar este id como "conocido" para no re-procesar sus updates (sent/delivered/read).
    await registrar_msg_bot(info["id"])

    if await bot_pausado(chat_id):
        await pausar_bot(chat_id)  # ya en control humano: solo refrescar los 30 min
        return

    log.info("takeover_supervisor", chat_id=chat_id)
    await pausar_bot(chat_id)
    await encolar_revision(
        chat_id, ["takeover_supervisor"],
        info.get("texto") or "(mensaje del supervisor desde YCloud)", None, "",
    )
    await panel_events.publicar(
        "control", chat_id, user_name="",
        donde="YCloud (mensaje del supervisor)",
        detalle="El supervisor le escribió al cliente desde YCloud. Bot pausado 30 min para ese chat.",
        texto=(info.get("texto") or "")[:200],
    )


async def _turno_diferido(msg: InboundMessage) -> None:
    try:
        if not await esperar_turno(msg):
            return
        async with conversation_lock(msg.chat_id) as obtenido:
            if not obtenido:
                log.info("turno_en_curso_reprogramando", chat_id=msg.chat_id)
                await asyncio.sleep(3)
                async with conversation_lock(msg.chat_id) as reintento:
                    if reintento:
                        await procesar_turno(msg)
                    else:
                        # No pudimos tomar el lock ni al reintento: en vez de
                        # descartar el mensaje en silencio (dejando al cliente sin
                        # respuesta), avisamos y escalamos.
                        log.warning("lock_no_obtenido_fallback", chat_id=msg.chat_id)
                        await _fallback_error(msg)
                return
            await procesar_turno(msg)
    except Exception as exc:  # noqa: BLE001
        log.exception("turno_fallo", chat_id=msg.chat_id, error=str(exc))
        await panel_events.publicar(
            "error", msg.chat_id, user_name=msg.user_name or "",
            donde="Procesando el turno (buffer/debounce/envío)",
            texto=(msg.content or "")[:300],
            detalle=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc()[-3500:],
            contexto={"content_type": msg.content_type, "media_url": msg.media_url,
                      "message_id": msg.message_id, "anuncio": bool(msg.referral)},
        )
        await _fallback_error(msg)


async def _fallback_error(msg: InboundMessage) -> None:
    """Si todo falla, el cliente no se queda hablando solo."""
    try:
        emisor = settings.ycloud_from or msg.instance_from
        await ycloud.enviar_texto(
            msg.destino_ycloud(),
            emisor,
            "Disculpa, tuve un inconveniente tecnico. Un companero del equipo te "
            "escribe enseguida.",
            simular_tipeo=False,
        )
        await ycloud.avisar_admin(
            emisor,
            f"ERROR tecnico atendiendo a {msg.user_name or 'cliente'} "
            f"({msg.chat_id}). Revisar logs.",
        )
    except Exception:  # noqa: BLE001
        log.exception("fallback_error_fallo", chat_id=msg.chat_id)


# ------------------------------------------------------------ combinado ---
async def _combinar(msgs: list[InboundMessage]) -> tuple[str, str, str, bool]:
    """Funde la ráfaga en un solo input. Devuelve (texto, tipo, imagen_url, es_comprobante).

    Port de `Reconstruir Input Combinado2` + `Switch type2` + análisis de media.
    """
    textos = [m.content for m in msgs if m.content_type == "text" and m.content]
    imagenes = [m for m in msgs if m.content_type == "image" and m.media_url]
    audios = [m for m in msgs if m.content_type == "audio" and m.media_url]
    ubicaciones = [m for m in msgs if m.es_ubicacion]
    otros = [
        m for m in msgs
        if m.content_type not in {"text", "image", "audio", "location"}
        and not m.es_ubicacion
    ]

    partes = list(textos)
    for u in ubicaciones:
        t = ubicacion_a_texto(
            u.location_lat, u.location_lng, u.location_name, u.location_address
        )
        if t:
            partes.append(t)

    for a in audios:
        transcripcion = await transcribir_audio(a.media_url)
        if transcripcion:
            partes.append(f"<audio>\n{transcripcion}\n</audio>")

    imagen_url = ""
    es_comprobante = False
    if imagenes:
        imagen_url = imagenes[0].media_url
        descripcion, es_comprobante = await analizar_imagen(imagen_url)
        bloque = ["# EL CLIENTE ENVIO UNA IMAGEN", "## ANALISIS VISUAL:", descripcion]
        if len(imagenes) > 1:
            bloque.append(
                f"[El cliente envio {len(imagenes)} imagenes. Analiza la primera; "
                "si necesitas ver las demas, pidelas de una en una.]"
            )
        partes.append("\n".join(bloque))

    texto = "\n".join(p for p in partes if p).strip()

    if not texto and otros:
        return ("", "no_soportado", "", False)

    tipo = "image" if imagenes else ("audio" if audios else "text")
    return (texto, tipo, imagen_url, es_comprobante)


# -------------------------------------------------------------- el turno ---
async def procesar_turno(trigger: InboundMessage) -> None:
    chat_id = trigger.chat_id
    msgs = await drenar(chat_id)
    if not msgs:
        msgs = [trigger]

    emisor = settings.ycloud_from or trigger.instance_from
    destino = trigger.destino_ycloud()
    cfg = await load_config()

    texto, tipo, imagen_url, es_comprobante = await _combinar(msgs)

    if tipo == "no_soportado":
        await ycloud.enviar_texto(destino, emisor, MSG_TIPO_NO_SOPORTADO, False)
        return
    if not texto:
        log.info("turno_vacio", chat_id=chat_id)
        return

    # ------- contexto del anuncio (Click to WhatsApp) -------
    referral = next((m.referral for m in msgs if m.referral), {})
    ad_id = str(referral.get("source_id", "") or "")
    ad_producto = await get_producto_de_anuncio(ad_id) if ad_id else None

    ctx = ConversationContext(
        chat_id=chat_id,
        telefono=trigger.telefono,
        user_name=next((m.user_name for m in msgs if m.user_name), ""),
        emisor=emisor,
        destino=destino,
        cfg=cfg,
        ad_id=ad_id,
        ad_headline=str(referral.get("headline", "") or ""),
        ad_producto_tmpl_id=(ad_producto or {}).get("product_tmpl_id"),
        ad_producto_nombre=(ad_producto or {}).get("nombre", ""),
        imagen_url=imagen_url,
        es_comprobante=es_comprobante,
    )
    if ad_id:
        log.info(
            "cliente_de_anuncio",
            chat_id=chat_id,
            ad_id=ad_id,
            producto=ctx.ad_producto_nombre or None,
            ctwa_clid=bool(referral.get("ctwa_clid")),
        )
        if not ad_producto:
            ctx.marcar_revision(f"anuncio_sin_mapear:{ad_id}")

    # ------- anti-frustración / anti-abuso: 3ra vez lo mismo -> supervisor -------
    reps = await contar_repeticion(chat_id, texto)
    if reps >= 3:
        log.info("repeticion_al_supervisor", chat_id=chat_id, reps=reps)
        await ycloud.enviar_texto(
            destino,
            emisor,
            "Veo que necesitas ayuda con esto. Ya le pasé tu caso a un compañero del "
            "equipo para que te atienda personalmente; te contacta enseguida.",
            simular_tipeo=False,
        )
        await ycloud.avisar_admin(
            emisor,
            f"Cliente {ctx.user_name or chat_id} ({ctx.telefono or chat_id}) repitio "
            f"lo mismo {reps} veces: '{texto[:150]}'. Bot pausado 30 min.",
        )
        await pausar_bot(chat_id)
        await reset_repeticion(chat_id)
        await encolar_revision(chat_id, ["repeticion_3x"], texto[:200], None, ctx.user_name)
        await panel_events.publicar(
            "handoff", chat_id, user_name=ctx.user_name,
            donde="Repetición (cliente preguntó lo mismo 3+ veces)",
            texto=texto[:200], motivos=["repeticion_3x"],
        )
        return

    # ------- fast-path determinista -------
    directa = respuesta_directa(
        texto, cfg, content_type=tipo, viene_de_anuncio=bool(ad_id)
    )
    if directa:
        log.info("fast_path", chat_id=chat_id)
        await ycloud.enviar_texto(destino, emisor, directa)
        await tocar_ventana_24h(chat_id)
        # El fast-path también va al historial, para que el agente no lo repita.
        await RedisSession(chat_id).add_items(
            [
                {"role": "user", "content": texto},
                {"role": "assistant", "content": directa},
            ]
        )
        await panel_events.tocar_chatmeta(
            chat_id, emisor=emisor, destino=destino,
            user_name=ctx.user_name, telefono=ctx.telefono or "", ultimo=texto,
        )
        await panel_events.publicar(
            "turn", chat_id, user_name=ctx.user_name, texto=texto,
            respuesta=directa, fast_path=True, agente="fast-path",
        )
        return

    # ------- agente -------
    respuesta = await _correr_agente(texto, ctx)
    if respuesta is None:
        await _fallback_error(trigger)
        return

    mensaje = _sanear(respuesta.mensaje, ctx)
    fotos = _resolver_fotos(respuesta.mostrar_productos, ctx)

    if fotos:
        # La primera imagen lleva el texto como caption: evita mensaje huérfano.
        items = [(url, cap) for url, cap in fotos]
        if mensaje:
            url0, cap0 = items[0]
            items[0] = (url0, f"{mensaje}\n\n{cap0}"[:1024])
        await ycloud.enviar_imagenes(destino, emisor, items)
    elif mensaje:
        await ycloud.enviar_texto(destino, emisor, mensaje)

    await tocar_ventana_24h(chat_id)
    await _efectos(ctx, respuesta, mensaje, trigger)

    await panel_events.tocar_chatmeta(
        chat_id, emisor=emisor, destino=destino,
        user_name=ctx.user_name, telefono=ctx.telefono or "", ultimo=texto,
    )
    await panel_events.publicar(
        "turn", chat_id, user_name=ctx.user_name, texto=texto,
        respuesta=mensaje, order_id=ctx.order_id, agente=ctx.agente,
        escalar=bool(ctx.escalar or respuesta.escalar),
    )


async def _correr_agente(
    texto: str, ctx: ConversationContext
) -> RespuestaBot | None:
    session = RedisSession(ctx.chat_id)
    # Enrutado determinista-first: elige el especialista y corre SOLO ese.
    nombre = await elegir_agente(texto, ctx, session)
    ctx.agente = nombre
    log.info("agente_elegido", chat_id=ctx.chat_id, agente=nombre)
    try:
        result = await asyncio.wait_for(
            Runner.run(
                ESPECIALISTAS[nombre],
                texto,
                context=ctx,
                session=session,
                max_turns=settings.agente_max_turns,
            ),
            timeout=settings.agente_timeout,
        )
        salida = result.final_output
        if isinstance(salida, RespuestaBot):
            return salida
        return RespuestaBot(mensaje=str(salida))
    except (MaxTurnsExceeded, asyncio.TimeoutError) as exc:
        motivo = "max_turns_excedido" if isinstance(exc, MaxTurnsExceeded) else "timeout_agente"
        log.error(motivo, chat_id=ctx.chat_id)
        ctx.escalar = True
        ctx.marcar_revision(motivo)
        return RespuestaBot(
            mensaje=(
                "Dejame verificar eso con el equipo y te confirmo enseguida por aqui."
            ),
            escalar=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("agente_fallo", chat_id=ctx.chat_id, error=str(exc))
        ctx.marcar_revision("error_agente")
        await panel_events.publicar(
            "error", ctx.chat_id, user_name=ctx.user_name,
            donde="Agente (razonamiento / llamadas a herramientas)",
            texto=(texto or "")[:300],
            detalle=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc()[-3500:],
            contexto={"partner_id": ctx.partner_id, "order_id": ctx.order_id,
                      "es_comprobante": ctx.es_comprobante,
                      "imagen": bool(ctx.imagen_url), "ad_id": ctx.ad_id},
        )
        return None


def _sanear(mensaje: str, ctx: ConversationContext) -> str:
    """Red de seguridad: no dejar pasar confirmaciones de pedido sin pedido real."""
    mensaje = (mensaje or "").strip()
    if not mensaje:
        return ""
    if ctx.order_id:
        return mensaje
    if RE_CLAIM_PEDIDO.search(mensaje):
        log.warning("claim_pedido_sin_order_id", chat_id=ctx.chat_id)
        ctx.marcar_revision("claim_pedido_sin_order_id")
        return (
            "Para confirmar tu pedido necesito la foto del comprobante de la "
            "transferencia. Me la envias por aqui?"
        )
    return mensaje


def _resolver_fotos(
    ids: list[int], ctx: ConversationContext
) -> list[tuple[str, str]]:
    """Sólo se envían fotos de productos que una tool devolvió EN ESTE TURNO.

    Esto es lo que hacía el bloque de prompt "COPIAR VERBATIM" de n8n, pero
    garantizado por código: el modelo no puede inventar una URL ni reusar la de
    un turno anterior porque nunca ve URLs.
    """
    out: list[tuple[str, str]] = []
    for tmpl_id in (ids or [])[: settings.max_imagenes_por_mensaje]:
        p = ctx.productos_ofrecidos.get(int(tmpl_id))
        if not p:
            log.warning(
                "producto_no_ofrecido_este_turno",
                chat_id=ctx.chat_id,
                tmpl_id=tmpl_id,
            )
            continue
        out.append((p.image_url, p.resumen()))
    return out


# -------------------------------------------------------------- efectos ---
async def _efectos(
    ctx: ConversationContext,
    respuesta: RespuestaBot,
    mensaje: str,
    trigger: InboundMessage,
) -> None:
    # 1. Pedido creado -> avisar al admin y adjuntar el comprobante en Odoo.
    if ctx.order_id:
        await panel_events.publicar(
            "order", ctx.chat_id, user_name=ctx.user_name,
            detalle=f"Pedido {ctx.order_id} creado", order_id=ctx.order_id,
        )
        await ycloud.enviar_plantilla(
            settings.admin_phone,
            ctx.emisor,
            settings.template_pedido_creado,
            [ctx.user_name or "Sin nombre", ctx.telefono or ctx.chat_id, mensaje],
        )
        if ctx.lineas_creadas == 0:
            ctx.marcar_revision("pedido_sin_lineas")
        if ctx.imagen_url and ctx.es_comprobante:
            await _adjuntar_comprobante(ctx)

    # 2. Comprobante que NO terminó en pedido: eso siempre necesita ojos.
    elif ctx.es_comprobante:
        ctx.marcar_revision("comprobante_sin_pedido")
        await ycloud.avisar_admin(
            ctx.emisor,
            "ALERTA: llego un comprobante pero el pedido NO se registro solo. "
            f"Cliente: {ctx.user_name or 'Sin nombre'} | Tel: "
            f"{ctx.telefono or ctx.chat_id} | Comprobante: {ctx.imagen_url or '-'}",
        )

    # 3. Handoff a humano.
    if ctx.escalar or respuesta.escalar:
        ctx.marcar_revision("handoff")
        await ycloud.enviar_plantilla(
            settings.admin_phone,
            ctx.emisor,
            settings.template_alerta_supervisor,
            [
                ctx.user_name or "Sin nombre",
                ctx.telefono or ctx.chat_id,
                trigger.content or mensaje,
            ],
        )
        await ycloud.enviar_texto(
            ctx.destino,
            ctx.emisor,
            "Un supervisor se comunicara contigo en breve. Ya fue notificado. "
            "Gracias por tu paciencia.",
            simular_tipeo=False,
        )

    # 4. Cola de revisión por excepción.
    await encolar_revision(
        ctx.chat_id,
        ctx.motivo_revision,
        mensaje,
        ctx.order_id,
        ctx.user_name,
    )
    if ctx.motivo_revision:
        await panel_events.publicar(
            "revision", ctx.chat_id, user_name=ctx.user_name,
            motivos=ctx.motivo_revision,
            texto=(trigger.content or "")[:200],
            resumen=mensaje[:200],
        )


async def _adjuntar_comprobante(ctx: ConversationContext) -> None:
    try:
        data = await descargar(ctx.imagen_url)
        mime = mime_de_url(ctx.imagen_url)
        ext = "png" if mime == "image/png" else "jpg"
        await odoo.create(
            "ir.attachment",
            {
                "res_model": "sale.order",
                "res_id": ctx.order_id,
                "name": f"comprobante_pago_{ctx.order_id}.{ext}",
                "type": "binary",
                "mimetype": mime,
                "datas": base64.b64encode(data).decode(),
            },
        )
        log.info("comprobante_adjuntado", chat_id=ctx.chat_id, order_id=ctx.order_id)
    except Exception as exc:  # noqa: BLE001
        log.error("comprobante_adjuntar_fallo", chat_id=ctx.chat_id, error=str(exc))
        ctx.marcar_revision("comprobante_no_adjuntado")
        await ycloud.avisar_admin(
            ctx.emisor,
            f"No pude adjuntar el comprobante al pedido {ctx.order_id}. "
            f"URL: {ctx.imagen_url}",
        )


async def precalentar() -> None:
    """Carga el catálogo al arrancar para que el primer cliente no pague la espera."""
    try:
        await catalogo.todos(force=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("precalentado_fallo", error=str(exc))
