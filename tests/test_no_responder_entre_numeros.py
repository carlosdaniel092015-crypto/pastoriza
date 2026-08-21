"""Ninguno de nuestros DOS números es un cliente.

Requisito textual: "el 829-471-6701 no debe responderle nunca al 809-922-1092 y si el
829-471-6701 se escribe así mismo no se debe responder el mismo tampoco, recuerda que
el 6701 lo estará manejando una persona".
"""
from __future__ import annotations

import pytest

from app.models import InboundMessage
from tests.fake_redis import FakeRedis

CANAL_A = "18099221092"  # 809-922-1092
CANAL_B = "18294716701"  # 829-471-6701, también ADMIN_PHONE en esta config


@pytest.fixture
def fake(monkeypatch):
    import app.redis_client as rc
    from app import business_config as bc

    f = FakeRedis()
    monkeypatch.setattr(rc, "_pool", f)
    bc._cache.clear()
    # Config común con los dos canales declarados (como en producción).
    import json

    f.kv[bc.CONFIG_KEY] = json.dumps(
        {"canales": f"{CANAL_A} = Tienda\n{CANAL_B} = Mayorista"}
    )
    return f


class TestEsUnoDeNuestrosNumeros:
    @pytest.mark.asyncio
    async def test_los_dos_canales_se_reconocen(self, fake):
        from app.pipeline import _es_uno_de_nuestros_numeros

        assert await _es_uno_de_nuestros_numeros(CANAL_A) is True
        assert await _es_uno_de_nuestros_numeros(CANAL_B) is True
        assert await _es_uno_de_nuestros_numeros("+1 829-471-6701") is True  # formatos raros

    @pytest.mark.asyncio
    async def test_un_cliente_real_no_es_ninguno_de_los_dos(self, fake):
        from app.pipeline import _es_uno_de_nuestros_numeros

        assert await _es_uno_de_nuestros_numeros("18091112222") is False

    @pytest.mark.asyncio
    async def test_vacio_no_es_ninguno(self, fake):
        from app.pipeline import _es_uno_de_nuestros_numeros

        assert await _es_uno_de_nuestros_numeros("") is False

    @pytest.mark.asyncio
    async def test_un_fallo_leyendo_config_falla_cerrado_hacia_el_cliente(
        self, fake, monkeypatch
    ):
        """Si no se puede leer la config, mejor atender un mensaje raro que dejar a un
        cliente real sin respuesta."""
        from app import pipeline

        async def _explota(*a, **kw):
            raise RuntimeError("redis caido")

        monkeypatch.setattr(pipeline, "canales_configurados", _explota)
        assert await pipeline._es_uno_de_nuestros_numeros("18091112222") is False


@pytest.fixture
def espia_acumular(monkeypatch):
    import app.pipeline as pipeline

    llamadas: list[InboundMessage] = []

    async def _acumular(msg):
        llamadas.append(msg)

    class _TareaFalsa:
        def add_done_callback(self, *a, **kw):
            pass

    def _create_task(coro):
        coro.close()
        return _TareaFalsa()

    monkeypatch.setattr(pipeline, "acumular", _acumular)
    monkeypatch.setattr(pipeline.asyncio, "create_task", _create_task)
    return llamadas


def _msg(chat_id: str, instance_from: str) -> InboundMessage:
    return InboundMessage(chat_id=chat_id, content="hola", instance_from=instance_from)


class TestManejarEntranteCortaElCrucePorAdentro:
    @pytest.mark.asyncio
    async def test_6701_no_le_responde_a_1092(self, fake, espia_acumular):
        """El caso pedido: un mensaje que llega a 6701 pero viene DE 1092."""
        from app.pipeline import manejar_entrante

        await manejar_entrante(_msg(chat_id=CANAL_A, instance_from=CANAL_B))
        assert espia_acumular == []

    @pytest.mark.asyncio
    async def test_6701_no_se_responde_a_si_mismo(self, fake, espia_acumular):
        from app.pipeline import manejar_entrante

        await manejar_entrante(_msg(chat_id=CANAL_B, instance_from=CANAL_B))
        assert espia_acumular == []

    @pytest.mark.asyncio
    async def test_1092_no_le_responde_a_6701(self, fake, espia_acumular):
        """Simétrico: tampoco al revés, aunque no se haya pedido explícitamente."""
        from app.pipeline import manejar_entrante

        await manejar_entrante(_msg(chat_id=CANAL_B, instance_from=CANAL_A))
        assert espia_acumular == []

    @pytest.mark.asyncio
    async def test_un_cliente_real_en_1092_sigue_andando_24h(self, fake, espia_acumular):
        """809-922-1092 sigue activo las 24 horas para clientes de verdad."""
        from app.pipeline import manejar_entrante

        await manejar_entrante(_msg(chat_id="18091112222", instance_from=CANAL_A))
        assert len(espia_acumular) == 1

    @pytest.mark.asyncio
    async def test_no_queda_registrado_en_el_panel(self, fake, espia_acumular):
        """No es una conversación real: no debe ensuciar el CRM con "1092" como
        si fuera un cliente."""
        from app.panel import events
        from app.pipeline import manejar_entrante

        await manejar_entrante(_msg(chat_id=CANAL_A, instance_from=CANAL_B))
        assert CANAL_A not in await events.todos_chatmeta()
