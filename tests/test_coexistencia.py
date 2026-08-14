"""Número COEXISTENTE (app de WhatsApp Business + API por YCloud).

Requisito de operación: el número coexistente debe comportarse igual que el otro.
Si el encargado le responde a un cliente DESDE EL CELULAR, el bot tiene que pausarse
30 min para ese cliente; y `.on` / `.off` deben funcionar escribiéndolos desde el
número.

El riesgo: la detección exigía que el evento fuera exactamente
`whatsapp.message.updated`. Si YCloud entrega el mensaje del celular con otro `type`
o con otro nombre de campo, el bot no se enteraría y seguiría respondiendo encima de
la persona. Estos tests cubren las variantes.
"""
from __future__ import annotations

from app.models import (
    bloque_saliente,
    es_evento_entrante,
    parse_inbound,
    parse_message_updated,
    parse_outbound_command,
)

CLIENTE = "18091112222"
NUESTRO = "18093334444"


def _saliente(clave: str, tipo: str, texto: str = "Ya te lo tengo listo") -> dict:
    return {
        "type": tipo,
        clave: {
            "id": "wamid.ABC123",
            "from": NUESTRO,
            "to": CLIENTE,
            "status": "sent",
            "text": {"body": texto},
        },
    }


ENTRANTE = {
    "type": "whatsapp.inbound_message.received",
    "whatsappInboundMessage": {
        "id": "wamid.IN1", "from": CLIENTE, "to": NUESTRO,
        "type": "text", "text": {"body": "hola"},
    },
}


class TestDeteccionDeSaliente:
    def test_forma_clasica_de_ycloud(self):
        info = parse_message_updated(_saliente("whatsappMessage", "whatsapp.message.updated"))
        assert info and info["to"] == CLIENTE and info["id"] == "wamid.ABC123"

    def test_otros_tipos_de_evento_tambien_cuentan(self):
        """Lo que puede pasar en coexistencia: el evento se llama distinto."""
        for tipo in ("whatsapp.message.sent", "whatsapp.outbound_message.sent",
                     "whatsapp.message.delivered", "algo.desconocido"):
            info = parse_message_updated(_saliente("whatsappMessage", tipo))
            assert info, f"no detectó el saliente con type={tipo}"
            assert info["to"] == CLIENTE

    def test_otros_nombres_de_campo_tambien_cuentan(self):
        for clave in ("whatsappMessage", "whatsappOutboundMessage",
                      "whatsappOutbound", "whatsappSentMessage"):
            info = parse_message_updated(_saliente(clave, "whatsapp.message.updated"))
            assert info, f"no detectó el saliente en el campo {clave}"
            assert info["texto"] == "Ya te lo tengo listo"

    def test_un_entrante_NO_es_saliente(self):
        """Clave: no confundir el mensaje del cliente con una toma de control."""
        assert parse_message_updated(ENTRANTE) is None
        assert es_evento_entrante(ENTRANTE) is True
        assert bloque_saliente(ENTRANTE) == {}

    def test_evento_sin_mensaje_no_dispara_nada(self):
        assert parse_message_updated({"type": "whatsapp.template.approved"}) is None
        assert parse_message_updated({}) is None

    def test_saliente_sin_destino_se_ignora(self):
        body = _saliente("whatsappMessage", "whatsapp.message.updated")
        body["whatsappMessage"]["to"] = ""
        assert parse_message_updated(body) is None


class TestComandosOnOff:
    def test_off_desde_el_numero(self):
        body = _saliente("whatsappMessage", "whatsapp.message.updated", ".off")
        assert parse_outbound_command(body) == (".off", CLIENTE)

    def test_on_desde_el_numero(self):
        body = _saliente("whatsappMessage", "whatsapp.message.updated", ".on")
        assert parse_outbound_command(body) == (".on", CLIENTE)

    def test_funcionan_con_cualquier_forma_del_evento(self):
        """También escribiéndolos desde el celular del número coexistente."""
        for clave in ("whatsappOutboundMessage", "whatsappOutbound", "whatsappSentMessage"):
            body = _saliente(clave, "whatsapp.outbound_message.sent", ".off")
            assert parse_outbound_command(body) == (".off", CLIENTE), clave

    def test_mayusculas_y_espacios(self):
        body = _saliente("whatsappMessage", "whatsapp.message.updated", "  .OFF  ")
        assert parse_outbound_command(body) == (".off", CLIENTE)

    def test_un_mensaje_normal_no_es_comando(self):
        body = _saliente("whatsappMessage", "whatsapp.message.updated", "off vamos")
        assert parse_outbound_command(body) is None


class TestEntranteSigueFuncionando:
    def test_el_entrante_se_parsea_igual(self):
        msg = parse_inbound(ENTRANTE)
        assert msg is not None
        assert msg.chat_id == CLIENTE
        # `instance_from` es NUESTRO número: por eso el bot responde por el mismo
        # canal por el que le escribieron (con YCLOUD_FROM vacío).
        assert msg.instance_from == NUESTRO
