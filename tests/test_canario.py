"""Tests del canario de producción.

Escenarios tomados de fallas REALES que sufrieron clientes: el catálogo quedó
vacío y el bot le dijo a todo el mundo "no tengo productos disponibles", sin que
nadie se enterara hasta ver la captura de un cliente perdido.
"""
from __future__ import annotations

import pytest

from app import canario


@pytest.fixture(autouse=True)
def _estado_limpio():
    canario._estado.fallos = set()
    canario._estado.ultimo_aviso = 0.0
    yield
    canario._estado.fallos = set()


class _CatalogoFalso:
    def __init__(self, productos): self._p = productos
    async def todos(self, force: bool = False): return self._p


def _parchar(monkeypatch, productos=None, redis_ok=True, escaladas=0, revienta=False):
    async def cat():
        if revienta:
            raise RuntimeError("Odoo no responde")
        if not productos:
            return ("catalogo", "El catálogo está VACÍO")
        return ("", f"{len(productos)} productos")

    async def red():
        return ("", "ok") if redis_ok else ("redis", "Redis no responde")

    async def esc():
        if escaladas >= 15:
            return ("escaladas", f"{escaladas} conversaciones pasaron a un humano")
        return ("", f"{escaladas} en la última hora")

    monkeypatch.setattr(canario, "_revisar_catalogo", cat)
    monkeypatch.setattr(canario, "_revisar_redis", red)
    monkeypatch.setattr(canario, "_revisar_escaladas", esc)


async def test_todo_bien_no_reporta_fallos(monkeypatch):
    _parchar(monkeypatch, productos=["a", "b", "c"])
    res = await canario.revisar()
    assert res["ok"] is True
    assert res["fallos"] == {}


async def test_catalogo_vacio_se_detecta(monkeypatch):
    """La falla que dejó al bot diciendo 'no tengo productos' a todos."""
    _parchar(monkeypatch, productos=[])
    res = await canario.revisar()
    assert res["ok"] is False
    assert "catalogo" in res["fallos"]


async def test_odoo_caido_se_detecta(monkeypatch):
    _parchar(monkeypatch, revienta=True)
    res = await canario.revisar()
    assert "catalogo" in res["fallos"]


async def test_redis_caido_se_detecta(monkeypatch):
    _parchar(monkeypatch, productos=["a"], redis_ok=False)
    res = await canario.revisar()
    assert "redis" in res["fallos"]


async def test_pico_de_escaladas_se_detecta(monkeypatch):
    _parchar(monkeypatch, productos=["a"], escaladas=40)
    res = await canario.revisar()
    assert "escaladas" in res["fallos"]


class TestAvisos:
    """Avisar cuando CAMBIA el estado: ni silencio ni spam cada 10 minutos."""

    async def test_avisa_al_romperse_y_al_recuperarse(self, monkeypatch):
        from app.panel import telegram as tg

        enviados: list[str] = []

        async def _fake(txt: str) -> bool:
            enviados.append(txt)
            return True

        # El canario hace `from app.panel import telegram` dentro de la función,
        # así que parchar el atributo del módulo alcanza.
        monkeypatch.setattr(tg, "enviar", _fake)

        # 1) se rompe -> avisa
        _parchar(monkeypatch, productos=[])
        await canario.revisar_y_avisar()
        assert enviados and "problema" in enviados[0].lower()

        # 2) sigue roto -> NO vuelve a avisar (sin spam)
        antes = len(enviados)
        await canario.revisar_y_avisar()
        assert len(enviados) == antes

        # 3) se recupera -> avisa la vuelta a la normalidad
        _parchar(monkeypatch, productos=["a", "b"])
        await canario.revisar_y_avisar()
        assert len(enviados) > antes
        assert "normalidad" in enviados[-1].lower()
