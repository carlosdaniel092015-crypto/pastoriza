"""En ENVÍO no hay pedido sin comprobante, y el comprobante tiene que cubrir el total.

Regla del negocio, textual: "cuando es envío primero debes pedir la foto del
comprobante, debe tener el mismo monto de la factura o mayor, y lo puedes crear pero le
dices que está en revisión del supervisor. Si no envió el comprobante no lo crees hasta
que lo suba, en el caso de envío".

Va en el CÓDIGO y no en el prompt (ADR-006): el modelo ya llegó a "confirmar" pagos que
nadie mandó. En RETIRO no aplica — ahí se paga en el mostrador.
"""
from __future__ import annotations

import pytest

from app import comprobante
from app.business_config import BusinessConfig
from app.context import ConversationContext
from app.tools import odoo_tools

COMPROBANTE = (
    "COMPROBANTE_PAGO: [Banco Popular, monto RD$5,860.00, referencia 004512345, "
    "14/08/2026]"
)


# ------------------------------------------------------------ parte pura ---
class TestLeerElMonto:
    @pytest.mark.parametrize("texto,esperado", [
        ("monto RD$5,860.00", 5860.0),
        ("RD$ 5,860.00", 5860.0),
        ("RD$5860", 5860.0),
        ("transferencia por $12,500.50", 12500.5),
        ("DOP 1,000.00", 1000.0),
        ("RD$5.860", 5860.0),   # punto de miles
        ("RD$5,86", 5.86),      # coma decimal
    ])
    def test_formatos_que_manda_el_banco(self, texto, esperado):
        assert comprobante.monto_pagado(texto) == esperado

    def test_la_referencia_y_la_fecha_NO_son_el_monto(self):
        """Sin esto, un número de referencia enorme daría el pago por cubierto."""
        assert comprobante.monto_pagado(COMPROBANTE) == 5860.0

    def test_sin_marca_de_moneda_no_se_lee_nada(self):
        assert comprobante.monto_pagado("referencia 004512345 del 14/08/2026") is None
        assert comprobante.monto_pagado("") is None

    def test_toma_el_mayor_cuando_hay_varios(self):
        """El comprobante suele mostrar también el balance de la cuenta."""
        assert comprobante.monto_pagado(
            "monto RD$5,860.00 balance RD$18,204.31"
        ) == 18204.31


class TestFaltante:
    def test_pago_exacto_no_falta_nada(self):
        assert comprobante.faltante(COMPROBANTE, 5860.0) is None

    def test_pago_de_mas_tampoco(self):
        """'el mismo monto de la factura o MAYOR'."""
        assert comprobante.faltante(COMPROBANTE, 4000.0) is None

    def test_pago_corto_devuelve_lo_que_falta(self):
        assert comprobante.faltante(COMPROBANTE, 9000.0) == 3140.0

    def test_un_centavo_de_redondeo_no_es_un_pago_corto(self):
        assert comprobante.faltante("RD$5,859.99", 5860.0) is None

    def test_sin_cotizacion_no_se_bloquea(self):
        """Si no hay contra qué comparar, decide la persona que ve la foto."""
        assert comprobante.faltante(COMPROBANTE, 0) is None

    def test_si_no_se_puede_leer_el_monto_no_se_bloquea(self):
        """Un falso 'no coincide' le dice a alguien que pagó bien que no pagó."""
        assert comprobante.faltante("COMPROBANTE_PAGO: [ilegible]", 5860.0) is None


# ------------------------------------------------------ la regla dura ---
def _ctx(**kw) -> ConversationContext:
    base = dict(
        chat_id="18091112222", telefono="18091112222", user_name="Clarys",
        emisor="18099221092", destino={"to": "18091112222"}, cfg=BusinessConfig(),
        partner_id=42,
    )
    base.update(kw)
    return ConversationContext(**base)


ENVIO = {
    "modalidad": "envio", "provincia": "Santo Domingo", "municipio": "Los Alcarrizos",
    "sector": "Palmarejo", "calle": "Calle 5 #12",
}


