from __future__ import annotations

import pytest

from app.tools.cotizar_tools import calcular


class TestCotizacion:
    def test_retiro_no_suma_envio(self):
        c = calcular(5.87, 100, "retiro")
        assert c.total_productos == 587.0
        assert c.envio == 0.0
        assert c.total_final == 587.0

    def test_envio_suma(self):
        c = calcular(5.87, 100, "envio", precio_envio=550.0)
        assert c.total_final == 1137.0

    def test_itbis_se_desglosa_bien(self):
        c = calcular(118.0, 1, "retiro")
        assert c.subtotal_sin_itbis == 100.0
        assert c.itbis == 18.0

    def test_sin_modalidad_no_suma_envio(self):
        c = calcular(10.0, 10)
        assert c.modalidad == ""
        assert c.total_final == 100.0

    def test_modalidad_abreviada(self):
        assert calcular(10.0, 1, "env").modalidad == "envio"
        assert calcular(10.0, 1, "Retiro en tienda").modalidad == "retiro"

    @pytest.mark.parametrize("cantidad", [0, -3, 200_000])
    def test_cantidad_invalida(self, cantidad):
        with pytest.raises(ValueError):
            calcular(10.0, cantidad)

    def test_precio_invalido(self):
        with pytest.raises(ValueError):
            calcular(0, 5)
