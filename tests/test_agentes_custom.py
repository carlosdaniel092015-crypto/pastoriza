"""Tests de los agentes PERSONALIZADOS creados desde el panel.

Lo importante: un agente creado ahí debe ATENDER de verdad (tener herramientas y
recibir conversaciones), no ser un prompt inerte. Y si no hay ninguno creado, el
comportamiento del bot no cambia en nada.
"""
from __future__ import annotations

import pytest

from app.agents import especialistas
from app.agents.enrutador import ruta_deterministica, ruta_personalizada
from app.panel import agentes_custom, prompt_store


@pytest.fixture(autouse=True)
def _registro_limpio():
    """Aísla el cache del registro (no toca Redis)."""
    previo = dict(agentes_custom._cache)
    yield
    agentes_custom._cache = previo


def _registrar(nombre="mayorista", palabras=("al por mayor", "mayorista"),
               herramientas=("catalogo", "cotizar"), modelo="mini"):
    agentes_custom._cache[nombre] = {
        "nombre": nombre,
        "descripcion": "Atiende compras al por mayor",
        "herramientas": list(herramientas),
        "palabras": list(palabras),
        "modelo": modelo,
        "activo": True,
    }
    agentes_custom._version += 1


class TestValidacion:
    def test_nombre_invalido(self):
        assert agentes_custom.validar("Ma yor", [], "mini")
        assert agentes_custom.validar("ab", [], "mini")          # muy corto
        assert agentes_custom.validar("9mayor", [], "mini")      # empieza con número

    def test_nombre_reservado(self):
        assert agentes_custom.validar("ventas", [], "mini")
        assert agentes_custom.validar("base_comun", [], "mini")

    def test_herramienta_desconocida(self):
        assert agentes_custom.validar("mayorista", ["inventada"], "mini")

    def test_modelo_invalido(self):
        assert agentes_custom.validar("mayorista", ["catalogo"], "gpt5")

    def test_valido(self):
        assert agentes_custom.validar("mayorista", ["catalogo", "cotizar"], "mini") == ""


class TestEnrutado:
    def test_sin_agentes_creados_no_cambia_nada(self):
        assert ruta_personalizada("quiero comprar al por mayor") is None
        # El enrutado base sigue igual.
        assert ruta_deterministica("quiero cancelar mi pedido") == "soporte"
        assert ruta_deterministica("precio de botella 8 oz") == "ventas"

    def test_palabra_clave_enruta_al_agente_creado(self):
        _registrar()
        assert ruta_personalizada("necesito precios al por mayor") == "mayorista"
        assert ruta_deterministica("necesito precios al por mayor") == "mayorista"

    def test_reclamo_gana_sobre_el_agente_creado(self):
        _registrar()
        # Una cancelación debe ir a soporte aunque mencione la palabra clave.
        assert ruta_deterministica("quiero cancelar mi pedido al por mayor") == "soporte"

    def test_gana_la_palabra_mas_especifica(self):
        _registrar(nombre="mayorista", palabras=("mayor",))
        _registrar(nombre="exportacion", palabras=("mayor para exportacion",))
        assert ruta_personalizada("precios mayor para exportacion") == "exportacion"

    def test_agente_inactivo_no_enruta(self):
        _registrar()
        agentes_custom._cache["mayorista"]["activo"] = False
        assert ruta_personalizada("compro al por mayor") is None


class TestConstruccion:
    def test_el_agente_creado_tiene_herramientas_y_atiende(self):
        _registrar(herramientas=("catalogo", "cotizar"))
        agente = especialistas.obtener("mayorista")
        assert agente is not None
        nombres = {getattr(t, "name", "") for t in agente.tools}
        assert "buscar_producto" in nombres, "sin catálogo no puede vender"
        assert "cotizar" in nombres
        # escalar_a_humano viene siempre (y está blindada por el determinador).
        assert "escalar_a_humano" in nombres

    def test_sin_pack_de_pedido_no_puede_crear_pedidos(self):
        _registrar(herramientas=("catalogo",))
        agente = especialistas.obtener("mayorista")
        nombres = {getattr(t, "name", "") for t in agente.tools}
        assert "crear_pedido" not in nombres

    def test_nombre_desconocido_cae_a_ventas(self):
        agente = especialistas.obtener("no_existe")
        assert agente is especialistas.ESPECIALISTAS["ventas"]

    def test_agentes_base_intactos(self):
        for n in ("ventas", "pedido", "soporte"):
            assert especialistas.obtener(n) is especialistas.ESPECIALISTAS[n]


class TestPromptStore:
    def test_el_agente_creado_es_editable_en_el_panel(self):
        _registrar()
        assert "mayorista" in prompt_store.agentes()
        # Los base siguen presentes.
        for n in prompt_store.AGENTES:
            assert n in prompt_store.agentes()
