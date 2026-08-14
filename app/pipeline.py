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

from app.agents import RespuestaBot, analizar_contexto
from app.agents import obtener as obtener_especialista
from app.business_config import get_producto_de_anuncio, load_config
from app.catalogo import catalogo
from app.context import ConversationContext
from app.debounce import acumular, drenar, es_duplicado, esperar_turno
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
from app import score
from app.session import RedisSession
from app.settings import settings
from app.ycloud import ycloud

log = get_logger(__name__)

MSG_TIPO_NO_SOPORTADO = (
    "Por ahora solo proceso texto, audio e imagenes. Escribeme tu consulta por "
    "texto y con gusto te ayudo."
)

# Sticker/emoji/reacción: inofensivos, no ameritan el mensaje robótico de arriba.
MSG_LIVIANO = "😊 Con gusto te ayudo. Dime qué envase o producto buscas."

# asyncio sólo guarda una referencia DÉBIL a las tasks: si no las retenemos acá,
# el recolector puede matar un turno a mitad de la ventana de debounce y el
# cliente se queda esperando una respuesta que nunca llega.
_TAREAS_VIVAS: set[asyncio.Task] = set()

# Cuánto esperar el lock de la conversación cuando otro turno del mismo chat está
# corriendo (ráfaga del cliente). Debe superar el peor turno (agente_timeout + envío)
# para no mandar un falso "inconveniente tecnico"; queda por debajo del TTL del lock
# (180s) para no esperar sobre un lock que ya expiró.
_ESPERA_LOCK_MAX = settings.agente_timeout + 40.0

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

    # Dedup: si YCloud reintenta el webhook o entrega el mismo mensaje dos veces,
    # lo procesamos UNA sola vez (si no, el bot responde duplicado).
    if await es_duplicado(msg):
        log.info("webhook_duplicado_ignorado", chat_id=msg.chat_id, message_id=msg.message_id)
        return

    # El chat aparece en el panel APENAS entra el mensaje. Antes sólo se registraba
    # DESPUÉS de que el bot respondía (debounce de 6s + el turno), así que una
    # conversación nueva tardaba en aparecer y a veces había que refrescar a mano.
    # Va antes de los cortes de abajo a propósito: con el bot apagado o pausado
    # (control humano) es cuando MÁS importa verla en el panel.
    await _registrar_entrante(msg)

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


# Qué mostrar en la lista cuando el mensaje no es texto (aún no se transcribió).
_ETIQUETA_MEDIA = {
    "audio": "🎤 (nota de voz)",
    "image": "📷 (imagen)",
    "location": "📍 (ubicación)",
    "video": "🎬 (video)",
    "document": "📄 (documento)",
    "sticker": "(sticker)",
}


