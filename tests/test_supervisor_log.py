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


class TestLasPlantillasQuedanRegistradas:
    """`enviar_plantilla` registra sola cuando el destino es el supervisor.

    Va en ycloud y no en cada llamada porque hay TRES lugares que le mandan plantillas
    al supervisor (escalamiento, pedido creado, fallback del aviso de aprobación) y el
    de escalamiento se había quedado afuera: los avisos de "Asistencia Humana
    Requerida" no aparecían en el panel.
    """

    @pytest.mark.asyncio
    async def test_el_escalamiento_queda_con_sus_etiquetas(self, fake, monkeypatch):
        from app.panel import supervisor_log
        from app.settings import settings
        from app.ycloud import ycloud

        async def _ok(*a, **kw):
            return {"id": "1"}

        monkeypatch.setattr(ycloud, "_post", _ok)
        await ycloud.enviar_plantilla(
            settings.admin_phone, "18099221092", "alerta_supervisor_cliente",
            ["Colmado o4", "+18096882021", "(nota de voz) necesito hablar con alguien"],
        )
        ms = await supervisor_log.listar()
        assert len(ms) == 1
        assert ms[0]["tipo"] == "escalamiento"
        assert ms[0]["enviado"] is True
        assert ms[0]["cliente"] == "Colmado o4"
        # Con etiquetas: si no, en el panel se ven tres valores sueltos.
        assert "Cliente: Colmado o4" in ms[0]["texto"]
        assert "Lo que pidió: (nota de voz) necesito hablar con alguien" in ms[0]["texto"]
        # Y enlaza a la conversación del cliente.
        assert ms[0]["chat_id"] == "18096882021"

    @pytest.mark.asyncio
    async def test_si_meta_la_rechaza_queda_marcada_como_no_entregada(self, fake, monkeypatch):
        """Es el caso que más importa: la plantilla no aprobada no avisa a nadie, y sin
        registro el síntoma es que no pasa nada y no hay dónde mirar."""
        from app.panel import supervisor_log
        from app.settings import settings
        from app.ycloud import ycloud

        async def _falla(*a, **kw):
            raise RuntimeError("template not approved")

        monkeypatch.setattr(ycloud, "_post", _falla)
        with pytest.raises(RuntimeError):
            await ycloud.enviar_plantilla(
                settings.admin_phone, "18099221092", "alerta_supervisor_cliente",
                ["Winifer", "+18294255310", "Ok yo soy de Santiago"],
            )
        ms = await supervisor_log.listar()
        assert len(ms) == 1
        assert ms[0]["enviado"] is False
        assert "PLANTILLA_META" in ms[0]["detalle"]
        assert await supervisor_log.sin_entregar() == 1

    @pytest.mark.asyncio
    async def test_una_plantilla_a_un_cliente_no_se_registra(self, fake, monkeypatch):
        """El módulo es lo que se le manda al SUPERVISOR: si entraran las de clientes,
        se volvería otro feed de conversaciones y no serviría para nada."""
        from app.panel import supervisor_log
        from app.ycloud import ycloud

        async def _ok(*a, **kw):
            return {"id": "1"}

        monkeypatch.setattr(ycloud, "_post", _ok)
        await ycloud.enviar_plantilla(
            "+18091112222", "18099221092", "alerta_supervisor_cliente", ["x", "y", "z"]
        )
        assert await supervisor_log.listar() == []

    @pytest.mark.asyncio
    async def test_un_fallo_del_registro_no_impide_el_aviso(self, fake, monkeypatch):
        from app.panel import supervisor_log
        from app.settings import settings
        from app.ycloud import ycloud

        enviados = []

        async def _ok(payload, *a, **kw):
            enviados.append(payload)
            return {"id": "1"}

        async def _explota(*a, **kw):
            raise RuntimeError("redis caido")

        monkeypatch.setattr(ycloud, "_post", _ok)
        monkeypatch.setattr(supervisor_log, "registrar", _explota)
        # No debe levantar: el aviso al supervisor vale más que su registro.
        await ycloud.enviar_plantilla(
            settings.admin_phone, "18099221092", "alerta_supervisor_cliente", ["a", "b", "c"]
        )
        assert len(enviados) == 1


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
