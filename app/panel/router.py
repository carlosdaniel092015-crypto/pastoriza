"""Rutas del panel de operación (/panel/*).

Auth simple por token (header X-Panel-Token o cookie). Si PANEL_TOKEN está vacío
se permite todo (solo para desarrollo local).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import time
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from app import media_publica, pagos
from app.media import convertir_a_jpg, convertir_audio_ogg

from app.business_config import (
    canales_configurados,
    config_as_dict,
    listar_anuncios,
    load_config,
    nombre_canal,
    norm_num,
    overrides_de_canal,
    overrides_por_canal,
    parsear_canales,
    resetear_canal,
    save_config,
)
from app.canales import COMUN, canal_id
from app.catalogo import catalogo
from app.estado import (
    bot_global_apagado,
    bot_pausado,
    encolar_revision,
    listar_revision,
    pausados,
    pausar_bot,
    reactivar_bot,
    set_bot_global,
)
from app.logging_conf import get_logger
from app.panel import agentes_custom, conocimiento, events, prompt_store
from app.panel.analista import analizar_y_sugerir
from app.panel.ui import MANIFEST, PANEL_HTML, SERVICE_WORKER
from app import score
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
    # no-store: el navegador no cachea el panel, así cada deploy se ve al instante
    # sin tener que forzar la recarga.
    return HTMLResponse(PANEL_HTML, headers={"Cache-Control": "no-store"})


# ------------------------------------------------------- PWA (público) ---
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_TYPES = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
    ".json": "application/json",
    ".ico": "image/x-icon",
}


@panel_router.get("/manifest.webmanifest")
async def panel_manifest() -> JSONResponse:
    # El manifest es público (no lleva datos sensibles). El navegador lo pide sin
    # cabeceras de token, por eso no exige _auth.
    return JSONResponse(
        MANIFEST,
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@panel_router.get("/sw.js")
async def panel_service_worker() -> Response:
    # Service-Worker-Allowed amplía el scope a /panel para que controle también la
    # URL sin barra final. no-store: cada deploy actualiza el SW al instante.
    return Response(
        content=SERVICE_WORKER,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-store",
            "Service-Worker-Allowed": "/panel",
        },
    )


@panel_router.get("/static/{name}")
async def panel_static(name: str) -> FileResponse:
    # Sirve iconos/favicon del PWA. Whitelist estricta por nombre (sin subrutas)
    # para evitar path traversal.
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=404, detail="no encontrado")
    path = (_STATIC_DIR / name).resolve()
    if _STATIC_DIR not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="no encontrado")
    media = _STATIC_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(
        path, media_type=media, headers={"Cache-Control": "public, max-age=604800"}
    )


@panel_router.get("/api/whoami")
async def whoami(x_panel_token: str | None = Header(default=None)) -> dict:
    # `version`: qué commit está corriendo. Si el panel muestra uno viejo, el
    # problema es el DEPLOY, no el código.
    from app import version

    return {
        "auth_requerida": bool(settings.panel_token), "ok": True,
        "version": version.info(),
    }


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
# El SCAN de las sesiones recorre TODO el keyspace y sólo sirve para las
# conversaciones que tienen historial pero no están en el índice del panel (chats
# viejos, de antes de `chatmeta`). No hace falta repetirlo en cada refresco.
_TTL_SCAN = 30.0
_cache_scan: tuple[float, list[str]] | None = None

# Cuántas lecturas de Redis en vuelo puede tener el panel a la vez. Deja conexiones
# libres del pool para el BOT, que es lo que no puede esperar (hay un cliente
# escribiendo del otro lado).
_CONCURRENCIA = 6

# Pestaña de las conversaciones que NO tienen número asignado (entraron antes de que
# el bot guardara el emisor). Id propio: con "" se confundía con "Todos" y al hacerle
# clic se veían todas las conversaciones.
SIN_CANAL = "-"


async def _chat_ids_de_sesiones() -> list[str]:
    global _cache_scan
    ahora = time.monotonic()
    if _cache_scan and (ahora - _cache_scan[0]) < _TTL_SCAN:
        return _cache_scan[1]

    prefijo = settings.key("session", "")

    async def _op(r: Any) -> list[str]:
        keys: list[str] = []
        async for k in r.scan_iter(match=SESSION_PATTERN, count=200):
            keys.append(k)
        return keys

    try:
        keys = await with_reconnect(_op)
    except Exception:  # noqa: BLE001
        return _cache_scan[1] if _cache_scan else []
    ids = [k[len(prefijo):] for k in keys]
    _cache_scan = (ahora, ids)
    return ids


# Lo derivado del historial se cachea por chat + marca de tiempo del último mensaje:
# mientras no entre nada nuevo, el resultado no puede cambiar. Sin esto, cada refresco
# de la lista releía el historial de TODOS los chats viejos (uno por uno).
_cache_ultimo: dict[str, tuple[float, tuple[str, str]]] = {}
_MAX_CACHE_ULTIMO = 2000


async def _ultimo_del_historial(
    chat_id: str, respaldo: str = "", version: float = 0.0
) -> tuple[str, str]:
    """(texto, quién) del último mensaje real de la conversación, leído del historial.

    Se usa para las conversaciones que ya existían antes de guardar `ultimo_de`.
    Devuelve `respaldo` si no se puede leer: nunca rompe la lista de chats.
    """
    hit = _cache_ultimo.get(chat_id)
    if hit and hit[0] == version:
        return hit[1]
    fila = await _leer_ultimo_del_historial(chat_id, respaldo)
    if len(_cache_ultimo) >= _MAX_CACHE_ULTIMO:
        _cache_ultimo.clear()
    _cache_ultimo[chat_id] = (version, fila)
    return fila


async def _leer_ultimo_del_historial(chat_id: str, respaldo: str = "") -> tuple[str, str]:
    try:
        items = await RedisSession(chat_id).get_items(limit=6)
    except Exception:  # noqa: BLE001
        return (respaldo, "cliente")
    for it in reversed(items or []):
        if str(it.get("type", "")).startswith("function_call"):
            continue  # las llamadas a tools no son mensajes
        contenido = it.get("content")
        if isinstance(contenido, list):
            contenido = " ".join(
                str((x or {}).get("text") or (x or {}).get("content") or "")
                for x in contenido
            )
        texto = str(contenido or "").strip()
        if not texto:
            continue
        if texto.startswith("{"):  # salida estructurada del agente
            try:
                texto = str(json.loads(texto).get("mensaje") or texto)
            except Exception:  # noqa: BLE001
                pass
        if texto.startswith("[SUPERVISOR]"):
            return (texto.replace("[SUPERVISOR]", "").strip()[:200], "asesor")
        if it.get("role") == "user":
            return (texto[:200], "cliente")
        return (texto[:200], "bot")
    return (respaldo, "cliente")


@panel_router.get("/api/chats")
async def api_chats(x_panel_token: str | None = Header(default=None)) -> dict:
    _auth(x_panel_token)
    # DEGRADADO: si no se puede LEER el índice, la lista sale vacía — y "no hay
    # conversaciones" se veía igual que "Redis está caído". El panel tiene que poder
    # distinguirlo, así que el fallo se reporta en vez de tragarse.
    degradado = ""
    try:
        meta = await events.todos_chatmeta(estricto=True)
    except Exception as exc:  # noqa: BLE001
        log.error("panel_chats_sin_indice", error=str(exc))
        meta, degradado = {}, f"{type(exc).__name__}: {exc}"[:200]
    ids = set(meta.keys()) | set(await _chat_ids_de_sesiones())
    # Nombres de los canales (números de YCloud) para mostrar en el panel.
    mapa_canales = parsear_canales((await load_config()).canales)
    # Los canales CONFIGURADOS aparecen siempre como pestaña, incluso sin
    # conversaciones todavía: si no, un número nuevo no se vería hasta el primer
    # cliente y no habría dónde configurarlo.
    resumen: dict[str, dict] = {
        num: {"canal": num, "nombre": nombre, "total": 0, "esperando": 0,
              "en_asesor": 0, "por_cerrar": 0}
        for num, nombre in mapa_canales.items()
    }
    # UNA sola ida a Redis para saber qué chats están en control humano (antes era
    # un `get` por conversación: cientos de idas y vueltas en cada refresco).
    en_pausa = await pausados(sorted(ids))

    async def _fila(cid: str) -> dict:
        m = meta.get(cid, {})
        ultimo = m.get("ultimo", "")
        ultimo_de = m.get("ultimo_de", "")
        if not ultimo_de:
            # Conversación anterior a que se guardara quién habló último (o sin meta):
            # lo deducimos del historial para que la lista sea correcta YA, sin
            # esperar a que llegue un mensaje nuevo. Se cachea por `ultimo_ts`: no
            # puede cambiar mientras no entre un mensaje nuevo.
            ultimo, ultimo_de = await _ultimo_del_historial(
                cid, ultimo, version=float(m.get("ultimo_ts") or 0)
            )
        # Canal = número NUESTRO por el que entró la conversación. Las que entraron
        # antes de que se guardara el emisor no tienen canal: van a SIN_CANAL, que es
        # un id propio (no "" ) para no confundirse con la pestaña "Todos".
        emisor = str(m.get("emisor") or "")
        canal = norm_num(emisor) or SIN_CANAL
        return {
            "chat_id": cid,
            "user_name": m.get("user_name", ""),
            "telefono": m.get("telefono", ""),
            "ultimo": ultimo,
            "ultimo_de": ultimo_de or "cliente",
            "ultimo_ts": m.get("ultimo_ts", 0),
            "pausado": cid in en_pausa,
            # Semáforo de cierre: ya viene calculado en el chatmeta (0 costo acá).
            # `sem` vacío = todavía no se le calculó nada: NO es "frío", es sin datos.
            "score": m.get("score"),
            # Lo que el supervisor movió A MANO gana sobre el cálculo: él sabe cosas que
            # el sistema no puede ver (lo llamó, pasó por la tienda, dijo que no compra).
            # El automático se conserva aparte para poder volver a él.
            "sem": m.get("sem_manual") or m.get("score_sem", "") or "",
            "sem_auto": m.get("score_sem", "") or "",
            "sem_manual": m.get("sem_manual", "") or "",
            "hitos": score.etiquetas(m.get("score_hitos")),
            # Pedido creado sin prueba de pago: en un envío hay que esperar la
            # transferencia antes de despachar; en un retiro se paga en el mostrador.
            "falta_pago": score.falta_pago(m.get("score_hitos")),
            # Pago esperando que el SUPERVISOR lo apruebe (el bot nunca lo da por bueno).
            "aprobacion": (m.get("aprobacion") or {}).get("estado", ""),
            "order_id": (m.get("aprobacion") or {}).get("order_id"),
            # Si hubo pago atrás o es un retiro: cambia lo que el panel puede decir
            # ("pago por verificar" vs "pedido por aprobar") y lo que recibe el cliente.
            "con_pago": bool((m.get("aprobacion") or {}).get("con_pago", True)),
            "canal": canal,
            "canal_nombre": (
                "Sin canal" if canal == SIN_CANAL else nombre_canal(emisor, mapa_canales)
            ),
        }

    # EN PARALELO PERO ACOTADO: lo que queda por chat (reconstruir el último mensaje
    # de los chats viejos) va concurrente, porque en serie con Redis remoto la lista
    # tardaba tanto que el panel se quedaba en "Cargando…". El semáforo es
    # imprescindible: sin tope, 300 chats pedían 300 conexiones a la vez y Redis
    # respondía "max number of clients reached" a TODO, incluido el bot atendiendo.
    limite = asyncio.Semaphore(_CONCURRENCIA)

    async def _fila_acotada(cid: str) -> dict:
        async with limite:
            return await _fila(cid)

    chats = list(await asyncio.gather(*(_fila_acotada(cid) for cid in ids)))

    for c in chats:
        r = resumen.setdefault(
            c["canal"],
            {"canal": c["canal"], "nombre": c["canal_nombre"],
             "total": 0, "esperando": 0, "en_asesor": 0, "por_cerrar": 0},
        )
        r["total"] += 1
        if c["sem"] == "verde":
            r["por_cerrar"] += 1
        if c["pausado"]:
            r["en_asesor"] += 1
        elif c["ultimo_de"] == "cliente":
            # El cliente escribió y nadie contestó todavía.
            r["esperando"] += 1

    chats.sort(key=lambda c: c.get("ultimo_ts", 0), reverse=True)
    # Orden estable (por número): las pestañas no deben saltar de lugar cuando entra
    # una conversación y un canal pasa al otro en cantidad.
    canales = sorted(resumen.values(), key=lambda c: c["canal"])
    return {
        "total": len(chats),
        "chats": chats,
        "canales": canales,
        # Vacío = todo bien. Con texto = no se pudo leer el índice (Redis): la lista
        # está vacía por FALLA, no porque no haya conversaciones.
        "degradado": degradado,
    }


@panel_router.post("/api/chats/asignar-canal")
async def api_asignar_canal(
    canal: str = "", x_panel_token: str | None = Header(default=None)
) -> dict:
    """Manda a un número todas las conversaciones que quedaron SIN canal.

    Las conversaciones que ya existían antes de que el bot guardara por qué número
    entraron no se pueden atribuir solas (el dato no está en ninguna parte), y no se
    adivina: lo decide quien opera, que sabe con qué número venía atendiendo. Desde
    el próximo mensaje, cada conversación queda en su canal por sí sola.
    """
    _auth(x_panel_token)
    c = canal_id(canal)
    if not c:
        raise HTTPException(status_code=400, detail="indicá a qué número asignarlas")

    meta = await events.todos_chatmeta(estricto=True)
    huerfanos = [cid for cid, m in meta.items() if not norm_num(str(m.get("emisor") or ""))]
    for cid in huerfanos:
        m = dict(meta[cid])
        m["emisor"] = c
        m.setdefault("destino", {"to": cid})
        try:
            await with_reconnect(
                lambda r, k=cid, v=m: r.hset(
                    events.CHATMETA_KEY, k, json.dumps(v, ensure_ascii=False)
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("asignar_canal_fallo", chat_id=cid, error=str(exc))

    log.info("canal_asignado_a_huerfanos", canal=c, total=len(huerfanos))
    await events.publicar(
        "control", "-", emisor=c,
        detalle=f"{len(huerfanos)} conversación(es) sin canal asignadas a {nombre_canal(c)}",
    )
    return {"ok": True, "asignadas": len(huerfanos), "canal": c}


@panel_router.post("/api/chats/{chat_id}/semaforo")
async def api_mover_semaforo(
    chat_id: str, request: Request, x_panel_token: str | None = Header(default=None)
) -> dict:
    """Mueve una conversación de columna A MANO. Body: {"sem": "verde"|""|...}.

    `sem` vacío = volver al automático. El cálculo del sistema NO se pisa nunca (se
    guarda aparte), así que esto es reversible y no borra nada.
    """
    _auth(x_panel_token)
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    sem = str((data or {}).get("sem", "")).strip().lower()
    if sem and sem not in score.PRIORIDAD:
        raise HTTPException(
            status_code=400,
            detail=f"sem debe ser uno de {sorted(score.PRIORIDAD)} o vacío (automático)",
        )
    if not await events.guardar_sem_manual(chat_id, sem):
        raise HTTPException(status_code=502, detail="no se pudo guardar (Redis)")
    log.info("semaforo_movido_a_mano", chat_id=chat_id, sem=sem or "auto")
    return {"ok": True, "sem": sem}


@panel_router.post("/api/chats/calcular-semaforo")
async def api_calcular_semaforo(
    canal: str = "",
    rehacer: bool = False,
    x_panel_token: str | None = Header(default=None),
) -> dict:
    """Calcula el semáforo de las conversaciones que YA existían, leyendo su historial.

    El semáforo se calcula al cerrar cada turno, así que los chats anteriores no tienen
    ninguno. Esto los pinta sin esperar a que el cliente vuelva a escribir. Se hace a
    pedido (no en cada refresco) porque cuesta una lectura del historial POR chat, y el
    resultado queda guardado: se paga una sola vez.

    Sólo mira mensajes del cliente y SALIDAS DE TOOLS: si el modelo escribió "tu pedido
    quedó registrado" sin que existiera, no cuenta como hito.
    """
    _auth(x_panel_token)
    c = canal_id(canal)
    meta = await events.todos_chatmeta(estricto=True)
    minimo = _monto_minimo_de((await load_config(c)).monto_minimo)

    # MISMO universo que la lista del panel: hay conversaciones que existen sólo como
    # sesión, sin fila en el índice (las más viejas). Si se iteraran sólo las del
    # índice, esas quedarían sin semáforo para siempre.
    pendientes = []
    for cid in set(meta.keys()) | set(await _chat_ids_de_sesiones()):
        m = meta.get(cid, {})
        if not rehacer and m.get("score") is not None:
            continue
        # Las que no tienen emisor no son de ningún número (pestaña "Sin canal"): sólo
        # entran cuando se pide sin filtrar por canal.
        if c and norm_num(str(m.get("emisor") or "")) != c:
            continue
        pendientes.append(cid)
    limite = asyncio.Semaphore(_CONCURRENCIA)
    hechos = {"calculadas": 0, "con_senales": 0}

    async def _una(cid: str) -> None:
        async with limite:
            try:
                items = await RedisSession(cid).get_items(limit=settings.session_max_items)
                puntos = score.reconstruir(items, minimo)
                if await events.guardar_score(
                    cid, puntos["score"], puntos["sem"], puntos["hitos"]
                ):
                    hechos["calculadas"] += 1
                    if puntos["hitos"]:
                        hechos["con_senales"] += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("semaforo_no_calculado", chat_id=cid, error=str(exc))

    await asyncio.gather(*(_una(cid) for cid in pendientes))
    log.info("semaforo_calculado", canal=c or "todos", **hechos)
    return {"ok": True, "pendientes": len(pendientes), **hechos}


def _monto_minimo_de(valor: Any) -> float:
    try:
        return float(str(valor).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


@panel_router.get("/api/chats/{chat_id}")
async def api_chat_hilo(
    chat_id: str, x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    # Items CRUDOS con su índice real (`_idx`): el panel permite editar/borrar
    # mensajes y necesita la posición exacta en Redis, no la de la lista saneada.
    items = await RedisSession(chat_id).items_con_indice()
    meta = await events.leer_chatmeta(chat_id)
    return {
        "chat_id": chat_id,
        "pausado": await bot_pausado(chat_id),
        "meta": meta,
        "items": items,
    }


@panel_router.patch("/api/chats/{chat_id}/mensajes/{indice}")
async def api_mensaje_editar(
    chat_id: str, indice: int, request: Request,
    x_panel_token: str | None = Header(default=None),
) -> dict:
    """Corrige el texto de un mensaje del historial (lo que el bot recuerda).

    No reenvía nada al cliente: sólo cambia la memoria de la conversación.
    """
    _auth(x_panel_token)
    data = await request.json()
    texto = str(data.get("texto", "")).strip()
    if not texto:
        raise HTTPException(status_code=400, detail="texto requerido")
    ok = await RedisSession(chat_id).editar_item(indice, texto)
    if not ok:
        raise HTTPException(status_code=404, detail="no se encontró ese mensaje")
    log.info("mensaje_editado", chat_id=chat_id, indice=indice)
    await events.publicar("control", chat_id, detalle="mensaje corregido en el historial")
    return {"ok": True}


@panel_router.delete("/api/chats/{chat_id}/mensajes/{indice}")
async def api_mensaje_borrar(
    chat_id: str, indice: int, x_panel_token: str | None = Header(default=None)
) -> dict:
    """Saca un mensaje del historial (ej. una respuesta mala del bot) para que no
    lo repita ni lo confunda. No borra nada en WhatsApp."""
    _auth(x_panel_token)
    ok = await RedisSession(chat_id).borrar_item(indice)
    if not ok:
        raise HTTPException(status_code=404, detail="no se encontró ese mensaje")
    log.info("mensaje_borrado", chat_id=chat_id, indice=indice)
    await events.publicar("control", chat_id, detalle="mensaje borrado del historial")
    return {"ok": True}


@panel_router.delete("/api/chats/{chat_id}/memoria")
async def api_memoria_limpiar(
    chat_id: str, x_panel_token: str | None = Header(default=None)
) -> dict:
    """Reinicia la memoria: el bot arranca de cero con este cliente. La conversación
    sigue en el panel (no se borra el chat, sólo lo que el bot recuerda)."""
    _auth(x_panel_token)
    await RedisSession(chat_id).clear_session()
    log.info("memoria_limpiada", chat_id=chat_id)
    await events.publicar("control", chat_id, detalle="memoria del chat reiniciada")
    return {"ok": True}


@panel_router.post("/api/chats/{chat_id}/nota")
async def api_nota_interna(
    chat_id: str, request: Request, x_panel_token: str | None = Header(default=None)
) -> dict:
    """Agrega contexto para el BOT (no se le manda al cliente).

    Ej: "este cliente es mayorista, cotízale por volumen". El bot lo lee en el
    próximo turno como parte del historial.
    """
    _auth(x_panel_token)
    data = await request.json()
    texto = str(data.get("texto", "")).strip()
    if not texto:
        raise HTTPException(status_code=400, detail="texto requerido")
    await RedisSession(chat_id).add_items([{
        "role": "assistant",
        "content": (
            f"[NOTA INTERNA del supervisor — NO se la menciones al cliente] {texto}"
        ),
    }])
    log.info("nota_interna", chat_id=chat_id)
    await events.publicar("control", chat_id, detalle=f"nota para el bot: {texto[:120]}")
    return {"ok": True}


@panel_router.delete("/api/chats/{chat_id}")
async def api_eliminar_chat(
    chat_id: str, x_panel_token: str | None = Header(default=None)
) -> dict:
    """Elimina la conversación del panel: borra el historial (Redis) y la saca del
    índice de chats. No toca Odoo (clientes/pedidos quedan intactos)."""
    _auth(x_panel_token)
    await RedisSession(chat_id).clear_session()
    await events.borrar_chatmeta(chat_id)
    log.info("chat_eliminado", chat_id=chat_id)
    return {"ok": True}


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

    # enviar_texto devuelve bool: _post no lanza (traga 4xx/timeouts), así que sin
    # mirar el retorno el panel decía "enviado" incluso cuando YCloud lo rechazaba.
    enviado = False
    try:
        enviado = await ycloud.enviar_texto(destino, emisor, texto, simular_tipeo=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("panel_responder_envio_fallo", chat_id=chat_id, error=str(exc))
    if not enviado:
        log.warning("panel_responder_no_enviado", chat_id=chat_id)

    # Pausa el bot 30 min y registra en historial + feed.
    await pausar_bot(chat_id)
    await RedisSession(chat_id).add_items(
        [{"role": "assistant", "content": f"[SUPERVISOR] {texto}"}]
    )
    await events.publicar(
        "manual", chat_id, detalle=texto[:200], enviado=enviado, user_name=meta.get("user_name", "")
    )
    # En la lista de chats, lo último pasa a ser lo que escribió el asesor.
    await events.tocar_chatmeta(
        chat_id, emisor=emisor, destino=destino,
        user_name=meta.get("user_name", ""), telefono=meta.get("telefono", ""),
        ultimo=texto, ultimo_de="asesor",
    )
    return {"ok": True, "enviado": enviado, "pausado": True}


# --------------------------------------------- aprobación del pago ---
@panel_router.post("/api/chats/{chat_id}/aprobar-pago")
async def api_aprobar_pago(
    chat_id: str, x_panel_token: str | None = Header(default=None)
) -> dict:
    """El SUPERVISOR da el pago por bueno: recién ahí el cliente recibe la confirmación.

    El bot nunca hace esto solo. Cuando llega el comprobante avisa que se está
    verificando; el número de pedido "registrado exitosamente" sale de acá.

    NO pausa el bot (a diferencia de responder a mano): este mensaje es del bot, no
    una toma de control del chat.
    """
    _auth(x_panel_token)
    res = await pagos.aprobar(chat_id, via="panel")
    if not res.get("ok"):
        raise HTTPException(
            status_code=int(res.get("status", 400)), detail=res.get("error", "")
        )
    return res


@panel_router.post("/api/chats/{chat_id}/rechazar-pago")
async def api_rechazar_pago(
    chat_id: str, request: Request, x_panel_token: str | None = Header(default=None)
) -> dict:
    """Marca el pago como NO aprobado. A propósito NO le escribe nada al cliente.

    Decirle a alguien que su pago no sirve es una conversación que tiene que tener una
    persona, con el motivo real (monto distinto, comprobante ilegible, transferencia no
    acreditada). El panel deja el chat marcado y el supervisor escribe.
    """
    _auth(x_panel_token)
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = {}
    motivo = str((data or {}).get("motivo", "")).strip()
    res = await pagos.rechazar(chat_id, motivo, via="panel")
    if not res.get("ok"):
        raise HTTPException(
            status_code=int(res.get("status", 400)), detail=res.get("error", "")
        )
    return res


# ------------------------------------------------ respuesta con imagen ---
# El supervisor sube una imagen desde el panel y YCloud la busca en
# /panel/media/{token} para reenviarla al cliente. El cache vive en
# `app/media_publica.py` porque lo comparte el pipeline: ahí se publica el
# comprobante para que WhatsApp pueda leerlo en la plantilla de aprobación.
def _media_guardar(data: bytes, content_type: str) -> str:
    return media_publica.guardar(data, content_type)


@panel_router.get("/media/{token}")
async def serve_media(token: str) -> Response:
    """Sirve un archivo (imagen/audio) subido por el supervisor. PÚBLICA (sin
    token): YCloud lo fetchea sin auth, igual que /img. Transitorio (en memoria)."""
    entry = media_publica.obtener(token)
    if entry is None:
        raise HTTPException(status_code=404, detail="media no encontrada")
    ctype, data = entry
    return Response(
        content=data,
        media_type=ctype,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@panel_router.post("/api/chats/{chat_id}/responder-imagen")
async def api_responder_imagen(
    chat_id: str,
    file: UploadFile = File(...),
    caption: str = Form(""),
    x_panel_token: str | None = Header(default=None),
) -> dict:
    """Respuesta manual con IMAGEN (adjunto o cámara). Igual que /responder:
    pausa el bot 30 min, manda por YCloud y guarda en el historial."""
    _auth(x_panel_token)
    if not settings.base_url:
        raise HTTPException(
            status_code=400,
            detail="PUBLIC_BASE_URL no configurado: YCloud no podría buscar la imagen.",
        )
    data = await file.read()
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="la imagen es muy grande (máx 12 MB).")
    # Pillow valida y convierte a la vez: si no es imagen, convertir_a_jpg falla.
    try:
        jpg = await convertir_a_jpg(data)
    except Exception as exc:  # noqa: BLE001
        log.warning("panel_imagen_convertir_fallo", chat_id=chat_id, error=str(exc))
        raise HTTPException(status_code=400, detail="el archivo no es una imagen válida.")

    token = _media_guardar(jpg, "image/jpeg")
    url = f"{settings.base_url}/panel/media/{token}.jpg"
    cap = (caption or "").strip()

    meta = await events.leer_chatmeta(chat_id)
    emisor = meta.get("emisor") or settings.ycloud_from
    destino = meta.get("destino") or {"to": chat_id}

    enviado = await ycloud.enviar_imagen(destino, emisor, url, cap[:1024])

    # Pausa el bot 30 min y registra en historial + feed (igual que el texto).
    await pausar_bot(chat_id)
    etiqueta = "[SUPERVISOR] (imagen)" + (f" {cap}" if cap else "")
    await RedisSession(chat_id).add_items(
        [{"role": "assistant", "content": etiqueta}]
    )
    await events.publicar(
        "manual", chat_id, detalle=etiqueta[:200], enviado=bool(enviado),
        user_name=meta.get("user_name", ""),
    )
    return {"ok": True, "enviado": bool(enviado), "pausado": True}


# mime del navegador -> extensión que le ponemos a la URL (WhatsApp mira ambos).
_AUDIO_EXT = {
    "audio/ogg": "ogg", "audio/opus": "ogg", "audio/webm": "webm",
    "audio/mpeg": "mp3", "audio/mp4": "mp4", "audio/aac": "aac", "audio/amr": "amr",
}


@panel_router.post("/api/chats/{chat_id}/responder-audio")
async def api_responder_audio(
    chat_id: str,
    file: UploadFile = File(...),
    x_panel_token: str | None = Header(default=None),
) -> dict:
    """Respuesta manual con NOTA DE VOZ. Igual que /responder: pausa el bot 30
    min, la manda por YCloud y la registra en el historial.

    Nota: el navegador suele grabar en webm/opus; WhatsApp prefiere ogg/opus. Si
    algún cliente no reproduce el audio, habría que convertir en el server (ffmpeg)."""
    _auth(x_panel_token)
    if not settings.base_url:
        raise HTTPException(
            status_code=400,
            detail="PUBLIC_BASE_URL no configurado: YCloud no podría buscar el audio.",
        )
    data = await file.read()
    if not data or len(data) > 16 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="audio vacío o muy grande (máx 16 MB).")
    ctype = (file.content_type or "audio/ogg").split(";")[0].strip().lower()
    # WhatsApp/YCloud NO acepta audio/webm (lo que graba Chrome). Convertimos a
    # ogg/opus con ffmpeg. Los formatos ya compatibles se mandan tal cual.
    soportados = {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/aac", "audio/amr"}
    if ctype not in soportados:
        try:
            data = await convertir_audio_ogg(data)
            ctype = "audio/ogg"
        except Exception as exc:  # noqa: BLE001
            log.warning("audio_convertir_fallo", chat_id=chat_id, error=str(exc))
            raise HTTPException(
                status_code=400,
                detail="no se pudo convertir el audio a un formato compatible.",
            )
    ext = _AUDIO_EXT.get(ctype, "ogg")
    token = _media_guardar(data, ctype)
    url = f"{settings.base_url}/panel/media/{token}.{ext}"

    meta = await events.leer_chatmeta(chat_id)
    emisor = meta.get("emisor") or settings.ycloud_from
    destino = meta.get("destino") or {"to": chat_id}

    enviado = await ycloud.enviar_audio(destino, emisor, url)

    await pausar_bot(chat_id)
    await RedisSession(chat_id).add_items(
        [{"role": "assistant", "content": "[SUPERVISOR] (nota de voz)"}]
    )
    await events.publicar(
        "manual", chat_id, detalle="(nota de voz)", enviado=bool(enviado),
        user_name=meta.get("user_name", ""),
    )
    return {"ok": True, "enviado": bool(enviado), "pausado": True}


# --------------------------------------------------------- config bot ---
@panel_router.get("/api/config")
async def api_config_get(
    canal: str = "", x_panel_token: str | None = Header(default=None)
) -> dict:
    """Config EFECTIVA del canal elegido (común + lo propio de ese número).

    `propios` dice qué campos tiene personalizados ese canal, para que el panel
    marque cuáles ya no siguen al común.
    """
    _auth(x_panel_token)
    c = canal_id(canal)
    cfg = config_as_dict(await load_config(c, force=True))
    propios = await overrides_de_canal(c) if c else {}
    salida = {**cfg, "_canal": c, "_propios": sorted(propios.keys())}
    if not c:
        # Editando la COMÚN: hay que avisar qué número no verá el cambio porque tiene
        # ese campo personalizado (si no, el operador cree que aplicó a los dos).
        salida["_propios_por_canal"] = {
            num: {"nombre": nombre_canal(num), "campos": campos}
            for num, campos in (await overrides_por_canal()).items()
        }
    return salida


@panel_router.post("/api/config")
async def api_config_set(
    request: Request, canal: str = "", x_panel_token: str | None = Header(default=None)
) -> dict:
    """Guarda la config. Con `canal` toca SÓLO ese número; con `ambos: true`, los dos."""
    _auth(x_panel_token)
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="se esperaba objeto JSON")
    ambos = bool(data.pop("_ambos", False))
    c = canal_id(canal)
    antes = set(await canales_configurados())
    await save_config(data, canal=c, ambos=ambos)
    # Si se agregó/quitó un número, los caches por canal (prompts, conocimiento,
    # agentes) tienen que aprender el canal nuevo YA: si no, el número recién dado de
    # alta atendería con lo común hasta el próximo reinicio.
    if set(await canales_configurados()) != antes:
        await prompt_store.cargar()
        await conocimiento.cargar()
        await agentes_custom.cargar()
    donde = "los dos números" if (ambos or not c) else nombre_canal(c)
    await events.publicar(
        "control", "-", emisor=c,
        detalle=f"config de negocio actualizada ({donde})",
    )
    return {"ok": True, "canal": c, "ambos": ambos or not c}


@panel_router.delete("/api/config")
async def api_config_reset(
    canal: str = "", x_panel_token: str | None = Header(default=None)
) -> dict:
    """Este canal vuelve a heredar la config común (borra lo propio)."""
    _auth(x_panel_token)
    c = canal_id(canal)
    if not c:
        raise HTTPException(status_code=400, detail="indicá el canal a resetear")
    await resetear_canal(c)
    await events.publicar(
        "control", "-", emisor=c,
        detalle=f"config de {nombre_canal(c)} vuelve a la común",
    )
    return {"ok": True}


@panel_router.get("/api/anuncios")
async def api_anuncios(x_panel_token: str | None = Header(default=None)) -> dict:
    _auth(x_panel_token)
    return await listar_anuncios()


# ------------------------------------------------------ prompts por agente ---
@panel_router.get("/api/prompts")
async def api_prompts_get(
    canal: str = "", x_panel_token: str | None = Header(default=None)
) -> dict:
    """Prompts VIGENTES en ese canal, más de dónde sale cada uno.

    `origen`: 'canal' (propio de este número), 'comun' (los dos) o 'base' (el .md).
    """
    _auth(x_panel_token)
    c = canal_id(canal)
    todos = prompt_store.agentes()
    return {
        "canal": c,
        "agentes": list(todos),
        "prompts": {
            a: {
                "base": prompt_store.get_base(a),
                "override": (
                    prompt_store.get_prompt(a, c)
                    if prompt_store.usando_override(a, c) else ""
                ),
                "usando_override": prompt_store.usando_override(a, c),
                "origen": prompt_store.origen(a, c),
                # El común, para poder comparar contra lo propio del canal.
                "comun": prompt_store.get_prompt(a, COMUN),
            }
            for a in todos
        },
        # Agentes que atienden en ESTE canal (propios del número + comunes).
        "personalizados": agentes_custom.listar(c),
        "packs": agentes_custom.PACKS,
    }


@panel_router.post("/api/prompts/{agente}")
async def api_prompt_set(
    agente: str,
    request: Request,
    canal: str = "",
    x_panel_token: str | None = Header(default=None),
) -> dict:
    """Guarda el prompt. Con `canal` sólo para ese número; con `ambos: true`, los dos."""
    _auth(x_panel_token)
    if agente not in prompt_store.agentes():
        raise HTTPException(status_code=404, detail=f"agente desconocido: {agente}")
    data = await request.json()
    texto = str(data.get("override", ""))
    ambos = bool(data.get("ambos", False))
    c = canal_id(canal)
    if texto.strip() and len(texto.strip()) < 40:
        raise HTTPException(
            status_code=400,
            detail="prompt demasiado corto; podría romper el agente. Mínimo 40 caracteres, o vacío para volver al .md base.",
        )
    await prompt_store.guardar(agente, texto, canal=c, ambos=ambos)
    donde = "los dos números" if (ambos or not c) else nombre_canal(c)
    await events.publicar(
        "control", "-", emisor=c,
        detalle=(
            f"prompt '{agente}' "
            f"{'guardado' if texto.strip() else 'restaurado al base'} ({donde})"
        ),
    )
    return {
        "ok": True,
        "agente": agente,
        "canal": c,
        "usando_override": prompt_store.usando_override(agente, c),
        "origen": prompt_store.origen(agente, c),
    }


# ------------------------------------------- agentes creados desde el panel ---
@panel_router.post("/api/agentes")
async def api_agente_crear(
    request: Request, canal: str = "", x_panel_token: str | None = Header(default=None)
) -> dict:
    """Crea (o actualiza) un agente PERSONALIZADO que de verdad atiende turnos.

    Body: {nombre, descripcion, herramientas:[...], palabras:[...], modelo, prompt, ambos}
    El prompt se guarda con prompt_store (igual que los agentes base), así que se
    puede editar o subir un .md después desde la misma pantalla.

    Con `canal` el agente atiende SÓLO ese número; con `ambos: true`, los dos.
    """
    _auth(x_panel_token)
    data = await request.json()
    nombre = str(data.get("nombre", "")).strip().lower()
    prompt = str(data.get("prompt", "") or "")
    herramientas = data.get("herramientas") or []
    palabras = data.get("palabras") or []
    ambos = bool(data.get("ambos", False))
    c = canal_id(canal)
    if isinstance(palabras, str):
        palabras = [p for p in palabras.split(",")]
    if len(prompt.strip()) < 40:
        raise HTTPException(
            status_code=400,
            detail="El prompt del agente debe tener al menos 40 caracteres: es lo que "
                   "define cómo se comporta.",
        )
    try:
        cfg = await agentes_custom.guardar(
            nombre=nombre,
            descripcion=str(data.get("descripcion", "")),
            herramientas=list(herramientas),
            palabras=list(palabras),
            modelo=str(data.get("modelo", "mini")),
            canal=c,
            ambos=ambos,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await prompt_store.guardar(nombre, prompt, canal=c, ambos=ambos)
    donde = "los dos números" if (ambos or not c) else nombre_canal(c)
    await events.publicar(
        "control", "-", emisor=c,
        detalle=f"agente '{nombre}' creado/actualizado desde el panel ({donde})",
    )
    return {"ok": True, "agente": cfg}


@panel_router.delete("/api/agentes/{nombre}")
async def api_agente_borrar(
    nombre: str, canal: str = "", x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    c = canal_id(canal)
    ok = await agentes_custom.borrar(nombre, canal=c)
    if not ok:
        raise HTTPException(status_code=404, detail=f"no existe el agente '{nombre}'")
    await events.publicar(
        "control", "-", emisor=c, detalle=f"agente '{nombre}' eliminado",
    )
    return {"ok": True}


# ------------------------------------------------------------ revisión ---
@panel_router.get("/api/revision")
async def api_revision(
    limite: int = 50, canal: str = "", x_panel_token: str | None = Header(default=None)
) -> dict:
    """Cola de revisión; con `canal`, sólo los casos de ese número."""
    _auth(x_panel_token)
    items = await listar_revision(limite)
    c = canal_id(canal)
    if c:
        meta = await events.todos_chatmeta()
        items = [
            i for i in items
            if canal_id(str((meta.get(str(i.get("chat_id", ""))) or {}).get("emisor", "")))
            == c
        ]
    return {"total": len(items), "items": items}


# --------------------------------------------------- mejora continua ---
@panel_router.get("/api/aprendizaje")
async def api_aprendizaje(
    canal: str = "", x_panel_token: str | None = Header(default=None)
) -> dict:
    """Lo que aplica a ese canal: sus reglas/correcciones propias + las comunes.

    Cada ítem trae `canal` ("" = común a los dos números) para que el panel lo marque.
    """
    _auth(x_panel_token)
    c = canal_id(canal)
    return {
        "canal": c,
        "reglas": await conocimiento.list_reglas(c),
        "correcciones": await conocimiento.list_correcciones(c),
        "sugerencias": await conocimiento.list_sugerencias(canal=c),
    }


@panel_router.post("/api/reglas")
async def api_regla_add(
    request: Request, canal: str = "", x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    data = await request.json()
    texto = str(data.get("texto", "")).strip()
    if len(texto) < 5:
        raise HTTPException(status_code=400, detail="regla demasiado corta")
    c = canal_id(canal)
    ambos = bool(data.get("ambos", False))
    r = await conocimiento.add_regla(texto, canal=c, ambos=ambos)
    donde = "los dos números" if (ambos or not c) else nombre_canal(c)
    await events.publicar(
        "control", "-", emisor=c, detalle=f"regla agregada ({donde}): {texto[:80]}",
    )
    return {"ok": True, "regla": r}


@panel_router.delete("/api/reglas/{rid}")
async def api_regla_del(
    rid: int, canal: str = "", x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    await conocimiento.del_regla(rid, canal=canal_id(canal))
    return {"ok": True}


@panel_router.post("/api/correcciones")
async def api_correccion_add(
    request: Request, canal: str = "", x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    data = await request.json()
    situacion = str(data.get("situacion", "")).strip()
    respuesta = str(data.get("respuesta_correcta", "")).strip()
    if not situacion or not respuesta:
        raise HTTPException(status_code=400, detail="situacion y respuesta_correcta requeridas")
    canal_efectivo = canal_id(canal)
    chat_id = str(data.get("chat_id", ""))
    # Si la corrección salió de una conversación, el canal es el de ESA conversación.
    if chat_id:
        emisor = (await events.leer_chatmeta(chat_id)).get("emisor", "")
        canal_efectivo = canal_id(emisor) or canal_efectivo
    c = await conocimiento.add_correccion(
        situacion, respuesta, str(data.get("motivo", "")), chat_id,
        canal=canal_efectivo, ambos=bool(data.get("ambos", False)),
    )
    await events.publicar(
        "control", chat_id or "-", emisor=canal_efectivo, detalle="corrección aprendida",
    )
    return {"ok": True, "correccion": c}


@panel_router.delete("/api/correcciones/{cid}")
async def api_correccion_del(
    cid: int, canal: str = "", x_panel_token: str | None = Header(default=None)
) -> dict:
    _auth(x_panel_token)
    await conocimiento.del_correccion(cid, canal=canal_id(canal))
    return {"ok": True}


@panel_router.post("/api/sugerencias/analizar")
async def api_analizar(
    canal: str = "", x_panel_token: str | None = Header(default=None)
) -> dict:
    """Analiza los casos de ese canal (sin canal, todos, por separado)."""
    _auth(x_panel_token)
    resultado = await analizar_y_sugerir(
        auto_aplicar_bajo_riesgo=False, canal=canal_id(canal)
    )
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
