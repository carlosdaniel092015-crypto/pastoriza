"""Telegram es para ALERTAS: errores y cosas rotas, nada más.

Requisito textual: "quiero que solo me muestres en telegram cuando hay errores o
problemas como alerta y ya".

El riesgo que evita: el analista corre cada 24h y mandaba una tarjeta POR sugerencia.
Con Telegram convertido en un feed, se ignora — y entonces también se ignoran los
errores de verdad, que es lo único que ahí importa.
"""
from __future__ import annotations

import pytest

from tests.fake_redis import FakeRedis


@pytest.fixture
def fake(monkeypatch):
    import app.redis_client as rc

    f = FakeRedis()
    monkeypatch.setattr(rc, "_pool", f)
    return f


@pytest.fixture
def espia_telegram(monkeypatch):
    """Registra lo que se hubiera mandado a Telegram, sin salir a la red."""
    from app.panel import telegram

    enviados: list[str] = []

    async def _enviar(texto, *a, **kw):
        enviados.append(texto)

    async def _enviar_sugerencia(s, *a, **kw):
        enviados.append(f"[tarjeta] {s}")

    monkeypatch.setattr(telegram, "enviar", _enviar)
    monkeypatch.setattr(telegram, "enviar_sugerencia", _enviar_sugerencia)
    monkeypatch.setattr(telegram, "configurado", lambda: True)
    return enviados


class TestLosErroresSiLleganPorTelegram:
    """Lo que el requisito SÍ quiere: que un error avise."""

    @pytest.mark.asyncio
    async def test_un_error_notifica(self, fake, espia_telegram):
        from app.panel import events

        await events.publicar(
            "error", "18091112222", donde="Agente", detalle="TypeError: algo"
        )
        assert len(espia_telegram) == 1
        assert "TypeError" in espia_telegram[0]

    @pytest.mark.asyncio
    async def test_lo_que_no_es_error_no_notifica(self, fake, espia_telegram):
        """handoff/revision/control quedan en el panel: son rutina, no urgencias."""
        from app.panel import events

        for kind in ("handoff", "revision", "control", "pedido", "turno"):
            await events.publicar(kind, "18091112222", detalle="rutina")
        assert espia_telegram == []
        # Pero SÍ quedaron en el feed del panel.
        assert len(await events.listar()) == 5

    def test_solo_los_errores_estan_en_la_lista_de_notificables(self):
        from app.panel import events

        assert events.KINDS_TELEGRAM == {"error"}


class TestLasSugerenciasNoNotifican:
    """Lo que el requisito quiere APAGADO: las sugerencias del analista."""

    def test_el_default_es_solo_alertas(self):
        from app.settings import settings

        assert settings.telegram_solo_alertas is True

    @pytest.mark.asyncio
    async def test_con_solo_alertas_no_manda_las_sugerencias(
        self, fake, espia_telegram, monkeypatch
    ):
        from app.panel import analista, conocimiento
        from app.settings import settings

        monkeypatch.setattr(settings, "telegram_solo_alertas", True)
        monkeypatch.setattr(analista, "listar_revision", _revision_falsa)
        monkeypatch.setattr(analista, "_openai", _OpenAIFalso())
        monkeypatch.setattr(conocimiento, "cargar", _nada)

        res = await analista.analizar_y_sugerir(auto_aplicar_bajo_riesgo=False)
        assert res["pendientes"] >= 1  # la sugerencia se creó...
        assert espia_telegram == []  # ...pero no salió por Telegram

    @pytest.mark.asyncio
    async def test_apagando_la_opcion_vuelven_a_llegar(
        self, fake, espia_telegram, monkeypatch
    ):
        """La función no se borró: quien la quiera la puede volver a prender."""
        from app.panel import analista, conocimiento
        from app.settings import settings

        monkeypatch.setattr(settings, "telegram_solo_alertas", False)
        monkeypatch.setattr(analista, "listar_revision", _revision_falsa)
        monkeypatch.setattr(analista, "_openai", _OpenAIFalso())
        monkeypatch.setattr(conocimiento, "cargar", _nada)

        await analista.analizar_y_sugerir(auto_aplicar_bajo_riesgo=False)
        assert espia_telegram  # ahora sí avisó
        assert any("Analista" in t for t in espia_telegram)


async def _nada(*a, **kw):
    return None


async def _revision_falsa(*a, **kw):
    return [
        {
            "chat_id": "18091112222",
            "motivo": "no entendio el precio",
            "texto": "cuanto vale la botella",
            "respuesta": "no se",
        }
    ]


_JSON_SUGERENCIA = (
    '{"sugerencias": [{"tipo": "regla", "riesgo": "alto",'
    ' "texto": "Si preguntan por precio, dar el del catalogo con ITBIS."}]}'
)


def _OpenAIFalso():
    """El analista le pide las sugerencias a OpenAI inline (no hay función que
    reemplazar), así que se sustituye el cliente entero: no gasta tokens ni sale a la
    red, y devuelve siempre una sugerencia de riesgo ALTO (queda pendiente, no se
    auto-aplica)."""
    from types import SimpleNamespace

    async def _create(*a, **kw):
        msg = SimpleNamespace(content=_JSON_SUGERENCIA)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
    )
