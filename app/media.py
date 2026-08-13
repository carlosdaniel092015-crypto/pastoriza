"""Media entrante: descarga, transcripción de audio y análisis de imagen.

Reemplaza los nodos `Obtener media audio/imagen`, `Transcribir audio1`
y `Describir imagen1`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from io import BytesIO

import httpx
from openai import AsyncOpenAI
from PIL import Image

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
Si no ves esos datos, NO escribas la palabra COMPROBANTE_PAGO en NINGUN caso: ni
siquiera para decir que no hay, ni como "COMPROBANTE_PAGO: [no hay datos]". Omite
el bloque por completo y pasa al que corresponda.

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


def _a_jpg(data: bytes) -> bytes:
    """Convierte cualquier imagen (webp/png/…) a JPG con fondo blanco. Síncrono."""
    img = Image.open(BytesIO(data))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        fondo = Image.new("RGB", img.size, (255, 255, 255))
        fondo.paste(img, mask=img.split()[-1])
        img = fondo
    else:
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


async def convertir_a_jpg(data: bytes) -> bytes:
    """Convierte a JPG en un hilo (Pillow es bloqueante)."""
    return await asyncio.to_thread(_a_jpg, data)


def _a_ogg(data: bytes) -> bytes:
    """Convierte audio (ej. webm/opus del navegador) a ogg/opus con ffmpeg.

    WhatsApp/YCloud NO acepta audio/webm; sí ogg/opus. Síncrono (bloqueante).
    """
    import os
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in")
        dst = os.path.join(d, "out.ogg")
        with open(src, "wb") as f:
            f.write(data)
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", src, "-c:a", "libopus", "-b:a", "32k", dst],
            capture_output=True,
        )
        if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
            raise RuntimeError("ffmpeg: " + r.stderr.decode("utf-8", "ignore")[:300])
        with open(dst, "rb") as f:
            return f.read()


async def convertir_audio_ogg(data: bytes) -> bytes:
    """Convierte audio a ogg/opus (formato de nota de voz de WhatsApp) en un hilo."""
    return await asyncio.to_thread(_a_ogg, data)


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


# El modelo de visión a veces ESCRIBE la etiqueta para decir que NO hay comprobante
# ("1) COMPROBANTE_PAGO: [no hay datos]"). Buscar el literal daba un falso positivo:
# tomaba la foto de unos envases como comprobante, avisaba al admin de un "pago sin
# pedido" y mandaba el turno al agente de PEDIDO en vez de al de ventas.
_RE_COMPROBANTE = re.compile(r"COMPROBANTE_PAGO\s*:?\s*(.*)", re.IGNORECASE)
_SIN_COMPROBANTE = (
    "no hay", "no aplica", "ninguno", "sin datos", "no se ve", "no corresponde",
    "no es", "no visible", "n/a", "none", "null", "no data", "vacio", "no detect",
)


def es_comprobante_de(texto: str) -> bool:
    """True SÓLO si el análisis trae datos REALES de un pago. Pura y testeable."""
    m = _RE_COMPROBANTE.search(texto or "")
    if not m:
        return False
    # Nos quedamos con esa línea/bloque (hasta el siguiente punto numerado).
    detalle = re.split(r"\n\s*\d\s*\)", m.group(1))[0]
    detalle = detalle.strip().strip("[]()").strip(" .:-").lower()
    if not detalle:
        return False
    if any(neg in detalle for neg in _SIN_COMPROBANTE):
        return False
    # Un comprobante real trae monto/referencia/fecha: sin ningún número, no lo es.
    return bool(re.search(r"\d", detalle))


async def analizar_imagen(url: str) -> tuple[str, bool]:
    """Devuelve (descripción, es_comprobante)."""
    try:
        data = await descargar(url)
        texto = await _vision(PROMPT_IMAGEN, data, mime_de_url(url))
        return texto, es_comprobante_de(texto)
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