async def _registrar_entrante(msg: InboundMessage) -> None:
    """Deja la conversación visible en el panel al instante (no espera al bot)."""
    texto = (msg.content or "").strip() or _ETIQUETA_MEDIA.get(
        msg.content_type, "(mensaje)"
    )
    try:
        await panel_events.tocar_chatmeta(
            msg.chat_id,
            emisor=settings.ycloud_from or msg.instance_from,
            destino=msg.destino_ycloud(),
            user_name=msg.user_name or "",
            telefono=msg.telefono or "",
            ultimo=texto[:200],
            ultimo_de="cliente",
        )
        await panel_events.publicar(
            "entrante", msg.chat_id, user_name=msg.user_name or "", texto=texto[:200],
            # `emisor` explícito: si no, publicar() lo busca en el chatmeta (1 lectura
            # extra a Redis por evento, y son varios eventos por turno).
            emisor=settings.ycloud_from or msg.instance_from,
        )
    except Exception as exc:  # noqa: BLE001
        # Que el panel no reciba el aviso NO puede impedir atender al cliente.
        log.warning("registrar_entrante_fallo", chat_id=msg.chat_id, error=str(exc))


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
    # Guardar el mensaje del supervisor en el historial: así el bot NO pierde el
    # hilo cuando retome, sin importar si respondiste desde el panel o desde YCloud.
    texto_sup = (info.get("texto") or "").strip()
    if texto_sup:
        await RedisSession(chat_id).add_items(
            [{"role": "assistant", "content": f"[SUPERVISOR] {texto_sup}"}]
        )
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
            if obtenido:
                await procesar_turno(msg)
                return

        # Otro turno de este MISMO chat está corriendo (ráfaga del cliente: patrón
        # normal en WhatsApp). Antes se esperaba 3s y se mandaba un falso "tuve un
        # inconveniente tecnico" + aviso al admin, y el mensaje quedaba sin atender:
        # el turno en curso puede tardar hasta agente_timeout (90s). Ahora esperamos
        # a que libere, con backoff, porque el mensaje sigue en el buffer y se drena
        # al tomar el lock.
        log.info("turno_en_curso_esperando", chat_id=msg.chat_id)
        esperado = 0.0
        paso = 2.0
        while esperado < _ESPERA_LOCK_MAX:
            await asyncio.sleep(paso)
            esperado += paso
            paso = min(paso * 1.5, 8.0)
            async with conversation_lock(msg.chat_id) as reintento:
                if reintento:
                    # exigir_buffer: si el turno anterior YA drenó (y respondió)
                    # nuestro mensaje, el buffer está vacío -> no responder de nuevo.
                    await procesar_turno(msg, exigir_buffer=True)
                    return
        # Esperamos más que el peor turno posible y el lock sigue tomado: esto sí es
        # una falla real (turno colgado o lock huérfano), no una simple ráfaga.
        log.warning("lock_no_obtenido_timeout", chat_id=msg.chat_id, esperado=esperado)
        await _fallback_error(msg)
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

    audios_fallidos = 0
    for a in audios:
        transcripcion = await transcribir_audio(a.media_url)
        if transcripcion:
            partes.append(f"<audio>\n{transcripcion}\n</audio>")
        else:
            audios_fallidos += 1

    imagen_url = ""
    es_comprobante = False
    if imagenes:
        imagen_url = imagenes[0].media_url
        descripcion, es_comprobante = await analizar_imagen(imagen_url)
        bloque = ["# EL CLIENTE ENVIO UNA IMAGEN"]
        # El CAPTION de la imagen es lo que el cliente ESCRIBIÓ ("quiero esta de 8
        # oz"): antes se descartaba (sólo entraban los content_type == "text") y se
        # perdía su intención, quedando únicamente el análisis visual.
        captions = [m.content for m in imagenes if m.content]
        if captions:
            bloque.append("## LO QUE ESCRIBIO CON LA IMAGEN: " + " | ".join(captions))
        bloque += ["## ANALISIS VISUAL:", descripcion]
        if len(imagenes) > 1:
            bloque.append(
                f"[El cliente envio {len(imagenes)} imagenes. Analiza la primera; "
                "si necesitas ver las demas, pidelas de una en una.]"
            )
        partes.append("\n".join(bloque))

    # Si Whisper falló y el audio era lo ÚNICO que mandó el cliente, sin esto el
    # turno terminaba en "turno_vacio" y el cliente se quedaba esperando para
    # siempre. Damos contexto al agente para que pida que lo escriba.
    if audios_fallidos and not partes:
        log.warning("audio_sin_transcripcion", audios=audios_fallidos)
        partes.append(
            "[AUDIO_NO_ENTENDIDO] El cliente mando una nota de voz que no se pudo "
            "escuchar. Pidele con amabilidad que la repita o que te lo escriba."
        )

    texto = "\n".join(p for p in partes if p).strip()

    if not texto and otros:
        # Sticker/emoji/reacción NO es un tipo "no soportado" de verdad: no mandamos
        # el mensaje robótico. Solo si hay algo realmente no procesable (video, doc…).
        harmless = {"sticker", "reaction"}
        if all(m.content_type in harmless for m in otros):
            return ("", "liviano", "", False)
        return ("", "no_soportado", "", False)

    tipo = "image" if imagenes else ("audio" if audios else "text")
    return (texto, tipo, imagen_url, es_comprobante)


