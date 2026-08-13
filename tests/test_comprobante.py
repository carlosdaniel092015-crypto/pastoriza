"""Detección de comprobante de pago a partir del análisis de la imagen.

Caso REAL: un cliente mandó la foto de unos envases (promo con precios) y el bot la
tomó como comprobante de pago. Consecuencias: alerta falsa al admin ("llegó un
comprobante pero el pedido NO se registró") y el turno enrutado al agente de PEDIDO
en vez del de ventas. La causa era buscar el literal "COMPROBANTE_PAGO" en el texto,
que el modelo escribe incluso para decir que NO hay comprobante.
"""
from __future__ import annotations

from app.media import es_comprobante_de

# Texto tal como lo devolvió el modelo en el caso real.
ANALISIS_FOTO_ENVASES = """1) COMPROBANTE_PAGO: [no hay datos]

2) SELECCION_PRODUCTO: [16 oz, 12 oz, 12 oz Lisa, 8 oz Lisa]

3) FOTO de envase:
TIPO_ENVASE: Botella / CAPACIDAD: 8 oz, 12 oz, 16 oz / COLOR: Rojo"""


class TestNoEsComprobante:
    def test_caso_real_foto_de_envases(self):
        assert es_comprobante_de(ANALISIS_FOTO_ENVASES) is False

    def test_etiqueta_sin_datos(self):
        for t in ("COMPROBANTE_PAGO: [no hay datos]",
                  "COMPROBANTE_PAGO: no aplica",
                  "COMPROBANTE_PAGO: ninguno",
                  "1) COMPROBANTE_PAGO: [no se ve el banco]",
                  "COMPROBANTE_PAGO: []",
                  "COMPROBANTE_PAGO:"):
            assert es_comprobante_de(t) is False, t

    def test_solo_foto_de_envase(self):
        assert es_comprobante_de("3) FOTO de envase: TIPO_ENVASE: Botella") is False

    def test_vacio(self):
        assert es_comprobante_de("") is False


class TestSiEsComprobante:
    def test_comprobante_bancario_real(self):
        assert es_comprobante_de(
            "COMPROBANTE_PAGO: [Banco Popular, RD$1,200.00, ref 998877, 13/08/2026]"
        ) is True

    def test_con_banco_y_monto(self):
        assert es_comprobante_de(
            "1) COMPROBANTE_PAGO: Banco Reservas, monto RD$ 5,870, referencia 4471"
        ) is True
