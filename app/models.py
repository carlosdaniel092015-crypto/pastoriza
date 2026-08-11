"""Parseo del webhook de YCloud -> objeto interno `InboundMessage`.

Reemplaza el nodo `Set Fields2`. Diferencia clave con n8n: acá el parseo es
tolerante y testeable, y el referral del anuncio se busca sin asumir el nombre
exacto del campo (todavía no está confirmado en la doc de YCloud).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

RE_TELEFONO = re.compile(r"^\+?[0-9]{8,15}$")

# Nombres posibles del objeto referral. Ampliar cuando confirmes el payload real.
CLAVES_REFERRAL = {"referral", "referrals", "adReferral", "ad_referral"}

# Alias camelCase (YCloud) <-> snake_case (Meta) para los campos del referral.
ALIAS_REFERRAL = {
    "source_id": ("source_id", "sourceId", "adId", "ad_id"),
    "source_url": ("source_url", "sourceUrl"),
    "source_type": ("source_type", "sourceType"),
    "headline": ("headline",),
    "body": ("body",),
    "media_type": ("media_type", "mediaType"),
    "image_url": ("image_url", "imageUrl"),
    "video_url": ("video_url", "videoUrl"),
    "ctwa_clid": ("ctwa_clid", "ctwaClid", "ctwaClId"),
}


def buscar_referral(payload: Any) -> dict | None:
    """Busca el objeto referral sin asumir nombre exacto ni nivel de anidamiento."""
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                if k in CLAVES_REFERRAL and isinstance(v, dict) and v:
                    return v
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)
    return None


def normalizar_referral(raw: dict | None) -> dict:
    """Devuelve el referral con claves snake_case estables, sea cual sea el origen."""
    if not raw:
        return {}
    out: dict[str, Any] = {}
    for canonico, alias in ALIAS_REFERRAL.items():
        for a in alias:
            if raw.get(a) not in (None, ""):
                out[canonico] = raw[a]
                break
    # Guardamos el crudo para poder auditar qué manda YCloud de verdad.
    out["_raw"] = raw
    return out


@dataclass
class InboundMessage:
    """Un mensaje entrante ya normalizado."""

    message_id: str = ""
    chat_id: str = ""  # identificador de conversación (teléfono o fromUserId)
    telefono: str | None = None  # solo si es un número real
    content_type: str = "text"  # text | image | audio | location | otro
    content: str = ""
    media_id: str = ""
    media_url: str = ""
    timestamp: str = ""
    user_name: str = ""
    location_lat: str = ""
    location_lng: str = ""
    location_name: str = ""
    location_address: str = ""
    referral: dict = field(default_factory=dict)
    instance_from: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def es_ubicacion(self) -> bool:
        return self.content_type == "location" or bool(
            self.location_lat and self.location_lng
        )

    def destino_ycloud(self) -> dict:
        """YCloud acepta `to` para números reales y `recipient` para IDs opacos."""
        if self.telefono:
            return {"to": self.telefono}
        return {"recipient": self.chat_id}


def _g(d: dict | None, *path: str, default: Any = "") -> Any:
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur not in (None, "") else default


def parse_inbound(body: dict) -> InboundMessage | None:
    """Convierte el body del webhook de YCloud en InboundMessage.

    Devuelve None si el evento NO es un mensaje entrante de cliente
    (equivale al nodo `Verifica quien Escribe2`).
    """
    tipo_evento = body.get("type", "")
    inbound = body.get("whatsappInboundMessage") or {}
    outbound = body.get("whatsappMessage") or {}

    if tipo_evento != "whatsapp.inbound_message.received" or not inbound:
        return None

    chat_id = (
        _g(inbound, "from")
        or _g(inbound, "fromUserId")
        or _g(outbound, "to")
        or ""
    )
    telefono = chat_id if RE_TELEFONO.match(str(chat_id)) else None

    tipo_raw = _g(inbound, "type", default="text")
    content_type = "text" if tipo_raw in {"button", "interactive", "reply"} else tipo_raw

    content = (
        _g(inbound, "text", "body")
        or _g(inbound, "button", "text")
        or _g(inbound, "interactive", "button_reply", "title")
        or _g(inbound, "interactive", "list_reply", "title")
        or _g(inbound, "image", "caption")
        or ""
    )

    return InboundMessage(
        message_id=_g(inbound, "id"),
        chat_id=str(chat_id),
        telefono=telefono,
        content_type=str(content_type or "text"),
        content=str(content),
        media_id=_g(inbound, "image", "id") or _g(inbound, "audio", "id"),
        media_url=_g(inbound, "audio", "link") or _g(inbound, "image", "link"),
        timestamp=str(_g(inbound, "sendTime")),
        user_name=_g(inbound, "customerProfile", "name"),
        location_lat=str(_g(inbound, "location", "latitude")),
        location_lng=str(_g(inbound, "location", "longitude")),
        location_name=_g(inbound, "location", "name"),
        location_address=_g(inbound, "location", "address"),
        referral=normalizar_referral(buscar_referral(body)),
        instance_from=_g(inbound, "to") or _g(outbound, "from"),
        raw=body,
    )


def parse_outbound_command(body: dict) -> tuple[str, str] | None:
    """Detecta los comandos `.on` / `.off` que manda el encargado desde el número.

    Devuelve (comando, chat_id) o None. Equivale a `Verifica Palabra Clave2`.
    """
    outbound = body.get("whatsappMessage") or {}
    if not outbound.get("id"):
        return None
    texto = str(_g(outbound, "text", "body")).strip().lower()
    if texto not in {".on", ".off"}:
        return None
    destino = str(outbound.get("to") or "")
    return (texto, destino)


def parse_message_updated(body: dict) -> dict | None:
    """Evento `whatsapp.message.updated`: actualización de un mensaje SALIENTE.

    Se usa para detectar cuando el supervisor le escribe al cliente desde YCloud.
    Devuelve {id, to, from, status, texto} o None si no aplica.
    """
    if body.get("type") != "whatsapp.message.updated":
        return None
    m = body.get("whatsappMessage") or body.get("whatsappOutboundMessage") or {}
    mid = str(_g(m, "id") or _g(m, "wamid") or "")
    to = str(_g(m, "to") or "")
    if not mid or not to:
        return None
    return {
        "id": mid,
        "to": to,
        "from": str(_g(m, "from") or ""),
        "status": str(_g(m, "status") or ""),
        "texto": _g(m, "text", "body"),
    }


def ubicacion_a_texto(
    lat: str, lng: str, nombre: str = "", direccion: str = ""
) -> str:
    partes = [p for p in (nombre, direccion) if p]
    txt = ", ".join(partes)
    if lat and lng:
        link = f"https://maps.google.com/?q={lat},{lng}"
        coord = f"coordenadas {lat}, {lng} - {link}"
        txt = f"{txt} ({coord})" if txt else coord
    if not txt:
        return ""
    return f"[UBICACION_WHATSAPP] El cliente compartio su ubicacion por WhatsApp: {txt}"