@pytest.fixture(autouse=True)
def _sin_odoo_ni_redis(monkeypatch):
    creados: list[dict] = []

    async def _create(modelo, valores):
        creados.append({"modelo": modelo, **valores})
        return 777

    async def _sin_cotizacion(chat_id):
        return 0.0

    monkeypatch.setattr(odoo_tools.odoo, "create", _create)
    monkeypatch.setattr(odoo_tools, "leer_cotizacion", _sin_cotizacion)
    return creados


async def _crear(ctx: ConversationContext, **kw) -> str:
    return await odoo_tools.crear_pedido_impl(ctx, **kw)


class TestEnvioSinComprobante:
    async def test_NO_crea_el_pedido(self, _sin_odoo_ni_redis):
        ctx = _ctx()
        salida = await _crear(ctx, **ENVIO)
        assert salida.startswith("ERROR")
        assert ctx.order_id is None
        assert _sin_odoo_ni_redis == [], "no se tocó Odoo"

    async def test_le_dice_al_modelo_que_pida_la_foto(self):
        salida = await _crear(_ctx(), **ENVIO)
        assert "comprobante" in salida.lower() and "foto" in salida.lower()

    async def test_y_le_ofrece_el_retiro_como_salida(self):
        """Quien no quiere transferir por adelantado todavía puede comprar."""
        assert "retiro" in (await _crear(_ctx(), **ENVIO)).lower()


class TestRetiroNoPideComprobante:
    async def test_crea_igual_sin_comprobante(self, _sin_odoo_ni_redis):
        ctx = _ctx()
        salida = await _crear(ctx, modalidad="retiro")
        assert salida.startswith("OK")
        assert ctx.order_id == 777
        assert ctx.pedido_modalidad == "retiro"

    async def test_pero_el_numero_TAMPOCO_se_le_da_todavia(self):
        """El retiro también lo aprueba el supervisor: sin pago, pero con decisión."""
        ctx = _ctx()
        salida = await _crear(ctx, modalidad="retiro")
        assert "NO se lo digas al cliente" in salida
        assert "supervisor" in salida.lower()
        assert ctx.espera_aprobacion is True
        assert ctx.comprobante_url == "", "en retiro no hay comprobante"


class TestEnvioConComprobante:
    async def test_con_el_monto_cubierto_crea_el_pedido(self, _sin_odoo_ni_redis):
        ctx = _ctx(es_comprobante=True, comprobante_texto=COMPROBANTE,
                   cotizado_total=5860.0)
        salida = await _crear(ctx, **ENVIO)
        assert salida.startswith("OK")
        assert ctx.order_id == 777 and ctx.pedido_modalidad == "envio"

    async def test_pero_el_numero_NO_se_le_da_al_cliente(self):
        """El pago lo aprueba el supervisor (ADR-013): el número sale de ahí."""
        ctx = _ctx(es_comprobante=True, comprobante_texto=COMPROBANTE,
                   cotizado_total=5860.0)
        salida = await _crear(ctx, **ENVIO)
        assert "NO se lo digas al cliente" in salida
        assert "verificaci" in salida.lower() and "supervisor" in salida.lower()

    async def test_si_el_monto_no_cubre_NO_crea_el_pedido(self, _sin_odoo_ni_redis):
        ctx = _ctx(es_comprobante=True, comprobante_texto="RD$2,000.00",
                   cotizado_total=5860.0)
        salida = await _crear(ctx, **ENVIO)
        assert salida.startswith("ERROR") and "3,860.00" in salida
        assert ctx.order_id is None
        assert _sin_odoo_ni_redis == []
        assert "comprobante_monto_corto" in ctx.motivo_revision

    async def test_el_total_cotizado_sobrevive_al_turno(self, monkeypatch):
        """La cotización pasó ANTES; en el turno del comprobante el ctx viene en cero."""
        async def _cotizacion_previa(chat_id):
            return 9000.0

        monkeypatch.setattr(odoo_tools, "leer_cotizacion", _cotizacion_previa)
        ctx = _ctx(es_comprobante=True, comprobante_texto=COMPROBANTE)
        salida = await _crear(ctx, **ENVIO)
        assert salida.startswith("ERROR") and "3,140.00" in salida

    async def test_si_no_hay_cotizacion_no_bloquea(self, _sin_odoo_ni_redis):
        """No se le cierra la puerta a un pago por no tener con qué compararlo."""
        ctx = _ctx(es_comprobante=True, comprobante_texto=COMPROBANTE)
        assert (await _crear(ctx, **ENVIO)).startswith("OK")

    async def test_la_direccion_sigue_siendo_obligatoria(self, _sin_odoo_ni_redis):
        ctx = _ctx(es_comprobante=True, comprobante_texto=COMPROBANTE)
        salida = await _crear(ctx, modalidad="envio", provincia="Santo Domingo")
        assert salida.startswith("ERROR") and "sector" in salida
        assert _sin_odoo_ni_redis == []


