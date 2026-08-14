"""El PANEL responde por canal: elegir un número cambia TODO lo que se ve y edita.

Requisito de operación, textual: "si le doy al 1092 se debe cambiar a todas las
configuraciones y conversaciones de este, si le doy al 6701 se debe cambiar a todas
las configuraciones de ese, y las conversaciones no se deben juntar; cada uno estará
con todo individual".

Se prueba contra la app real (TestClient) con un Redis de mentira: así se verifica el
contrato HTTP completo que consume el panel, no sólo las funciones internas.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.fake_redis import FakeRedis

A, B = "18099221092", "18294716701"
CA, CB = "8099221092", "8294716701"
CANALES = f"{A} = Tienda\n{B} = Mayorista"


@pytest.fixture
def cliente(monkeypatch):
    import app.redis_client as rc
    from app import business_config as bc
    from app.main import app
    from app.panel import agentes_custom, conocimiento, prompt_store

    fake = FakeRedis()
    monkeypatch.setattr(rc, "_pool", fake)
    fake.kv[bc.CONFIG_KEY] = json.dumps(
        {"precio_envio": "550", "monto_minimo": "1000", "canales": CANALES}
    )
    bc.invalidar()
    bc._ultima_buena.clear()
    # Caches de proceso limpios y con los dos canales cargados.
    prompt_store._override.clear()
    prompt_store._override[""] = {}
    conocimiento._reglas.clear()
    conocimiento._correc.clear()
    agentes_custom._cache.clear()
    agentes_custom._cache[""] = {}

    with TestClient(app) as c:  # el lifespan carga prompts/conocimiento/agentes
        yield c

    bc.invalidar()
    bc._ultima_buena.clear()
    rc._pool = None


def _get(c, path, canal=""):
    r = c.get(path + (f"?canal={canal}" if canal else ""))
    assert r.status_code == 200, r.text
    return r.json()


def _post(c, path, body, canal=""):
    r = c.post(path + (f"?canal={canal}" if canal else ""), json=body)
    assert r.status_code == 200, r.text
    return r.json()


class TestPestanas:
    def test_los_dos_numeros_aparecen_aunque_no_tengan_conversaciones(self, cliente):
        """Un número recién dado de alta debe verse: si no, no hay dónde configurarlo."""
        d = _get(cliente, "/panel/api/chats")
        canales = {c["canal"]: c["nombre"] for c in d["canales"]}
        assert canales == {CA: "Tienda", CB: "Mayorista"}


class TestConfig:
    def test_guardar_en_un_canal_no_toca_el_otro(self, cliente):
        _post(cliente, "/panel/api/config",
              {"precio_envio": "700", "monto_minimo": "1000"}, canal=CB)
        assert _get(cliente, "/panel/api/config", CB)["precio_envio"] == "700"
        assert _get(cliente, "/panel/api/config", CA)["precio_envio"] == "550"
        assert _get(cliente, "/panel/api/config")["precio_envio"] == "550"

    def test_el_panel_sabe_que_campos_son_propios(self, cliente):
        _post(cliente, "/panel/api/config", {"precio_envio": "700"}, canal=CB)
        assert "precio_envio" in _get(cliente, "/panel/api/config", CB)["_propios"]
        assert _get(cliente, "/panel/api/config", CA)["_propios"] == []

    def test_aplicar_a_los_dos(self, cliente):
        _post(cliente, "/panel/api/config", {"precio_envio": "700"}, canal=CB)
        _post(cliente, "/panel/api/config",
              {"precio_envio": "900", "canales": CANALES, "_ambos": True}, canal=CB)
        assert _get(cliente, "/panel/api/config", CA)["precio_envio"] == "900"
        assert _get(cliente, "/panel/api/config", CB)["precio_envio"] == "900"

    def test_volver_a_la_comun(self, cliente):
        _post(cliente, "/panel/api/config", {"precio_envio": "700"}, canal=CB)
        r = cliente.request("DELETE", f"/panel/api/config?canal={CB}")
        assert r.status_code == 200, r.text
        assert _get(cliente, "/panel/api/config", CB)["precio_envio"] == "550"

    def test_sin_canal_no_se_puede_resetear(self, cliente):
        assert cliente.request("DELETE", "/panel/api/config").status_code == 400


class TestPrompts:
    LARGO = "Sos Michelle y atendes SOLO al mayorista del 6701, con precios por fardo."

    def test_el_prompt_guardado_en_un_canal_no_aplica_al_otro(self, cliente):
        d = _post(cliente, "/panel/api/prompts/ventas", {"override": self.LARGO}, canal=CB)
        assert d["origen"] == "canal"
        de_b = _get(cliente, "/panel/api/prompts", CB)["prompts"]["ventas"]
        de_a = _get(cliente, "/panel/api/prompts", CA)["prompts"]["ventas"]
        assert de_b["override"] == self.LARGO and de_b["usando_override"] is True
        assert de_a["usando_override"] is False and de_a["origen"] == "base"

    def test_aplicar_a_los_dos(self, cliente):
        _post(cliente, "/panel/api/prompts/ventas",
              {"override": self.LARGO, "ambos": True}, canal=CB)
        for canal in (CA, CB):
            p = _get(cliente, "/panel/api/prompts", canal)["prompts"]["ventas"]
            assert p["override"] == self.LARGO, canal
            assert p["origen"] == "comun"

    def test_borrar_el_propio_vuelve_al_heredado(self, cliente):
        _post(cliente, "/panel/api/prompts/ventas",
              {"override": self.LARGO, "ambos": True}, canal=CB)
        otro = "Sos Michelle del 6701 y solo hablas de envases para exportacion."
        _post(cliente, "/panel/api/prompts/ventas", {"override": otro}, canal=CB)
        assert _get(cliente, "/panel/api/prompts", CB)["prompts"]["ventas"]["override"] == otro
        _post(cliente, "/panel/api/prompts/ventas", {"override": ""}, canal=CB)
        p = _get(cliente, "/panel/api/prompts", CB)["prompts"]["ventas"]
        assert p["override"] == self.LARGO and p["origen"] == "comun"

    def test_prompt_corto_se_rechaza(self, cliente):
        r = cliente.post(f"/panel/api/prompts/ventas?canal={CB}", json={"override": "corto"})
        assert r.status_code == 400


class TestAprendizaje:
    def test_una_regla_del_canal_no_se_ve_en_el_otro(self, cliente):
        _post(cliente, "/panel/api/reglas", {"texto": "Al 6701 se cobra envio aparte"}, canal=CB)
        de_b = [r["texto"] for r in _get(cliente, "/panel/api/aprendizaje", CB)["reglas"]]
        de_a = [r["texto"] for r in _get(cliente, "/panel/api/aprendizaje", CA)["reglas"]]
        assert de_b == ["Al 6701 se cobra envio aparte"]
        assert de_a == []

    def test_una_regla_para_los_dos(self, cliente):
        _post(cliente, "/panel/api/reglas",
              {"texto": "Nunca prometer entrega el mismo dia", "ambos": True}, canal=CB)
        for canal in (CA, CB):
            reglas = _get(cliente, "/panel/api/aprendizaje", canal)["reglas"]
            assert [r["canal"] for r in reglas] == [""], canal

    def test_correccion_por_canal(self, cliente):
        _post(cliente, "/panel/api/correcciones",
              {"situacion": "pide descuento", "respuesta_correcta": "pasalo a un asesor"},
              canal=CB)
        assert len(_get(cliente, "/panel/api/aprendizaje", CB)["correcciones"]) == 1
        assert _get(cliente, "/panel/api/aprendizaje", CA)["correcciones"] == []


class TestAgentes:
    PROMPT = "Sos Michelle y atendes compras al por mayor con precios por fardo cerrado."

    def test_un_agente_creado_en_un_canal_no_existe_en_el_otro(self, cliente):
        _post(cliente, "/panel/api/agentes", {
            "nombre": "mayorista", "descripcion": "al por mayor",
            "herramientas": ["catalogo"], "palabras": ["al por mayor"],
            "modelo": "mini", "prompt": self.PROMPT,
        }, canal=CB)
        en_b = [a["nombre"] for a in _get(cliente, "/panel/api/prompts", CB)["personalizados"]]
        en_a = [a["nombre"] for a in _get(cliente, "/panel/api/prompts", CA)["personalizados"]]
        assert en_b == ["mayorista"]
        assert en_a == []

    def test_un_agente_para_los_dos(self, cliente):
        _post(cliente, "/panel/api/agentes", {
            "nombre": "mayorista", "descripcion": "al por mayor",
            "herramientas": ["catalogo"], "palabras": ["al por mayor"],
            "modelo": "mini", "prompt": self.PROMPT, "ambos": True,
        }, canal=CB)
        for canal in (CA, CB):
            en = _get(cliente, "/panel/api/prompts", canal)["personalizados"]
            assert [a["nombre"] for a in en] == ["mayorista"], canal
            assert en[0]["canal"] == ""
