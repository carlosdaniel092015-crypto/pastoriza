"""Tests de las reglas duras de negocio de las tools de pedido (ADR-006).

El blindaje de precio es una garantía de SEGURIDAD (el modelo o un cliente no
pueden meter un precio distinto al del catálogo), no cosmética. Estaba sin test:
un refactor podía reabrir el agujero sin que nadie se enterara.
"""
from __future__ import annotations

import pytest

from app.tools.odoo_tools import (
    datos_envio_faltantes,
    nombre_no_valido,
    nota_entrega,
    precio_blindado,
)


class TestPrecioBlindado:
    def test_corrige_precio_manipulado_al_de_catalogo(self):
        precio, corregido = precio_blindado(1.00, 5.87)
        assert precio == 5.87
        assert corregido is True

    def test_precio_correcto_no_se_toca(self):
        precio, corregido = precio_blindado(5.87, 5.87)
        assert precio == 5.87
        assert corregido is False

    def test_tolerancia_de_un_centavo(self):
        # Diferencia por redondeo (<= 1 centavo): se acepta tal cual.
        precio, corregido = precio_blindado(5.87, 5.88)
        assert corregido is False
        assert precio == 5.87

    def test_precio_inflado_tambien_se_corrige(self):
        precio, corregido = precio_blindado(999.99, 5.87)
        assert precio == 5.87
        assert corregido is True


class TestDatosEnvioFaltantes:
    def test_todos_presentes_no_falta_nada(self):
        assert datos_envio_faltantes("SD", "DN", "Piantini", "Av. Lincoln") == []

    def test_reporta_los_vacios(self):
        faltan = datos_envio_faltantes("SD", "", "  ", "Av. Lincoln")
        assert "municipio/pueblo" in faltan
        assert "sector" in faltan
        assert "provincia" not in faltan


class TestNotaEntrega:
    def test_retiro_es_escueto(self):
        assert nota_entrega("retiro") == "ENTREGA: Retiro en tienda"

    def test_envio_incluye_direccion_detallada(self):
        nota = nota_entrega(
            "envio", "SD", "DN", "Piantini", "Av. Lincoln",
            numero_casa="12", referencia="frente al colmado",
        )
        assert nota.startswith("ENTREGA: Envío a domicilio")
        assert "Provincia: SD" in nota
        assert "No. casa/edificio: 12" in nota
        assert "Referencia: frente al colmado" in nota


class TestNombreDelCliente:
    """El contacto queda en Odoo para siempre: no se crea con el alias de WhatsApp.

    Caso real: el bot registró el pedido 160 a nombre de "la patrona RD Hija Rey 🎉"
    sin preguntarle el nombre al cliente.
    """

    def test_alias_de_whatsapp_se_rechaza(self):
        assert nombre_no_valido("la patrona RD Hija Rey", "la patrona RD Hija Rey")

    def test_emojis_se_rechazan(self):
        assert nombre_no_valido("Yoma 🎉🎉", "otro")

    def test_vacio_se_rechaza(self):
        assert nombre_no_valido("", "otro")

    def test_nombre_real_se_acepta(self):
        assert nombre_no_valido("Juan Pérez", "la patrona RD") == ""
        assert nombre_no_valido("Yoselin", "la patrona RD") == ""
        assert nombre_no_valido("María de los Ángeles", "x") == ""