class TestLoQueRecibeElCliente:
    """El monto que falta lo dice el CÓDIGO, no el modelo: es un dato, no una frase."""

    def _sanear(self, mensaje: str, **kw) -> tuple[str, ConversationContext]:
        from app.pipeline import _sanear

        ctx = _ctx(es_comprobante=True, **kw)
        return _sanear(mensaje, ctx), ctx

    def test_le_dice_cuanto_falta_con_el_monto_exacto(self):
        salida, _ = self._sanear("lo que sea", comprobante_faltante=3860.0)
        assert "3,860.00" in salida

    def test_y_NO_le_pide_la_foto_que_acaba_de_mandar(self):
        """Sin esto, 'recibí tu comprobante...' cae en la regla anti-claim y el
        cliente que SÍ mandó el comprobante recibe 'mándame el comprobante'."""
        salida, _ = self._sanear(
            "Recibi tu comprobante, pero faltan RD$3,860.00.",
            comprobante_faltante=3860.0,
        )
        assert "3,860.00" in salida
        assert "Me la envias" not in salida

    def test_el_mensaje_es_editable_desde_el_panel(self):
        salida, _ = self._sanear(
            "x", comprobante_faltante=500.0,
            cfg=BusinessConfig(msg_monto_corto="Faltan RD${falta} para completar."),
        )
        assert salida == "Faltan RD$500.00 para completar."

    def test_si_alguien_rompe_la_llave_del_mensaje_igual_sale_el_monto(self):
        salida, _ = self._sanear(
            "x", comprobante_faltante=500.0,
            cfg=BusinessConfig(msg_monto_corto="Falta plata {loquesea}"),
        )
        assert "500.00" in salida

    def test_con_el_pago_completo_sigue_el_aviso_de_verificacion(self):
        salida, ctx = self._sanear(
            "Tu pedido 777 quedo registrado", order_id=777, espera_aprobacion=True,
            comprobante_url="http://x/c.jpg",
        )
        assert salida == ctx.cfg.msg_comprobante
        assert "777" not in salida


