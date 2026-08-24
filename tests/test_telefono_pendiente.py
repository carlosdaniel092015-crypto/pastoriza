"""Sin un teléfono real, se le pide al cliente y el aviso al supervisor sale recién
cuando lo da — para el handoff (asistencia) Y para crear_pedido (regla dura).

Caso real: "Génesis Akemy" escribió desde Instagram (chat_id interno de YCloud, sin
número), pidió asistencia y el aviso al supervisor le mandó ese ID como "teléfono".
Ahora, sin teléfono, primero se le pide al cliente (ver test_efectos.py); este archivo
cubre el otro lado: qué pasa cuando el cliente SÍ responde con su número.
"""
from __future__ import annotations

import pytest

from app.business_config import BusinessConfig
from app.context import ConversationContext
from app.tools.odoo_tools import crear_pedido_impl


@pytest.fixture
def fake(monkeypatch):
    import app.redis_client as rc

    from tests.fake_redis import FakeRedis

    f = FakeRedis()
    monkeypatch.setattr(rc, "_pool", f)
    return f


from app.pipeline import _extraer_telefono_libre


class TestEstadoTelefonoPendiente:
    """`app.estado.pedir_telefono`/`telefono_pendiente`/`limpiar_telefono_pendiente`
    directo, sin pasar por pipeline: un chat puede tener MÁS DE UN tipo pendiente a la
    vez, y pedir uno no puede pisar al otro."""

    async def test_pedir_agrega_sin_pisar_lo_que_ya_habia(self, fake):
        from app.estado import pedir_telefono, telefono_pendiente

        await pedir_telefono("18091112222", "pedido", "comprobante sin pedido")
        await pedir_telefono("18091112222", "asistencia", "quiere cancelar")

        pend = await telefono_pendiente("18091112222")
        assert set(pend.keys()) == {"pedido", "asistencia"}
        assert pend["pedido"]["resumen"] == "comprobante sin pedido"
        assert pend["asistencia"]["resumen"] == "quiere cancelar"

    async def test_pedir_el_mismo_tipo_dos_veces_actualiza_el_resumen(self, fake):
        from app.estado import pedir_telefono, telefono_pendiente

        await pedir_telefono("18091112222", "asistencia", "primero")
        await pedir_telefono("18091112222", "asistencia", "segundo")

        pend = await telefono_pendiente("18091112222")
        assert pend.keys() == {"asistencia"}
        assert pend["asistencia"]["resumen"] == "segundo"

    async def test_limpiar_un_tipo_deja_el_otro(self, fake):
        from app.estado import limpiar_telefono_pendiente, pedir_telefono, telefono_pendiente

        await pedir_telefono("18091112222", "pedido", "x")
        await pedir_telefono("18091112222", "asistencia", "y")
        await limpiar_telefono_pendiente("18091112222", "asistencia")

        pend = await telefono_pendiente("18091112222")
        assert set(pend.keys()) == {"pedido"}

    async def test_limpiar_el_ultimo_tipo_borra_la_key_entera(self, fake):
        from app.estado import limpiar_telefono_pendiente, pedir_telefono, telefono_pendiente

        await pedir_telefono("18091112222", "pedido", "x")
        await limpiar_telefono_pendiente("18091112222", "pedido")

        assert await telefono_pendiente("18091112222") == {}

    async def test_limpiar_sin_tipo_borra_todo(self, fake):
        from app.estado import limpiar_telefono_pendiente, pedir_telefono, telefono_pendiente

        await pedir_telefono("18091112222", "pedido", "x")
        await pedir_telefono("18091112222", "asistencia", "y")
        await limpiar_telefono_pendiente("18091112222")

        assert await telefono_pendiente("18091112222") == {}

    async def test_sin_chat_id_no_hace_nada(self, fake):
        from app.estado import limpiar_telefono_pendiente, pedir_telefono, telefono_pendiente

        await pedir_telefono("", "asistencia", "x")
        assert await telefono_pendiente("") == {}
        await limpiar_telefono_pendiente("")  # no debe lanzar


class TestExtraerTelefonoLibre:
    @pytest.mark.parametrize("texto,esperado", [
        ("mi numero es 8293837395", "8293837395"),
        ("puedes llamarme al 829-383-7395", "8293837395"),
        ("+1 (809) 383 7395 ese es", "8093837395"),
        ("18493837395", "8493837395"),
    ])
    def test_formatos_reconocidos(self, texto, esperado):
        assert _extraer_telefono_libre(texto) == esperado

    def test_sin_codigo_de_area_de_rd_no_reconoce_nada(self):
        """Evita confundir cualquier seguidilla de 10 dígitos con un teléfono."""
        assert _extraer_telefono_libre("el pedido 1234567890 ya salio") == ""

    def test_texto_sin_numeros(self):
        assert _extraer_telefono_libre("ok gracias") == ""

    def test_vacio(self):
        assert _extraer_telefono_libre("") == ""


