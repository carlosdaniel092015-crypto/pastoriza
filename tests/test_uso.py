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


class TestUsoPorConversacion:
    """Para poder responder CUÁL conversación se está comiendo los tokens, no sólo
    cuántos en total."""

    @pytest.mark.asyncio
    async def test_desglosa_entrada_salida_y_agente_por_chat(self, fake):
        from app.panel import uso

        await uso.registrar("ventas", UsageFalso(1000, 100, 1100, 2), 900, "18091112222")
        await uso.registrar("ventas", UsageFalso(500, 50, 550, 1), 300, "18091112222")
        await uso.registrar("pedido", UsageFalso(200, 80, 280, 1), 1200, "18091112222")
        await uso.registrar("ventas", UsageFalso(30, 10, 40, 1), 100, "18093334444")

        d = await uso.por_chat("18091112222")
        assert d["total"]["tokens_entrada"] == 1700
        assert d["total"]["tokens_salida"] == 230
        assert d["total"]["tokens_total"] == 1930
        assert d["total"]["turnos"] == 3
        # Y se sabe QUÉ agente lo gastó: no es lo mismo mini que gpt-4o.
        assert d["por_agente"]["ventas"]["turnos"] == 2
        assert d["por_agente"]["ventas"]["tokens_total"] == 1650
        assert d["por_agente"]["pedido"]["tokens_total"] == 280

        # La otra conversación quedó aparte.
        assert (await uso.por_chat("18093334444"))["total"]["tokens_total"] == 40
        assert (await uso.por_chat("nadie"))["total"]["turnos"] == 0
        assert (await uso.por_chat(""))["total"]["turnos"] == 0

    @pytest.mark.asyncio
    async def test_el_ranking_va_de_mayor_a_menor(self, fake):
        from app.panel import uso

        await uso.registrar("ventas", UsageFalso(50, 10, 60, 1), 100, "chico")
        await uso.registrar("ventas", UsageFalso(9000, 900, 9900, 3), 100, "grande")
        await uso.registrar("ventas", UsageFalso(500, 50, 550, 1), 100, "medio")

        top = await uso.top_chats()
        assert [c["chat_id"] for c in top] == ["grande", "medio", "chico"]
        assert top[0]["tokens_total"] == 9900
        assert top[0]["agentes"] == ["ventas"]

    @pytest.mark.asyncio
    async def test_el_resumen_general_incluye_los_chats(self, fake):
        from app.panel import uso

        await uso.registrar("ventas", UsageFalso(100, 20, 120, 1), 500, "18091112222")
        d = await uso.resumen(1)
        assert [c["chat_id"] for c in d["chats"]] == ["18091112222"]
        assert d["chats"][0]["tokens_entrada"] == 100

    @pytest.mark.asyncio
    async def test_sin_chat_id_solo_cuenta_en_el_total(self, fake):
        """El fast-path y el enrutador no siempre tienen chat_id: no debe romperse ni
        aparecer una conversación fantasma en el ranking."""
        from app.panel import uso

        await uso.registrar("ventas", UsageFalso(10, 5, 15, 1), 100)
        assert (await uso.resumen(1))["total"]["tokens_total"] == 15
        assert await uso.top_chats() == []

    @pytest.mark.asyncio
    async def test_el_costo_sale_del_modelo_de_cada_agente(self, fake):
        """El coste en dólares depende del MODELO, no del agente: los mismos tokens en
        gpt-4o cuestan ~17x más que en mini."""
        from app.panel import precios, uso

        await uso.registrar(
            "ventas", UsageFalso(100_000, 10_000, 110_000, 5), 900, "1809", "gpt-4o-mini"
        )
        await uso.registrar(
            "pedido", UsageFalso(100_000, 10_000, 110_000, 1), 900, "1809", "gpt-4o"
        )
        d = await uso.resumen(1)
        v = d["por_agente"]["ventas"]
        p = d["por_agente"]["pedido"]
        assert v["modelo"] == "gpt-4o-mini" and p["modelo"] == "gpt-4o"
        assert v["costo_usd"] == pytest.approx(precios.costo("gpt-4o-mini", 100_000, 10_000))
        assert p["costo_usd"] / v["costo_usd"] > 15  # mismos tokens, muchísimo más caro
        assert d["total"]["costo_usd"] == pytest.approx(v["costo_usd"] + p["costo_usd"])
        assert d["total"]["costo_completo"] is True
        # Y por conversación da lo mismo que el total (acá sólo hay una).
        assert (await uso.por_chat("1809"))["total"]["costo_usd"] == pytest.approx(
            d["total"]["costo_usd"]
        )

    @pytest.mark.asyncio
    async def test_un_modelo_sin_tarifa_marca_el_total_como_incompleto(self, fake):
        """Mejor decir "faltan datos" que sumar de menos y parecer más barato."""
        from app.panel import uso

        await uso.registrar("ventas", UsageFalso(1000, 100, 1100, 1), 100, "x", "gpt-4o-mini")
        await uso.registrar("raro", UsageFalso(1000, 100, 1100, 1), 100, "x", "modelo-inventado")
        d = await uso.resumen(1)
        assert d["por_agente"]["raro"]["costo_usd"] is None
        assert d["por_agente"]["ventas"]["costo_usd"] > 0
        assert d["total"]["costo_completo"] is False

    @pytest.mark.asyncio
    async def test_sin_modelo_el_costo_es_desconocido_no_cero(self, fake):
        """Los turnos registrados ANTES de este cambio no tienen modelo guardado."""
        from app.panel import uso

        await uso.registrar("ventas", UsageFalso(1000, 100, 1100, 1), 100, "y")
        d = await uso.resumen(1)
        assert d["por_agente"]["ventas"]["costo_usd"] is None
        assert d["por_agente"]["ventas"]["tokens_total"] == 1100  # los tokens sí están

    def test_el_resumen_informa_las_tarifas_usadas(self, cliente, fake):
        """La proyección tiene que ser auditable: con qué precios se hizo la cuenta."""
        d = _get(cliente, "/panel/api/uso?dias=1")
        assert d["tarifas"]["gpt-4o-mini"] == {"entrada": 0.15, "salida": 0.60}

    def test_el_hilo_devuelve_el_uso_de_esa_conversacion(self, cliente, fake):
        import asyncio

        from app.panel import uso

        asyncio.run(uso.registrar("ventas", UsageFalso(70, 30, 100, 1), 400, "1809"))
        r = cliente.get("/panel/api/chats/1809")
        assert r.status_code == 200
        u = r.json()["uso"]
        assert u["total"]["tokens_entrada"] == 70
        assert u["total"]["tokens_salida"] == 30
        assert u["por_agente"]["ventas"]["turnos"] == 1
