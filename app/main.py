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

import contextlib
import hmac
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.media import close_http as close_media_http

from app.business_config import (
    config_as_dict,
    listar_anuncios,
    load_config,
    save_config,
    set_producto_de_anuncio,
)
from app.catalogo import catalogo
from app.estado import limpiar_revision, listar_revision, pausar_bot, reactivar_bot
from app.logging_conf import get_logger, setup_logging
from app.models import parse_inbound, parse_outbound_command
from app.odoo import odoo
from app.panel import conocimiento, prompt_store, telegram
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


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "arrancando",
        env=settings.app_env,
        modelo=settings.model_agente,
        allowlist=sorted(settings.allowlist) or "todos",
    )
    await precalentar()
    await prompt_store.cargar()
    await conocimiento.cargar()
    yield
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
    if settings.webhook_token and not _token_ok(x_webhook_token, settings.webhook_token):
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

    # Mensaje SALIENTE (whatsapp.message.updated): detectar intervención del
    # supervisor desde YCloud para pausar el bot en ese chat.
    if body.get("type") == "whatsapp.message.updated":
        background.add_task(manejar_saliente, body)
        return JSONResponse({"ok": True, "saliente": True})

    msg = parse_inbound(body)
    if msg is None:
        return JSONResponse({"ok": True, "ignored": body.get("type", "?")})

    log.info(
        "entrante",
        chat_id=msg.chat_id,
        tipo=msg.content_type,
        message_id=msg.message_id,
        anuncio=bool(msg.referral),
    )
    background.add_task(manejar_entrante, msg)
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


# ------------------------------------------------------------- health ---
@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/health/deep")
async def health_deep() -> dict:
    estado = {"redis": False, "odoo": False, "catalogo": 0}
    with contextlib.suppress(Exception):
        estado["redis"] = bool(await get_redis().ping())
    estado["odoo"] = await odoo.ping()
    with contextlib.suppress(Exception):
        estado["catalogo"] = len(await catalogo.todos())
    estado["status"] = "ok" if estado["redis"] and estado["odoo"] else "degraded"
    return estado
