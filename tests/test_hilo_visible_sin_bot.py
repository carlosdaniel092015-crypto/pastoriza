"""El hilo del panel muestra lo que el cliente escribió AUNQUE el bot no responda.

Reporte real: "solo puedo ver los mensajes cuando el bot responde, sino responde
entonces no veo nada... en las conversaciones se ve que ella escribió [pero el hilo
sale vacío]". Causa: el hilo se arma desde `RedisSession` (la memoria del agente), que
sólo se escribe cuando el agente corre de verdad — bot pausado, apagado, fuera de
horario o un turno que se cae con error nunca llegan a escribir ahí, aunque `chatmeta`
(la vista previa de la lista) sí se actualiza siempre.
"""
from __future__ import annotations

import pytest

from app.models import InboundMessage
from app.session import RedisSession
from tests.fake_redis import FakeRedis

CANAL_A = "18099221092"


@pytest.fixture
def fake(monkeypatch):
    import app.redis_client as rc
    from app import business_config as bc

    f = FakeRedis()
    monkeypatch.setattr(rc, "_pool", f)
    bc._cache.clear()
    return f


@pytest.fixture
def espia_acumular(monkeypatch):
    """No hace falta llegar al turno real: sólo confirmar si pasó el corte o no."""
    import app.pipeline as pipeline

    llamadas = []

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


def _msg(chat_id: str, texto: str = "hola", tipo: str = "text") -> InboundMessage:
    return InboundMessage(
        chat_id=chat_id, content=texto, content_type=tipo, instance_from=CANAL_A
    )


async def _historial(chat_id: str) -> list[dict]:
    return await RedisSession(chat_id).get_items()


class TestBotPausado:
    @pytest.mark.asyncio
    async def test_el_mensaje_del_cliente_queda_en_el_hilo(self, fake, espia_acumular):
        from app.estado import pausar_bot
        from app.pipeline import manejar_entrante

        await pausar_bot("18091112222")
        await manejar_entrante(_msg("18091112222", "la botella de 12 onza con la tapa"))

        h = await _historial("18091112222")
        assert h == [{"role": "user", "content": "la botella de 12 onza con la tapa"}]
        assert espia_acumular == []  # el bot no lo procesó: lo atiende un humano


class TestBotGlobalApagado:
    @pytest.mark.asyncio
    async def test_el_mensaje_del_cliente_queda_en_el_hilo(self, fake, espia_acumular):
        from app.estado import set_bot_global

        await set_bot_global(False)

        from app.pipeline import manejar_entrante

        await manejar_entrante(_msg("18092223333", "quiero cotizar 500 unidades"))
        h = await _historial("18092223333")
        assert h == [{"role": "user", "content": "quiero cotizar 500 unidades"}]
        assert espia_acumular == []


class TestFueraDeHorario:
    @pytest.mark.asyncio
    async def test_el_mensaje_del_cliente_queda_en_el_hilo(
        self, fake, espia_acumular, monkeypatch
    ):
        from datetime import datetime

        import app.horario as horario_mod
        from app.business_config import save_config

        CANAL_NOCTURNO = "18294716701"
        await save_config(
            {"horario_activo_desde": "19:00", "horario_activo_hasta": "05:00"},
            canal=CANAL_NOCTURNO,
        )
        monkeypatch.setattr(
            horario_mod, "datetime",
            type("D", (), {"now": staticmethod(lambda: datetime(2026, 1, 15, 12, 0))}),
        )

        from app.pipeline import manejar_entrante

        msg = InboundMessage(
            chat_id="18093334444", content="tienen fardos de 16 oz",
            instance_from=CANAL_NOCTURNO,
        )
        await manejar_entrante(msg)
        h = await _historial("18093334444")
        assert h == [{"role": "user", "content": "tienen fardos de 16 oz"}]
        assert espia_acumular == []

    @pytest.mark.asyncio
    async def test_una_nota_de_voz_bloqueada_muestra_un_placeholder(
        self, fake, espia_acumular, monkeypatch
    ):
        """Sin transcribir (no se corrió el agente): mejor un placeholder que nada."""
        from datetime import datetime

        import app.horario as horario_mod
        from app.business_config import save_config

        CANAL_NOCTURNO = "18294716701"
        await save_config(
            {"horario_activo_desde": "19:00", "horario_activo_hasta": "05:00"},
            canal=CANAL_NOCTURNO,
        )
        monkeypatch.setattr(
            horario_mod, "datetime",
            type("D", (), {"now": staticmethod(lambda: datetime(2026, 1, 15, 12, 0))}),
        )

        from app.pipeline import manejar_entrante

        msg = InboundMessage(
            chat_id="18095556666", content="", content_type="audio",
            instance_from=CANAL_NOCTURNO,
        )
        await manejar_entrante(msg)
        h = await _historial("18095556666")
        assert h == [{"role": "user", "content": "🎤 (nota de voz)"}]


class TestElFlujoNormalNoSeDuplica:
    """Un turno que SÍ se procesa no debe verse afectado por este fix: el registro
    manual sólo pasa en los caminos de corte, nunca cuando el agente corre normal."""

    @pytest.mark.asyncio
    async def test_un_mensaje_que_pasa_el_corte_no_escribe_nada_por_su_cuenta(
        self, fake, espia_acumular
    ):
        from app.pipeline import manejar_entrante

        await manejar_entrante(_msg("18099998888", "hola"))
        assert len(espia_acumular) == 1
        # manejar_entrante en sí no toca RedisSession: eso lo hace el agente después,
        # cuando drena el buffer y corre de verdad (fuera del alcance de este test).
        assert await _historial("18099998888") == []


class TestFallbackDeError:
    @pytest.mark.asyncio
    async def test_registra_lo_que_dijo_el_cliente_y_el_aviso_del_bot(self, fake):
        from app.pipeline import _fallback_error

        async def _enviar_texto_falso(*a, **kw):
            return True

        async def _avisar_admin_falso(*a, **kw):
            return None

        import app.pipeline as pipeline

        pipeline.ycloud.enviar_texto = _enviar_texto_falso
        pipeline.ycloud.avisar_admin = _avisar_admin_falso

        msg = InboundMessage(chat_id="18097778888", content="hola", instance_from=CANAL_A)
        await _fallback_error(msg, "quiero 3 fardos de 12 oz")

        h = await _historial("18097778888")
        assert h[0] == {"role": "user", "content": "quiero 3 fardos de 12 oz"}
        assert h[1]["role"] == "assistant"
        assert "inconveniente tecnico" in h[1]["content"]

    @pytest.mark.asyncio
    async def test_sin_texto_combinado_usa_el_del_mensaje_crudo(self, fake):
        from app.pipeline import _fallback_error

        import app.pipeline as pipeline

        async def _noop(*a, **kw):
            return True

        pipeline.ycloud.enviar_texto = _noop
        pipeline.ycloud.avisar_admin = _noop

        msg = InboundMessage(
            chat_id="18096667777", content="precio de botellon", instance_from=CANAL_A
        )
        await _fallback_error(msg)  # sin `texto`: cae a msg.content
        h = await _historial("18096667777")
        assert h[0]["content"] == "precio de botellon"
