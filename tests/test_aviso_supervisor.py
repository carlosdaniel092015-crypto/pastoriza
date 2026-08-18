"""El aviso que aprueba el supervisor: qué dice y qué pasa cuando toca el botón.

Pedido textual del negocio: "el supervisor quiere que sea enviado directamente la
imagen del comprobante, el nombre del cliente con la dirección, los productos con
cantidades, con el subtotal, con el ITBIS, con el total y envío en caso que sea de
envío, y la aprobación la hará desde la plantilla que se le enviará por WhatsApp".

Se protegen tres cosas:
  1. que los MONTOS del aviso sean los mismos que se le cotizaron al cliente;
  2. que el botón del supervisor apruebe DE VERDAD (el cliente recibe su número);
  3. que un mensaje cualquiera del supervisor NO se coma como si fuera una aprobación.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import aprobacion
from app.models import parse_inbound
from tests.fake_redis import FakeRedis

A = "18099221092"  # canal por el que entró el cliente
ADMIN = "+18294716701"  # supervisor
CLIENTE = "18091112222"

LINEAS = [
    {"nombre": "BOTELLA LISA 8 OZ", "cantidad": 300, "precio": 11.8, "total": 3540.0},
    {"nombre": "TAPA 28MM", "cantidad": 300, "precio": 5.9, "total": 1770.0},
]


# ------------------------------------------------------------ parte pura ---
class TestMontos:
    def test_desagrega_el_itbis_hacia_atras(self):
        """Los precios del catálogo YA vienen con ITBIS: subtotal = total / 1.18."""
        m = aprobacion.montos(LINEAS)
        assert m["productos"] == 5310.0
        assert m["subtotal"] == 4500.0
        assert m["itbis"] == 810.0
        assert round(m["subtotal"] + m["itbis"], 2) == m["productos"]

    def test_el_envio_suma_al_total_pero_no_al_itbis(self):
        m = aprobacion.montos(LINEAS, envio=550)
        assert m["itbis"] == 810.0
        assert m["total"] == 5860.0

    def test_sin_lineas_no_revienta(self):
        assert aprobacion.montos([])["total"] == 0.0
        assert aprobacion.montos(None)["total"] == 0.0


class TestParametrosDeLaPlantilla:
    def test_trae_todo_lo_que_pidio_el_supervisor(self):
        p = aprobacion.parametros(
            order_id=160, modalidad="envio", cliente="Clarys Rey",
            telefono="18091112222", direccion="Calle 5 #12, Los Alcarrizos",
            lineas=LINEAS, envio=550,
        )
        assert len(p) == 9, "la plantilla de Meta tiene 9 variables"
        assert p[0] == "160"
        assert "ENVÍO" in p[1]
        assert "Clarys Rey" in p[2] and "18091112222" in p[2]
        assert "Los Alcarrizos" in p[3]
        assert "300 x BOTELLA LISA 8 OZ" in p[4] and "TAPA 28MM" in p[4]
        assert p[5] == "4,500.00"  # subtotal
        assert p[6] == "810.00"    # ITBIS
        assert p[7] == "550.00"    # envío
        assert p[8] == "5,860.00"  # total

    def test_retiro_en_tienda_no_cobra_envio(self):
        p = aprobacion.parametros(
            order_id=161, modalidad="retiro", cliente="Hija Rey", telefono="1809",
            direccion="", lineas=LINEAS, envio=550,
        )
        assert "RETIRO" in p[1]
        assert p[3] == "Retiro en tienda"
        assert p[7] == "0.00"
        assert p[8] == "5,310.00"

    def test_ninguna_variable_lleva_salto_de_linea(self):
        """Meta RECHAZA la plantilla si una variable trae \\n o tab."""
        p = aprobacion.parametros(
            order_id=1, modalidad="envio", cliente="Ana\nMaría", telefono="809",
            direccion="Calle 1\n\tApto 2\nSanto Domingo", lineas=LINEAS,
        )
        for v in p:
            assert "\n" not in v and "\t" not in v and "\r" not in v

    def test_ninguna_variable_queda_vacia(self):
        """Una variable vacía también hace fallar el envío."""
        p = aprobacion.parametros(
            order_id=1, modalidad="", cliente="", telefono="", direccion="",
            lineas=[],
        )
        assert all(v.strip() for v in p)

    def test_el_cuerpo_entero_entra_en_el_tope_de_1024(self):
        """Si el cuerpo se pasa de 1024, WhatsApp rechaza el envío y el supervisor
        NO se entera del pago. El peor caso tiene que entrar igual."""
        p = aprobacion.parametros(
            order_id=999999, modalidad="envio", cliente="X" * 200, telefono="1" * 15,
            direccion="Y" * 400,
            lineas=[{"nombre": "PRODUCTO LARGUÍSIMO " * 5, "cantidad": 9999,
                     "total": 1000000.0} for _ in range(20)],
            envio=99999,
        )
        # 260 = el texto FIJO más largo de las dos plantillas, el de retiro
        # (ver PLANTILLA_META.md). Si cambiás ese texto, rehacé la cuenta.
        assert sum(len(v) for v in p) + 260 <= 1024


class TestPayloadDelBoton:
    def test_cabe_en_los_128_caracteres_de_whatsapp(self):
        pay = aprobacion.payload(aprobacion.ACCION_APROBAR, "1" * 200, 160)
        assert len(pay) <= 128

    def test_ida_y_vuelta(self):
        pay = aprobacion.payload(aprobacion.ACCION_APROBAR, CLIENTE, 160)
        assert aprobacion.parsear_respuesta(pay) == ("aprobar", CLIENTE, 160)

    def test_rechazar(self):
        pay = aprobacion.payload(aprobacion.ACCION_RECHAZAR, CLIENTE, 160)
        assert aprobacion.parsear_respuesta(pay) == ("rechazar", CLIENTE, 160)

    def test_tambien_se_puede_escribir_a_mano(self):
        """Por si la plantilla todavía no está aprobada por Meta."""
        assert aprobacion.parsear_respuesta("aprobar 160") == ("aprobar", "", 160)
        assert aprobacion.parsear_respuesta("ok 160") == ("aprobar", "", 160)
        assert aprobacion.parsear_respuesta("rechazar 160") == ("rechazar", "", 160)

    @pytest.mark.parametrize("texto", [
        "", "hola", "aprobar", "ok", "buenos dias", "160", "aprobado el pedido",
        "necesito 300 botellas de 8 oz",
    ])
    def test_lo_que_no_es_una_aprobacion_no_lo_parece(self, texto):
        assert aprobacion.parsear_respuesta(texto) is None


# --------------------------------------------------------------- webhook ---
class TestElBotonLlegaDesdeYCloud:
    def _body(self, **inbound) -> dict:
        base = {
            "id": "wamid.1", "from": ADMIN, "to": A, "type": "button",
            "sendTime": "2026-08-15T10:00:00Z",
        }
        base.update(inbound)
        return {"type": "whatsapp.inbound_message.received",
                "whatsappInboundMessage": base}

    def test_se_lee_el_payload_del_boton_no_su_titulo(self):
        msg = parse_inbound(self._body(
            button={"text": "Aprobar pago", "payload": f"aprobar:{CLIENTE}:160"}
        ))
        assert msg is not None
        assert msg.boton_payload == f"aprobar:{CLIENTE}:160"

    def test_boton_interactivo_tambien(self):
        msg = parse_inbound(self._body(
            type="interactive",
            interactive={"button_reply": {"id": f"rechazar:{CLIENTE}:160",
                                          "title": "No aprobar"}},
        ))
        assert msg is not None and msg.boton_payload == f"rechazar:{CLIENTE}:160"

    def test_un_mensaje_normal_no_trae_payload(self):
        msg = parse_inbound(self._body(type="text", text={"body": "hola"}))
        assert msg is not None and msg.boton_payload == ""


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
    admin: list[str] = []

    async def _enviar(destino, emisor, texto, simular_tipeo=True):
        enviados.append((destino, emisor, texto))
        return True

    async def _avisar(emisor, texto):
        admin.append(texto)

    monkeypatch.setattr(ycloud, "enviar_texto", _enviar)
    monkeypatch.setattr(ycloud, "avisar_admin", _avisar)

    fake.hashes.setdefault(events.CHATMETA_KEY, {})[CLIENTE] = json.dumps({
        "chat_id": CLIENTE, "emisor": A, "user_name": "Clarys",
        "destino": {"to": CLIENTE}, "ultimo": "(comprobante)",
        "ultimo_de": "cliente", "ultimo_ts": 1000,
        "aprobacion": {"estado": "pendiente", "order_id": 160},
    })
    with TestClient(app) as c:
        c.enviados = enviados  # type: ignore[attr-defined]
        c.admin = admin  # type: ignore[attr-defined]
        yield c
    bc.invalidar()
    rc._pool = None


def _webhook(c: TestClient, desde: str, *, payload: str = "", texto: str = "") -> dict:
    inbound: dict = {"id": "wamid.x", "from": desde, "to": A,
                     "sendTime": "2026-08-15T10:00:00Z"}
    if payload:
        inbound.update(type="button", button={"text": "Aprobar pago",
                                              "payload": payload})
    else:
        inbound.update(type="text", text={"body": texto})
    r = c.post("/webhook/ycloud", json={
        "type": "whatsapp.inbound_message.received", "whatsappInboundMessage": inbound,
    })
    assert r.status_code == 200, r.text
    return r.json()


class TestAprobarDesdeWhatsApp:
    def test_el_boton_del_supervisor_le_confirma_al_cliente(self, cliente):
        res = _webhook(cliente, ADMIN, payload=f"aprobar:{CLIENTE}:160")
        assert res.get("aprobacion") is True

        assert len(cliente.enviados) == 1
        destino, _emisor, texto = cliente.enviados[0]
        assert destino == {"to": CLIENTE}, "el mensaje va al CLIENTE, no al supervisor"
        assert "160" in texto and "verificado" in texto.lower()

        fila = cliente.get("/panel/api/chats").json()["chats"][0]
        assert fila["aprobacion"] == "aprobado"

    def test_al_supervisor_se_le_confirma_que_se_hizo(self, cliente):
        _webhook(cliente, ADMIN, payload=f"aprobar:{CLIENTE}:160")
        assert cliente.admin and "160" in cliente.admin[0]

    def test_el_boton_de_rechazo_le_avisa_al_cliente_sin_dar_el_motivo(self, cliente):
        _webhook(cliente, ADMIN, payload=f"rechazar:{CLIENTE}:160")
        assert len(cliente.enviados) == 1
        texto = cliente.enviados[0][2].lower()
        assert "no pudimos confirmar" in texto and "829" in texto
        assert "160" not in texto, "sin número: el pedido NO quedó confirmado"
        assert cliente.get("/panel/api/chats").json()["chats"][0]["aprobacion"] == (
            "rechazado"
        )

    def test_y_al_supervisor_se_le_dice_que_el_motivo_lo_explica_el(self, cliente):
        _webhook(cliente, ADMIN, payload=f"rechazar:{CLIENTE}:160")
        assert cliente.admin and "motivo" in cliente.admin[0].lower()

    def test_escrito_a_mano_encuentra_el_chat_por_el_numero_de_pedido(self, cliente):
        """'aprobar 160' no trae chat_id: hay que ubicarlo por el pedido."""
        _webhook(cliente, ADMIN, texto="aprobar 160")
        assert len(cliente.enviados) == 1 and "160" in cliente.enviados[0][2]

    def test_un_mensaje_cualquiera_del_supervisor_sigue_su_curso(
        self, cliente, monkeypatch
    ):
        """El supervisor también escribe cosas normales: no todo es una aprobación."""
        import app.main as m

        vistos: list = []

        async def _entrante(msg):
            vistos.append(msg)

        monkeypatch.setattr(m, "manejar_entrante", _entrante)
        res = _webhook(cliente, ADMIN, texto="hola, cómo va todo")
        assert res.get("aprobacion") is None
        assert len(vistos) == 1
        assert cliente.enviados == []

    def test_un_CLIENTE_no_puede_aprobarse_el_pago(self, cliente, monkeypatch):
        """Lo más importante de todo: sólo el 6701 aprueba."""
        import app.main as m

        vistos: list = []

        async def _entrante(msg):
            vistos.append(msg)

        monkeypatch.setattr(m, "manejar_entrante", _entrante)
        res = _webhook(cliente, CLIENTE, payload=f"aprobar:{CLIENTE}:160")
        assert res.get("aprobacion") is None
        assert cliente.enviados == [], "nadie le confirmó el pago"
        assert len(vistos) == 1, "va al flujo normal de venta"
        assert cliente.get("/panel/api/chats").json()["chats"][0]["aprobacion"] == (
            "pendiente"
        )

    def test_un_pedido_que_no_existe_avisa_y_no_toca_nada(self, cliente):
        _webhook(cliente, ADMIN, texto="aprobar 9999")
        assert cliente.enviados == []
        assert cliente.admin and "9999" in cliente.admin[0]
        assert cliente.get("/panel/api/chats").json()["chats"][0]["aprobacion"] == (
            "pendiente"
        )


class TestElAvisoQueSaleDeVerdad:
    """`pagos.avisar_supervisor`: lo que YCloud termina recibiendo."""

    @pytest.fixture
    def espia(self, monkeypatch):
        from app import media_publica, pagos
        from app.settings import settings
        from app.ycloud import ycloud

        monkeypatch.setattr(settings, "public_base_url", "https://bot.test")
        monkeypatch.setattr(
            pagos, "descargar", lambda url, **kw: _futuro(b"\xff\xd8jpeg")
        )
        salidas: list[dict] = []

        async def _plantilla(telefono, emisor, nombre, parametros,
                             imagen_url="", botones=None):
            salidas.append({
                "telefono": telefono, "emisor": emisor, "nombre": nombre,
                "parametros": parametros, "imagen_url": imagen_url,
                "botones": botones or [],
            })
            return True

        monkeypatch.setattr(ycloud, "enviar_plantilla_botones", _plantilla)
        media_publica._CACHE.clear()
        return salidas

    async def _avisar(self, **kw):
        from app import pagos

        base = dict(
            chat_id=CLIENTE, emisor=A, order_id=160, modalidad="envio",
            cliente="Clarys", telefono=CLIENTE, direccion="Calle 5 #12",
            lineas=LINEAS, envio=550, imagen_url="https://ycloud/media/abc.jpg",
        )
        base.update(kw)
        return await pagos.avisar_supervisor(**base)

    async def test_va_al_supervisor_por_el_canal_del_cliente(self, espia):
        from app.settings import settings

        assert await self._avisar() is True
        assert espia[0]["telefono"] == settings.admin_phone
        assert espia[0]["emisor"] == A

    async def test_lleva_los_9_datos_y_los_dos_botones(self, espia):
        await self._avisar()
        assert len(espia[0]["parametros"]) == 9
        assert espia[0]["botones"] == [
            f"aprobar:{CLIENTE}:160", f"rechazar:{CLIENTE}:160",
        ]

    async def test_el_comprobante_se_republica_en_NUESTRO_dominio(self, espia):
        """Las URLs de YCloud exigen X-API-Key: Meta no puede descargarlas."""
        from app import media_publica

        await self._avisar()
        url = espia[0]["imagen_url"]
        assert url.startswith("https://bot.test/panel/media/")
        token = url.rsplit("/", 1)[1].split(".")[0]
        assert media_publica.obtener(token) is not None

    async def test_si_no_se_puede_bajar_la_foto_el_aviso_sale_igual(
        self, espia, monkeypatch
    ):
        """Sin foto el supervisor todavía puede aprobar; sin aviso, no."""
        from app import pagos

        def _explota(url, **kw):
            raise RuntimeError("404")

        monkeypatch.setattr(pagos, "descargar", _explota)
        assert await self._avisar() is True
        assert espia[0]["imagen_url"] == ""

    async def test_no_lanza_si_ycloud_se_cae(self, espia, monkeypatch):
        """El cliente ya recibió su respuesta: el aviso no puede tumbar el turno."""
        from app.ycloud import ycloud

        async def _explota(*a, **kw):
            raise RuntimeError("timeout")

        monkeypatch.setattr(ycloud, "enviar_plantilla_botones", _explota)
        assert await self._avisar() is False


def _futuro(valor):
    async def _f():
        return valor

    return _f()


class TestPlantillaSinEncabezadoDeImagen:
    """Si la plantilla se dio de alta SIN encabezado, Meta rechaza el mensaje ENTERO
    cuando le mandamos la foto: el supervisor se quedaría sin aviso y sin botones."""

    @pytest.fixture
    def espia(self, monkeypatch):
        from app import media_publica, pagos
        from app.settings import settings
        from app.ycloud import ycloud

        monkeypatch.setattr(settings, "public_base_url", "https://bot.test")
        monkeypatch.setattr(
            pagos, "descargar", lambda url, **kw: _futuro(b"\xff\xd8jpeg")
        )
        intentos: list[str] = []
        imagenes: list[tuple] = []

        async def _plantilla(telefono, emisor, nombre, parametros,
                             imagen_url="", botones=None):
            intentos.append(imagen_url)
            return not imagen_url  # la plantilla NO acepta encabezado

        async def _imagen(destino, emisor, url, caption=""):
            imagenes.append((destino, url, caption))
            return True

        monkeypatch.setattr(ycloud, "enviar_plantilla_botones", _plantilla)
        monkeypatch.setattr(ycloud, "enviar_imagen", _imagen)
        media_publica._CACHE.clear()
        return intentos, imagenes

    async def _avisar(self):
        from app import pagos

        return await pagos.avisar_supervisor(
            chat_id=CLIENTE, emisor=A, order_id=160, modalidad="envio",
            cliente="Clarys", telefono=CLIENTE, direccion="Calle 5 #12",
            lineas=LINEAS, envio=550, imagen_url="https://ycloud/media/abc.jpg",
        )

    async def test_reintenta_sin_foto_para_que_el_aviso_salga_igual(self, espia):
        intentos, _ = espia
        assert await self._avisar() is True
        assert len(intentos) == 2
        assert intentos[0].startswith("https://bot.test/")  # primero, con foto
        assert intentos[1] == ""                            # después, sin ella

    async def test_y_el_comprobante_le_llega_aparte(self, espia):
        from app.settings import settings

        _, imagenes = espia
        await self._avisar()
        assert len(imagenes) == 1
        destino, url, caption = imagenes[0]
        assert destino == {"to": settings.admin_phone}
        assert url.startswith("https://bot.test/panel/media/")
        assert "160" in caption

    async def test_si_la_foto_suelta_falla_el_aviso_sigue_valiendo(
        self, espia, monkeypatch
    ):
        """La ventana de 24 h puede estar cerrada: eso no invalida la aprobación."""
        from app.ycloud import ycloud

        async def _explota(*a, **kw):
            raise RuntimeError("fuera de ventana")

        monkeypatch.setattr(ycloud, "enviar_imagen", _explota)
        assert await self._avisar() is True

    async def test_si_la_plantilla_no_existe_no_se_reintenta_para_siempre(
        self, monkeypatch
    ):
        """Meta no la aprobó todavía: los dos intentos fallan y se cae al plan B."""
        from app import media_publica, pagos
        from app.settings import settings
        from app.ycloud import ycloud

        monkeypatch.setattr(settings, "public_base_url", "https://bot.test")
        monkeypatch.setattr(
            pagos, "descargar", lambda url, **kw: _futuro(b"\xff\xd8jpeg")
        )
        media_publica._CACHE.clear()
        intentos: list[str] = []

        async def _plantilla(*a, **kw):
            intentos.append(kw.get("imagen_url", ""))
            return False

        monkeypatch.setattr(ycloud, "enviar_plantilla_botones", _plantilla)
        assert await self._avisar() is False
        assert len(intentos) == 2


class TestRetiroTambienSeAprueba:
    """En retiro no hay comprobante, pero el pedido igual lo aprueba el supervisor.

    Y va por OTRA plantilla: una con encabezado de imagen EXIGE una imagen en cada
    envío, así que la de pago no se puede usar para un aviso sin foto.
    """

    @pytest.fixture
    def espia(self, monkeypatch):
        from app.ycloud import ycloud

        salidas: list[dict] = []

        async def _plantilla(telefono, emisor, nombre, parametros,
                             imagen_url="", botones=None):
            salidas.append({"nombre": nombre, "imagen_url": imagen_url,
                            "botones": botones or [], "parametros": parametros})
            return True

        monkeypatch.setattr(ycloud, "enviar_plantilla_botones", _plantilla)
        return salidas

    async def test_usa_la_plantilla_SIN_encabezado(self, espia):
        from app import pagos
        from app.settings import settings

        ok = await pagos.avisar_supervisor(
            chat_id=CLIENTE, emisor=A, order_id=161, modalidad="retiro",
            cliente="Hija Rey", telefono=CLIENTE, direccion="", lineas=LINEAS,
            envio=0.0, imagen_url="",  # retiro: no hay comprobante
        )
        assert ok is True
        assert espia[0]["nombre"] == settings.template_aprobacion_retiro
        assert espia[0]["imagen_url"] == ""

    async def test_con_los_mismos_dos_botones(self, espia):
        from app import pagos

        await pagos.avisar_supervisor(
            chat_id=CLIENTE, emisor=A, order_id=161, modalidad="retiro",
            cliente="Hija Rey", telefono=CLIENTE, direccion="", lineas=LINEAS,
        )
        assert espia[0]["botones"] == [
            f"aprobar:{CLIENTE}:161", f"rechazar:{CLIENTE}:161",
        ]
        assert len(espia[0]["parametros"]) == 9, "el cuerpo es el mismo"


class TestQueRecibeElClienteAlAprobar:
    def _meta(self, con_pago: bool) -> str:
        return json.dumps({
            "chat_id": CLIENTE, "emisor": A, "user_name": "Clarys",
            "destino": {"to": CLIENTE}, "ultimo": "ok", "ultimo_ts": 1000,
            "aprobacion": {"estado": "pendiente", "order_id": 161,
                           "modalidad": "retiro" if not con_pago else "envio",
                           "con_pago": con_pago},
        })

    def test_al_de_retiro_NO_se_le_dice_que_su_pago_fue_verificado(self, cliente):
        """Le va a pagar en la tienda: decirle "pago verificado" es mentirle."""
        import app.redis_client as rc
        from app.panel import events

        rc._pool.hashes[events.CHATMETA_KEY][CLIENTE] = self._meta(con_pago=False)
        r = cliente.post(f"/panel/api/chats/{CLIENTE}/aprobar-pago")
        assert r.status_code == 200, r.text

        texto = cliente.enviados[0][2]
        assert "161" in texto
        assert "pago" not in texto.lower() or "pagas al retirar" in texto.lower()
        assert "verificado" not in texto.lower()

    def test_al_que_pago_si(self, cliente):
        import app.redis_client as rc
        from app.panel import events

        rc._pool.hashes[events.CHATMETA_KEY][CLIENTE] = self._meta(con_pago=True)
        cliente.post(f"/panel/api/chats/{CLIENTE}/aprobar-pago")
        texto = cliente.enviados[0][2]
        assert "verificado" in texto.lower() and "161" in texto

    def test_el_panel_distingue_los_dos_casos(self, cliente):
        import app.redis_client as rc
        from app.panel import events

        rc._pool.hashes[events.CHATMETA_KEY][CLIENTE] = self._meta(con_pago=False)
        fila = cliente.get("/panel/api/chats").json()["chats"][0]
        assert fila["aprobacion"] == "pendiente" and fila["con_pago"] is False


class TestAprobarCierraElPedido:
    """Decidido el pedido, el próximo que pida el cliente es un pedido NUEVO.

    Si el pedido siguiera "abierto" después de aprobarlo, la próxima transferencia del
    mismo cliente se aplicaría encima de un pedido ya despachado.
    """

    @pytest.fixture
    def con_abierto(self, monkeypatch):
        from app import pagos

        cerrados: list[str] = []

        async def _cerrar(chat_id):
            cerrados.append(chat_id)

        monkeypatch.setattr(pagos, "cerrar_pedido_abierto", _cerrar)
        return cerrados

    def test_al_aprobar(self, cliente, con_abierto):
        r = cliente.post(f"/panel/api/chats/{CLIENTE}/aprobar-pago")
        assert r.status_code == 200, r.text
        assert con_abierto == [CLIENTE]

    def test_al_rechazar(self, cliente, con_abierto):
        r = cliente.post(f"/panel/api/chats/{CLIENTE}/rechazar-pago",
                         json={"motivo": "monto distinto"})
        assert r.status_code == 200
        assert con_abierto == [CLIENTE]

    def test_si_el_envio_al_cliente_falla_NO_se_cierra(self, cliente, con_abierto,
                                                       monkeypatch):
        """El pago sigue pendiente: el pedido tiene que seguir abierto."""
        from app.ycloud import ycloud

        async def _falla(*a, **kw):
            return False

        monkeypatch.setattr(ycloud, "enviar_texto", _falla)
        assert cliente.post(f"/panel/api/chats/{CLIENTE}/aprobar-pago").status_code == 502
        assert con_abierto == []
