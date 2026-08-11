"""Regla dura de dirección de envío: se exige ubicación detallada en código."""
from __future__ import annotations

from app.tools.odoo_tools import datos_envio_faltantes, nota_entrega


class TestDatosEnvioFaltantes:
    def test_todo_vacio_faltan_los_cuatro(self):
        assert datos_envio_faltantes("", "", "", "") == [
            "provincia",
            "municipio/pueblo",
            "sector",
            "calle",
        ]

    def test_completo_no_falta_nada(self):
        assert datos_envio_faltantes("Santo Domingo", "Este", "Ozama", "C/ 5") == []

    def test_falta_solo_sector(self):
        assert datos_envio_faltantes("Santiago", "Santiago", "", "C/ Duarte") == [
            "sector"
        ]

    def test_espacios_no_cuentan(self):
        assert "provincia" in datos_envio_faltantes("   ", "x", "y", "z")


class TestNotaEntrega:
    def test_retiro(self):
        assert nota_entrega("retiro") == "ENTREGA: Retiro en tienda"

    def test_envio_incluye_obligatorios(self):
        nota = nota_entrega(
            "envio", "Santo Domingo", "Este", "Ozama", "Calle 5"
        )
        assert "Envío a domicilio" in nota
        for token in ("Provincia: Santo Domingo", "Municipio/Pueblo: Este",
                      "Sector: Ozama", "Calle: Calle 5"):
            assert token in nota

    def test_envio_incluye_opcionales_y_mapa(self):
        nota = nota_entrega(
            "envio", "SD", "DN", "Piantini", "Av. Lincoln",
            numero_casa="12", tipo_lugar="negocio",
            referencia="frente al banco", ubicacion_mapa="https://maps.google.com/?q=1,2",
        )
        assert "No. casa/edificio: 12" in nota
        assert "Tipo: negocio" in nota
        assert "Referencia: frente al banco" in nota
        assert "Ubicación (mapa): https://maps.google.com/?q=1,2" in nota

    def test_envio_omite_opcionales_vacios(self):
        nota = nota_entrega("envio", "SD", "DN", "Piantini", "Av. Lincoln")
        assert "Referencia:" not in nota
        assert "Ubicación (mapa):" not in nota
