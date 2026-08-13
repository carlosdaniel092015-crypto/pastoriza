"""Tests de `_efectos`: el lado PRODUCTIVO del invariante order_id.

`_sanear`/`_resolver_fotos` (la mitad defensiva) ya estaban cubiertos en
test_seguridad. `_efectos` —que dispara avisos, adjunta comprobante y marca la
cola de revisión según order_id— no tenía ningún test. Un cambio que rompa la
detección de `comprobante_sin_pedido` haría que un pago recibido sin pedido pase
inadvertido (dinero sin orden). Esta es la red.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

from app import pipeline
from app.agents import RespuestaBot
from app.business_config import BusinessConfig
from app.context import ConversationContext
from app.models import InboundMessage


def ctx_nuevo(**kw) -> ConversationContext:
    base = dict(
        chat_id="18090000000",
        telefono="18090000000",
        user_name="Test",
        emisor="test",
        destino={"to": "18090000000"},
        cfg=BusinessConfig(),
    )
    base.update(kw)
    return ConversationContext(**base)


def _mockear(monkeypatch):
    yc, pe, enc = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(pipeline, "ycloud", yc)
    monkeypatch.setattr(pipeline, "panel_events", pe)
    monkeypatch.setattr(pipeline, "encolar_revision", enc)
    return yc, pe, enc


async def test_comprobante_sin_pedido_marca_revision_y_avisa(monkeypatch):
    yc, _pe, _enc = _mockear(monkeypatch)
    ctx = ctx_nuevo(es_comprobante=True, imagen_url="http://x/comprobante.jpg")
    trigger = InboundMessage(content="[foto de comprobante]")

    await pipeline._efectos(ctx, RespuestaBot(mensaje="ok"), "ok", trigger)

    assert "comprobante_sin_pedido" in ctx.motivo_revision
    yc.avisar_admin.assert_awaited()  # el equipo se entera del pago sin pedido


async def test_pedido_sin_lineas_marca_revision(monkeypatch):
    yc, _pe, _enc = _mockear(monkeypatch)
    ctx = ctx_nuevo(order_id=1234, lineas_creadas=0)
    trigger = InboundMessage(content="confirmo")

    await pipeline._efectos(ctx, RespuestaBot(mensaje="listo"), "listo", trigger)

    assert "pedido_sin_lineas" in ctx.motivo_revision
    yc.enviar_plantilla.assert_awaited()  # avisa al admin del pedido creado


async def test_pedido_con_comprobante_lo_adjunta(monkeypatch):
    yc, _pe, _enc = _mockear(monkeypatch)
    odoo_mock = AsyncMock()
    monkeypatch.setattr(pipeline, "odoo", odoo_mock)
    monkeypatch.setattr(pipeline, "descargar", AsyncMock(return_value=b"\x89PNG-datos"))
    ctx = ctx_nuevo(
        order_id=1234, lineas_creadas=1, es_comprobante=True,
        imagen_url="http://x/comprobante.png",
    )
    trigger = InboundMessage(content="confirmo")

    await pipeline._efectos(ctx, RespuestaBot(mensaje="listo"), "listo", trigger)

    odoo_mock.create.assert_awaited()  # se adjuntó el comprobante en Odoo
    modelo, valores = odoo_mock.create.await_args.args
    assert modelo == "ir.attachment"
    assert valores["res_model"] == "sale.order"
    assert valores["res_id"] == 1234


async def test_handoff_envia_plantilla_y_mensaje(monkeypatch):
    yc, _pe, _enc = _mockear(monkeypatch)
    ctx = ctx_nuevo()
    # El cliente PIDE una persona: el determinador da el visto bueno. Sin él, la
    # escalada queda bloqueada a propósito (ver el test de abajo).
    ctx.permite_escalar = True
    trigger = InboundMessage(content="quiero hablar con una persona")

    await pipeline._efectos(
        ctx, RespuestaBot(mensaje="ok", escalar=True), "ok", trigger
    )

    assert "handoff" in ctx.motivo_revision
    yc.enviar_plantilla.assert_awaited()
    yc.enviar_texto.assert_awaited()


async def test_handoff_bloqueado_si_el_determinador_no_lo_habilita(monkeypatch):
    """El modelo puede pedir escalada por su salida (sin usar la tool): ese camino
    pasa por el mismo candado. Caso real: llegó a escalar un SALUDO al supervisor."""
    yc, _pe, _enc = _mockear(monkeypatch)
    ctx = ctx_nuevo()  # permite_escalar = False (default)
    trigger = InboundMessage(content="hola buenas tardes como estas")

    await pipeline._efectos(
        ctx, RespuestaBot(mensaje="ok", escalar=True), "ok", trigger
    )

    assert "handoff" not in ctx.motivo_revision
    assert "escalada_bloqueada" in ctx.motivo_revision
    yc.enviar_plantilla.assert_not_awaited()  # el supervisor NO fue molestado