class TestAtenderTelefonoPendiente:
    async def test_sin_nada_pendiente_no_hace_nada(self, fake):
        from app.pipeline import _atender_telefono_pendiente

        numero, avisados = await _atender_telefono_pendiente(
            "DO.111", "mi numero es 8293837395", "18099221092", {"recipient": "DO.111"},
        )
        assert numero == "" and avisados == frozenset()

    async def test_pendiente_pero_sin_telefono_en_el_mensaje_no_completa_nada(self, fake):
        from app.estado import pedir_telefono
        from app.pipeline import _atender_telefono_pendiente

        await pedir_telefono("DO.222", "asistencia", "necesita ayuda con su pedido")
        numero, avisados = await _atender_telefono_pendiente(
            "DO.222", "ok gracias", "18099221092", {"recipient": "DO.222"},
        )
        assert numero == "" and avisados == frozenset()

    async def test_pendiente_con_telefono_completa_y_avisa(self, fake, monkeypatch):
        import app.pipeline as pipeline
        from app.estado import pedir_telefono, telefono_pendiente

        llamadas = []

        async def _plantilla(admin_phone, emisor, plantilla, params):
            llamadas.append((admin_phone, emisor, plantilla, params))
            return True

        monkeypatch.setattr(pipeline.ycloud, "enviar_plantilla", _plantilla)

        await pedir_telefono("DO.333", "asistencia", "quiere cancelar su pedido")
        numero, avisados = await pipeline._atender_telefono_pendiente(
            "DO.333", "claro, mi numero es 8293837395", "18099221092",
            {"recipient": "DO.333"}, "Génesis Akemy",
        )

        assert numero == "8293837395" and avisados == frozenset({"asistencia"})
        assert await telefono_pendiente("DO.333") == {}  # se limpió
        assert llamadas[0] == (
            pipeline.settings.admin_phone, "18099221092",
            pipeline.settings.template_alerta_supervisor,
            ["Génesis Akemy", "8293837395", "quiere cancelar su pedido"],
        )
        # El "gracias" al cliente NO lo manda esta función (ver el comentario junto a
        # TEXTO_GRACIAS_TELEFONO_PENDIENTE): lo manda procesar_turno, DESPUÉS de que el
        # SDK ya escribió el mensaje del cliente en la sesión.

    async def test_un_fallo_de_redis_no_lanza(self, fake, monkeypatch):
        import app.pipeline as pipeline

        async def _explota(chat_id):
            raise RuntimeError("redis caido")

        monkeypatch.setattr(pipeline, "telefono_pendiente", _explota)
        numero, avisados = await pipeline._atender_telefono_pendiente(
            "DO.444", "8293837395", "18099221092", {"recipient": "DO.444"},
        )
        assert numero == "" and avisados == frozenset()

    async def test_si_la_plantilla_falla_cae_al_aviso_de_texto_plano(self, fake, monkeypatch):
        """`enviar_plantilla` no devuelve bool: completa en silencio o LANZA. Si Meta
        no tiene la plantilla aprobada, el respaldo es un texto plano, igual que
        `pagos.avisar_supervisor` — no se pierde el aviso por eso."""
        import app.pipeline as pipeline
        from app.estado import pedir_telefono, telefono_pendiente

        avisos_admin = []

        async def _plantilla_rota(*a, **kw):
            raise RuntimeError("plantilla no aprobada por Meta")

        async def _avisar_admin(emisor, texto):
            avisos_admin.append(texto)
            return True

        async def _texto(*a, **kw):
            return True

        monkeypatch.setattr(pipeline.ycloud, "enviar_plantilla", _plantilla_rota)
        monkeypatch.setattr(pipeline.ycloud, "avisar_admin", _avisar_admin)
        monkeypatch.setattr(pipeline.ycloud, "enviar_texto", _texto)

        await pedir_telefono("DO.666", "asistencia", "quiere cambiar su pedido")
        numero, avisados = await pipeline._atender_telefono_pendiente(
            "DO.666", "mi numero es 8293837395", "18099221092", {"recipient": "DO.666"},
        )

        assert numero == "8293837395" and avisados == frozenset({"asistencia"})
        assert await telefono_pendiente("DO.666") == {}  # se limpió: SÍ se avisó
        assert avisos_admin and "8293837395" in avisos_admin[0]

    async def test_si_ambos_avisos_fallan_no_limpia_el_pendiente(self, fake, monkeypatch):
        """Sin haber avisado a nadie, el pendiente se queda para poder reintentar. Y NO
        se devuelve el número: si se devolviera, quedaría como `ctx.telefono` de este
        turno, pasaría al chatmeta persistido y el próximo mensaje ya no volvería a
        intentar avisar (ver el comentario en _atender_telefono_pendiente)."""
        import app.pipeline as pipeline
        from app.estado import pedir_telefono, telefono_pendiente

        async def _explota(*a, **kw):
            raise RuntimeError("YCloud caido")

        monkeypatch.setattr(pipeline.ycloud, "enviar_plantilla", _explota)
        monkeypatch.setattr(pipeline.ycloud, "avisar_admin", _explota)

        await pedir_telefono("DO.777", "asistencia", "necesita ayuda")
        numero, avisados = await pipeline._atender_telefono_pendiente(
            "DO.777", "8293837395", "18099221092", {"recipient": "DO.777"},
        )

        assert numero == ""
        assert avisados == frozenset()
        assert await telefono_pendiente("DO.777") != {}  # NO se limpió

    async def test_los_dos_tipos_pendientes_a_la_vez_se_avisan_los_dos(self, fake, monkeypatch):
        """El bug que esto reemplaza: una sola key por chat que un tipo nuevo pisaba,
        perdiendo el aviso del otro para siempre (ej: un comprobante sin pedido, y por
        separado pidió asistencia, antes de dar su teléfono)."""
        import app.pipeline as pipeline
        from app.estado import pedir_telefono, telefono_pendiente

        plantillas = []
        avisos_admin = []

        async def _plantilla(admin_phone, emisor, plantilla, params):
            plantillas.append(params)
            return True

        async def _avisar_admin(emisor, texto):
            avisos_admin.append(texto)
            return True

        async def _texto(*a, **kw):
            return True

        monkeypatch.setattr(pipeline.ycloud, "enviar_plantilla", _plantilla)
        monkeypatch.setattr(pipeline.ycloud, "avisar_admin", _avisar_admin)
        monkeypatch.setattr(pipeline.ycloud, "enviar_texto", _texto)

        await pedir_telefono("DO.101", "pedido", "comprobante sin pedido")
        await pedir_telefono("DO.101", "asistencia", "quiere cancelar")

        numero, avisados = await pipeline._atender_telefono_pendiente(
            "DO.101", "8293837395", "18099221092", {"recipient": "DO.101"},
        )

        assert numero == "8293837395"
        assert avisados == frozenset({"pedido", "asistencia"})
        assert await telefono_pendiente("DO.101") == {}  # los dos se limpiaron
        assert len(plantillas) == 1 and plantillas[0][2] == "quiere cancelar"
        assert len(avisos_admin) == 1 and "comprobante sin pedido" in avisos_admin[0]

    async def test_si_uno_de_los_dos_falla_el_otro_se_avisa_y_ese_no_se_limpia(
        self, fake, monkeypatch
    ):
        import app.pipeline as pipeline
        from app.estado import pedir_telefono, telefono_pendiente

        async def _plantilla_rota(*a, **kw):
            raise RuntimeError("plantilla no aprobada")

        avisos_admin = []

        async def _avisar_admin(emisor, texto):
            avisos_admin.append(texto)
            return True

        async def _texto(*a, **kw):
            return True

        monkeypatch.setattr(pipeline.ycloud, "enviar_plantilla", _plantilla_rota)
        monkeypatch.setattr(pipeline.ycloud, "avisar_admin", _avisar_admin)
        monkeypatch.setattr(pipeline.ycloud, "enviar_texto", _texto)

        await pedir_telefono("DO.102", "pedido", "comprobante sin pedido")
        await pedir_telefono("DO.102", "asistencia", "quiere cancelar")

        numero, avisados = await pipeline._atender_telefono_pendiente(
            "DO.102", "8293837395", "18099221092", {"recipient": "DO.102"},
        )

        # "asistencia" cae a avisar_admin como respaldo -sale-; "pedido" ya intentaba
        # avisar_admin directo, así que también sale: el único que realmente puede
        # fallar acá es la plantilla, y tiene respaldo. Se prueba igual el caso donde
        # SÍ queda uno sin avisar en el siguiente test, con ambos rotos.
        assert avisados == frozenset({"pedido", "asistencia"})
        assert await telefono_pendiente("DO.102") == {}

    async def test_si_avisar_admin_tambien_falla_solo_pedido_queda_pendiente(
        self, fake, monkeypatch
    ):
        import app.pipeline as pipeline
        from app.estado import pedir_telefono, telefono_pendiente

        async def _plantilla_rota(*a, **kw):
            raise RuntimeError("plantilla no aprobada")

        llamadas_admin = []

        async def _avisar_admin(emisor, texto):
            llamadas_admin.append(texto)
            if "cancelar" in texto:  # el respaldo de "asistencia" sí sale
                return True
            return False  # el aviso directo de "pedido" falla

        async def _texto(*a, **kw):
            return True

        monkeypatch.setattr(pipeline.ycloud, "enviar_plantilla", _plantilla_rota)
        monkeypatch.setattr(pipeline.ycloud, "avisar_admin", _avisar_admin)
        monkeypatch.setattr(pipeline.ycloud, "enviar_texto", _texto)

        await pedir_telefono("DO.103", "pedido", "comprobante sin pedido")
        await pedir_telefono("DO.103", "asistencia", "quiere cancelar")

        numero, avisados = await pipeline._atender_telefono_pendiente(
            "DO.103", "8293837395", "18099221092", {"recipient": "DO.103"},
        )

        assert avisados == frozenset({"asistencia"})
        pendientes = await telefono_pendiente("DO.103")
        assert set(pendientes.keys()) == {"pedido"}  # sólo el que falló sigue ahí


