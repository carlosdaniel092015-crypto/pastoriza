"""Copia de las fotos y notas de voz que manda el cliente, para verlas en el panel.

POR QUÉ EXISTE: en el hilo del panel, una foto se veía sólo como el texto que el bot
le pasó al modelo ("## ANALISIS VISUAL: TIPO_ENVASE: Botella / ...") y una nota de voz
sólo como su transcripción. Quien opera necesita ver la foto y escuchar el audio para
juzgar si el bot entendió bien.

A DISCO, NO A REDIS, a propósito: en Redis vive la config del negocio con
`--maxmemory-policy noeviction`, así que llenarlo con fotos de clientes haría FALLAR
escrituras reales (precios, pedidos). Un archivo de 2 MB en disco es gratis; en esa
Redis es un riesgo para la venta.

El índice (qué archivo va con qué conversación) sí va a Redis, porque son unos pocos
bytes por entrada y ahí ya vive el resto del CRM.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

from app.logging_conf import get_logger
from app.redis_client import run_write, with_reconnect
from app.settings import settings

log = get_logger(__name__)

# Mismo horizonte que el historial de conversación (SESSION_TTL_SECONDS): si el bot ya
# no recuerda la charla, la foto tampoco tiene a qué acompañar.
TTL_SEGUNDOS = 7 * 86_400
# Tope por archivo. Una foto de WhatsApp ronda 100-800 KB; 8 MB es el techo de Meta.
MAX_BYTES = 8 * 1024 * 1024
# Cuántos archivos se recuerdan por conversación (los más nuevos).
MAX_POR_CHAT = 40

DIRECTORIO = Path(os.getenv("MEDIA_DIR", "/srv/media"))


def _key(chat_id: str) -> str:
    return settings.key("panel", f"media:{chat_id}")


def _ruta(token: str) -> Path:
    # `token` viene de la URL: sin esto, "../../etc/passwd" leería fuera del directorio.
    # El punto se conserva (hace falta para la extensión) pero ".." se rechaza.
    limpio = "".join(c for c in str(token or "") if c.isalnum() or c in "-_.")
    if not limpio or ".." in limpio or limpio.startswith("."):
        raise ValueError(f"token inválido: {token!r}")
    return DIRECTORIO / limpio


def extension(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "png" in ct:
        return "png"
    if "webp" in ct:
        return "webp"
    if "ogg" in ct or "opus" in ct:
        return "ogg"
    if "mpeg" in ct or "mp3" in ct:
        return "mp3"
    if "mp4" in ct or "m4a" in ct:
        return "m4a"
    return "jpg"


async def guardar(
    chat_id: str, data: bytes, content_type: str, tipo: str, texto: str = ""
) -> str:
    """Guarda el archivo y lo anota en el índice de la conversación. Devuelve el token
    (o "" si no se pudo): esto NUNCA debe romper el turno del cliente."""
    if not data or len(data) > MAX_BYTES:
        if data:
            log.info("media_chat_muy_grande", bytes=len(data), chat_id=chat_id)
        return ""
    token = f"{secrets.token_urlsafe(16)}.{extension(content_type)}"
    try:
        DIRECTORIO.mkdir(parents=True, exist_ok=True)
        _ruta(token).write_bytes(data)
    except OSError as exc:
        log.warning("media_chat_no_guardado", chat_id=chat_id, error=str(exc))
        return ""

    entrada = json.dumps(
        {
            "token": token,
            "tipo": tipo,  # "imagen" | "audio"
            "content_type": content_type,
            "bytes": len(data),
            "texto": (texto or "")[:400],  # transcripción o análisis, para el pie
            "ts": int(time.time()),
        },
        ensure_ascii=False,
    )

    async def _op(r):
        pipe = r.pipeline()
        pipe.rpush(_key(chat_id), entrada)
        pipe.ltrim(_key(chat_id), -MAX_POR_CHAT, -1)
        pipe.expire(_key(chat_id), TTL_SEGUNDOS)
        return await pipe.execute()

    try:
        await run_write(_op)
    except Exception as exc:  # noqa: BLE001
        log.warning("media_chat_indice_fallo", chat_id=chat_id, error=str(exc))
    return token


async def listar(chat_id: str) -> list[dict]:
    """Los archivos de esa conversación, del más viejo al más nuevo. Se saltan los que
    ya no están en disco (el archivo se pudo borrar antes que su entrada)."""
    try:
        crudo = await with_reconnect(lambda r: r.lrange(_key(chat_id), 0, -1))
    except Exception as exc:  # noqa: BLE001
        log.warning("media_chat_listar_fallo", chat_id=chat_id, error=str(exc))
        return []
    out: list[dict] = []
    for item in crudo or []:
        try:
            entrada = json.loads(item)
        except (json.JSONDecodeError, TypeError):
            continue
        try:
            if not _ruta(entrada.get("token", "")).exists():
                continue
        except ValueError:
            continue
        out.append(entrada)
    return out


def leer(token: str) -> tuple[str, bytes] | None:
    """Devuelve (content_type, bytes) del archivo, o None si no está."""
    try:
        ruta = _ruta(token)
    except ValueError:
        return None
    if not ruta.exists():
        return None
    ext = ruta.suffix.lstrip(".").lower()
    tipos = {
        "png": "image/png", "webp": "image/webp", "jpg": "image/jpeg",
        "ogg": "audio/ogg", "mp3": "audio/mpeg", "m4a": "audio/mp4",
    }
    try:
        return (tipos.get(ext, "application/octet-stream"), ruta.read_bytes())
    except OSError:
        return None


def limpiar_viejos() -> int:
    """Borra del disco lo que pasó el TTL. Se llama al arrancar: sin esto el volumen
    crece para siempre, porque el TTL de Redis vence la ENTRADA pero no el ARCHIVO."""
    if not DIRECTORIO.exists():
        return 0
    corte = time.time() - TTL_SEGUNDOS
    borrados = 0
    for f in DIRECTORIO.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < corte:
                f.unlink()
                borrados += 1
        except OSError:
            continue
    if borrados:
        log.info("media_chat_limpieza", borrados=borrados)
    return borrados
