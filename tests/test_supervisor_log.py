"""Lo que el bot le manda al SUPERVISOR se puede ver en el panel.

El panel muestra conversaciones con CLIENTES, así que la plantilla de aprobación que
sale hacia ADMIN_PHONE no aparecía en ninguna parte: si Meta la rechazaba, el síntoma
era que no llegaba nada y no había dónde mirar.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.fake_redis import FakeRedis


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


class TestRegistro:
    @pytest.mark.asyncio
    async def test_guarda_lo_mandado_y_lo_devuelve_del_mas_nuevo_al_mas_viejo(self, fake):
        from app.panel import supervisor_log

        await supervisor_log.registrar("aviso", texto="primero")
        await supervisor_log.registrar(
            "aprobacion", chat_id="1809", cliente="Juan", plantilla="aprobacion_pago",
            order_id=171, texto="Pedido: 171\nTOTAL: RD$1,000.00",
        )
        ms = await supervisor_log.listar()
        assert [m["tipo"] for m in ms] == ["aprobacion", "aviso"]
        assert ms[0]["order_id"] == 171
        assert ms[0]["plantilla"] == "aprobacion_pago"
        assert ms[0]["enviado"] is True

    @pytest.mark.asyncio
    async def test_cuenta_los_que_no_llegaron(self, fake):
        from app.panel import supervisor_log

        await supervisor_log.registrar("aprobacion", order_id=1, enviado=True)
        await supervisor_log.registrar("aprobacion", order_id=2, enviado=False)
        await supervisor_log.registrar("aprobacion", order_id=3, enviado=False)
        assert await supervisor_log.sin_entregar() == 2

    @pytest.mark.asyncio
    async def test_un_fallo_de_redis_no_propaga(self, fake, monkeypatch):
        """Perder el registro no vale perder el aviso al supervisor."""
        from app.panel import supervisor_log

        async def _explota(_op):
            raise RuntimeError("redis caido")

        monkeypatch.setattr(supervisor_log, "run_write", _explota)
        await supervisor_log.registrar("aviso", texto="algo")  # no debe levantar

    @pytest.mark.asyncio
    async def test_no_crece_sin_techo(self, fake):
        from app.panel import supervisor_log

        for i in range(supervisor_log.MAX + 25):
            await supervisor_log.registrar("aviso", texto=f"n{i}")
        ms = await supervisor_log.listar(supervisor_log.MAX)
        assert len(ms) == supervisor_log.MAX
        assert ms[0]["texto"] == f"n{supervisor_log.MAX + 24}"  # el más nuevo primero


class TestResumenLegible:
    def test_etiqueta_las_variables_de_la_plantilla(self):
        from app import aprobacion

        params = aprobacion.parametros(
            order_id=171, modalidad="envio", cliente="Juan", telefono="18091112222",
            direccion="Calle 1", lineas=[{"total": 1180.0}], envio=200.0,
        )
        texto = aprobacion.resumen_legible(params)
        # Sin etiquetas serían nueve valores sueltos: cuál es el total y cuál el subtotal.
        assert texto.startswith("Pedido: 171")
        assert "Modalidad: ENVÍO A DOMICILIO" in texto
        assert "Cliente: Juan (18091112222)" in texto
        assert "TOTAL:" in texto and "Subtotal:" in texto

    def test_sin_parametros_no_explota(self):
        from app import aprobacion

        assert aprobacion.resumen_legible([]) == ""


class TestEndpoint:
    def test_devuelve_los_mensajes_y_el_numero(self, cliente, fake):
        import asyncio

        from app.panel import supervisor_log
        from app.settings import settings

        asyncio.run(supervisor_log.registrar("aprobacion", order_id=9, enviado=False))
        r = cliente.get("/panel/api/supervisor")
        assert r.status_code == 200
        d = r.json()
        assert d["numero"] == settings.admin_phone
        assert d["total"] == 1 and d["sin_entregar"] == 1
        assert d["mensajes"][0]["order_id"] == 9

    def test_exige_token(self, cliente, monkeypatch):
        from app.settings import settings

        monkeypatch.setattr(settings, "panel_token", "secreto")
        assert cliente.get("/panel/api/supervisor").status_code == 401

    def test_vacio_no_explota(self, cliente, fake):
        d = cliente.get("/panel/api/supervisor").json()
        assert d["mensajes"] == [] and d["total"] == 0
