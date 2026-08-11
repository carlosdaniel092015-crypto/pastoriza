"""Enrutador: decide a qué especialista va cada mensaje.

DETERMINISTA primero (0 tokens); clasificador mini solo ante duda. Las FAQ ya las
resuelve el fast-path (`app/router.py`) antes de llegar aquí; esto decide entre
ventas / pedido / soporte.
"""
from __future__ import annotations

import re

from openai import AsyncOpenAI

from app.logging_conf import get_logger
from app.matching import quitar_tildes
from app.settings import settings

log = get_logger(__name__)

_openai = AsyncOpenAI(api_key=settings.openai_api_key)

VALIDOS = ("ventas", "pedido", "soporte")

# Nota: solo `\b` de INICIO. Sin `\b` final, para que los stems matcheen sus
# inflexiones ("cancel" -> "cancelar", "botella" -> "botellas").
RE_SOPORTE = re.compile(
    r"\b(cancel|anul|reembols|devol|queja|reclam|molest|enojad|furios|estaf|"
    r"quit|elimin|"
    r"hablar con (una|un)? ?(persona|humano|asesor|encargado|supervisor)|"
    r"(una|un) (persona|humano|asesor) real)"
)
RE_CIERRE = re.compile(
    r"\b(me llamo|mi nombre es|soy el |soy la |confirmo|lo confirmo|"
    r"s[ií],? (lo )?(quiero|confirmo)|proced|"
    r"mi (direccion|telefono|numero|cedula|rnc)|"
    r"(para|es) (envio|retiro)|a domicilio|lo retiro|"
    r"comprobante|ya (te |le )?(transferi|pague|deposite|hice la transferencia))"
)
RE_VENTAS = re.compile(
    r"\b(tienen|precio|cu[aá]nto (cuesta|vale|es|sale)|busco|me interesa|"
    r"quiero (comprar|ver|una|un|el|la|los|las|unas|unos)|"
    r"botella|galon|tarro|frasco|pomo|tapa|envase|atomizador|jarra|vaso|"
    r"onza|\boz\b|catalogo|producto|disponible)"
)


def _norm(texto: str) -> str:
    return quitar_tildes(str(texto or "")).lower().strip()


def ruta_deterministica(
    texto: str, es_comprobante: bool = False, tiene_imagen: bool = False
) -> str | None:
    """Enrutado SIN LLM (0 tokens). Devuelve el agente, o None si es ambiguo.

    Función pura y testeable (ver tests/test_enrutador.py).
    """
    # 1. Señales por media/estado.
    if es_comprobante:
        return "pedido"
    if tiene_imagen:
        return "ventas"
    n = _norm(texto)
    # 2. Reclamo/cancelación gana sobre todo.
    if RE_SOPORTE.search(n):
        return "soporte"
    # 3. Señales claras de cierre -> pedido.
    if RE_CIERRE.search(n):
        return "pedido"
    # 4. Señales de catálogo -> ventas.
    if RE_VENTAS.search(n):
        return "ventas"
    return None


async def elegir_agente(texto: str, ctx, session=None) -> str:
    """Devuelve 'ventas' | 'pedido' | 'soporte'."""
    ruta = ruta_deterministica(
        texto,
        es_comprobante=bool(getattr(ctx, "es_comprobante", False)),
        tiene_imagen=bool(getattr(ctx, "imagen_url", "")),
    )
    if ruta:
        return ruta
    # Ambiguo -> clasificador mini con contexto reciente.
    return await _clasificar(texto, session)


async def _clasificar(texto: str, session=None) -> str:
    from app.panel.prompt_store import get_prompt

    contexto = ""
    if session is not None:
        try:
            items = await session.get_items(limit=6)
            contexto = "\n".join(
                f"{i.get('role', '?')}: {str(i.get('content', ''))[:200]}" for i in items
            )
        except Exception:  # noqa: BLE001
            contexto = ""

    try:
        resp = await _openai.chat.completions.create(
            model=settings.model_mini,
            max_tokens=4,
            temperature=0,
            messages=[
                {"role": "system", "content": get_prompt("enrutador")},
                {
                    "role": "user",
                    "content": (
                        f"Conversación reciente:\n{contexto}\n\n"
                        f"Mensaje nuevo del cliente: {texto}\n\nAgente:"
                    ),
                },
            ],
        )
        out = (resp.choices[0].message.content or "").strip().lower()
        for a in ("pedido", "soporte", "ventas"):  # pedido/soporte antes que ventas
            if a in out:
                return a
    except Exception as exc:  # noqa: BLE001
        log.warning("enrutador_llm_fallo", error=str(exc))
    return "ventas"  # default seguro
