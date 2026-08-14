"""Las INSTRUCCIONES que recibe el modelo son las del canal del cliente.

Es el punto donde todo lo de "cada número individual" se vuelve comportamiento real:
el mismo Agent atiende los dos números, así que si el armado de instrucciones no
mirara el `emisor` del contexto, el 6701 respondería con el prompt, las reglas y los
precios del 1092.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.agents.base import armar_instrucciones
from app.business_config import BusinessConfig
from app.context import ConversationContext
from app.panel import conocimiento, prompt_store

A = "18099221092"
B = "+1 829 471-6701"
CA, CB = "8099221092", "8294716701"


@dataclass
class _Wrapper:
    """Lo que el SDK le pasa al callable de instrucciones."""

    context: ConversationContext


def _ctx(emisor: str, **kw) -> ConversationContext:
    base = dict(
        chat_id="18091112222",
        telefono="18091112222",
        user_name="Cliente",
        emisor=emisor,
        destino={"to": "18091112222"},
        cfg=BusinessConfig(**kw),
    )
    return ConversationContext(**base)


def _instrucciones(emisor: str, agente: str = "ventas", **cfg) -> str:
    return armar_instrucciones(agente)(_Wrapper(_ctx(emisor, **cfg)), None)


class TestPromptDelCanal:
    def setup_method(self) -> None:
        self._base = dict(prompt_store._base)
        self._ov = {k: dict(v) for k, v in prompt_store._override.items()}
        self._reglas = {k: list(v) for k, v in conocimiento._reglas.items()}
        self._correc = {k: list(v) for k, v in conocimiento._correc.items()}
        self._cargado = conocimiento._cargado

    def teardown_method(self) -> None:
        prompt_store._base.clear()
        prompt_store._base.update(self._base)
        prompt_store._override.clear()
        prompt_store._override.update(self._ov)
        conocimiento._reglas.clear()
        conocimiento._reglas.update(self._reglas)
        conocimiento._correc.clear()
        conocimiento._correc.update(self._correc)
        conocimiento._cargado = self._cargado

    def test_cada_numero_recibe_su_prompt(self):
        prompt_store._base["base_comun"] = "COMUN BASE"
        prompt_store._base["ventas"] = "VENTAS BASE"
        prompt_store._override.clear()
        prompt_store._override[""] = {}
        prompt_store._override[CB] = {"ventas": "SOLO EL 6701 VENDE ASI"}

        assert "SOLO EL 6701 VENDE ASI" in _instrucciones(B)
        assert "SOLO EL 6701 VENDE ASI" not in _instrucciones(A)
        assert "VENTAS BASE" in _instrucciones(A)

    def test_las_reglas_aprendidas_son_del_canal(self):
        prompt_store._base["base_comun"] = "COMUN BASE"
        prompt_store._base["ventas"] = "VENTAS BASE"
        prompt_store._override.clear()
        prompt_store._override[""] = {}
        conocimiento._reglas.clear()
        conocimiento._reglas[""] = [{"texto": "regla comun", "activa": True}]
        conocimiento._reglas[CB] = [{"texto": "regla del 6701", "activa": True}]
        conocimiento._correc.clear()
        conocimiento._cargado = True

        de_b = _instrucciones(B)
        de_a = _instrucciones(A)
        assert "regla del 6701" in de_b and "regla comun" in de_b
        assert "regla del 6701" not in de_a and "regla comun" in de_a

    def test_los_precios_del_bloque_dinamico_son_los_del_canal(self):
        """El precio de envío que ve el modelo sale de la cfg del turno."""
        assert "RD$700" in _instrucciones(B, precio_envio="700", monto_minimo="700")
        assert "RD$2000" in _instrucciones(A, monto_minimo="2000")

    def test_sin_emisor_no_rompe(self):
        """Un turno sin canal (dato viejo) usa lo común, no explota."""
        prompt_store._base["base_comun"] = "COMUN BASE"
        prompt_store._base["ventas"] = "VENTAS BASE"
        prompt_store._override.clear()
        prompt_store._override[""] = {"ventas": "OVERRIDE COMUN"}
        assert "OVERRIDE COMUN" in _instrucciones("")