# -------------------------------------------------------------- el turno ---
async def procesar_turno(
    trigger: InboundMessage, exigir_buffer: bool = False
) -> None:
    chat_id = trigger.chat_id
    msgs = await drenar(chat_id)
    if not msgs:
        if exigir_buffer:
            # Vinimos de esperar el lock: el turno anterior ya drenó y respondió
            # este mensaje. Reprocesar el trigger mandaría una respuesta DUPLICADA.
            log.info("turno_ya_atendido_por_otro", chat_id=chat_id)
            return
        msgs = [trigger]

    emisor = settings.ycloud_from or trigger.instance_from
    destino = trigger.destino_ycloud()
    # Config DEL CANAL (número nuestro por el que entró): precios, envío, cuentas y
    # mínimos pueden ser distintos en cada número. Sin canal propio hereda la común.
    cfg = await load_config(emisor)

    texto, tipo, imagen_url, es_comprobante = await _combinar(msgs)

    if tipo == "no_soportado":
        await ycloud.enviar_texto(destino, emisor, MSG_TIPO_NO_SOPORTADO, False)
        return
    if tipo == "liviano":
        await ycloud.enviar_texto(destino, emisor, MSG_LIVIANO, False)
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
            # Opción B: si el anuncio no está mapeado pero el referral trae la imagen
            # del anuncio, la analizamos con visión para saber qué envase muestra y
            # que el agente entienda "ese modelo" sin preguntar.
            ad_img = str(referral.get("image_url", "") or "")
            if ad_img:
                try:
                    desc, _ = await analizar_imagen(ad_img)
                    if desc:
                        ctx.ad_descripcion = desc[:600]
                        log.info("anuncio_imagen_analizada", chat_id=chat_id, ad_id=ad_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("anuncio_imagen_fallo", chat_id=chat_id, error=str(exc))
            else:
                log.info("anuncio_sin_imagen_en_referral", chat_id=chat_id, ad_id=ad_id)

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
            "handoff", chat_id, user_name=ctx.user_name, emisor=emisor,
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
        # El fast-path también mueve el semáforo: quien pregunta por las cuentas de
        # banco lo hace muchas veces por acá (0 tokens), y es la señal más fuerte.
        puntos = await _puntuar(chat_id, texto, ctx)
        await panel_events.tocar_chatmeta(
            chat_id, emisor=emisor, destino=destino,
            user_name=ctx.user_name, telefono=ctx.telefono or "",
            # Lo último de la conversación es la respuesta del BOT, no lo que escribió
            # el cliente: así en la lista se ve qué contestó y quién habló al final.
            ultimo=directa, ultimo_de="bot",
            ad_id=ctx.ad_id, ad_headline=ctx.ad_headline,
            ad_producto=ctx.ad_producto_nombre or ctx.ad_descripcion.replace("\n", " ")[:140],
            score=puntos["score"], score_sem=puntos["sem"], score_hitos=puntos["hitos"],
        )
        await panel_events.publicar(
            "turn", chat_id, user_name=ctx.user_name, texto=texto, emisor=emisor,
            respuesta=directa, fast_path=True, agente="fast-path",
            score=puntos["score"], score_sem=puntos["sem"],
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

    # Semáforo de cierre: acá el turno está CERRADO y los efectos son definitivos
    # (order_id, líneas y la cotización los acaba de fijar _efectos/las tools). Es
    # cálculo puro y viaja en el hset que ya se hace: 0 tokens, 0 Redis extra.
    puntos = await _puntuar(chat_id, texto, ctx)

    # Si el bot no llegó a decir nada (p. ej. sólo mandó fotos), queda lo del cliente.
    ultimo_txt = mensaje or (f"({len(fotos)} foto(s))" if fotos else texto)
    await panel_events.tocar_chatmeta(
        chat_id, emisor=emisor, destino=destino,
        user_name=ctx.user_name, telefono=ctx.telefono or "",
        ultimo=ultimo_txt, ultimo_de="bot" if (mensaje or fotos) else "cliente",
        score=puntos["score"], score_sem=puntos["sem"], score_hitos=puntos["hitos"],
    )
    await panel_events.publicar(
        "turn", chat_id, user_name=ctx.user_name, texto=texto, emisor=emisor,
        respuesta=mensaje, order_id=ctx.order_id, agente=ctx.agente,
        escalar=bool(ctx.escalar or respuesta.escalar),
        score=puntos["score"], score_sem=puntos["sem"],
    )


async def _puntuar(chat_id: str, texto: str, ctx: ConversationContext) -> dict:
    """Semáforo de cierre de la conversación (app/score.py). Nunca tumba el turno.

    Los hitos son acumulativos, así que hay que leer los previos: se toman del mismo
    chatmeta que el panel ya usa (1 lectura, la misma que `tocar_chatmeta` hace justo
    después). Es sólo para ORDENAR la atención del equipo: no se le pasa al modelo ni
    cambia una sola respuesta del bot.
    """
    try:
        previos = (await panel_events.leer_chatmeta(chat_id)).get("score_hitos") or []
        nuevos = score.detectar(
            texto,
            es_comprobante=ctx.es_comprobante,
            order_id=ctx.order_id,
            partner_id=ctx.partner_id,
            lineas_creadas=ctx.lineas_creadas,
            cotizado_unidades=ctx.cotizado_unidades,
            cotizado_total=ctx.cotizado_total,
            cotizado_modalidad=ctx.cotizado_modalidad,
            pedido_modalidad=ctx.pedido_modalidad,
            monto_minimo=_monto_minimo(ctx.cfg),
        )
        return score.puntuar(previos, nuevos)
    except Exception as exc:  # noqa: BLE001
        # Fail-open y explícito: si esto falla, NADIE queda marcado en frío.
        log.warning("score_fallo", chat_id=chat_id, error=str(exc))
        return {"score": None, "sem": "", "hitos": []}


def _monto_minimo(cfg) -> float:
    try:
        return float(str(cfg.monto_minimo).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


async def _correr_agente(
    texto: str, ctx: ConversationContext
) -> RespuestaBot | None:
    session = RedisSession(ctx.chat_id)
    # DETERMINADOR: elige el especialista Y decide si el caso amerita una persona.
    # `permite_escalar` es lo que habilita `escalar_a_humano` (ver odoo_tools).
    veredicto = await analizar_contexto(texto, ctx, session)
    nombre = veredicto.agente
    ctx.agente = nombre
    ctx.permite_escalar = veredicto.permite_escalar
    ctx.motivo_determinador = veredicto.motivo
    log.info("agente_elegido", chat_id=ctx.chat_id, agente=nombre, canal=ctx.emisor)
    try:
        result = await asyncio.wait_for(
            Runner.run(
                # obtener(): agente base o PERSONALIZADO creado desde el panel (los
                # personalizados pueden existir sólo en uno de los dos números).
                obtener_especialista(nombre, ctx.emisor),
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
        # Si tenemos dominio público, servimos la foto ya en JPG desde /img
        # (confiable); si no, la URL de Odoo (webp) + proxy weserv como fallback.
        url = f"{settings.base_url}/img/{p.tmpl_id}.jpg" if settings.base_url else p.image_url
        out.append((url, p.resumen()))
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
            "order", ctx.chat_id, user_name=ctx.user_name, emisor=ctx.emisor,
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

    # 3. Handoff a humano. El modelo también puede pedirlo por su salida
    # (respuesta.escalar), saltándose la tool: ese camino pasa por el MISMO candado
    # del determinador. `ctx.escalar` sólo lo pone la tool (ya validada) o el
    # anti-repetición, que es determinista.
    if respuesta.escalar and not ctx.escalar and not ctx.permite_escalar:
        log.warning(
            "escalada_modelo_bloqueada",
            chat_id=ctx.chat_id, determinador=ctx.motivo_determinador,
        )
        ctx.marcar_revision("escalada_bloqueada")
        respuesta.escalar = False
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
            "revision", ctx.chat_id, user_name=ctx.user_name, emisor=ctx.emisor,
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
