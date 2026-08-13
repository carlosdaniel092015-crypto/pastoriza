"""Tests del fast-path. Corren en milisegundos y no gastan un token."""
from __future__ import annotations

import pytest

from app.business_config import BusinessConfig
from app.router import respuesta_directa

CFG = BusinessConfig()


def r(texto: str, **kw):
    return respuesta_directa(texto, CFG, **kw)


class TestRespondeSolo:
    @pytest.mark.parametrize("texto", ["hola", "Buenas", "klk", "Buenos dias!"])
    def test_saludos(self, texto):
        assert r(texto) is not None

    @pytest.mark.parametrize("texto", ["gracias", "Muchas gracias", "ok gracias", "chao"])
    def test_cierres(self, texto):
        assert r(texto) is not None

    def test_horario(self):
        out = r("cual es el horario?")
        assert out and CFG.horario_tienda in out

    def test_direccion_tienda(self):
        out = r("donde estan ubicados")
        assert out and CFG.direccion in out

    def test_cuentas_bancarias(self):
        out = r("a que cuenta transfiero")
        assert out and CFG.banco1_cuenta in out and CFG.banco2_cuenta in out

    def test_costo_envio(self):
        out = r("cuanto cuesta el envio")
        assert out and CFG.precio_envio in out

    def test_telefono(self):
        out = r("cual es el telefono")
        assert out and CFG.telefono in out

    def test_estado_pedido_deriva(self):
        out = r("donde esta mi pedido")
        assert out and "829" in out


class TestVaAlAgente:
    """None = lo maneja el modelo. Estos casos NUNCA deben cortocircuitarse."""

    @pytest.mark.parametrize(
        "texto",
        [
            "quiero 3 cajas de botella lisa 8 oz",
            "tienen galones?",
            "cuanto sale el botellon",
            "necesito una cotizacion de 500 unidades",
        ],
    )
    def test_intencion_de_compra(self, texto):
        assert r(texto) is None

    @pytest.mark.parametrize(
        "texto",
        [
            "vivo en la calle Duarte #45, Herrera",
            "es en Santo Domingo, proximo al colmado",
            "av. Independencia km 9",
        ],
    )
    def test_direcciones_van_al_agente(self, texto):
        assert r(texto) is None

    def test_queja_de_repeticion(self):
        assert r("ya te di la direccion muchas veces") is None

    def test_ya_pague(self):
        assert r("ya te hice la transferencia") is None

    def test_ubicacion_compartida(self):
        assert r("[UBICACION_WHATSAPP] compartio su ubicacion maps.google") is None

    def test_no_texto_va_al_agente(self):
        assert r("hola", content_type="image") is None

    def test_desde_anuncio_nunca_corta(self):
        """Si viene de un anuncio, ni el 'hola' se responde genérico:
        el agente ya sabe qué producto vio."""
        assert r("hola", viene_de_anuncio=True) is None

    def test_vacio(self):
        assert r("   ") is None


class TestMultiIntencion:
    """Caso real: '¿Precio botellas? / Donde están ubicado' -> el fast-path devolvía
    SOLO la dirección y la pregunta del precio se perdía en silencio."""

    def test_faq_mas_producto_va_al_agente(self):
        assert r("¿Precio botellas?\nDonde están ubicado") is None

    def test_dos_faq_van_al_agente(self):
        assert r("cuanto cuesta el envio y donde estan ubicados") is None

    def test_una_sola_faq_sigue_en_fast_path(self):
        assert r("donde estan ubicados")
        assert r("cuanto cuesta el envio")
        assert r("a que hora abren")

    def test_estado_de_pedido_no_se_confunde_con_direccion(self):
        # "donde esta mi pedido" también matchea la regex de dirección: debe
        # seguir respondiendo el estado del pedido, no irse al agente.
        out = r("donde esta mi pedido")
        assert out and "829" in out
