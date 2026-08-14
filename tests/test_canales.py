"""Canales: el panel se divide por número de YCloud (Etapa 1).

El bot atiende DOS números y cada uno tiene sus conversaciones. El canal de un chat
es el número NUESTRO por el que entró (el `emisor` que guarda la meta del chat).
"""
from __future__ import annotations

from app.business_config import nombre_canal, norm_num, parsear_canales

A = "18099221092"
B = "+1 829 471-6701"


class TestNormalizacion:
    def test_mismo_numero_en_distintos_formatos(self):
        # Todos deben caer en el mismo canal.
        for x in ("18099221092", "+18099221092", "809-922-1092", "8099221092",
                  "+1 809 922 1092"):
            assert norm_num(x) == "8099221092", x

    def test_el_otro_numero_es_otro_canal(self):
        assert norm_num(B) == "8294716701"
        assert norm_num(A) != norm_num(B)

    def test_vacio(self):
        assert norm_num("") == ""
        assert norm_num(None) == ""


class TestNombres:
    def test_nombres_puestos_por_el_operador(self):
        mapa = parsear_canales("18099221092 = Tienda\n18294716701 = Mayorista")
        assert nombre_canal(A, mapa) == "Tienda"
        assert nombre_canal(B, mapa) == "Mayorista"

    def test_acepta_comas_y_espacios(self):
        mapa = parsear_canales(" 8099221092 = Tienda ,  8294716701 = Mayorista ")
        assert mapa == {"8099221092": "Tienda", "8294716701": "Mayorista"}

    def test_sin_nombre_muestra_el_numero_formateado(self):
        assert nombre_canal(A, {}) == "809-922-1092"
        assert nombre_canal(B, {}) == "829-471-6701"

    def test_sin_emisor_es_sin_canal(self):
        assert nombre_canal("", {}) == "Sin canal"

    def test_lineas_invalidas_se_ignoran(self):
        assert parsear_canales("basura\n= sin numero\n18099221092 = Ok") == {
            "8099221092": "Ok"
        }

    def test_vacio_no_rompe(self):
        assert parsear_canales("") == {}
        assert parsear_canales(None) == {}
