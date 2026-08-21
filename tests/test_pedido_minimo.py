"""Pedido mínimo (regla de negocio): RD$1000 de SUBTOTAL, siempre informado.

Reporte real: el bot cotizó 19 potes de 16 oz (RD$271.64 de subtotal, muy por debajo
del mínimo) y pasó directo a "cómo deseas pagar" sin avisar del mínimo. La regla ya
estaba en el prompt ("NO cierres un pedido por debajo del mínimo"), pero sólo ahí: el
modelo no la respetó. Va en el código, no sólo en el prompt (mismo criterio que el
comprobante, ADR-006): `cotizar` deja el aviso escrito en lo que el modelo tiene que
mostrar, y `crear_pedido` bloquea la creación si el subtotal no llega.
"""
from __future__ import annotations

import pytest

from app.business_config import BusinessConfig
from app.context import ConversationContext
from app.tools import odoo_tools
from app.tools.cotizar_tools import cotizar_impl


def _ctx(**kw) -> ConversationContext:
    base = dict(
        chat_id="18091112222", telefono="18091112222", user_name="Maria",
        emisor="18099221092", destino={"to": "18091112222"}, cfg=BusinessConfig(),
        partner_id=42,
    )
    base.update(kw)
    return ConversationContext(**base)


class TestCotizarAvisaElMinimo:
    async def test_por_debajo_del_minimo_incluye_el_aviso(self):
        ctx = _ctx()
        salida = await cotizar_impl(ctx, precio_unitario=16.87, cantidad=19, modalidad="envio")
        assert "AVISO" in salida
        assert "pedido minimo es RD$1,000.00" in salida
        assert "271.64" in salida  # el subtotal real que no llega

    async def test_por_encima_del_minimo_no_lo_incluye(self):
        ctx = _ctx()
        salida = await cotizar_impl(ctx, precio_unitario=16.87, cantidad=500, modalidad="envio")
        assert "AVISO" not in salida

    async def test_guarda_el_subtotal_en_el_contexto(self):
        ctx = _ctx()
        await cotizar_impl(ctx, precio_unitario=16.87, cantidad=19, modalidad="envio")
        assert ctx.cotizado_subtotal == pytest.approx(271.64, abs=0.01)

    async def test_minimo_en_cero_desactiva_el_aviso(self):
        """Config vacía/rota: no se avisa de un mínimo que no existe (fail open)."""
        ctx = _ctx(cfg=BusinessConfig(monto_minimo="0"))
        salida = await cotizar_impl(ctx, precio_unitario=16.87, cantidad=19, modalidad="envio")
        assert "AVISO" not in salida


ENVIO = {
    "modalidad": "envio", "provincia": "Santo Domingo", "municipio": "Los Alcarrizos",
    "sector": "Palmarejo", "calle": "Calle 5 #12",
}
COMPROBANTE = "COMPROBANTE_PAGO: [Banco Popular, monto RD$300.00, referencia 1, 21/08/2026]"


@pytest.fixture(autouse=True)
def _sin_odoo_ni_redis(monkeypatch):
    creados: list[dict] = []

    async def _create(modelo, valores):
        creados.append({"modelo": modelo, **valores})
        return 777

    async def _sin_cotizacion(chat_id):
        return 0.0

    async def _sin_pedido_abierto(chat_id):
        return {}

    monkeypatch.setattr(odoo_tools.odoo, "create", _create)
    monkeypatch.setattr(odoo_tools, "leer_cotizacion", _sin_cotizacion)
    monkeypatch.setattr(odoo_tools, "leer_cotizacion_subtotal", _sin_cotizacion)
    monkeypatch.setattr(odoo_tools, "leer_pedido_abierto", _sin_pedido_abierto)
    return creados


async def _crear(ctx: ConversationContext, **kw) -> str:
    return await odoo_tools.crear_pedido_impl(ctx, **kw)


class TestCrearPedidoBloqueaPorDebajoDelMinimo:
    async def test_no_crea_el_pedido_si_el_subtotal_no_llega(self, _sin_odoo_ni_redis):
        ctx = _ctx(cotizado_subtotal=271.64, es_comprobante=True, comprobante_texto=COMPROBANTE)
        salida = await _crear(ctx, **ENVIO)
        assert salida.startswith("ERROR")
        assert "1,000.00" in salida and "271.64" in salida
        assert ctx.order_id is None
        assert _sin_odoo_ni_redis == []

    async def test_le_dice_al_modelo_que_ofrezca_sumar_unidades(self):
        ctx = _ctx(cotizado_subtotal=271.64)
        salida = await _crear(ctx, **ENVIO)
        assert "sumar" in salida.lower()

    async def test_aplica_tambien_a_retiro(self, _sin_odoo_ni_redis):
        """El mínimo no es sólo para envío: aplica al pedido en general."""
        ctx = _ctx(cotizado_subtotal=271.64)
        salida = await _crear(ctx, modalidad="retiro")
        assert salida.startswith("ERROR")
        assert ctx.order_id is None

    async def test_si_llega_al_minimo_crea_normal(self, _sin_odoo_ni_redis):
        ctx = _ctx(cotizado_subtotal=1000.0)
        salida = await _crear(ctx, modalidad="retiro")
        assert salida.startswith("OK")
        assert ctx.order_id == 777

    async def test_el_subtotal_de_un_turno_anterior_tambien_bloquea(self, monkeypatch):
        """La cotización pasó ANTES; en el turno del comprobante el ctx viene en cero
        (mismo criterio que `leer_cotizacion` para el total, ver test_comprobante_envio)."""
        async def _subtotal_previo(chat_id):
            return 271.64

        monkeypatch.setattr(odoo_tools, "leer_cotizacion_subtotal", _subtotal_previo)
        ctx = _ctx(es_comprobante=True, comprobante_texto=COMPROBANTE)
        salida = await _crear(ctx, **ENVIO)
        assert salida.startswith("ERROR") and "271.64" in salida
        assert ctx.order_id is None

    async def test_sin_ninguna_cotizacion_no_bloquea(self, _sin_odoo_ni_redis):
        """No hay con qué comparar: no se le cierra la puerta a nadie por eso (mismo
        criterio que el comprobante sin cotización)."""
        ctx = _ctx()
        salida = await _crear(ctx, modalidad="retiro")
        assert salida.startswith("OK")

    async def test_no_interfiere_con_adoptar_un_pedido_abierto(self, monkeypatch):
        """Un pago que se aplica a un pedido YA existente no se re-valida contra el
        mínimo: ese pedido ya pasó por esta regla cuando se creó."""
        async def _abierto(chat_id):
            return {"order_id": 162, "modalidad": "envio", "direccion": "Calle X"}

        async def _search_read(modelo, dominio, campos, limit=80, **kw):
            return []

        monkeypatch.setattr(odoo_tools, "leer_pedido_abierto", _abierto)
        monkeypatch.setattr(odoo_tools.odoo, "search_read", _search_read)
        ctx = _ctx(
            cotizado_subtotal=271.64, es_comprobante=True,
            comprobante_texto="COMPROBANTE_PAGO: [Banreservas, monto RD$300.00, ref 1, 21/08]",
        )
        salida = await _crear(ctx, **ENVIO)
        assert salida.startswith("OK")
        assert ctx.order_id == 162