def _ctx_pedido(**kw) -> ConversationContext:
    base = dict(
        chat_id="DO.555", telefono=None, user_name="Cliente Anuncio",
        emisor="18099221092", destino={"recipient": "DO.555"}, cfg=BusinessConfig(),
        partner_id=42,
    )
    base.update(kw)
    return ConversationContext(**base)


class TestCrearPedidoExigeTelefono:
    """Regla dura en crear_pedido: sin `c.telefono`, no se crea el pedido (ver el
    comentario junto al ERROR en odoo_tools.py)."""

    @pytest.fixture(autouse=True)
    def _sin_odoo_ni_redis(self, monkeypatch):
        import app.tools.odoo_tools as odoo_tools

        creados = []

        async def _create(modelo, valores):
            creados.append(valores)
            return 999

        async def _cero(chat_id):
            return 0.0

        async def _sin_abierto(chat_id):
            return {}

        monkeypatch.setattr(odoo_tools.odoo, "create", _create)
        monkeypatch.setattr(odoo_tools, "leer_cotizacion", _cero)
        monkeypatch.setattr(odoo_tools, "leer_cotizacion_subtotal", _cero)
        monkeypatch.setattr(odoo_tools, "leer_pedido_abierto", _sin_abierto)
        self.creados = creados

    async def test_sin_telefono_no_crea_el_pedido(self):
        ctx = _ctx_pedido(telefono=None)
        salida = await crear_pedido_impl(ctx, modalidad="retiro")
        assert salida.startswith("ERROR")
        assert ctx.order_id is None
        assert self.creados == []

    async def test_con_telefono_crea_normal(self):
        ctx = _ctx_pedido(telefono="8293837395")
        salida = await crear_pedido_impl(ctx, modalidad="retiro")
        assert salida.startswith("OK")
        assert ctx.order_id == 999

    async def test_pide_el_telefono_antes_de_reintentar(self):
        ctx = _ctx_pedido(telefono=None)
        salida = await crear_pedido_impl(ctx, modalidad="retiro")
        assert "telefono" in salida.lower() or "teléfono" in salida.lower()


