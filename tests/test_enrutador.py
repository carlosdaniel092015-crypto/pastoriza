"""Enrutado determinista a especialista (sin LLM, 0 tokens)."""
from __future__ import annotations

from app.agents.enrutador import ruta_deterministica


class TestMediaEstado:
    def test_comprobante_va_a_pedido(self):
        assert ruta_deterministica("aquí está", es_comprobante=True) == "pedido"

    def test_foto_de_envase_va_a_ventas(self):
        assert ruta_deterministica("mira esta", tiene_imagen=True) == "ventas"

    def test_comprobante_gana_a_imagen(self):
        assert ruta_deterministica("", es_comprobante=True, tiene_imagen=True) == "pedido"


class TestSoporte:
    def test_cancelar(self):
        assert ruta_deterministica("quiero cancelar mi pedido") == "soporte"

    def test_quitar_producto(self):
        assert ruta_deterministica("quítame una de las botellas") == "soporte"

    def test_hablar_con_persona(self):
        assert ruta_deterministica("quiero hablar con una persona") == "soporte"

    def test_reclamo(self):
        assert ruta_deterministica("tengo una queja, llegó roto") == "soporte"


class TestCierrePedido:
    def test_da_nombre(self):
        assert ruta_deterministica("me llamo Juan Pérez") == "pedido"

    def test_elige_envio(self):
        assert ruta_deterministica("es para envío a domicilio") == "pedido"

    def test_confirma(self):
        assert ruta_deterministica("sí, lo confirmo") == "pedido"

    def test_comprobante_texto(self):
        assert ruta_deterministica("ya te transferí, aquí va") == "pedido"


class TestVentas:
    def test_pregunta_producto(self):
        assert ruta_deterministica("¿tienen botellas de 8 oz?") == "ventas"

    def test_precio(self):
        assert ruta_deterministica("cuánto cuesta el galón") == "ventas"

    def test_busco(self):
        assert ruta_deterministica("busco un frasco con tapa") == "ventas"


class TestAmbiguo:
    def test_saludo_suelto_es_ambiguo(self):
        assert ruta_deterministica("buenas, una pregunta") is None

    def test_vacio_es_ambiguo(self):
        assert ruta_deterministica("") is None
