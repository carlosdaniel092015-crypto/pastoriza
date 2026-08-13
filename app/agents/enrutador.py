"""Enrutador: decide a qué especialista va cada mensaje.

DETERMINISTA primero (0 tokens); clasificador mini solo ante duda. Las FAQ ya las
resuelve el fast-path (`app/router.py`) antes de llegar aquí; esto decide entre
ventas / pedido / soporte.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

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
    r"botella|galon|tarro|frasco|pomo|tapa|envase|atomizador|jarra|vaso|pote|"
    r"onza|\boz\b|catalogo|producto|disponible|vac[ií]o)"
)


# ---------------------------------------------------------- determinador ---
# Señales EXPLÍCITAS de que el caso SÍ amerita una persona. Son deterministas
# (0 tokens) y habilitan la escalada; sin una de estas (o sin el visto bueno del
# determinador con IA), `escalar_a_humano` queda BLOQUEADA.
RE_PIDE_HUMANO = re.compile(
    r"(hablar con (una |un )?(persona|humano|asesor|encargado|supervisor|alguien|"
    r"representante|vendedor)|(una|un) (persona|humano|asesor) real|"
    r"pasame con|comunicame con|quiero hablar con)"
)
RE_AMERITA_HUMANO = re.compile(
    r"\b(cancel|anul|reembols|devol|queja|reclam|estaf|fraude|denunc|abogad|"
    r"demand|molest|enojad|furios|indignad|pesim|horrible|malisim|robo|robaron|"
    r"enganaron|enganad|quit|elimin|no me llego|no ha llegado|llego roto|"
    r"llego dana|defectuos|mal estado)"
)


@dataclass
class Veredicto:
    """Salida del determinador: a quién va el turno y si puede escalar a un humano."""

    agente: str
    permite_escalar: bool
    motivo: str = ""


def _norm(texto: str) -> str:
    return quitar_tildes(str(texto or "")).lower().strip()


def senales_humano(texto: str) -> bool:
    """True si el TEXTO trae una señal explícita de que amerita una persona."""
    n = _norm(texto)
    return bool(RE_PIDE_HUMANO.search(n) or RE_AMERITA_HUMANO.search(n))


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
    v = await analizar_contexto(texto, ctx, session)
    return v.agente


async def analizar_contexto(texto: str, ctx, session=None) -> Veredicto:
    """DETERMINADOR: decide el especialista Y si el caso amerita una persona.

    Corre ANTES del enrutado y es lo que habilita (o no) `escalar_a_humano`. Existe
    porque el modelo llegaba a escalar cosas que debe resolver él —incluso un
    SALUDO—: ahora la escalada necesita una señal explícita del cliente o el visto
    bueno de este análisis, que sí mira el CONTEXTO de la conversación.

    Determinista primero (0 tokens); el modelo mini sólo entra ante duda, y en ese
    caso resuelve agente + escalada en UNA sola llamada (la que ya se hacía).
    """
    explicito = senales_humano(texto)
    ruta = ruta_deterministica(
        texto,
        es_comprobante=bool(getattr(ctx, "es_comprobante", False)),
        tiene_imagen=bool(getattr(ctx, "imagen_url", "")),
    )
    if ruta:
        return Veredicto(
            agente=ruta,
            permite_escalar=explicito,
            motivo="senal_explicita" if explicito else "ruta_deterministica",
        )
    if explicito:
        # Pide una persona / reclamo claro, pero la ruta no era obvia: a soporte.
        return Veredicto(agente="soporte", permite_escalar=True, motivo="senal_explicita")
    # Ambiguo -> el determinador con IA mira la conversación reciente.
    return await _determinar(texto, session)


INSTR_DETERMINADOR = """Además de elegir el agente, decide si el caso amerita una PERSONA.
amerita_humano = true SOLO si: el cliente pide explícitamente hablar con alguien, quiere
cancelar/quitar algo de un pedido, hay una queja seria o real (producto dañado, no llegó,
cobro mal), o hay insultos/abuso.
amerita_humano = false para TODO lo normal: saludos, cortesías ("hola", "buenas tardes,
cómo estás", "ok", "gracias"), preguntas de precio, medidas, envío, ubicación, pago,
disponibilidad, fotos y pedidos. Eso lo resuelve el bot.
Responde SOLO este JSON: {"agente":"ventas|pedido|soporte","amerita_humano":true|false,"motivo":"3 palabras"}"""


async def _determinar(texto: str, session=None) -> Veredicto:
    """Determinador con IA: agente + si amerita humano, mirando la conversación."""
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
            max_tokens=60,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": get_prompt("enrutador") + "\n\n" + INSTR_DETERMINADOR,
                },
                {
                    "role": "user",
                    "content": (
                        f"Conversación reciente:\n{contexto}\n\n"
                        f"Mensaje nuevo del cliente: {texto}\n\nJSON:"
                    ),
                },
            ],
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        agente = str(data.get("agente", "")).strip().lower()
        if agente not in VALIDOS:
            # El modelo pudo responder con texto libre: buscamos el nombre dentro.
            agente = next((a for a in ("pedido", "soporte", "ventas") if a in agente), "ventas")
        permite = bool(data.get("amerita_humano") is True)
        motivo = str(data.get("motivo", ""))[:60]
        log.info("determinador", agente=agente, permite_escalar=permite, motivo=motivo)
        return Veredicto(agente=agente, permite_escalar=permite, motivo=motivo or "ia")
    except Exception as exc:  # noqa: BLE001
        log.warning("determinador_fallo", error=str(exc))
    # Default seguro: ventas y SIN permiso de escalar (el bot atiende).
    return Veredicto(agente="ventas", permite_escalar=False, motivo="fallback")