class TestAtenderTelefonoPendienteTipoPedido:
    """El comprobante-sin-pedido (ver _efectos) también difiere el aviso si no hay
    teléfono real — mismo mecanismo que "asistencia", pero con el texto plano
    original (sin plantilla) y sin respaldo, porque nunca tuvo plantilla."""

    async def test_completa_con_el_texto_de_la_alerta_original(self, fake, monkeypatch):
        import app.pipeline as pipeline
        from app.estado import pedir_telefono, telefono_pendiente

        avisos = []

        async def _avisar_admin(emisor, texto):
            avisos.append(texto)
            return True

        async def _texto(*a, **kw):
            return True

        monkeypatch.setattr(pipeline.ycloud, "avisar_admin", _avisar_admin)
        monkeypatch.setattr(pipeline.ycloud, "enviar_texto", _texto)

        await pedir_telefono(
            "DO.888", "pedido",
            "Mando un comprobante pero el pedido no se registro. Comprobante: http://x/c.jpg",
        )
        numero, avisados = await pipeline._atender_telefono_pendiente(
            "DO.888", "mi numero es 8293837395", "18099221092", {"recipient": "DO.888"},
        )

        assert numero == "8293837395" and avisados == frozenset({"pedido"})
        assert await telefono_pendiente("DO.888") == {}
        assert len(avisos) == 1
        assert "8293837395" in avisos[0] and "comprobante" in avisos[0].lower()

    async def test_sin_respaldo_de_plantilla_si_avisar_admin_falla(self, fake, monkeypatch):
        """A diferencia de "asistencia", "pedido" nunca tuvo plantilla que reintentar:
        si el único envío (texto plano) falla, no hay a qué caer."""
        import app.pipeline as pipeline
        from app.estado import pedir_telefono, telefono_pendiente

        async def _explota(*a, **kw):
            raise RuntimeError("YCloud caido")

        monkeypatch.setattr(pipeline.ycloud, "avisar_admin", _explota)

        await pedir_telefono("DO.999", "pedido", "resumen")
        numero, avisados = await pipeline._atender_telefono_pendiente(
            "DO.999", "8293837395", "18099221092", {"recipient": "DO.999"},
        )

        assert numero == "" and avisados == frozenset()
        assert await telefono_pendiente("DO.999") != {}  # no se limpió: se reintenta


