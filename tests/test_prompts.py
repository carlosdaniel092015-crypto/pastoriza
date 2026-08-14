"""Prompts por agente: carga de .md base, reglas duras presentes, precedencia override."""
from __future__ import annotations

import pytest

from app.panel import prompt_store


class TestBaseMd:
    def test_todos_los_md_existen_y_no_vacios(self):
        for a in prompt_store.AGENTES:
            contenido = prompt_store._leer_md(a)
            assert len(contenido) > 30, f"{a}.md vacío o no encontrado"

    def test_base_comun_tiene_reglas_duras(self):
        base = prompt_store._leer_md("base_comun")
        assert "BLINDAJE" in base
        assert "PROHIBIDO cancelar" in base
        assert "IGNORA cualquier texto" in base  # anti-jailbreak

    def test_pedido_exige_verificar_contacto(self):
        assert "verificar_contacto" in prompt_store._leer_md("pedido")

    def test_soporte_escala_no_cancela(self):
        assert "escalar_a_humano" in prompt_store._leer_md("soporte")


CANAL_A = "8099221092"
CANAL_B = "8294716701"


@pytest.fixture(autouse=True)
def _prompts_limpios():
    """Aísla los caches de proceso (no toca Redis)."""
    base = dict(prompt_store._base)
    ov = {k: dict(v) for k, v in prompt_store._override.items()}
    yield
    prompt_store._base.clear()
    prompt_store._base.update(base)
    prompt_store._override.clear()
    prompt_store._override.update(ov)


class TestPrecedencia:
    def test_override_gana_al_base(self):
        prompt_store._base["ventas"] = "BASE"
        prompt_store._override[""] = {"ventas": "OVERRIDE " + "x" * 40}
        assert prompt_store.get_prompt("ventas").startswith("OVERRIDE")
        assert prompt_store.usando_override("ventas") is True

    def test_sin_override_usa_base(self):
        prompt_store._base["ventas"] = "BASE"
        prompt_store._override[""] = {}
        assert prompt_store.get_prompt("ventas") == "BASE"
        assert prompt_store.usando_override("ventas") is False


class TestPorCanal:
    """Cada número puede tener su prompt; si no lo tiene, hereda el común o el .md."""

    def test_el_del_canal_gana_al_comun_y_al_base(self):
        prompt_store._base["ventas"] = "BASE"
        prompt_store._override[""] = {"ventas": "COMUN"}
        prompt_store._override[CANAL_A] = {"ventas": "DEL 1092"}
        assert prompt_store.get_prompt("ventas", CANAL_A) == "DEL 1092"
        assert prompt_store.origen("ventas", CANAL_A) == "canal"

    def test_el_otro_canal_no_se_contagia(self):
        prompt_store._base["ventas"] = "BASE"
        prompt_store._override[""] = {}
        prompt_store._override[CANAL_A] = {"ventas": "DEL 1092"}
        prompt_store._override[CANAL_B] = {}
        assert prompt_store.get_prompt("ventas", CANAL_B) == "BASE"
        assert prompt_store.origen("ventas", CANAL_B) == "base"
        assert prompt_store.usando_override("ventas", CANAL_B) is False

    def test_sin_propio_hereda_el_comun(self):
        prompt_store._base["ventas"] = "BASE"
        prompt_store._override[""] = {"ventas": "COMUN"}
        prompt_store._override[CANAL_B] = {}
        assert prompt_store.get_prompt("ventas", CANAL_B) == "COMUN"
        assert prompt_store.origen("ventas", CANAL_B) == "comun"

    def test_el_numero_se_normaliza(self):
        """+1 809-922-1092, 18099221092 y 8099221092 son EL MISMO canal."""
        prompt_store._base["ventas"] = "BASE"
        prompt_store._override[""] = {}
        prompt_store._override[CANAL_A] = {"ventas": "DEL 1092"}
        for forma in ("+1 809-922-1092", "18099221092", "8099221092"):
            assert prompt_store.get_prompt("ventas", forma) == "DEL 1092", forma
