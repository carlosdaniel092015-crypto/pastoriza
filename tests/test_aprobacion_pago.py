"""El bot NO aprueba pagos: sólo el supervisor.

Regla del negocio, textual: "el bot sigue creando el pedido y adjuntando el
comprobante; cuando el cliente envía el comprobante le dice que estamos verificando
el pago y que en un momento nuestro supervisor estará contactando. Cuando el
supervisor aprueba, el bot le dice que ya fue verificado y aceptado, le dice cuál es
el número y que fue registrado exitosamente".

Lo que se protege acá es la frontera: el cliente NUNCA debe recibir una confirmación
de pago que ninguna persona miró.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.business_config import BusinessConfig
from app.context import ConversationContext
from app.pipeline import _sanear
from tests.fake_redis import FakeRedis

A = "18099221092"
CA = "8099221092"


def _ctx(**kw) -> ConversationContext:
    base = dict(
        chat_id="18091112222", telefono="18091112222", user_name="Clarys",
        emisor=A, destino={"to": "18091112222"}, cfg=BusinessConfig(),
    )
    base.update(kw)
    return ConversationContext(**base)


class TestElBotNoConfirmaElPago:
    def test_con_comprobante_y_pedido_avisa_que_se_esta_verificando(self):
        ctx = _ctx(es_comprobante=True, order_id=160, espera_aprobacion=True,
                   comprobante_url="http://x/c.jpg")
        salida = _sanear(
            "¡Listo! Recibí tu comprobante. Tu pedido 160 quedó registrado con éxito.",
            ctx,
        )
        assert salida == ctx.cfg.msg_comprobante
        assert "verificando" in salida.lower()
        # Y no se le filtra el número ni un "registrado".
        assert "160" not in salida
        assert "registrado" not in salida.lower()

    def test_el_mensaje_es_editable_por_canal(self):
        ctx = _ctx(es_comprobante=True, order_id=9, espera_aprobacion=True,
                   comprobante_url="http://x/c.jpg",
                   cfg=BusinessConfig(msg_comprobante="Estamos chequeando tu pago."))
        assert _sanear("Tu pedido quedó confirmado", ctx) == "Estamos chequeando tu pago."

    def test_sin_comprobante_no_cambia_nada(self):
        ctx = _ctx(order_id=160)
        assert _sanear("Tu pedido 160 quedó registrado.", ctx) == (
            "Tu pedido 160 quedó registrado."
        )

    def test_comprobante_sin_pedido_sigue_pidiendo_el_comprobante(self):
        """Si no se creó el pedido, no se puede decir que se recibió el pago."""
        ctx = _ctx(es_comprobante=True)
        salida = _sanear("Recibí tu comprobante, tu pedido quedó registrado.", ctx)
        assert "comprobante" in salida.lower() and "160" not in salida
        assert "claim_pedido_sin_order_id" in ctx.motivo_revision


@pytest.fixture
def cliente(monkeypatch):
    import app.redis_client as rc
    from app import business_config as bc
    from app.main import app
    from app.panel import events, router as pr
    from app.ycloud import ycloud

    fake = FakeRedis()
    monkeypatch.setattr(rc, "_pool", fake)
    monkeypatch.setattr(pr, "_cache_scan", None)
    pr._cache_ultimo.clear()
    fake.kv[bc.CONFIG_KEY] = json.dumps({"canales": f"{A} = Tienda"})
    bc.invalidar()
    bc._ultima_buena.clear()

    enviados: list[tuple] = []

    async def _enviar(destino, emisor, texto, simular_tipeo=True):
        enviados.append((destino, emisor, texto))
        return True

    monkeypatch.setattr(ycloud, "enviar_texto", _enviar)

    fake.hashes.setdefault(events.CHATMETA_KEY, {})["18091112222"] = json.dumps({
        "chat_id": "18091112222", "emisor": A, "user_name": "Clarys",
        "destino": {"to": "18091112222"}, "ultimo": "(comprobante)",
        "ultimo_de": "cliente", "ultimo_ts": 1000,
        "aprobacion": {"estado": "pendiente", "order_id": 160},
    })
    with TestClient(app) as c:
        c.enviados = enviados  # type: ignore[attr-defined]
        yield c
    bc.invalidar()
    rc._pool = None


class TestAprobacionDesdeElPanel:
    def test_aprobar_le_confirma_al_cliente_con_el_numero(self, cliente):
        r = cliente.post("/panel/api/chats/18091112222/aprobar-pago")
        assert r.status_code == 200, r.text
        assert r.json()["order_id"] == 160

        assert len(cliente.enviados) == 1
        texto = cliente.enviados[0][2]
        assert "160" in texto
        assert "verificado" in texto.lower() and "aceptado" in texto.lower()

        fila = cliente.get("/panel/api/chats").json()["chats"][0]
        assert fila["aprobacion"] == "aprobado"
        # El mensaje queda como lo último de la conversación, dicho por el BOT.
        assert fila["ultimo_de"] == "bot" and "160" in fila["ultimo"]

    def test_aprobar_no_pausa_el_bot(self, cliente):
        """Es un mensaje DEL BOT, no una toma de control del chat por un humano."""
        cliente.post("/panel/api/chats/18091112222/aprobar-pago")
        assert cliente.get("/panel/api/chats").json()["chats"][0]["pausado"] is False

    def test_aprobar_dos_veces_no_le_escribe_dos_veces_al_cliente(self, cliente):
        cliente.post("/panel/api/chats/18091112222/aprobar-pago")
        r = cliente.post("/panel/api/chats/18091112222/aprobar-pago")
        assert r.status_code == 200 and r.json()["ya_estaba"] is True
        assert len(cliente.enviados) == 1

    def test_si_no_hay_pago_pendiente_no_hace_nada(self, cliente):
        import json as _json

        from app.panel import events

        import app.redis_client as rc
        rc._pool.hashes[events.CHATMETA_KEY]["18093334444"] = _json.dumps(
            {"chat_id": "18093334444", "emisor": A, "ultimo": "hola"}
        )
        r = cliente.post("/panel/api/chats/18093334444/aprobar-pago")
        assert r.status_code == 400
        assert not cliente.enviados

    def test_si_el_envio_falla_el_pago_NO_queda_aprobado(self, cliente, monkeypatch):
        """Si el cliente no recibió nada, quedaría creyendo que sigue en verificación."""
        from app.ycloud import ycloud

        async def _falla(*a, **kw):
            return False

        monkeypatch.setattr(ycloud, "enviar_texto", _falla)
        r = cliente.post("/panel/api/chats/18091112222/aprobar-pago")
        assert r.status_code == 502
        assert cliente.get("/panel/api/chats").json()["chats"][0]["aprobacion"] == (
            "pendiente"
        )

    def test_rechazar_SI_le_avisa_al_cliente(self, cliente):
        """El cliente pidió y esperó: no puede quedarse en silencio para siempre."""
        r = cliente.post("/panel/api/chats/18091112222/rechazar-pago",
                         json={"motivo": "el monto no coincide"})
        assert r.status_code == 200 and r.json()["enviado"] is True
        assert len(cliente.enviados) == 1
        assert cliente.get("/panel/api/chats").json()["chats"][0]["aprobacion"] == (
            "rechazado"
        )

    def test_pero_NO_le_dice_el_motivo(self, cliente):
        """El motivo real lo explica una persona: el bot no acusa a nadie."""
        cliente.post("/panel/api/chats/18091112222/rechazar-pago",
                     json={"motivo": "el comprobante es falso"})
        texto = cliente.enviados[0][2]
        assert "falso" not in texto.lower()
        assert "829" in texto, "tiene que darle a dónde escribir"

    def test_si_no_se_le_pudo_avisar_igual_queda_rechazado(self, cliente, monkeypatch):
        """El estado es lo que ve el supervisor: perderlo lo dejaría creyendo que sigue
        pendiente después de que él ya decidió. Pero se marca que nadie le avisó."""
        from app.ycloud import ycloud

        async def _falla(*a, **kw):
            return False

        monkeypatch.setattr(ycloud, "enviar_texto", _falla)
        r = cliente.post("/panel/api/chats/18091112222/rechazar-pago", json={})
        assert r.status_code == 200 and r.json()["enviado"] is False
        assert cliente.get("/panel/api/chats").json()["chats"][0]["aprobacion"] == (
            "rechazado"
        )
        revision = cliente.get("/panel/api/revision").json()
        motivos = [m for it in revision.get("items", []) for m in it.get("motivos", [])]
        assert "cliente_sin_aviso" in motivos

    def test_el_pago_pendiente_sobrevive_a_que_el_asesor_responda(self, cliente):
        """Responder desde el panel reescribe el índice: no debe borrar el pendiente."""
        import asyncio

        from app.panel import events

        asyncio.get_event_loop()  # el TestClient ya corre su loop
        r = cliente.post("/panel/api/chats/18091112222/responder",
                         json={"texto": "ya te confirmo"})
        assert r.status_code == 200
        fila = cliente.get("/panel/api/chats").json()["chats"][0]
        assert fila["aprobacion"] == "pendiente" and fila["order_id"] == 160
