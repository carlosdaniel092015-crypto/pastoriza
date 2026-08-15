"""Archivos que un TERCERO tiene que poder descargar sin credenciales.

WhatsApp (Meta/YCloud) va a buscar por HTTP las imágenes que le mandamos por link:
la foto que responde el supervisor desde el panel, y el comprobante que viaja en la
plantilla de aprobación. Las URLs de media de YCloud NO sirven para eso porque exigen
`X-API-Key` (ver `media.descargar`), así que el archivo se vuelve a publicar acá, en
nuestro dominio, sin token.

Es un cache EN MEMORIA y a propósito: el archivo sólo tiene que vivir los segundos
que WhatsApp tarda en buscarlo. Corre en 1 worker (ver Dockerfile), así que un dict
por proceso alcanza; se acota para no crecer sin fin.
"""
from __future__ import annotations

import secrets

MAX = 80
_CACHE: dict[str, tuple[str, bytes]] = {}  # token -> (content_type, bytes)


def guardar(data: bytes, content_type: str) -> str:
    token = secrets.token_urlsafe(16)
    _CACHE[token] = (content_type, data)
    while len(_CACHE) > MAX:
        _CACHE.pop(next(iter(_CACHE)))
    return token


def obtener(token: str) -> tuple[str, bytes] | None:
    return _CACHE.get(str(token or "").split(".")[0])


def extension(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "png" in ct:
        return "png"
    if "ogg" in ct or "opus" in ct:
        return "ogg"
    if "mp" in ct:
        return "mp3"
    return "jpg"


__all__ = ["MAX", "extension", "guardar", "obtener"]
