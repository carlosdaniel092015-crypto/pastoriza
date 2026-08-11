"""Rutas del panel de operación (/panel/*).

Auth simple por token (header X-Panel-Token o cookie). Si PANEL_TOKEN está vacío
se permite todo (solo para desarrollo local).
"""
from __future__ import annotations

import hmac
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.business_config import (
    config_as_dict,
    listar_anuncios,
    load_config,
    save_config,
)
from app.catalogo import catalogo
from app.estado import (
    bot_global_apagado,
    bot_pausado,
    encolar_revision,
    listar_revision,
    pausar_bot,
    reactivar_bot,
    set_bot_global,
)
from app.logging_conf import get_logger
from app.panel import conocimiento, events, prompt_store
from app.panel.analista import analizar_y_sugerir
from app.panel.ui import PANEL_HTML
from app.redis_client import get_redis, with_reconnect
from app.session import RedisSession
from app.settings import settings
from app.ycloud import ycloud

log = get_logger(__name__)

panel_router = APIRouter(prefix="/panel", tags=["panel"])

SESSION_PATTERN = settings.key("session", "*")


def _auth(token: str | None) -> None:
    if not settings.panel_token:
        return  # dev: sin auth
    # Comparación en tiempo constante (evita ataque de timing sobre el token).
    if not (token and hmac.compare_digest(token, settings.panel_token)):
        raise HTTPException(status_code=401, detail="token de panel inválido")


# ------------------------------------------------------------- página ---
@panel_router.get("", response_class=HTMLResponse)
@panel_router.get("/", response_class=HTMLResponse)
async def panel_home() -> HTMLResponse:
    # La página es pública; los datos (APIs) sí piden token.
    return HTMLResponse(PANEL_HTML)


@panel_router.get("/api/whoami")
async def whoami(x_panel_token: str | None = Header(default=None)) -> dict:
    return {"auth_requerida": bool(settings.panel_token), "ok": True}


# ------------------------------------------------- encendido global ---
@panel_router.get("/api/bot")
async def api_bot_estado(x_panel_token: str | None = Header(default=None)) -> dict:
    _auth(x_panel_token)
    return {"encendido": not await bot_global_apagado()}


