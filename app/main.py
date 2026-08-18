"""Punto de entrada FastAPI.

Endpoints:
  POST /webhook/ycloud        <- YCloud manda acá los eventos de WhatsApp
  POST /webhook/debug         <- loguea el payload crudo (para confirmar el referral)
  GET  /pastoriza-config-load <- compatible con el panel actual
  POST /pastoriza-config-save <- compatible con el panel actual
  GET  /admin/revision        <- cola de revisión por excepción
  GET/POST /admin/anuncios    <- mapa ad_id -> producto
  GET  /health, GET /health/deep
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from app.media import close_http as close_media_http
from app.media import convertir_a_jpg, descargar

from app import aprobacion, canario, pagos, version
from app.canales import canal_id
from app.business_config import (
    canales_configurados,
    config_as_dict,
    listar_anuncios,
    load_config,
    save_config,
    set_producto_de_anuncio,
)
from app.catalogo import catalogo
from app.estado import limpiar_revision, listar_revision, pausar_bot, reactivar_bot
from app.logging_conf import get_logger, setup_logging
from app.models import (
    InboundMessage,
    bloque_saliente,
    parse_inbound,
    parse_outbound_command,
)
from app.odoo import odoo
from app.panel import agentes_custom, conocimiento, prompt_store, telegram
from app.panel.analista import analizar_y_sugerir
from app.panel.router import panel_router
from app.pipeline import manejar_entrante, manejar_saliente, precalentar
from app.redis_client import close_redis, get_redis
from app.settings import settings
from app.ycloud import ycloud

setup_logging()
log = get_logger("main")


def _token_ok(recibido: str | None, esperado: str) -> bool:
    """Comparación en tiempo constante (evita ataque de timing sobre el token)."""
    return bool(recibido) and hmac.compare_digest(recibido or "", esperado)


def require_token(x_panel_token: str | None = Header(default=None)) -> None:
    """Protege endpoints de operación (/admin/*, config). Fail-open SOLO si no hay
    PANEL_TOKEN configurado (desarrollo local); en producción exige el token.

    Antes estos endpoints estaban ABIERTOS: cualquiera con acceso de red podía
    reescribir la config de negocio (¡incluidas las cuentas bancarias!) o leer la
    PII de la cola de revisión. Ahora comparten el mismo token que el panel.
    """
    if not settings.panel_token:
        return
    if not _token_ok(x_panel_token, settings.panel_token):
        raise HTTPException(status_code=401, detail="token inválido")


async def _loop_analista() -> None:
    """Corre el analista de aprendizaje cada `analista_intervalo_horas` (default 24h).

    Vive en el único worker (ver Dockerfile). Espera un rato al arrancar para dejar
    acumular casos y no analizar sobre una cola recién levantada.
    """
    intervalo = max(1, settings.analista_intervalo_horas) * 3600
    await asyncio.sleep(min(intervalo, 3600))
    while True:
        try:
            res = await analizar_y_sugerir(auto_aplicar_bajo_riesgo=False)
            log.info("analista_auto_corrida", **{
                k: v for k, v in res.items() if isinstance(v, (int, str))
            })
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("analista_auto_fallo", error=str(exc))
        await asyncio.sleep(intervalo)


async def _loop_canario() -> None:
    """Vigila el bot (catálogo, Redis, escaladas) y avisa por Telegram si se rompe.

    Existe porque las fallas graves son SILENCIOSAS para quien opera: el bot sigue
    contestando pero le dice a todos "no tengo productos". Sin esto, el operador se
    entera por la captura de un cliente que ya se fue.
    """
    intervalo = max(1, settings.canario_intervalo_minutos) * 60
    await asyncio.sleep(45)  # dejar que arranque y se cargue el catálogo
    try:
        await canario.revisar_y_avisar(arranque=True)
    except Exception as exc:  # noqa: BLE001
        log.error("canario_fallo", error=str(exc))
    while True:
        await asyncio.sleep(intervalo)
        try:
            await canario.revisar_y_avisar()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("canario_fallo", error=str(exc))


async def _revisar_canales() -> None:
    """Avisa si la configuración de los DOS números no puede funcionar.

    `YCLOUD_FROM` fija a la fuerza el número emisor: con dos canales eso haría que
    todo salga por uno solo y que las conversaciones del otro se registren en el
    canal equivocado. Es un error de configuración silencioso, así que se grita.
    """
    try:
        canales = await canales_configurados()
    except Exception as exc:  # noqa: BLE001
        log.warning("canales_no_leidos", error=str(exc))
        return
    log.info("canales_configurados", canales=list(canales), total=len(canales))
    if len(canales) > 1 and settings.ycloud_from:
        log.error(
            "ycloud_from_fijo_con_varios_canales",
            ycloud_from=settings.ycloud_from,
            canales=list(canales),
            detalle=(
                "YCLOUD_FROM fuerza un solo número emisor: dejala VACÍA para que el "
                "bot responda por el mismo número por el que le escribieron."
            ),
        )
        with contextlib.suppress(Exception):
            await telegram.enviar(
                "⚠️ <b>Configuración</b>: hay 2 canales configurados pero "
                f"<code>YCLOUD_FROM={settings.ycloud_from}</code> fuerza uno solo. "
                "Dejala vacía para que cada número responda por sí mismo."
            )


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "arrancando",
        env=settings.app_env,
        modelo=settings.model_agente,
        allowlist=sorted(settings.allowlist) or "todos",
    )
    await precalentar()
    # agentes_custom PRIMERO: prompt_store carga también los prompts de los agentes
    # creados desde el panel, y necesita saber sus nombres.
    await agentes_custom.cargar()
    await prompt_store.cargar()
    await conocimiento.cargar()
    await _revisar_canales()

    tareas: list[asyncio.Task] = []
    if settings.analista_auto:
        tareas.append(asyncio.create_task(_loop_analista()))
        log.info("analista_scheduler_on", cada_horas=settings.analista_intervalo_horas)
    if settings.canario_activo:
        tareas.append(asyncio.create_task(_loop_canario()))
        log.info("canario_on", cada_minutos=settings.canario_intervalo_minutos)
    # Registrar el webhook de Telegram para los botones Aprobar/Rechazar.
    if telegram.configurado() and settings.base_url and settings.telegram_webhook_secret:
        await telegram.set_webhook(
            f"{settings.base_url}/webhook/telegram", settings.telegram_webhook_secret
        )

    yield

    for t in tareas:
        t.cancel()
    with contextlib.suppress(Exception):
        await asyncio.gather(*tareas, return_exceptions=True)
    await ycloud.close()
    await telegram.close()
    await close_media_http()
    await close_redis()
    log.info("apagando")


app = FastAPI(title="Pastoriza WhatsApp Bot", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(panel_router)


@app.exception_handler(Exception)
async def _log_excepcion_no_manejada(request: Request, exc: Exception) -> JSONResponse:
    """Red de seguridad: cualquier excepción no manejada en un endpoint queda
    logueada en JSON (con traceback) en vez de perderse en el log genérico de
    Starlette. Así 'saber si algo explota' no depende de recordar dónde mirar.
    Las HTTPException (401/404/…) siguen su curso normal: no pasan por aquí.
    """
    log.error(
        "request_no_manejado",
        ruta=str(request.url.path),
        metodo=request.method,
        error=str(exc),
        exc_info=exc,
    )
    return JSONResponse({"ok": False, "error": "internal"}, status_code=500)


# ------------------------------------------------------------- webhook ---
@app.post("/webhook/ycloud")
async def webhook_ycloud(
    request: Request,
    background: BackgroundTasks,
    x_webhook_token: str | None = Header(default=None),
) -> JSONResponse:
    """Recibe eventos de YCloud. Responde 200 SIEMPRE y rápido.

    Si tardás o devolvés error, YCloud reintenta y terminás procesando el
    mismo mensaje dos veces.
    """
    if settings.webhook_token:
        # YCloud no permite agregar headers custom en su config de webhook (solo
        # URL + eventos), así que aceptamos el token TAMBIÉN por query param para
        # poder incrustarlo en la URL: .../webhook/ycloud?token=EL_TOKEN
        provisto = x_webhook_token or request.query_params.get("token")
        if not _token_ok(provisto, settings.webhook_token):
            raise HTTPException(status_code=401, detail="token inválido")

    try:
        body: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": True, "ignored": "body no es JSON"})

    # Comandos del encargado desde el propio número: .on / .off
    comando = parse_outbound_command(body)
    if comando:
        cmd, destino = comando
        if destino:
            background.add_task(reactivar_bot if cmd == ".on" else pausar_bot, destino)
        return JSONResponse({"ok": True, "comando": cmd})

    msg = parse_inbound(body)
    if msg is None:
        # Mensaje SALIENTE: es cómo detectamos que un humano le escribió al cliente
        # (desde YCloud o, en un número COEXISTENTE, desde el celular) para pausar el
        # bot 30 min en ese chat. No se exige un `type` puntual: alcanza con que traiga
        # un mensaje saliente, porque el nombre del evento cambia según el caso.
        if bloque_saliente(body):
            background.add_task(manejar_saliente, body)
            return JSONResponse({"ok": True, "saliente": True})
        # Evento que no sabemos interpretar: lo logueamos con sus claves para poder
        # ver qué manda YCloud de verdad (clave para depurar la coexistencia).
        log.info(
            "evento_no_reconocido",
            tipo=body.get("type", "?"),
            claves=sorted(body.keys())[:12],
        )
        return JSONResponse({"ok": True, "ignored": body.get("type", "?")})

    # El SUPERVISOR contestando la plantilla de aprobación del pago. Se atiende antes
    # que la venta: es una orden de operación, no una conversación con un cliente.
    # Sólo se intercepta si el mensaje ES una respuesta de aprobación (ver
    # `aprobacion.parsear_respuesta`); cualquier otra cosa que escriba sigue su curso.
    if _es_supervisor(msg.chat_id) and aprobacion.parsear_respuesta(
        msg.boton_payload or msg.content
    ):
        background.add_task(_aprobar_desde_whatsapp, msg)
        return JSONResponse({"ok": True, "aprobacion": True})

    log.info(
        "entrante",
        chat_id=msg.chat_id,
        tipo=msg.content_type,
        message_id=msg.message_id,
        anuncio=bool(msg.referral),
    )
    background.add_task(manejar_entrante, msg)
    return JSONResponse({"ok": True})


def _es_supervisor(numero: str) -> bool:
    """Compara por los últimos 10 dígitos: el mismo número llega con y sin +1."""
    admin = canal_id(settings.admin_phone)
    return bool(admin) and canal_id(numero) == admin


async def _aprobar_desde_whatsapp(msg: InboundMessage) -> None:
    """Aplica el botón que tocó el supervisor y le confirma qué pasó.

    Le contesta SIEMPRE, incluso si falló: quien aprueba un pago tiene que saber si el
    cliente recibió su número de pedido o si quedó esperando.
    """
    try:
        res = await pagos.procesar_respuesta_supervisor(
            msg.boton_payload or msg.content
        )
    except Exception as exc:  # noqa: BLE001
        log.error("aprobacion_whatsapp_fallo", error=str(exc), exc_info=exc)
        res = {"ok": False, "error": "error interno"}
    if res is None:
        return

    pedido = res.get("order_id") or "?"
    if not res.get("ok"):
        aviso = f"No pude procesar el pedido {pedido}: {res.get('error', 'error')}"
    elif res.get("accion") == aprobacion.ACCION_APROBAR:
        aviso = (
            f"Listo: pedido {pedido} APROBADO. Ya le avisé al cliente con su número."
            if not res.get("ya_estaba")
            else f"El pedido {pedido} ya estaba aprobado."
        )
    elif res.get("enviado"):
        aviso = (
            f"Pedido {pedido} marcado como NO aprobado. Al cliente le avise que no se "
            "pudo confirmar y que te escriba; el MOTIVO se lo explicas vos."
        )
    else:
        aviso = (
            f"Pedido {pedido} marcado como NO aprobado, pero NO pude avisarle al "
            "cliente. Escribile vos: quedo esperando."
        )
    with contextlib.suppress(Exception):
        await ycloud.avisar_admin(msg.instance_from or settings.ycloud_from, aviso)


@app.post("/webhook/telegram")
async def webhook_telegram(request: Request) -> JSONResponse:
    """Recibe los clics de los botones Aprobar/Rechazar de las sugerencias.

    Seguridad: Telegram reenvía el secreto (fijado con setWebhook) en el header
    X-Telegram-Bot-Api-Secret-Token; además exigimos que el chat sea el autorizado.
    """
    if not settings.telegram_webhook_secret:
        raise HTTPException(status_code=404, detail="webhook de telegram no habilitado")
    got = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not _token_ok(got, settings.telegram_webhook_secret):
        raise HTTPException(status_code=401, detail="secret inválido")

    try:
        update: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": True})

    cq = update.get("callback_query") or {}
    if not cq:
        return JSONResponse({"ok": True})

    msg = cq.get("message") or {}
    chat_id = str(((msg.get("chat") or {}).get("id")) or "")
    # Solo el chat configurado puede accionar (evita que un tercero apruebe reglas).
    if settings.telegram_chat_id and chat_id != str(settings.telegram_chat_id):
        await telegram.responder_callback(str(cq.get("id", "")), "No autorizado")
        return JSONResponse({"ok": True})

    partes = str(cq.get("data") or "").split(":")
    if len(partes) == 3 and partes[0] == "sug":
        accion, sid_raw = partes[1], partes[2]
        try:
            sid = int(sid_raw)
        except ValueError:
            sid = None
        if sid is not None:
            if accion == "aprobar":
                s = await conocimiento.aprobar_sugerencia(sid)
                estado = "✅ Aprobada y aplicada" if s else "No encontrada"
            elif accion == "rechazar":
                s = await conocimiento.rechazar_sugerencia(sid)
                estado = "❌ Rechazada" if s else "No encontrada"
            else:
                s, estado = None, "Acción desconocida"
            await telegram.responder_callback(str(cq.get("id", "")), estado)
            contenido = str((s or {}).get("contenido", ""))
            await telegram.editar_texto(
                chat_id, msg.get("message_id"),
                f"<b>Sugerencia #{sid}</b> — {estado}\n\n{contenido}",
            )
    return JSONResponse({"ok": True})


@app.post("/webhook/debug")
async def webhook_debug(request: Request, _: None = Depends(require_token)) -> dict:
    """Loguea el payload CRUDO. Apuntá YCloud acá, hacé clic en tu anuncio y mandá
    un mensaje: así confirmás de una vez el nombre real del campo `referral`.

    Protegido con PANEL_TOKEN: loguea PII (teléfono, nombre, contenido)."""
    body = await request.json()
    log.info("payload_crudo", payload=body)
    return {"ok": True}


# ---------------------------------------------- config (panel existente) ---
@app.get("/pastoriza-config-load")
async def config_load(_: None = Depends(require_token)) -> dict:
    return config_as_dict(await load_config(force=True))


@app.post("/pastoriza-config-save")
async def config_save(request: Request, _: None = Depends(require_token)) -> dict:
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="se esperaba un objeto JSON")
    await save_config(data)
    return {"ok": True}


# ------------------------------------------------------------- admin ---
@app.get("/admin/revision")
async def admin_revision(limite: int = 50, _: None = Depends(require_token)) -> dict:
    items = await listar_revision(limite)
    return {"total": len(items), "items": items}


@app.delete("/admin/revision")
async def admin_revision_limpiar(_: None = Depends(require_token)) -> dict:
    await limpiar_revision()
    return {"ok": True}


@app.get("/admin/anuncios")
async def admin_anuncios(_: None = Depends(require_token)) -> dict:
    return await listar_anuncios()


@app.post("/admin/anuncios")
async def admin_anuncios_set(request: Request, _: None = Depends(require_token)) -> dict:
    """Body: {"ad_id": "52579732276546", "product_tmpl_id": 42}"""
    data = await request.json()
    ad_id = str(data.get("ad_id", "")).strip()
    tmpl_id = int(data.get("product_tmpl_id", 0))
    if not ad_id or not tmpl_id:
        raise HTTPException(status_code=400, detail="ad_id y product_tmpl_id requeridos")
    p = await catalogo.por_tmpl_id(tmpl_id)
    if not p:
        raise HTTPException(status_code=404, detail=f"producto {tmpl_id} no existe")
    await set_producto_de_anuncio(ad_id, tmpl_id, p.nombre)
    return {"ok": True, "ad_id": ad_id, "producto": p.nombre}


@app.get("/admin/catalogo")
async def admin_catalogo(_: None = Depends(require_token)) -> dict:
    productos = await catalogo.todos()
    return {
        "total": len(productos),
        "productos": [
            {"id": p.tmpl_id, "nombre": p.nombre, "precio": p.precio_con_itbis}
            for p in productos
        ],
    }


@app.post("/admin/pausar/{chat_id}")
async def admin_pausar(chat_id: str, _: None = Depends(require_token)) -> dict:
    await pausar_bot(chat_id)
    return {"ok": True}


@app.post("/admin/reactivar/{chat_id}")
async def admin_reactivar(chat_id: str, _: None = Depends(require_token)) -> dict:
    await reactivar_bot(chat_id)
    return {"ok": True}


# --------------------------------------------------------------- fotos ---
_IMG_CACHE: dict[int, bytes] = {}


@app.get("/img/{fname}")
async def serve_img(fname: str) -> Response:
    """Sirve la foto de un producto ya convertida a JPG, tomándola de Odoo.

    Reemplaza el proxy externo weserv: YCloud busca la imagen acá, en nuestro
    propio dominio, de forma confiable. Pública (YCloud la fetchea sin token).
    """
    try:
        tmpl_id = int(str(fname).split(".")[0])
    except ValueError:
        raise HTTPException(status_code=404, detail="id inválido")

    jpg = _IMG_CACHE.get(tmpl_id)
    if jpg is None:
        url = (
            f"{settings.odoo_url.rstrip('/')}"
            f"/web/image/product.template/{tmpl_id}/image_1024"
        )
        try:
            jpg = await convertir_a_jpg(await descargar(url))
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=404, detail="sin imagen")
        _IMG_CACHE[tmpl_id] = jpg

    return Response(
        content=jpg,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ------------------------------------------------------------- health ---
@app.get("/health")
async def health() -> dict:
    """Incluye el commit desplegado: así se sabe si un cambio llegó a producción o
    si el deploy se quedó atrás (antes no había forma de distinguirlo)."""
    return {"status": "ok", **version.info()}


@app.get("/health/deep")
async def health_deep() -> dict:
    estado = {"redis": False, "odoo": False, "catalogo": 0}
    with contextlib.suppress(Exception):
        estado["redis"] = bool(await get_redis().ping())
    estado["odoo"] = await odoo.ping()
    with contextlib.suppress(Exception):
        estado["catalogo"] = len(await catalogo.todos())
    # El catálogo vacío es una falla REAL aunque Redis y Odoo respondan: el bot le
    # dice a todos los clientes que no hay productos. Que el health lo refleje.
    estado["status"] = (
        "ok"
        if estado["redis"] and estado["odoo"] and estado["catalogo"] > 0
        else "degraded"
    )
    return estado


@app.get("/health/canario")
async def health_canario(_: None = Depends(require_token)) -> dict:
    """Estado del canario a demanda: qué está roto y por qué, en texto claro."""
    return await canario.revisar()