class TestElComprobanteSobreviveAlTurno:
    """Caso real: manda la foto ANTES de dar la dirección.

    El pedido no se puede crear todavía (falta la dirección). En el turno siguiente,
    cuando por fin la da, el comprobante YA NO está en el contexto del turno. Sin
    memoria, el bot le vuelve a pedir la foto que acaba de mandar — y otra vez, y otra.
    """

    @pytest.fixture
    def guardado(self, monkeypatch):
        estado: dict = {"comp": {"url": "https://ycloud/c.jpg", "texto": COMPROBANTE}}

        async def _leer(chat_id):
            return dict(estado["comp"])

        async def _consumir(chat_id):
            estado["comp"] = {}

        monkeypatch.setattr(odoo_tools, "leer_comprobante", _leer)
        monkeypatch.setattr(odoo_tools, "consumir_comprobante", _consumir)
        return estado

    async def test_el_pedido_se_crea_con_el_comprobante_del_turno_anterior(
        self, guardado
    ):
        ctx = _ctx()  # este turno NO trae imagen
        salida = await _crear(ctx, **ENVIO)
        assert salida.startswith("OK"), salida
        assert ctx.order_id == 777

    async def test_y_el_pago_queda_marcado_para_que_lo_apruebe_el_supervisor(
        self, guardado
    ):
        """Es `espera_aprobacion` —no `es_comprobante`— lo que dispara el aviso."""
        ctx = _ctx()
        await _crear(ctx, **ENVIO)
        assert ctx.espera_aprobacion is True
        assert ctx.comprobante_url == "https://ycloud/c.jpg"

    async def test_el_monto_tambien_se_revisa_contra_el_guardado(self, guardado):
        ctx = _ctx(cotizado_total=9000.0)
        salida = await _crear(ctx, **ENVIO)
        assert salida.startswith("ERROR") and "3,140.00" in salida
        assert ctx.order_id is None

    async def test_un_comprobante_respalda_UN_pedido_no_dos(self, guardado):
        """Si no se consumiera, una foto habilitaría pedidos toda la noche."""
        ctx1 = _ctx()
        assert (await _crear(ctx1, **ENVIO)).startswith("OK")
        assert guardado["comp"] == {}, "el comprobante se consumió"

        ctx2 = _ctx()  # otro pedido, sin mandar nada nuevo
        assert (await _crear(ctx2, **ENVIO)).startswith("ERROR")
        assert ctx2.order_id is None

    async def test_el_de_ESTE_turno_manda_sobre_el_guardado(self, guardado):
        ctx = _ctx(es_comprobante=True, comprobante_texto="RD$9,999.00",
                   imagen_url="https://ycloud/nuevo.jpg", cotizado_total=5860.0)
        await _crear(ctx, **ENVIO)
        assert ctx.comprobante_url == "https://ycloud/nuevo.jpg"


