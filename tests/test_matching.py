"""Tests del scoring portado desde n8n.

IMPORTANTE: estos casos son de ejemplo. Antes de apagar n8n, reemplazá los
nombres por productos REALES de tu catálogo y agregá los casos que hoy sabés
que funcionan bien. Esta es la red que te avisa si el port rompió el matching.
"""
from __future__ import annotations

from app.matching import (
    caps,
    es_busqueda_tipo_envase,
    excluidas,
    norm,
    score,
    score_ficha,
)


class TestNormalizacion:
    def test_quita_tildes_y_mayusculas(self):
        assert norm("BOTELLÓN Cilíndrico") == "botellon cilindrico"

    def test_separa_numero_de_unidad(self):
        assert "8 oz" in norm("botella 8oz")

    def test_medio_se_vuelve_fraccion(self):
        assert "1/2" in norm("medio galon")

    def test_quita_simbolo_precio(self):
        assert "rd" not in norm("RD$5.87")


class TestCapacidades:
    def test_onzas(self):
        assert caps("botella lisa 8 oz") == ["8 oz"]

    def test_galon_normalizado(self):
        assert caps("envase de 1 galon") == ["1 galon"]
        assert caps("1 gal") == ["1 galon"]

    def test_onza_singular_a_oz(self):
        assert caps("16 onzas") == ["16 oz"]

    def test_sin_capacidad(self):
        assert caps("botella cualquiera") == []


class TestExclusiones:
    def test_que_no_sea(self):
        assert "cuadrada" in excluidas("una botella que no sea cuadrada")

    def test_sin(self):
        assert "tapa" in excluidas("botellon sin tapa")

    def test_ninguna(self):
        assert excluidas("botella de 8 oz") == set()


class TestScore:
    def test_capacidad_exacta_pesa_mucho(self):
        con = score("botella 8 oz", "BOTELLA LISA ECO 8 OZ")
        sin = score("botella 8 oz", "BOTELLA LISA ECO 16 OZ")
        assert con > sin

    def test_misma_unidad_distinta_cantidad_suma_poco(self):
        # 16 oz no es 8 oz, pero al menos es la misma familia.
        assert score("botella 8 oz", "BOTELLA LISA ECO 16 OZ") > 0

    def test_modelo_pesa_mas_que_tipo(self):
        con_modelo = score("botella eco", "BOTELLA LISA ECO 8 OZ")
        solo_tipo = score("botella", "BOTELLA LISA ECO 8 OZ")
        assert con_modelo > solo_tipo

    def test_exclusion_penaliza(self):
        normal = score("botella cuadrada", "BOTELLA CUADRADA 8 OZ")
        excluido = score("botella que no sea cuadrada", "BOTELLA CUADRADA 8 OZ")
        assert excluido < normal

    def test_sin_coincidencia_da_cero_o_menos(self):
        assert score("tornillo de acero", "BOTELLA LISA ECO 8 OZ") <= 1


class TestTipoEnvase:
    def test_una_palabra_tipo(self):
        assert es_busqueda_tipo_envase("botellas") is True

    def test_dos_palabras_con_tipo(self):
        assert es_busqueda_tipo_envase("envases plasticos") is True  # 'plasticos' es stop

    def test_busqueda_especifica_no_es_tipo(self):
        assert es_busqueda_tipo_envase("botella lisa eco 8 oz") is False

    def test_vacio(self):
        assert es_busqueda_tipo_envase("") is False


class TestScoreFicha:
    def test_identicas_puntuan_alto(self):
        f = {
            "tipo": "botella",
            "forma": "cilindrica",
            "proporcion": "alta",
            "transparencia": "transparente",
            "tapa": "rosca",
            "tapa_color": "blanco",
            "capacidad": "8 oz",
        }
        assert score_ficha(f, f) == 22

    def test_capacidad_distinta_penaliza(self):
        a = {"tipo": "botella", "capacidad": "8 oz"}
        b = {"tipo": "botella", "capacidad": "16 oz"}
        assert score_ficha(a, b) == 5 - 2

    def test_galon_se_normaliza(self):
        a = {"tipo": "galon", "capacidad": "1 galon"}
        b = {"tipo": "galon", "capacidad": "galones"}
        assert score_ficha(a, b) == 5 + 6
