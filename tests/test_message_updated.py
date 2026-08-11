"""Tests de parse_message_updated: base de la toma de control del supervisor
(ADR-009). `manejar_saliente` depende 100% de esto y no tenía ni un test.
El payload real de YCloud aún no está confirmado; estas variantes son la red.
"""
from __future__ import annotations

from app.models import parse_message_updated


def test_evento_de_otro_tipo_es_none():
    assert parse_message_updated({"type": "whatsapp.inbound_message.received"}) is None


def test_message_updated_basico():
    info = parse_message_updated(
        {
            "type": "whatsapp.message.updated",
            "whatsappMessage": {
                "id": "wamid.ABC",
                "to": "18091112222",
                "from": "18093334444",
                "status": "delivered",
                "text": {"body": "hola"},
            },
        }
    )
    assert info == {
        "id": "wamid.ABC",
        "to": "18091112222",
        "from": "18093334444",
        "status": "delivered",
        "texto": "hola",
    }


def test_message_updated_via_wamid_y_outbound_message():
    info = parse_message_updated(
        {
            "type": "whatsapp.message.updated",
            "whatsappOutboundMessage": {
                "wamid": "wamid.XYZ",
                "to": "18091112222",
                "status": "sent",
            },
        }
    )
    assert info is not None
    assert info["id"] == "wamid.XYZ"
    assert info["to"] == "18091112222"


def test_sin_id_o_sin_to_es_none():
    assert parse_message_updated(
        {"type": "whatsapp.message.updated", "whatsappMessage": {"to": "1809"}}
    ) is None
    assert parse_message_updated(
        {"type": "whatsapp.message.updated", "whatsappMessage": {"id": "x"}}
    ) is None
