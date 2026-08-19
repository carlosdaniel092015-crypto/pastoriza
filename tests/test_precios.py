"""El coste en dólares de lo que gasta el bot.

Es una PROYECCIÓN (tokens × tarifa), no una factura. Lo que se prueba acá es que la
cuenta sea correcta y que un modelo desconocido NO se informe como gratis: un cero
mentiría diciendo que ese agente no cuesta nada.
"""
from __future__ import annotations

import pytest

from app.panel import precios


class TestTarifa:
    def test_modelo_conocido(self):
        assert precios.tarifa("gpt-4o") == (2.50, 10.00)
        assert precios.tarifa("gpt-4o-mini") == (0.15, 0.60)

    def test_no_importan_mayusculas_ni_espacios(self):
        assert precios.tarifa("  GPT-4o-Mini ") == precios.tarifa("gpt-4o-mini")

    def test_le_quita_la_fecha_del_snapshot(self):
        # El SDK devuelve ids con versión; anotar una fila por snapshot no escala.
        assert precios.tarifa("gpt-4o-2024-08-06") == precios.tarifa("gpt-4o")
        assert precios.tarifa("gpt-4o-mini-2024-07-18") == precios.tarifa("gpt-4o-mini")

    def test_snapshot_desconocido_de_familia_conocida_usa_la_familia(self):
        assert precios.tarifa("gpt-4o-mini-turbo-nuevo") == precios.tarifa("gpt-4o-mini")
        # Y gana el prefijo MÁS LARGO: mini no debe cobrarse como gpt-4o.
        assert precios.tarifa("gpt-4o-mini-algo") != precios.tarifa("gpt-4o")

    def test_modelo_desconocido_es_none_no_cero(self):
        assert precios.tarifa("llama-de-la-esquina") is None
        assert precios.tarifa("") is None


class TestCosto:
    def test_la_cuenta(self):
        # 1M de entrada en gpt-4o-mini = $0.15; 1M de salida = $0.60.
        assert precios.costo("gpt-4o-mini", 1_000_000, 0) == pytest.approx(0.15)
        assert precios.costo("gpt-4o-mini", 0, 1_000_000) == pytest.approx(0.60)
        assert precios.costo("gpt-4o-mini", 1_000_000, 1_000_000) == pytest.approx(0.75)

    def test_gpt4o_cuesta_mucho_mas_que_mini(self):
        """La diferencia es la razón por la que se guarda el modelo: si se asumiera uno
        solo, el coste estaría mal por más de un orden de magnitud."""
        caro = precios.costo("gpt-4o", 100_000, 10_000)
        barato = precios.costo("gpt-4o-mini", 100_000, 10_000)
        assert caro / barato > 15

    def test_desconocido_no_inventa_un_cero(self):
        assert precios.costo("modelo-raro", 999_999, 999_999) is None

    def test_sin_tokens_es_cero(self):
        assert precios.costo("gpt-4o", 0, 0) == 0


class TestFormato:
    def test_las_fracciones_de_centavo_no_se_aplastan_a_cero(self):
        # Con 2 decimales, un coste por mensaje daría "$0.00" y parecería gratis.
        assert precios.fmt(0.00352) == "$0.00352"
        assert precios.fmt(0.0523) == "$0.0523"
        assert precios.fmt(3.52) == "$3.52"

    def test_desconocido_se_muestra_como_guion(self):
        assert precios.fmt(None) == "—"
        assert precios.fmt(0) == "$0.00"
