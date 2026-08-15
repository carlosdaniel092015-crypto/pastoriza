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
    # Payload del BOTÓN de una plantilla (lo que definimos al enviarla), no su texto.
    # Es lo que identifica qué aprueba el supervisor: el título del botón dice
    # "Aprobar pago" y nada más; el payload trae la acción, el chat y el pedido.
    boton_payload: str = ""
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

    # El payload viaja con distinto nombre según de qué botón se trate (quick_reply de
    # una plantilla vs. botón interactivo) y YCloud no normaliza: se prueban todos.
    boton = (
        _g(inbound, "button", "payload")
        or _g(inbound, "button", "text")
        or _g(inbound, "interactive", "button_reply", "id")
        or _g(inbound, "interactive", "list_reply", "id")
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
        boton_payload=str(boton),
        referral=normalizar_referral(buscar_referral(body)),
        instance_from=_g(inbound, "to") or _g(outbound, "from"),
        raw=body,
    )


# Nombres posibles del bloque de un mensaje SALIENTE. YCloud usa `whatsappMessage`,
# pero con un número COEXISTENTE (app de WhatsApp Business + API) el mensaje que el
# encargado manda desde el celular puede llegar con otro nombre o con otro `type` de
# evento. Si no lo reconocemos, el bot no se enteraría de que un humano contestó y
# seguiría respondiéndole encima al cliente.
CLAVES_SALIENTE = (
    "whatsappMessage",
    "whatsappOutboundMessage",
    "whatsappOutbound",
    "whatsappSentMessage",
)


def bloque_saliente(body: dict) -> dict:
    """Devuelve el bloque del mensaje saliente, sea cual sea el nombre del campo."""
    for clave in CLAVES_SALIENTE:
        v = body.get(clave)
        if isinstance(v, dict) and v:
            return v
    return {}


def es_evento_entrante(body: dict) -> bool:
    """True si el evento trae un mensaje ENTRANTE del cliente."""
    return bool(body.get("whatsappInboundMessage")) or (
        body.get("type") == "whatsapp.inbound_message.received"
    )


def parse_outbound_command(body: dict) -> tuple[str, str] | None:
    """Detecta los comandos `.on` / `.off` que manda el encargado desde el número.

    Devuelve (comando, chat_id) o None. Equivale a `Verifica Palabra Clave2`.
    Funciona con cualquier forma del bloque saliente (ver CLAVES_SALIENTE), así el
    comando también sirve escribiéndolo desde el celular del número coexistente.
    """
    outbound = bloque_saliente(body)
    if not outbound.get("id"):
        return None
    texto = str(_g(outbound, "text", "body")).strip().lower()
    if texto not in {".on", ".off"}:
        return None
    destino = str(outbound.get("to") or "")
    return (texto, destino)


def parse_message_updated(body: dict) -> dict | None:
    """Datos de un mensaje SALIENTE (lo que salió hacia el cliente).

    Se usa para detectar cuándo un humano le escribió al cliente: desde YCloud o —en
    un número COEXISTENTE— desde la app de WhatsApp en el celular. Ya NO se exige que
    el evento sea exactamente `whatsapp.message.updated`: alcanza con que traiga un
    mensaje saliente con id y destino, porque el nombre del evento cambia según el
    caso. Que sea del bot o de un humano lo decide `es_msg_bot` (el bot registra todo
    lo que envía), así que relajar esto no genera falsas tomas de control.

    Devuelve {id, to, from, status, texto} o None si no aplica.
    """
    if es_evento_entrante(body):
        return None
    m = bloque_saliente(body)
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
