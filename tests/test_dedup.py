"""Dedup por message_id: el bot no debe responder dos veces el mismo mensaje
cuando YCloud reintenta el webhook o lo entrega duplicado."""
from __future__ import annotations

from app import debounce
from app.models import InboundMessage


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


async def test_primera_vez_no_es_duplicado_segunda_si(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(debounce, "get_redis", lambda: fake)
    msg = InboundMessage(message_id="wamid.1", chat_id="c1")

    assert await debounce.es_duplicado(msg) is False  # primera entrega: se procesa
    assert await debounce.es_duplicado(msg) is True   # reintento: se ignora


async def test_mensajes_distintos_no_son_duplicados(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(debounce, "get_redis", lambda: fake)
    a = InboundMessage(message_id="wamid.A", chat_id="c1")
    b = InboundMessage(message_id="wamid.B", chat_id="c1")

    assert await debounce.es_duplicado(a) is False
    assert await debounce.es_duplicado(b) is False  # otro id: no es duplicado


async def test_sin_message_id_no_bloquea(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(debounce, "get_redis", lambda: fake)
    assert await debounce.es_duplicado(InboundMessage(message_id="", chat_id="c1")) is False
