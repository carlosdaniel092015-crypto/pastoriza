"""Analista de mejora: revisa fallos recientes y PROPONE reglas nuevas.

No cambia nada solo (salvo lo de bajo riesgo, si está habilitado): genera
sugerencias que el supervisor aprueba/edita/rechaza en el panel. Es el
"pregúntame acciones" del ciclo de mejora.

Se dispara a mano (endpoint /panel/api/sugerencias/analizar) y luego se puede
programar con cron. Cuesta ~1 llamada al modelo por corrida.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any

from openai import AsyncOpenAI

from app.canales import COMUN, canal_id, formatear
from app.estado import listar_revision
from app.logging_conf import get_logger
from app.panel import conocimiento, events, telegram
from app.settings import settings

log = get_logger(__name__)

_openai = AsyncOpenAI(api_key=settings.openai_api_key)

PROMPT_ANALISTA = """Eres un analista de calidad del bot de ventas de Pastoriza Plastics (envases plásticos, WhatsApp).
Te paso un resumen de casos recientes que necesitaron revisión humana (motivos y ejemplos).
Propón hasta 3 REGLAS concretas, cortas y accionables que, agregadas a las instrucciones del bot, reducirían esos casos.
Cada regla debe ser una sola frase imperativa, en español, aplicable por el bot sin ambigüedad.
NO propongas cancelar/eliminar pedidos (está prohibido). NO cambies precios ni políticas de pago.
Devuelve SOLO este JSON: {"sugerencias":[{"texto":"...","riesgo":"bajo|alto"}]}.
riesgo=bajo si es un ajuste menor de redacción/FAQ; riesgo=alto si cambia el flujo de venta o toca datos sensibles."""


async def _canal_de(chat_id: str) -> str:
    """Canal (número nuestro) por el que entró esa conversación."""
    try:
        meta = await events.leer_chatmeta(str(chat_id))
        return canal_id(meta.get("emisor", ""))
    except Exception:  # noqa: BLE001
        return COMUN


async def analizar_y_sugerir(
    auto_aplicar_bajo_riesgo: bool = False, canal: str = COMUN
) -> dict:
    """Analiza la cola de revisión y crea sugerencias, POR CANAL.

    Cada número es un negocio aparte para la operación: sus casos se analizan por
    separado y las mejoras que salen de ahí se aplican a ESE número. Con `canal` se
    analiza sólo uno; sin canal se analizan todos los que tengan casos (una llamada
    al modelo por canal).
    """
    revision = await listar_revision(80)
    if not revision:
        return {"analizado": 0, "sugeridas": 0, "auto_aplicadas": 0, "nota": "sin casos"}

    pedido = canal_id(canal)
    # Agrupar los casos por canal: el resumen que ve el modelo no debe mezclar números.
    por_canal: dict[str, list[dict]] = {}
    for item in revision:
        c = await _canal_de(str(item.get("chat_id", "")))
        if pedido and c != pedido:
            continue
        por_canal.setdefault(c, []).append(item)

    if not por_canal:
        return {"analizado": 0, "sugeridas": 0, "auto_aplicadas": 0, "nota": "sin casos"}

    total = {"analizado": 0, "sugeridas": 0, "auto_aplicadas": 0, "pendientes": 0}
    detalle: dict[str, dict] = {}
    for c, items in por_canal.items():
        res = await _analizar_canal(items, auto_aplicar_bajo_riesgo, c)
        detalle[c or "comun"] = res
        for k in ("analizado", "sugeridas", "auto_aplicadas", "pendientes"):
            total[k] += int(res.get(k, 0) or 0)
    if len(detalle) > 1:
        total["por_canal"] = detalle
    else:
        total.update({k: v for k, v in next(iter(detalle.values())).items() if k not in total})
    return total


async def _analizar_canal(
    revision: list[dict], auto_aplicar_bajo_riesgo: bool, canal: str
) -> dict:
    motivos = Counter()
    ejemplos: list[str] = []
    chats: list[str] = []
    for item in revision:
        for m in item.get("motivos", []):
            motivos[m] += 1
        if item.get("resumen"):
            ejemplos.append(str(item["resumen"])[:160])
        cid = item.get("chat_id")
        if cid and str(cid) not in chats:
            chats.append(str(cid))

    resumen = "Motivos más frecuentes:\n" + "\n".join(
        f"- {m}: {n}" for m, n in motivos.most_common(10)
    )
    if ejemplos:
        resumen += "\n\nEjemplos de respuestas del bot en esos casos:\n" + "\n".join(
            f"- {e}" for e in ejemplos[:12]
        )

    try:
        resp = await _openai.chat.completions.create(
            model=settings.model_agente,
            max_tokens=500,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": PROMPT_ANALISTA},
                {"role": "user", "content": resumen},
            ],
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception as exc:  # noqa: BLE001
        log.error("analista_fallo", error=str(exc))
        return {"error": str(exc), "analizado": len(revision)}

    propuestas = data.get("sugerencias", []) if isinstance(data, dict) else []
    creadas = 0
    auto = 0
    pendientes: list[dict] = []
    for p in propuestas:
        texto = str(p.get("texto", "")).strip()
        if not texto:
            continue
        riesgo = "alto" if str(p.get("riesgo", "")).lower().startswith("alt") else "bajo"
        if riesgo == "bajo" and auto_aplicar_bajo_riesgo:
            await conocimiento.add_regla(texto, origen="analisis-auto", canal=canal)
            s = await conocimiento.add_sugerencia(
                "regla", texto, riesgo, "analisis", chats[:8], canal=canal
            )
            await conocimiento._set_estado_sugerencia(s["id"], "aprobada")
            auto += 1
        else:
            s = await conocimiento.add_sugerencia(
                "regla", texto, riesgo, "analisis", chats[:8], canal=canal
            )
            pendientes.append(s)
        creadas += 1

    # Aviso a Telegram: resumen + una tarjeta por sugerencia con botones Aprobar/Rechazar.
    # Se dice de QUÉ número salieron: al aprobarlas se aplican sólo a ese canal.
    #
    # Con `telegram_solo_alertas` (el default) esto NO se manda: el analista corre cada
    # 24h y una tarjeta por sugerencia convierte a Telegram en un feed que se ignora, y
    # entonces también se ignoran los errores de verdad. Las sugerencias no son urgentes
    # y siguen enteras en el panel → Aprendizaje.
    de_quien = f" del número {formatear(canal)}" if canal else ""
    if settings.telegram_solo_alertas:
        log.info(
            "analista_sin_aviso_telegram",
            pendientes=len(pendientes), auto=auto,
            detalle="telegram_solo_alertas=True: las sugerencias quedan en el panel",
        )
    elif pendientes and telegram.configurado():
        await telegram.enviar(
            f"🧠 Analista: {len(pendientes)} sugerencia(s){de_quien} para revisar. "
            "Apruébalas o recházalas aquí abajo (o en el panel → Aprendizaje)."
        )
        for s in pendientes:
            await telegram.enviar_sugerencia(s)
    elif auto and telegram.configurado():
        await telegram.enviar(
            f"🧠 Analista: {auto} sugerencia(s){de_quien} aplicada(s) automáticamente."
        )

    return {
        "canal": canal,
        "analizado": len(revision),
        "sugeridas": creadas,
        "auto_aplicadas": auto,
        "pendientes": len(pendientes),
        "motivos_top": dict(motivos.most_common(5)),
    }
