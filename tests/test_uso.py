"""Uso del bot: tokens gastados y latencia, acumulados por día y por agente.

Se prueba contra el endpoint real del panel (TestClient + Redis de mentira), no sólo
la función interna: lo que se rompe en producción es el contrato HTTP.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from tests.fake_redis import FakeRedis


@dataclass
class UsageFalso:
    """Lo que expone `result.context_wrapper.usage` del SDK de agents."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    requests: int = 0


@pytest.fixture
def fake(monkeypatch):
    import app.redis_client as rc

    f = FakeRedis()
    monkeypatch.setattr(rc, "_pool", f)
    return f


@pytest.fixture
def cliente(fake):
    from app.main import app

    with TestClient(app) as c:
        yield c


def _get(cliente, url):
    r = cliente.get(url, headers={"X-Panel-Token": "test-token"})
    assert r.status_code == 200, r.text
    return r.json()


class TestRegistrarUso:
    @pytest.mark.asyncio
    async def test_suma_tokens_y_turnos_por_agente(self, fake):
        from app.panel import uso

        await uso.registrar("ventas", UsageFalso(100, 40, 140, 2), 1500)
        await uso.registrar("ventas", UsageFalso(60, 20, 80, 1), 500)
        await uso.registrar("pedido", UsageFalso(200, 50, 250, 1), 3000)

        d = await uso.resumen(1)
        assert d["por_agente"]["ventas"]["turnos"] == 2
        assert d["por_agente"]["ventas"]["tokens_total"] == 220
        assert d["por_agente"]["ventas"]["tokens_entrada"] == 160
        assert d["por_agente"]["ventas"]["duracion_ms"] == 2000
        assert d["por_agente"]["pedido"]["turnos"] == 1
        # El total general suma los dos agentes.
        assert d["total"]["turnos"] == 3
        assert d["total"]["tokens_total"] == 470
        assert d["total"]["requests"] == 4

    @pytest.mark.asyncio
    async def test_sin_datos_devuelve_ceros_no_explota(self, fake):
        from app.panel import uso

        d = await uso.resumen(7)
        assert d["total"]["turnos"] == 0
        assert d["por_agente"] == {}
        assert len(d["dias"]) == 7  # un día por fecha, aunque estén vacíos

    @pytest.mark.asyncio
    async def test_un_fallo_de_redis_no_propaga(self, fake, monkeypatch):
        """Perder una métrica NUNCA puede tumbar el turno del cliente."""
        from app.panel import uso

        async def _explota(_op):
            raise RuntimeError("redis caido")

        monkeypatch.setattr(uso, "run_write", _explota)
        await uso.registrar("ventas", UsageFalso(1, 1, 2, 1), 10)  # no debe levantar


class TestEndpointUso:
    def test_devuelve_el_resumen(self, cliente, fake):
        import asyncio

        from app.panel import uso

        asyncio.run(uso.registrar("ventas", UsageFalso(10, 5, 15, 1), 800))
        d = _get(cliente, "/panel/api/uso?dias=1")
        assert d["total"]["turnos"] == 1
        assert d["total"]["tokens_total"] == 15
        assert d["por_agente"]["ventas"]["tokens_salida"] == 5

    def test_exige_token(self, cliente, monkeypatch):
        from app.settings import settings

        monkeypatch.setattr(settings, "panel_token", "secreto")
        assert cliente.get("/panel/api/uso").status_code == 401

    def test_dias_se_acota(self, cliente, fake):
        # Un `dias` absurdo no debe pedir 10.000 keys a Redis.
        d = _get(cliente, "/panel/api/uso?dias=9999")
        assert len(d["dias"]) == 45
