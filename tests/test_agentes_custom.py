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
    previo = {k: dict(v) for k, v in agentes_custom._cache.items()}
    yield
    agentes_custom._cache.clear()
    agentes_custom._cache.update(previo)


def _registrar(nombre="mayorista", palabras=("al por mayor", "mayorista"),
               herramientas=("catalogo", "cotizar"), modelo="mini", canal=""):
    """Registra un agente en el cache. `canal` vacío = común a los dos números."""
    agentes_custom._cache.setdefault(canal, {})[nombre] = {
        "nombre": nombre,
        "descripcion": "Atiende compras al por mayor",
        "herramientas": list(herramientas),
        "palabras": list(palabras),
        "modelo": modelo,
        "activo": True,
        "canal": canal,
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
        agentes_custom._cache[""]["mayorista"]["activo"] = False
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

    def test_un_agente_de_un_solo_canal_tambien_es_editable(self):
        _registrar(nombre="mayorista", canal=CANAL_A)
        assert "mayorista" in prompt_store.agentes()


# ------------------------------------------------------------ por canal ---
CANAL_A = "8099221092"
CANAL_B = "8294716701"


class TestPorCanal:
    """Un agente creado para un número NO atiende en el otro."""

    def test_solo_atiende_en_su_canal(self):
        _registrar(nombre="mayorista", canal=CANAL_A)
        assert agentes_custom.get("mayorista", CANAL_A) is not None
        assert agentes_custom.get("mayorista", CANAL_B) is None
        assert agentes_custom.nombres(CANAL_A) == ("mayorista",)
        assert agentes_custom.nombres(CANAL_B) == ()

    def test_no_enruta_en_el_otro_canal(self):
        _registrar(nombre="mayorista", palabras=("al por mayor",), canal=CANAL_A)
        assert ruta_personalizada("compro al por mayor", CANAL_A) == "mayorista"
        assert ruta_personalizada("compro al por mayor", CANAL_B) is None
        # En el otro número ese agente no captura: sigue el enrutado normal.
        assert ruta_deterministica("compro al por mayor", canal=CANAL_B) != "mayorista"
        assert ruta_deterministica("precio de botella 8 oz", canal=CANAL_B) == "ventas"

    def test_un_agente_comun_atiende_en_los_dos(self):
        _registrar(nombre="mayorista", palabras=("al por mayor",), canal="")
        assert ruta_personalizada("compro al por mayor", CANAL_A) == "mayorista"
        assert ruta_personalizada("compro al por mayor", CANAL_B) == "mayorista"

    def test_el_del_canal_gana_al_comun(self):
        _registrar(nombre="mayorista", herramientas=("catalogo",), canal="")
        _registrar(nombre="mayorista", herramientas=("catalogo", "pedido"), canal=CANAL_A)
        assert agentes_custom.get("mayorista", CANAL_A)["herramientas"] == [
            "catalogo", "pedido"
        ]
        assert agentes_custom.get("mayorista", CANAL_B)["herramientas"] == ["catalogo"]
        # Y no se duplica en el listado del canal.
        nombres_a = [a["nombre"] for a in agentes_custom.listar(CANAL_A)]
        assert nombres_a == ["mayorista"]

    def test_el_especialista_se_construye_por_canal(self):
        _registrar(nombre="mayorista", herramientas=("catalogo",), canal=CANAL_A)
        agente_a = especialistas.obtener("mayorista", CANAL_A)
        nombres = {getattr(t, "name", "") for t in agente_a.tools}
        assert "buscar_producto" in nombres
        # En el otro número ese agente no existe: cae a ventas, no explota.
        assert especialistas.obtener("mayorista", CANAL_B) is (
            especialistas.ESPECIALISTAS["ventas"]
        )
