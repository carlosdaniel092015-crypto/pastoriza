"""Tests del parseo del webhook.

La extracción del referral es tolerante A PROPÓSITO: todavía no está confirmado
el nombre exacto del campo en YCloud. Estos tests cubren las tres variantes más
probables. Cuando confirmes el payload real con /webhook/debug, agregá el caso
real acá y dejá los demás como red.
"""
from __future__ import annotations

from app.models import (
    buscar_referral,
    normalizar_referral,
    parse_inbound,
    parse_outbound_command,
    ubicacion_a_texto,
)


def payload_texto(**extra) -> dict:
    inbound = {
        "id": "msg_123",
        "from": "18091234567",
        "to": "18299999999",
        "type": "text",
        "text": {"body": "hola quiero botellas"},
        "sendTime": "2026-08-09T14:45:00Z",
        "customerProfile": {"name": "Carolina Soto"},
    }
    inbound.update(extra)
    return {
        "type": "whatsapp.inbound_message.received",
        "whatsappInboundMessage": inbound,
    }


class TestParseInbound:
    def test_texto_basico(self):
        m = parse_inbound(payload_texto())
        assert m is not None
        assert m.chat_id == "18091234567"
        assert m.telefono == "18091234567"
        assert m.content == "hola quiero botellas"
        assert m.user_name == "Carolina Soto"

    def test_evento_que_no_es_entrante(self):
        assert parse_inbound({"type": "whatsapp.message.updated"}) is None

    def test_boton_se_trata_como_texto(self):
        p = payload_texto(
            type="interactive",
            interactive={"button_reply": {"title": "Envio o retiro"}},
            text=None,
        )
        m = parse_inbound(p)
        assert m and m.content_type == "text"
        assert m.content == "Envio o retiro"

    def test_id_opaco_de_anuncio_no_es_telefono(self):
        p = payload_texto(**{"from": None, "fromUserId": "DO.abc123xyz"})
        m = parse_inbound(p)
        assert m and m.telefono is None
        assert m.chat_id == "DO.abc123xyz"
        assert m.destino_ycloud() == {"recipient": "DO.abc123xyz"}

    def test_telefono_real_usa_to(self):
        m = parse_inbound(payload_texto())
        assert m and m.destino_ycloud() == {"to": "18091234567"}

    def test_imagen_con_caption(self):
        p = payload_texto(
            type="image",
            text=None,
            image={"id": "img1", "link": "https://x/y.jpg", "caption": "esta"},
        )
        m = parse_inbound(p)
        assert m and m.content_type == "image"
        assert m.media_url == "https://x/y.jpg"
        assert m.content == "esta"


class TestReferral:
    def test_snake_case_meta(self):
        p = payload_texto(
            referral={
                "source_id": "52579732276546",
                "source_url": "https://fb.me/abc",
                "headline": "Botellones para tu hogar",
                "body": "Envios a todo el pais",
                "ctwa_clid": "clid_xyz",
                "source_type": "ad",
            }
        )
        m = parse_inbound(p)
        assert m and m.referral["source_id"] == "52579732276546"
        assert m.referral["ctwa_clid"] == "clid_xyz"
        assert m.referral["headline"] == "Botellones para tu hogar"

    def test_camel_case_ycloud(self):
        p = payload_texto(
            referral={
                "sourceId": "52579732276546",
                "sourceUrl": "https://fb.me/abc",
                "ctwaClid": "clid_xyz",
                "mediaType": "image",
            }
        )
        m = parse_inbound(p)
        assert m and m.referral["source_id"] == "52579732276546"
        assert m.referral["ctwa_clid"] == "clid_xyz"
        assert m.referral["media_type"] == "image"

    def test_anidado_profundo(self):
        p = payload_texto()
        p["entry"] = [
            {"changes": [{"value": {"messages": [{"referral": {"source_id": "999"}}]}}]}
        ]
        assert buscar_referral(p) == {"source_id": "999"}

    def test_sin_referral(self):
        m = parse_inbound(payload_texto())
        assert m and m.referral == {}

    def test_guarda_el_crudo_para_auditar(self):
        raw = {"sourceId": "1", "campoDesconocido": "x"}
        out = normalizar_referral(raw)
        assert out["_raw"]["campoDesconocido"] == "x"


class TestComandos:
    def test_off(self):
        body = {
            "type": "whatsapp.message.updated",
            "whatsappMessage": {"id": "o1", "to": "18091234567",
                                "text": {"body": ".off"}},
        }
        assert parse_outbound_command(body) == (".off", "18091234567")

    def test_on_con_espacios(self):
        body = {
            "whatsappMessage": {"id": "o1", "to": "18091234567",
                                "text": {"body": "  .ON  "}},
        }
        assert parse_outbound_command(body) == (".on", "18091234567")

    def test_mensaje_normal_del_encargado_no_es_comando(self):
        body = {
            "whatsappMessage": {"id": "o1", "to": "18091234567",
                                "text": {"body": "buenas tardes"}},
        }
        assert parse_outbound_command(body) is None


class TestUbicacion:
    def test_con_nombre_y_coords(self):
        t = ubicacion_a_texto("18.4", "-69.9", "Casa", "Calle 1")
        assert "Casa, Calle 1" in t and "maps.google.com" in t

    def test_vacia(self):
        assert ubicacion_a_texto("", "", "", "") == ""
