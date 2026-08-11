"""Lógica de detección de mensajes repetidos (parte pura, sin Redis)."""
from __future__ import annotations

from app.repeticion import normalizar, son_lo_mismo


class TestNormalizar:
    def test_tildes_y_mayusculas(self):
        assert normalizar("  ¿CUÁNTO   es EL Envío? ") == "¿cuanto es el envio?"

    def test_vacio(self):
        assert normalizar("") == ""


class TestSonLoMismo:
    def test_identico(self):
        assert son_lo_mismo("cuanto es el envio", "cuanto es el envio")

    def test_muy_parecido(self):
        assert son_lo_mismo("cuanto cuesta el envio", "cuanto es el envio")

    def test_subcadena(self):
        assert son_lo_mismo("envio", "cuanto es el envio")

    def test_distinto(self):
        assert not son_lo_mismo("cuanto es el envio", "quiero botellas de 8 oz")

    def test_vacios(self):
        assert not son_lo_mismo("", "algo")
