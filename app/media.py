"""Media entrante: descarga, transcripción de audio y análisis de imagen.

Reemplaza los nodos `Obtener media audio/imagen`, `Transcribir audio1`
y `Describir imagen1`.
"""
from __future__ import annotations

import base64
import json
import re

import httpx
from openai import AsyncOpenAI

from app.logging_conf import get_logger
from app.settings import settings

log = get_logger(__name__)

_openai = AsyncOpenAI(api_key=settings.openai_api_key, timeout=settings.openai_timeout)

# Cliente HTTP reusado para descargas de media (keep-alive + pool). Antes se
# creaba uno nuevo por descarga, tirando el pool en cada llamada.
_http: httpx.AsyncClient | None = None


def _http_client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(follow_redirects=True)
    return _http


async def close_http() -> None:
    global _http
    if _http is not None:
        try:
            await _http.aclose()
        finally:
            _http = None

PROMPT_IMAGEN = """Eres el asistente de una empresa de envases plasticos. Analiza la imagen y responde SIEMPRE en espanol, solo con el bloque que corresponda.

1) COMPROBANTE BANCARIO REAL (SOLO si ves banco + monto RD$ + referencia + fecha):
COMPROBANTE_PAGO: [banco, monto y referencia]
Si no ves esos datos, NO uses COMPROBANTE_PAGO.

2) SELECCION_PRODUCTO: [numero] [nombre]

3) FOTO de envase:
TIPO_ENVASE / CAPACIDAD / COLOR / USO_PROBABLE / CARACTERISTICAS / BUSQUEDA (prioriza el tipo que el cliente escribio)."""

PROMPT_FICHA = (
    'Analiza SOLO el ENVASE (ignora el liquido/contenido, la marca y el fondo). '
    'Devuelve UNICAMENTE este JSON, sin texto extra: '
    '{"tipo":"botella|galon|botellon|tarro|frasco|pomo|tapa|atomizador|jarra|vaso|otro",'
    '"forma":"cilindrica|cuadrada|rectangular|redonda|conica|con_asa|otro",'
    '"proporcion":"alta|media|baja",'
    '"transparencia":"transparente|semi|opaco",'
    '"tapa":"rosca|presion|dosificador|spray|flip|sin_tapa|no_visible",'
    '"tapa_color":"blanco|negro|azul|rojo|verde|dorado|transparente|otro|no_visible",'
    '"capacidad":"","rasgos":""}. '
    'proporcion = relacion alto/ancho del envase. '
    'capacidad SOLO si hay etiqueta legible con oz o galon.'
)

MAX_BYTES = int(4.5 * 1024 * 1024)


async def descargar(url: str, timeout: float = 25.0) -> bytes:
    headers = {}
    if "ycloud" in url:
        headers["X-API-Key"] = settings.ycloud_api_key
    r = await _http_client().get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.content


def mime_de_url(url: str) -> str:
    u = (url or "").lower()
    if ".png" in u:
        return "image/png"
    if ".webp" in u:
        return "image/webp"
    return "image/jpeg"


def es_imagen_valida(buf: bytes) -> bool:
    if len(buf) < 100 or len(buf) > MAX_BYTES:
        return False
    jpg = buf[0:3] == b"\xff\xd8\xff"
    png = buf[0:4] == b"\x89PNG"
    webp = buf[0:4] == b"RIFF" and buf[8:12] == b"WEBP"
    return jpg or png or webp


async def transcribir_audio(url: str) -> str:
    data = await descargar(url)
    nombre = "audio.ogg"
    try:
        resp = await _openai.audio.transcriptions.create(
            model=settings.model_transcripcion,
            file=(nombre, data),
            language="es",
        )
        return (resp.text or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.error("transcripcion_fallo", error=str(exc))
        return ""


async def _vision(prompt: str, data: bytes, mime: str, max_tokens: int = 400) -> str:
    b64 = base64.b64encode(data).decode()
    resp = await _openai.chat.completions.create(
        model=settings.model_vision,
        max_tokens=max_tokens,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                ],
            }
        ],
    )
    return (resp.choices[0].message.content or "").strip()


async def analizar_imagen(url: str) -> tuple[str, bool]:
    """Devuelve (descripción, es_comprobante)."""
    try:
        data = await descargar(url)
        texto = await _vision(PROMPT_IMAGEN, data, mime_de_url(url))
        return texto, "COMPROBANTE_PAGO" in texto
    except Exception as exc:  # noqa: BLE001
        log.error("analisis_imagen_fallo", error=str(exc))
        return "", False


async def ficha_visual(data: bytes, mime: str = "image/jpeg") -> dict:
    """Extrae la ficha estructurada de un envase (para BuscarPorFoto/indexado)."""
    try:
        txt = await _vision(PROMPT_FICHA, data, mime, max_tokens=250)
        txt = txt.replace("```json", "").replace("```", "").strip()
        try:
            return json.loads(txt)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", txt)
            return json.loads(m.group(0)) if m else {}
    except Exception as exc:  # noqa: BLE001
        log.error("ficha_visual_fallo", error=str(exc))
        return {}


async def ficha_visual_de_url(url: str) -> dict:
    data = await descargar(url)
    return await ficha_visual(data, mime_de_url(url))
