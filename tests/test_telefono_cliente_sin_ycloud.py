"""El teléfono que da el cliente por chat debe llegar al aviso del supervisor.

Reporte real: en un chat sin el teléfono de YCloud (llega de un anuncio, chat_id
es un ID interno como "DO.1378138860957512"), el bot le pide el número al cliente
para crear el pedido, el cliente lo da (visible en el hilo del panel), pero la
plantilla que le llega al supervisor mostró igual el ID de YCloud en vez del
teléfono real. Causa: `crear_contacto` guardaba el teléfono en Odoo pero nunca lo
dejaba en `ConversationContext.telefono`, así que `_avisar_aprobacion`
(app/pipeline.py) caía a `ctx.telefono or ctx.chat_id` y usaba el chat_id.
"""
from __future__ import annotations

import pytest

from app.business_config import BusinessConfig
from app.context import ConversationContext
from app.tools.odoo_tools import actualizar_contacto_impl, crear_contacto_impl


def _ctx(**kw) -> ConversationContext:
    base = dict(
        chat_id="DO.1378138860957512", telefono=None, user_name="Bexy",
        emisor="18099221092", destino={"to": "DO.1378138860957512"}, cfg=BusinessConfig(),
    )
    base.update(kw)
    return ConversationContext(**base)


@pytest.fixture(autouse=True)
def _sin_odoo(monkeypatch):
    import app.tools.odoo_tools as odoo_tools

    async def _create(modelo, valores):
        return 555

    async def _write(modelo, id_, valores):
        return True

    monkeypatch.setattr(odoo_tools.odoo, "create", _create)
    monkeypatch.setattr(odoo_tools.odoo, "write", _write)


class TestCrearContactoGuardaElTelefonoEnElContexto:
    async def test_sin_telefono_de_ycloud_usa_el_que_dio_el_cliente(self):
        ctx = _ctx(telefono=None)
        salida = await crear_contacto_impl(ctx, nombre="Bexy Familia", telefono="8293837395")
        assert salida.startswith("OK")
        assert ctx.telefono == "8293837395"

    async def test_con_telefono_de_ycloud_no_lo_pisa(self):
        """Si YCloud SÍ trae el número, el que el cliente teclee no lo reemplaza."""
        ctx = _ctx(telefono="18091112222")
        await crear_contacto_impl(ctx, nombre="Bexy Familia", telefono="8293837395")
        assert ctx.telefono == "18091112222"

    async def test_sin_ninguno_de_los_dos_queda_vacio(self):
        ctx = _ctx(telefono=None)
        await crear_contacto_impl(ctx, nombre="Bexy Familia")
        assert not ctx.telefono

    async def test_nombre_invalido_no_crea_ni_toca_el_telefono(self):
        ctx = _ctx(telefono=None)
        salida = await crear_contacto_impl(ctx, nombre="Bexy", telefono="8293837395")
        assert salida.startswith("ERROR")
        assert ctx.telefono is None


class TestActualizarContactoGuardaElTelefonoEnElContexto:
    async def test_al_corregir_el_telefono_queda_en_el_contexto(self):
        ctx = _ctx(telefono=None, partner_id=42)
        salida = await actualizar_contacto_impl(ctx, telefono="8293837395")
        assert salida.startswith("OK")
        assert ctx.telefono == "8293837395"

    async def test_actualizar_otro_campo_no_toca_el_telefono(self):
        ctx = _ctx(telefono="18091112222", partner_id=42)
        await actualizar_contacto_impl(ctx, calle="Calle Nueva #5")
        assert ctx.telefono == "18091112222"

    async def test_sin_partner_id_no_actualiza_nada(self):
        ctx = _ctx(telefono=None, partner_id=None)
        salida = await actualizar_contacto_impl(ctx, telefono="8293837395")
        assert salida.startswith("ERROR")
        assert ctx.telefono is None


class TestElAvisoAlSupervisorUsaElTelefonoReal:
    """El caso completo: sin esto, `telefono=ctx.telefono or ctx.chat_id`
    (app/pipeline.py:_avisar_aprobacion) manda el ID de YCloud."""

    async def test_despues_de_crear_contacto_el_fallback_ya_no_hace_falta(self):
        ctx = _ctx(telefono=None)
        await crear_contacto_impl(ctx, nombre="Bexy Familia", telefono="8293837395")
        # Esto es exactamente lo que hace _avisar_aprobacion.
        telefono_para_supervisor = ctx.telefono or ctx.chat_id
        assert telefono_para_supervisor == "8293837395"
        assert telefono_para_supervisor != ctx.chat_id