class TestConfirmarTelefonoPendiente:
    """`_confirmar_telefono_pendiente`: el "gracias" al cliente, llamado por el
    fast-path Y por el camino normal del agente (ver el bug real: el fast-path lo
    saltaba porque el mensaje que daba el teléfono también matcheaba un FAQ)."""

    async def test_sin_avisos_no_manda_nada(self, monkeypatch):
        import app.pipeline as pipeline

        llamado = False

        async def _texto(*a, **kw):
            nonlocal llamado
            llamado = True

        monkeypatch.setattr(pipeline.ycloud, "enviar_texto", _texto)
        await pipeline._confirmar_telefono_pendiente("18091112222", {}, "e", frozenset())
        assert llamado is False

    async def test_con_avisos_manda_y_registra(self, fake, monkeypatch):
        import app.pipeline as pipeline

        enviados = []

        async def _texto(destino, emisor, texto, **kw):
            enviados.append((destino, emisor, texto))

        monkeypatch.setattr(pipeline.ycloud, "enviar_texto", _texto)
        await pipeline._confirmar_telefono_pendiente(
            "18091112222", {"to": "18091112222"}, "e", frozenset({"asistencia"})
        )

        assert len(enviados) == 1
        assert "avise al supervisor" in enviados[0][2].lower()
        from app.session import RedisSession

        h = await RedisSession("18091112222").get_items()
        assert h[-1] == {"role": "assistant", "content": pipeline.TEXTO_GRACIAS_TELEFONO_PENDIENTE}

    async def test_un_fallo_de_ycloud_no_lanza(self, monkeypatch):
        import app.pipeline as pipeline

        async def _explota(*a, **kw):
            raise RuntimeError("YCloud caido")

        monkeypatch.setattr(pipeline.ycloud, "enviar_texto", _explota)
        await pipeline._confirmar_telefono_pendiente(
            "18091112222", {}, "e", frozenset({"pedido"})
        )  # no debe lanzar


class TestPedirTelefonoAlCliente:
    """`_pedir_telefono_al_cliente`: una sola pregunta por turno aunque `_efectos`
    tenga dos motivos para pedirla (comprobante sin pedido Y handoff, mismo turno)."""

    def _ctx(self):
        from app.business_config import BusinessConfig
        from app.context import ConversationContext

        return ConversationContext(
            chat_id="18091112222", telefono=None, user_name="Cliente",
            emisor="18099221092", destino={"recipient": "18091112222"},
            cfg=BusinessConfig(),
        )

    async def test_primera_vez_manda_la_pregunta(self, fake, monkeypatch):
        import app.pipeline as pipeline

        enviados = []

        async def _texto(destino, emisor, texto, **kw):
            enviados.append(texto)

        monkeypatch.setattr(pipeline.ycloud, "enviar_texto", _texto)
        resultado = await pipeline._pedir_telefono_al_cliente(self._ctx(), "¿numero?", False)

        assert resultado is True
        assert enviados == ["¿numero?"]

    async def test_segunda_vez_en_el_mismo_turno_no_repite(self, fake, monkeypatch):
        import app.pipeline as pipeline

        enviados = []

        async def _texto(destino, emisor, texto, **kw):
            enviados.append(texto)

        monkeypatch.setattr(pipeline.ycloud, "enviar_texto", _texto)
        resultado = await pipeline._pedir_telefono_al_cliente(
            self._ctx(), "¿numero de nuevo?", True,  # ya_pedido=True: ya se preguntó
        )

        assert resultado is True
        assert enviados == []  # no se repitió la pregunta
