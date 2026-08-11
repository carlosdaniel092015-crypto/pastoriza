"""Prompts por agente: carga de .md base, reglas duras presentes, precedencia override."""
from __future__ import annotations

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


class TestPrecedencia:
    def test_override_gana_al_base(self):
        prompt_store._base["ventas"] = "BASE"
        prompt_store._override["ventas"] = "OVERRIDE " + "x" * 40
        assert prompt_store.get_prompt("ventas").startswith("OVERRIDE")
        assert prompt_store.usando_override("ventas") is True

    def test_sin_override_usa_base(self):
        prompt_store._base["ventas"] = "BASE"
        prompt_store._override["ventas"] = None
        assert prompt_store.get_prompt("ventas") == "BASE"
        assert prompt_store.usando_override("ventas") is False