class TestUnPagoNoCreaOtroPedido:
    """El escenario REAL que se rompió, con sus números.

    El viernes se creó el pedido S00163 con su línea (300 x BOTELLA LISA ECO 8 OZ,
    total RD$1,761.00 + RD$550 de envío = RD$2,311.00 cotizados). El lunes el cliente
    transfirió, el modelo volvió a llamar `crear_pedido` y quedó un SEGUNDO pedido
    (S00166) VACÍO, en RD$0.00, con el comprobante adjunto al vacío. El supervisor
    recibía un pago de RD$2,311 contra un pedido de RD$0.00.
    """

    LINEAS_ODOO = [
        {"name": "[BLISA8] BOTELLA LISA ECO. 8 OZ (INCLUYE TAPAS)",
         "product_uom_qty": 300.0, "price_unit": 5.87, "price_total": 1761.0},
    ]
    COMP_2311 = "COMPROBANTE_PAGO: [Banreservas, monto RD$2,311.00, ref 998877, 17/08]"

    @pytest.fixture
    def viernes(self, monkeypatch):
        """Hay un pedido abierto (162) de hace tres días, con sus líneas en Odoo."""
        estado = {"abierto": {"order_id": 162, "modalidad": "envio",
                              "direccion": "Calle Isabel Aguiar #240, Herrera"},
                  "consumido": False}

        async def _leer_abierto(chat_id):
            return dict(estado["abierto"])

        async def _consumir(chat_id):
            estado["consumido"] = True

        async def _search_read(modelo, dominio, campos, limit=80, **kw):
            assert modelo == "sale.order.line"
            assert dominio == [["order_id", "=", 162]]
            return list(self.LINEAS_ODOO)

        async def _cotizado(chat_id):
            return 2311.0

        monkeypatch.setattr(odoo_tools, "leer_pedido_abierto", _leer_abierto)
        monkeypatch.setattr(odoo_tools, "consumir_comprobante", _consumir)
        monkeypatch.setattr(odoo_tools, "leer_cotizacion", _cotizado)
        monkeypatch.setattr(odoo_tools.odoo, "search_read", _search_read)
        return estado

    async def test_el_pago_va_al_pedido_QUE_YA_EXISTIA(self, viernes, _sin_odoo_ni_redis):
        ctx = _ctx(es_comprobante=True, comprobante_texto=self.COMP_2311,
                   imagen_url="https://ycloud/comp.jpg")
        salida = await _crear(ctx, **ENVIO)

        assert salida.startswith("OK"), salida
        assert ctx.order_id == 162, "es el pedido del viernes, no uno nuevo"
        assert _sin_odoo_ni_redis == [], "NO se creó un segundo sale.order"

    async def test_y_el_aviso_lleva_las_lineas_reales_del_pedido(self, viernes):
        """Sin esto el supervisor decide sobre un pago viendo '(sin líneas cargadas)'."""
        ctx = _ctx(es_comprobante=True, comprobante_texto=self.COMP_2311,
                   imagen_url="https://ycloud/comp.jpg")
        await _crear(ctx, **ENVIO)

        assert ctx.lineas_creadas == 1
        assert ctx.lineas[0]["cantidad"] == 300
        assert "BOTELLA LISA ECO. 8 OZ" in ctx.lineas[0]["nombre"]
        assert ctx.lineas[0]["total"] == 1761.0
        assert ctx.espera_aprobacion is True
        assert ctx.comprobante_url == "https://ycloud/comp.jpg"
        assert ctx.direccion_entrega.startswith("Calle Isabel Aguiar")

    async def test_le_dice_al_modelo_que_NO_agregue_las_lineas_de_nuevo(self, viernes):
        """Si las agregara otra vez, el pedido quedaría con el doble de mercancía."""
        ctx = _ctx(es_comprobante=True, comprobante_texto=self.COMP_2311)
        salida = await _crear(ctx, **ENVIO)
        assert "NO crees otro pedido" in salida
        assert "NO agregues líneas" in salida

    async def test_el_comprobante_se_consume(self, viernes):
        ctx = _ctx(es_comprobante=True, comprobante_texto=self.COMP_2311)
        await _crear(ctx, **ENVIO)
        assert viernes["consumido"] is True

    async def test_un_pago_corto_no_se_aplica_al_pedido_viejo(
        self, viernes, _sin_odoo_ni_redis
    ):
        ctx = _ctx(es_comprobante=True, comprobante_texto="RD$500.00")
        salida = await _crear(ctx, **ENVIO)
        assert salida.startswith("ERROR") and "1,811.00" in salida
        assert ctx.order_id is None, "no queda marcado como pagado"
        assert ctx.espera_aprobacion is False
        assert _sin_odoo_ni_redis == []

    async def test_sin_comprobante_NO_se_adopta_nada(self, viernes):
        """Adoptar sólo aplica a un PAGO. Sin pago, en envío no hay pedido."""
        ctx = _ctx()
        salida = await _crear(ctx, **ENVIO)
        assert salida.startswith("ERROR") and "comprobante" in salida.lower()
        assert ctx.order_id is None

    async def test_si_Odoo_no_da_las_lineas_el_pago_igual_se_aplica(
        self, viernes, monkeypatch
    ):
        """Perder el detalle es malo; perder el pago es peor. Queda para revisión."""
        async def _explota(*a, **kw):
            raise RuntimeError("odoo caido")

        monkeypatch.setattr(odoo_tools.odoo, "search_read", _explota)
        ctx = _ctx(es_comprobante=True, comprobante_texto=self.COMP_2311)
        salida = await _crear(ctx, **ENVIO)
        assert salida.startswith("OK") and ctx.order_id == 162
        assert "pedido_sin_lineas" in ctx.motivo_revision


class TestElPedidoQuedaAbierto:
    async def test_al_crearlo_se_registra_para_el_pago_que_venga_despues(
        self, monkeypatch, _sin_odoo_ni_redis
    ):
        guardados: list[tuple] = []

        async def _guardar(chat_id, order_id, modalidad="", direccion=""):
            guardados.append((chat_id, order_id, modalidad, direccion))

        async def _sin_abierto(chat_id):
            return {}

        monkeypatch.setattr(odoo_tools, "guardar_pedido_abierto", _guardar)
        monkeypatch.setattr(odoo_tools, "leer_pedido_abierto", _sin_abierto)
        ctx = _ctx()
        await _crear(ctx, modalidad="retiro")

        assert guardados == [(ctx.chat_id, 777, "retiro", "Retiro en tienda")]