@panel_router.post("/api/bot/{accion}")
async def api_bot_set(
    accion: str, x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    if accion not in ("on", "off"):
        raise HTTPException(status_code=400, detail="acción debe ser on u off")
    await set_bot_global(accion == "on")
    await events.publicar(
        "control", "-",
        detalle=f"Bot {'encendido' if accion == 'on' else 'apagado'} globalmente desde el panel",
    )
    return {"ok": True, "encendido": accion == "on"}


# ------------------------------------------------------------- chats ---
async def _chat_ids_de_sesiones() -> list[str]:
    prefijo = settings.key("session", "")

    async def _op(r: Any) -> list[str]:
        keys: list[str] = []
        async for k in r.scan_iter(match=SESSION_PATTERN, count=200):
            keys.append(k)
        return keys

    try:
        keys = await with_reconnect(_op)
    except Exception:  # noqa: BLE001
        return []
    return [k[len(prefijo):] for k in keys]


@panel_router.get("/api/chats")
async def api_chats(x_panel_token: str | None = Header(default=None)) -> dict:
    _auth(x_panel_token)
    meta = await events.todos_chatmeta()
    ids = set(meta.keys()) | set(await _chat_ids_de_sesiones())
    chats = []
    for cid in ids:
        m = meta.get(cid, {})
        pausado = await bot_pausado(cid)
        chats.append(
            {
                "chat_id": cid,
                "user_name": m.get("user_name", ""),
                "telefono": m.get("telefono", ""),
                "ultimo": m.get("ultimo", ""),
                "ultimo_ts": m.get("ultimo_ts", 0),
                "pausado": pausado,
            }
        )
    chats.sort(key=lambda c: c.get("ultimo_ts", 0), reverse=True)
    return {"total": len(chats), "chats": chats}


@panel_router.get("/api/chats/{chat_id}")
async def api_chat_hilo(
    chat_id: str, x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    items = await RedisSession(chat_id).get_items()
    meta = await events.leer_chatmeta(chat_id)
    return {
        "chat_id": chat_id,
        "pausado": await bot_pausado(chat_id),
        "meta": meta,
        "items": items,
    }


@panel_router.get("/api/productos")
async def api_productos(
    ids: str = "", x_panel_token: str | None = Header(default=None)
) -> dict:
    """Resuelve tmpl_ids -> {id, nombre, precio} para los chips de producto del hilo."""
    _auth(x_panel_token)
    out = []
    for raw in ids.split(","):
        raw = raw.strip()
        if not raw.isdigit():
            continue
        p = await catalogo.por_tmpl_id(int(raw))
        if p:
            out.append({"id": p.tmpl_id, "nombre": p.nombre, "precio": p.precio_con_itbis})
    return {"productos": out}


@panel_router.post("/api/chats/{chat_id}/revisar")
async def api_marcar_revision(
    chat_id: str, x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    meta = await events.leer_chatmeta(chat_id)
    await encolar_revision(
        chat_id, ["marcado_manual"], "Marcado para revisión desde el panel", None,
        meta.get("user_name", ""),
    )
    await events.publicar(
        "revision", chat_id, user_name=meta.get("user_name", ""),
        motivos=["marcado_manual"], donde="Marcado por el asesor",
    )
    return {"ok": True}


# ------------------------------------------------------------ eventos ---
@panel_router.get("/api/events")
async def api_events(
    after: int = 0, x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    evs = await events.listar(after=after)
    ult = evs[-1]["id"] if evs else after
    return {"eventos": evs, "ultimo_id": ult}


# ------------------------------------------------------- control bot ---
@panel_router.post("/api/chats/{chat_id}/pausar")
async def api_pausar(
    chat_id: str, x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    await pausar_bot(chat_id)
    await events.publicar("control", chat_id, detalle="bot pausado (manual)")
    return {"ok": True, "pausado": True}


@panel_router.post("/api/chats/{chat_id}/reactivar")
async def api_reactivar(
    chat_id: str, x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    await reactivar_bot(chat_id)
    await events.publicar("control", chat_id, detalle="bot reactivado (manual)")
    return {"ok": True, "pausado": False}


@panel_router.post("/api/chats/{chat_id}/responder")
async def api_responder(
    chat_id: str, request: Request, x_panel_token: str | None = Header(default=None)
) -> dict:
    """Respuesta manual del supervisor. Pausa el bot 30 min (takeover) y manda
    el mensaje por YCloud, además de guardarlo en el historial."""
    _auth(x_panel_token)
    data = await request.json()
    texto = str(data.get("texto", "")).strip()
    if not texto:
        raise HTTPException(status_code=400, detail="texto requerido")

    meta = await events.leer_chatmeta(chat_id)
    emisor = meta.get("emisor") or settings.ycloud_from
    destino = meta.get("destino") or {"to": chat_id}

    enviado = True
    try:
        await ycloud.enviar_texto(destino, emisor, texto, simular_tipeo=False)
    except Exception as exc:  # noqa: BLE001
        enviado = False
        log.warning("panel_responder_envio_fallo", chat_id=chat_id, error=str(exc))

    # Pausa el bot 30 min y registra en historial + feed.
    await pausar_bot(chat_id)
    await RedisSession(chat_id).add_items(
        [{"role": "assistant", "content": f"[SUPERVISOR] {texto}"}]
    )
    await events.publicar(
        "manual", chat_id, detalle=texto[:200], enviado=enviado, user_name=meta.get("user_name", "")
    )
    return {"ok": True, "enviado": enviado, "pausado": True}


# --------------------------------------------------------- config bot ---
@panel_router.get("/api/config")
async def api_config_get(x_panel_token: str | None = Header(default=None)) -> dict:
    _auth(x_panel_token)
    return config_as_dict(await load_config(force=True))


@panel_router.post("/api/config")
async def api_config_set(
    request: Request, x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="se esperaba objeto JSON")
    await save_config(data)
    await events.publicar("control", "-", detalle="config de negocio actualizada")
    return {"ok": True}


@panel_router.get("/api/anuncios")
async def api_anuncios(x_panel_token: str | None = Header(default=None)) -> dict:
    _auth(x_panel_token)
    return await listar_anuncios()


# ------------------------------------------------------ prompts por agente ---
@panel_router.get("/api/prompts")
async def api_prompts_get(x_panel_token: str | None = Header(default=None)) -> dict:
    _auth(x_panel_token)
    return {
        "agentes": list(prompt_store.AGENTES),
        "prompts": {
            a: {
                "base": prompt_store.get_base(a),
                "override": prompt_store.get_prompt(a) if prompt_store.usando_override(a) else "",
                "usando_override": prompt_store.usando_override(a),
            }
            for a in prompt_store.AGENTES
        },
    }


@panel_router.post("/api/prompts/{agente}")
async def api_prompt_set(
    agente: str, request: Request, x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    if agente not in prompt_store.AGENTES:
        raise HTTPException(status_code=404, detail=f"agente desconocido: {agente}")
    data = await request.json()
    texto = str(data.get("override", ""))
    if texto.strip() and len(texto.strip()) < 40:
        raise HTTPException(
            status_code=400,
            detail="prompt demasiado corto; podría romper el agente. Mínimo 40 caracteres, o vacío para volver al .md base.",
        )
    await prompt_store.guardar(agente, texto)
    await events.publicar(
        "control", "-",
        detalle=f"prompt '{agente}' {'guardado' if texto.strip() else 'restaurado al base'}",
    )
    return {"ok": True, "agente": agente, "usando_override": prompt_store.usando_override(agente)}


# ------------------------------------------------------------ revisión ---
@panel_router.get("/api/revision")
async def api_revision(
    limite: int = 50, x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    items = await listar_revision(limite)
    return {"total": len(items), "items": items}


# --------------------------------------------------- mejora continua ---
@panel_router.get("/api/aprendizaje")
async def api_aprendizaje(x_panel_token: str | None = Header(default=None)) -> dict:
    _auth(x_panel_token)
    return {
        "reglas": await conocimiento.list_reglas(),
        "correcciones": await conocimiento.list_correcciones(),
        "sugerencias": await conocimiento.list_sugerencias(),
    }


@panel_router.post("/api/reglas")
async def api_regla_add(
    request: Request, x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    data = await request.json()
    texto = str(data.get("texto", "")).strip()
    if len(texto) < 5:
        raise HTTPException(status_code=400, detail="regla demasiado corta")
    r = await conocimiento.add_regla(texto)
    await events.publicar("control", "-", detalle=f"regla agregada: {texto[:80]}")
    return {"ok": True, "regla": r}


@panel_router.delete("/api/reglas/{rid}")
async def api_regla_del(
    rid: int, x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    await conocimiento.del_regla(rid)
    return {"ok": True}


@panel_router.post("/api/correcciones")
async def api_correccion_add(
    request: Request, x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    data = await request.json()
    situacion = str(data.get("situacion", "")).strip()
    respuesta = str(data.get("respuesta_correcta", "")).strip()
    if not situacion or not respuesta:
        raise HTTPException(status_code=400, detail="situacion y respuesta_correcta requeridas")
    c = await conocimiento.add_correccion(
        situacion, respuesta, str(data.get("motivo", "")), str(data.get("chat_id", ""))
    )
    await events.publicar("control", data.get("chat_id", "-"), detalle="corrección aprendida")
    return {"ok": True, "correccion": c}


@panel_router.delete("/api/correcciones/{cid}")
async def api_correccion_del(
    cid: int, x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    await conocimiento.del_correccion(cid)
    return {"ok": True}


@panel_router.post("/api/sugerencias/analizar")
async def api_analizar(x_panel_token: str | None = Header(default=None)) -> dict:
    _auth(x_panel_token)
    resultado = await analizar_y_sugerir(auto_aplicar_bajo_riesgo=True)
    return {"ok": True, **resultado}


@panel_router.post("/api/sugerencias/{sid}/aprobar")
async def api_sugerencia_aprobar(
    sid: int, x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    s = await conocimiento.aprobar_sugerencia(sid)
    return {"ok": True, "sugerencia": s}


@panel_router.post("/api/sugerencias/{sid}/rechazar")
async def api_sugerencia_rechazar(
    sid: int, x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    s = await conocimiento.rechazar_sugerencia(sid)
    return {"ok": True, "sugerencia": s}
