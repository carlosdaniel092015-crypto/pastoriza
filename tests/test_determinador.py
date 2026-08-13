"""Tests del DETERMINADOR: qué amerita una persona y qué resuelve el bot.

Nace de un caso real: el cliente saludó ("Hola" + "Buenas tardes cómo estás") y el
bot llamó a `escalar_a_humano` y le dijo que ya había avisado al supervisor. La
escalada ya no la decide el modelo: necesita el visto bueno del determinador.
"""
from __future__ import annotations

import asyncio

from app.agents.enrutador import Veredicto, analizar_contexto, senales_humano
from app.router import es_solo_saludo, normalizar


class _Ctx:
    """Contexto mínimo para el determinador (sin Redis ni OpenAI)."""

    es_comprobante = False
    imagen_url = ""


def _veredicto(texto: str) -> Veredicto:
    return asyncio.run(analizar_contexto(texto, _Ctx(), None))


class TestSaludos:
    def test_saludo_simple(self):
        assert es_solo_saludo(normalizar("Hola")) is True

    def test_saludo_compuesto_de_rafaga(self):
        # El caso real: dos mensajes que se combinan en un solo texto.
        assert es_solo_saludo(normalizar("Hola\nBuenas tardes cómo estás")) is True

    def test_cortesias_dominicanas(self):
        for t in ("klk", "buenas", "buenas tardes", "saludos, como esta usted",
                  "hola buenos dias", "que lo que"):
            assert es_solo_saludo(normalizar(t)) is True, t

    def test_saludo_con_intencion_NO_es_solo_saludo(self):
        # Si además pide algo, no es un saludo: lo atiende el agente.
        for t in ("hola quiero botellas", "buenas tardes precio de 8 oz",
                  "hola, donde estan ubicados"):
            assert es_solo_saludo(normalizar(t)) is False, t


class TestSenalesHumano:
    def test_pide_persona(self):
        for t in ("quiero hablar con una persona", "pasame con un asesor",
                  "necesito hablar con alguien", "una persona real por favor"):
            assert senales_humano(t) is True, t

    def test_reclamo_real(self):
        for t in ("quiero cancelar mi pedido", "esto es una estafa",
                  "el envase llego roto", "no me llego el pedido",
                  "quiero un reembolso"):
            assert senales_humano(t) is True, t

    def test_conversacion_normal_NO_amerita_humano(self):
        for t in ("hola buenas tardes como estas", "cuanto cuesta el envio",
                  "precio de la botella de 12 oz", "donde estan ubicados",
                  "ok", "gracias", "quiero 200 botellas"):
            assert senales_humano(t) is False, t


class TestVeredicto:
    def test_saludo_no_habilita_escalada(self):
        v = _veredicto("hola buenas tardes como estas")
        assert v.permite_escalar is False

    def test_pedir_persona_habilita_escalada(self):
        v = _veredicto("quiero hablar con una persona")
        assert v.permite_escalar is True
        assert v.agente == "soporte"

    def test_cancelar_habilita_escalada_y_va_a_soporte(self):
        v = _veredicto("quiero cancelar mi pedido")
        assert v.permite_escalar is True
        assert v.agente == "soporte"

    def test_pregunta_de_precio_no_habilita_escalada(self):
        v = _veredicto("precio de la botella de 8 oz")
        assert v.permite_escalar is False
        assert v.agente == "ventas"
