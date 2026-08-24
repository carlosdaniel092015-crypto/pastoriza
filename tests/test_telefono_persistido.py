"""El teléfono capturado en un turno ANTERIOR sobrevive a un turno sin contacto.

Caso real: un chat que llega de un anuncio tiene chat_id interno de YCloud
(`DO.3712360618920826`, no un número). El cliente ya había dado su teléfono en un
pedido anterior (crear_contacto lo guardó en el chatmeta al final de ESE turno), pero
en un turno posterior sin ninguna tool de contacto de por medio -por ejemplo, pide
"Asistencia humana requerida"- el bot volvía a armar el contexto con
`telefono=trigger.telefono` (vacío otra vez, YCloud nunca lo manda para este chat_id) y
el aviso al supervisor caía a `ctx.telefono or ctx.chat_id`, mandando el ID de YCloud.

`_telefono_persistido` es el fallback: si el chatmeta de ese chat_id ya tiene un
teléfono guardado de una vez anterior, se usa ese en vez de repetir el chat_id.
"""
from __future__ import annotations

import pytest

from app.pipeline import _telefono_persistido


@pytest.fixture
def fake(monkeypatch):
    import app.redis_client as rc

    from tests.fake_redis import FakeRedis

    f = FakeRedis()
    monkeypatch.setattr(rc, "_pool", f)
    return f


class TestConTelefonoGuardado:
    async def test_devuelve_el_telefono_del_chatmeta(self, fake):
        from app.panel import events

        await events.tocar_chatmeta(
            "DO.3712360618920826", emisor="18099221092",
            user_name="Génesis Akemy", telefono="18293837395",
        )
        assert await _telefono_persistido("DO.3712360618920826") == "18293837395"


class TestSinTelefonoGuardado:
    async def test_chat_nunca_visto_da_vacio(self, fake):
        assert await _telefono_persistido("DO.999999999") == ""

    async def test_chatmeta_sin_telefono_da_vacio(self, fake):
        from app.panel import events

        await events.tocar_chatmeta("DO.111", emisor="18099221092", user_name="Cliente")
        assert await _telefono_persistido("DO.111") == ""

    async def test_chat_id_vacio_no_consulta_nada(self, fake):
        assert await _telefono_persistido("") == ""


class TestNoBloqueaPorUnFalloDeRedis:
    async def test_si_redis_falla_no_lanza(self, fake, monkeypatch):
        from app.panel import events

        async def _explota(chat_id):
            raise RuntimeError("redis caido")

        monkeypatch.setattr(events, "leer_chatmeta", _explota)
        assert await _telefono_persistido("DO.111") == ""
