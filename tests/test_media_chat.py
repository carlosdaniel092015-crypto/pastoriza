"""La foto y la nota de voz del cliente se pueden VER en el panel.

En el hilo sólo queda el texto que se le pasó al modelo (la transcripción del audio, el
análisis visual de la foto), y con eso quien opera no puede juzgar si el bot entendió
bien lo que le mandaron. Se guarda una copia del archivo A DISCO (no a Redis: ahí vive
la config del negocio con noeviction).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.fake_redis import FakeRedis

JPG = b"\xff\xd8\xff\xe0" + b"x" * 200  # cabecera de JPEG + relleno
OGG = b"OggS" + b"y" * 200


@pytest.fixture
def fake(monkeypatch, tmp_path):
    import app.redis_client as rc
    from app.panel import media_chat

    f = FakeRedis()
    monkeypatch.setattr(rc, "_pool", f)
    # A disco de verdad, pero en el tmp del test: nunca en /srv/media.
    monkeypatch.setattr(media_chat, "DIRECTORIO", tmp_path / "media")
    return f


@pytest.fixture
def cliente(fake):
    from app.main import app

    with TestClient(app) as c:
        yield c


class TestGuardarYLeer:
    @pytest.mark.asyncio
    async def test_guarda_la_foto_y_la_devuelve_igual(self, fake):
        from app.panel import media_chat

        token = await media_chat.guardar("18091112222", JPG, "image/jpeg", "imagen", "botella 8 oz")
        assert token and token.endswith(".jpg")
        assert media_chat.leer(token) == ("image/jpeg", JPG)

    @pytest.mark.asyncio
    async def test_el_indice_queda_por_conversacion(self, fake):
        from app.panel import media_chat

        await media_chat.guardar("18091112222", JPG, "image/jpeg", "imagen", "una botella")
        await media_chat.guardar("18091112222", OGG, "audio/ogg", "audio", "quiero 5 fardos")
        await media_chat.guardar("18093334444", JPG, "image/jpeg", "imagen", "otra cosa")

        de_uno = await media_chat.listar("18091112222")
        assert [m["tipo"] for m in de_uno] == ["imagen", "audio"]
        assert de_uno[1]["texto"] == "quiero 5 fardos"  # la transcripción, para el pie
        assert len(await media_chat.listar("18093334444")) == 1
        assert await media_chat.listar("18095556666") == []

    @pytest.mark.asyncio
    async def test_un_archivo_gigante_no_se_guarda(self, fake):
        from app.panel import media_chat

        enorme = b"z" * (media_chat.MAX_BYTES + 1)
        assert await media_chat.guardar("1809", enorme, "image/jpeg", "imagen") == ""
        assert await media_chat.listar("1809") == []

    @pytest.mark.asyncio
    async def test_si_el_archivo_ya_no_esta_no_se_lista(self, fake):
        from app.panel import media_chat

        token = await media_chat.guardar("1809", JPG, "image/jpeg", "imagen")
        media_chat._ruta(token).unlink()  # el TTL del disco venció antes que el índice
        assert await media_chat.listar("1809") == []

    def test_un_token_con_path_traversal_no_lee_fuera(self, fake):
        from app.panel import media_chat

        assert media_chat.leer("../../etc/passwd") is None
        assert media_chat.leer("") is None
        assert media_chat.leer("noexiste.jpg") is None

    @pytest.mark.asyncio
    async def test_limpiar_viejos_borra_lo_vencido_y_deja_lo_nuevo(self, fake):
        import os
        import time

        from app.panel import media_chat

        nuevo = await media_chat.guardar("1809", JPG, "image/jpeg", "imagen")
        viejo = await media_chat.guardar("1809", JPG, "image/jpeg", "imagen")
        antiguo = time.time() - media_chat.TTL_SEGUNDOS - 60
        os.utime(media_chat._ruta(viejo), (antiguo, antiguo))

        assert media_chat.limpiar_viejos() == 1
        assert media_chat.leer(nuevo) is not None
        assert media_chat.leer(viejo) is None


class TestEndpointMedia:
    def test_sirve_el_archivo(self, cliente, fake):
        import asyncio

        from app.panel import media_chat

        token = asyncio.run(media_chat.guardar("1809", JPG, "image/jpeg", "imagen"))
        r = cliente.get(f"/panel/api/media/{token}", headers={"X-Panel-Token": ""})
        assert r.status_code == 200
        assert r.content == JPG
        assert r.headers["content-type"].startswith("image/jpeg")

    def test_inexistente_da_404(self, cliente):
        assert cliente.get("/panel/api/media/nada.jpg").status_code == 404

    def test_exige_token(self, cliente, monkeypatch):
        """Son fotos de clientes y comprobantes de pago: NO se publican sin auth."""
        from app.settings import settings

        monkeypatch.setattr(settings, "panel_token", "secreto")
        assert cliente.get("/panel/api/media/algo.jpg").status_code == 401

    def test_el_hilo_incluye_la_media(self, cliente, fake):
        import asyncio

        from app.panel import media_chat

        asyncio.run(media_chat.guardar("1809", OGG, "audio/ogg", "audio", "hola"))
        r = cliente.get("/panel/api/chats/1809")
        assert r.status_code == 200
        media = r.json()["media"]
        assert len(media) == 1 and media[0]["tipo"] == "audio"
        assert media[0]["texto"] == "hola"
