"""El horario configurado por canal SÍ corta el turno en manejar_entrante.

Requisito real: "quiero que el bot 8294716701 solamente se active automáticamente de
7 pm a 5 am todos los días". Se prueba contra la función de entrada real (no sólo la
función pura de horario.py), para asegurar que el corte pasa ANTES del debounce/turno
y que el otro canal, sin ese horario configurado, sigue andando normal.
"""
from __future__ import annotations

import pytest

from app.models import InboundMessage
from tests.fake_redis import FakeRedis

CANAL_NOCTURNO = "18294716701"
OTRO_CANAL = "18099221092"


@pytest.fixture
def fake(monkeypatch):
    import app.redis_client as rc
    from app import business_config as bc

    f = FakeRedis()
    monkeypatch.setattr(rc, "_pool", f)
    bc._cache.clear()  # el cache en memoria no debe arrastrar config de otro test
    return f


@pytest.fixture
def espia_acumular(monkeypatch):
    """No hace falta llegar hasta el envío por WhatsApp: alcanza con ver si el mensaje
    pasó el corte y entró al flujo normal (acumular)."""
    import app.pipeline as pipeline

    llamadas: list[InboundMessage] = []

    async def _acumular(msg):
        llamadas.append(msg)

    class _TareaFalsa:
        def add_done_callback(self, *a, **kw):
            pass

    def _create_task(coro):
        coro.close()  # no correr _turno_diferido de verdad (llamaría a OpenAI)
        return _TareaFalsa()

    monkeypatch.setattr(pipeline, "acumular", _acumular)
    monkeypatch.setattr(pipeline.asyncio, "create_task", _create_task)
    return llamadas


def _msg(chat_id: str, canal: str) -> InboundMessage:
    return InboundMessage(chat_id=chat_id, content="hola", instance_from=canal)


async def _configurar_horario(canal: str, desde: str, hasta: str) -> None:
    from app.business_config import save_config

    await save_config(
        {"horario_activo_desde": desde, "horario_activo_hasta": hasta}, canal=canal
    )


class TestElCanalConfiguradoRespetaElHorario:
    @pytest.mark.asyncio
    async def test_fuera_de_horario_no_pasa_a_acumular(
        self, fake, espia_acumular, monkeypatch
    ):
        from datetime import datetime

        import app.horario as horario_mod

        await _configurar_horario(CANAL_NOCTURNO, "19:00", "05:00")
        monkeypatch.setattr(
            horario_mod, "datetime",
            type("D", (), {"now": staticmethod(lambda: datetime(2026, 1, 15, 12, 0))}),
        )

        from app.pipeline import manejar_entrante

        await manejar_entrante(_msg("18091112222", CANAL_NOCTURNO))
        assert espia_acumular == []

    @pytest.mark.asyncio
    async def test_dentro_de_horario_si_pasa(self, fake, espia_acumular, monkeypatch):
        from datetime import datetime

        import app.horario as horario_mod

        await _configurar_horario(CANAL_NOCTURNO, "19:00", "05:00")
        monkeypatch.setattr(
            horario_mod, "datetime",
            type("D", (), {"now": staticmethod(lambda: datetime(2026, 1, 15, 22, 0))}),
        )

        from app.pipeline import manejar_entrante

        await manejar_entrante(_msg("18091112222", CANAL_NOCTURNO))
        assert len(espia_acumular) == 1

    @pytest.mark.asyncio
    async def test_el_otro_canal_sin_horario_configurado_no_se_ve_afectado(
        self, fake, espia_acumular, monkeypatch
    ):
        """Configurar el horario de UN canal no puede silenciar al otro."""
        from datetime import datetime

        import app.horario as horario_mod

        await _configurar_horario(CANAL_NOCTURNO, "19:00", "05:00")
        monkeypatch.setattr(
            horario_mod, "datetime",
            type("D", (), {"now": staticmethod(lambda: datetime(2026, 1, 15, 12, 0))}),
        )

        from app.pipeline import manejar_entrante

        await manejar_entrante(_msg("18091112223", OTRO_CANAL))
        assert len(espia_acumular) == 1

    @pytest.mark.asyncio
    async def test_el_mensaje_sigue_visible_en_el_panel_aunque_no_responda(
        self, fake, espia_acumular, monkeypatch
    ):
        """Fuera de horario el bot se calla, pero la conversación no desaparece: hay
        que poder atenderla a mano (igual que con el bot pausado)."""
        from datetime import datetime

        import app.horario as horario_mod
        from app.panel import events

        await _configurar_horario(CANAL_NOCTURNO, "19:00", "05:00")
        monkeypatch.setattr(
            horario_mod, "datetime",
            type("D", (), {"now": staticmethod(lambda: datetime(2026, 1, 15, 12, 0))}),
        )

        from app.pipeline import manejar_entrante

        await manejar_entrante(_msg("18091112224", CANAL_NOCTURNO))
        assert "18091112224" in await events.todos_chatmeta()

    @pytest.mark.asyncio
    async def test_sin_configurar_nada_el_comportamiento_no_cambia(
        self, fake, espia_acumular
    ):
        """Default: ningún canal tiene horario configurado, todo sigue como siempre."""
        from app.pipeline import manejar_entrante

        await manejar_entrante(_msg("18091112225", CANAL_NOCTURNO))
        await manejar_entrante(_msg("18091112226", OTRO_CANAL))
        assert len(espia_acumular) == 2
