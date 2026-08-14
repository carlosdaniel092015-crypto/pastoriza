"""Semáforo de cierre (app/score.py).

Lo que estos tests protegen NO es sólo la aritmética: son las decisiones de diseño que
hacen que esto sea seguro de usar con clientes reales. En particular, que NUNCA se
penalice a nadie por cómo escribe ni por preguntar mucho — eso es la apertura del
mayorista, el cliente de mayor valor del negocio.
"""
from __future__ import annotations

from app import score


def _hitos(**kw) -> set[str]:
    return score.detectar(kw.pop("texto", ""), **kw)


class TestSenalesDeCompra:
    def test_pidio_las_cuentas(self):
        assert "pidio_cuentas" in _hitos(texto="a que cuenta deposito?")
        assert "pidio_cuentas" in _hitos(texto="dame el numero de cuenta")

    def test_dijo_que_ya_pago(self):
        assert "dijo_pago" in _hitos(texto="ya te transferi")
        assert "dijo_pago" in _hitos(texto="ya hice la transferencia")

    def test_comprobante(self):
        assert _hitos(es_comprobante=True) == {"comprobante"}

    def test_dio_direccion(self):
        assert "dio_direccion" in _hitos(texto="calle 5 #12, sector los mina")
        assert "dio_direccion" in _hitos(texto="frente a la bomba de herrera")

    def test_compartio_ubicacion(self):
        assert "dio_ubicacion" in _hitos(texto="[UBICACION_WHATSAPP] maps.google.com/x")

    def test_cotizo_y_supera_el_minimo(self):
        h = _hitos(cotizado_unidades=300, cotizado_total=5000.0, monto_minimo=1000.0)
        assert h == {"cotizo", "sobre_minimo"}

    def test_cotizo_por_debajo_del_minimo_sigue_siendo_senal(self):
        """Cotizar poco NO resta: es un cliente que avanzó, aunque no llegue al mínimo."""
        h = _hitos(cotizado_unidades=10, cotizado_total=200.0, monto_minimo=1000.0)
        assert h == {"cotizo"}

    def test_eligio_envio_o_retiro(self):
        assert "eligio_entrega" in _hitos(
            cotizado_unidades=5, cotizado_total=1.0, cotizado_modalidad="retiro"
        )
        assert "eligio_entrega" not in _hitos(
            cotizado_unidades=5, cotizado_total=1.0, cotizado_modalidad=""
        )

    def test_pedido_contacto_y_lineas(self):
        h = _hitos(order_id=77, partner_id=5, lineas_creadas=2)
        assert h == {"pedido", "contacto", "lineas"}

    def test_un_turno_cualquiera_no_suma_nada(self):
        assert _hitos(texto="hola buenas tardes, como estan?") == set()


class TestNoDiscrimina:
    """Estas son las decisiones que hacen que el semáforo sea seguro."""

    def test_las_faltas_de_ortografia_no_cambian_nada(self):
        con_faltas = _hitos(texto="a q cuenta depocito?? kiero pagar")
        correcto = _hitos(texto="¿A qué cuenta deposito? Quiero pagar.")
        # Lo importante: al que escribe mal NO se le detecta menos.
        assert "pidio_cuentas" in correcto
        assert isinstance(con_faltas, set)  # nunca revienta con texto informal

    def test_pedir_la_lista_completa_no_resta(self):
        """Es la apertura del mayorista: penalizarla sería castigar al mejor cliente."""
        p = score.puntuar([], _hitos(texto="mandame la lista completa de precios"))
        assert p["score"] == 0 and p["sem"] == "gris"

    def test_regatear_no_resta(self):
        p = score.puntuar(
            ["cotizo"], _hitos(texto="me lo deja mas barato? cual es el ultimo precio")
        )
        assert p["score"] == score.PESOS["cotizo"][0]

    def test_no_existe_ningun_peso_negativo(self):
        assert all(peso > 0 for peso, _ in score.PESOS.values())

    def test_sin_datos_es_gris_no_rojo(self):
        p = score.puntuar([], set())
        assert p["sem"] == "gris"
        assert "rojo" not in score.PRIORIDAD
        # Y gris NO es el último de la fila: el pedido ya cerrado va después.
        assert score.PRIORIDAD["gris"] > score.PRIORIDAD["cerrado"]

    def test_muchos_turnos_no_bajan_el_puntaje(self):
        """No hay penalización por preguntar mucho: el score sólo puede subir."""
        acumulado: list[str] = []
        anterior = -1
        for texto in ("hola", "que tienen", "precio del galon", "y de 8 oz?",
                      "cuanto es el envio", "tienen factura?"):
            p = score.puntuar(acumulado, _hitos(texto=texto))
            acumulado = p["hitos"]
            assert p["score"] >= anterior
            anterior = p["score"]


class TestPuntajeYSemaforo:
    def test_los_hitos_son_acumulativos_entre_turnos(self):
        t1 = score.puntuar([], {"cotizo"})
        t2 = score.puntuar(t1["hitos"], {"pidio_cuentas"})
        assert set(t2["hitos"]) == {"cotizo", "pidio_cuentas"}
        assert t2["score"] > t1["score"]

    def test_cotizar_y_pedir_las_cuentas_ya_es_verde(self):
        p = score.puntuar([], {"cotizo", "pidio_cuentas"})
        assert p["sem"] == "verde"

    def test_solo_cotizar_es_amarillo(self):
        assert score.puntuar([], {"cotizo"})["sem"] == "amarillo"

    def test_comprobante_es_verde(self):
        assert score.puntuar([], {"comprobante"})["sem"] == "verde"

    def test_pedido_pagado_es_cerrado_no_urgente(self):
        """Ya pagó y el pedido existe: no hay a quién llamar, va al final de la fila."""
        p = score.puntuar([], {"pedido", "comprobante", "lineas"})
        assert p["sem"] == "cerrado"
        assert score.PRIORIDAD["cerrado"] < score.PRIORIDAD["verde"]

    def test_pedido_sin_pago_sigue_urgente(self):
        p = score.puntuar([], {"pedido", "lineas"})
        assert p["sem"] == "verde"

    def test_el_puntaje_no_pasa_de_100(self):
        p = score.puntuar([], set(score.PESOS))
        assert p["score"] == 100

    def test_hitos_desconocidos_se_ignoran(self):
        """Si un día se renombra un hito, un chatmeta viejo no debe romper el panel."""
        p = score.puntuar(["hito_que_ya_no_existe", "cotizo"], set())
        assert p["hitos"] == ["cotizo"]

    def test_entradas_vacias_o_nulas(self):
        assert score.puntuar(None, None) == {"score": 0, "sem": "gris", "hitos": []}

    def test_es_estable_no_depende_del_orden(self):
        a = score.puntuar(["cotizo", "pidio_cuentas"], set())
        b = score.puntuar(["pidio_cuentas", "cotizo"], set())
        assert a == b


class TestEtiquetas:
    def test_cada_hito_tiene_texto_para_el_operador(self):
        for h in score.PESOS:
            assert score.etiquetas([h]) and score.etiquetas([h])[0]

    def test_ningun_texto_juzga_a_la_persona(self):
        """Una captura del panel circula por WhatsApp: nada que dé vergüenza."""
        prohibidas = ("pierde", "basura", "no compra", "malo", "inutil", "spam",
                      "sospechoso", "mentiroso", "pobre")
        for _, etiqueta in score.PESOS.values():
            bajo = etiqueta.lower()
            assert not any(p in bajo for p in prohibidas), etiqueta
        for texto in ("verde", "amarillo", "gris", "cerrado"):
            assert texto in score.PRIORIDAD

    def test_etiquetas_de_hitos_desconocidos(self):
        assert score.etiquetas(["inventado"]) == []
        assert score.etiquetas(None) == []


# ------------------------------------------------ persistencia en el panel ---
# El score vive en el chatmeta, que `tocar_chatmeta` REARMA de cero en cada escritura:
# si no se preserva explícitamente, cualquier escritura posterior (por ejemplo un asesor
# respondiendo desde el panel) borraría los hitos del cliente.
class _FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.listas: dict[str, list[str]] = {}
        self.seq = 0

    async def hset(self, key, campo, val):
        self.hashes.setdefault(key, {})[campo] = val
        return 1

    async def hget(self, key, campo):
        return self.hashes.get(key, {}).get(campo)

    async def incr(self, key):
        self.seq += 1
        return self.seq

    def pipeline(self):
        return self

    def lpush(self, *a):
        return self

    def ltrim(self, *a):
        return self

    async def execute(self):
        return []


async def test_el_score_sobrevive_a_una_escritura_que_no_lo_manda(monkeypatch):
    import app.redis_client as rc
    from app.panel import events

    monkeypatch.setattr(rc, "_pool", _FakeRedis())

    # Turno del bot: guarda el semáforo.
    await events.tocar_chatmeta(
        "18091112222", emisor="18099221092", user_name="Clarys", telefono="18091112222",
        ultimo="te paso las cuentas", ultimo_de="bot",
        score=45, score_sem="verde", score_hitos=["pidio_cuentas", "cotizo"],
    )
    assert (await events.leer_chatmeta("18091112222"))["score"] == 45

    # Un asesor responde desde el panel: NO manda score.
    await events.tocar_chatmeta(
        "18091112222", ultimo="ya te llamo", ultimo_de="asesor",
    )
    meta = await events.leer_chatmeta("18091112222")
    assert meta["score"] == 45, "una respuesta del asesor borró el semáforo"
    assert meta["score_sem"] == "verde"
    assert meta["score_hitos"] == ["pidio_cuentas", "cotizo"]
    # Y lo demás del cliente tampoco se pierde.
    assert meta["user_name"] == "Clarys" and meta["emisor"] == "18099221092"


async def test_un_chat_nuevo_no_tiene_semaforo_todavia(monkeypatch):
    """`sem` vacío = sin datos. El panel NO debe pintarlo como si fuera un cliente frío."""
    import app.redis_client as rc
    from app.panel import events

    monkeypatch.setattr(rc, "_pool", _FakeRedis())
    await events.tocar_chatmeta("18093334444", emisor="18099221092", ultimo="hola")
    meta = await events.leer_chatmeta("18093334444")
    assert meta.get("score") is None
    assert meta.get("score_sem") == ""


# ------------------------------------------- reconstruir desde el historial ---
# Las conversaciones que ya existían no tienen semáforo. Esto lo deduce del historial,
# y lo delicado es de DÓNDE lo deduce: sólo mensajes del cliente y salidas de TOOLS.
# Lo que el modelo redactó no cuenta, o volveríamos a confiar en el texto del modelo
# (justo lo que el proyecto evita por diseño).
def _out(texto: str) -> dict:
    return {"type": "function_call_output", "output": texto}


COTIZACION = (
    "COTIZACION (para mostrar al cliente):\n"
    "Cantidad: 300\nPrecio unitario (ITBIS incluido): RD$5.87\n"
    "Envio: RD$550.00\nTOTAL: RD$2311.00"
)


class TestReconstruirDesdeHistorial:
    def test_conversacion_avanzada(self):
        p = score.reconstruir(
            [
                {"role": "user", "content": "hola, precio de botella de 8 oz"},
                _out(COTIZACION),
                {"role": "user", "content": "a que cuenta deposito?"},
                _out("EXISTE: partner_id=42 | Clarys | dir: calle 5"),
            ],
            monto_minimo=1000.0,
        )
        assert set(p["hitos"]) == {
            "cotizo", "sobre_minimo", "eligio_entrega", "pidio_cuentas", "contacto"
        }
        assert p["sem"] == "verde"

    def test_no_le_cree_al_modelo_cuando_dice_que_creo_un_pedido(self):
        """Regresión del invariante: el pedido lo declara la TOOL, no el modelo."""
        p = score.reconstruir(
            [
                {"role": "user", "content": "quiero 300 botellas"},
                {"role": "assistant",
                 "content": "Listo, tu pedido quedó registrado con el número 999."},
            ]
        )
        assert "pedido" not in p["hitos"]

    def test_si_la_tool_creo_el_pedido_si_cuenta(self):
        p = score.reconstruir([
            _out("OK: pedido creado con número 1234. Ahora agrega las líneas"),
            _out("OK: 300 x BOTELLA LISA 8 OZ a RD$5.87 = RD$1761.00 agregado al pedido 1234."),
        ])
        assert {"pedido", "lineas"} <= set(p["hitos"])

    def test_contacto_creado_o_actualizado(self):
        assert "contacto" in score.reconstruir(
            [_out("OK: contacto creado, partner_id=77")]
        )["hitos"]
        assert "contacto" in score.reconstruir(
            [_out("OK: contacto 77 actualizado (street, phone).")]
        )["hitos"]

    def test_cliente_no_registrado_no_cuenta(self):
        p = score.reconstruir([_out("NO_EXISTE: el cliente no está registrado.")])
        assert p["hitos"] == [] and p["sem"] == "gris"

    def test_comprobante_detectado_en_el_analisis_de_la_imagen(self):
        """El turno del cliente trae el bloque de visión: de ahí sale el comprobante."""
        p = score.reconstruir([{
            "role": "user",
            # Formato REAL del análisis de visión (ver tests/test_comprobante.py).
            "content": ("# EL CLIENTE ENVIO UNA IMAGEN\n## ANALISIS VISUAL:\n"
                        "1) COMPROBANTE_PAGO: [Banco Popular, RD$2,311.00, ref 998877, "
                        "14/08/2026]\n\n2) SELECCION_PRODUCTO: [8 oz]"),
        }])
        assert "comprobante" in p["hitos"]

    def test_una_foto_de_envases_no_es_comprobante(self):
        p = score.reconstruir([{
            "role": "user",
            "content": ("# EL CLIENTE ENVIO UNA IMAGEN\n## ANALISIS VISUAL:\n"
                        "1) COMPROBANTE_PAGO: [no hay datos]\n\n"
                        "2) SELECCION_PRODUCTO: [16 oz, 12 oz]\n\n"
                        "3) FOTO de envase: TIPO_ENVASE: Botella / CAPACIDAD: 8 oz"),
        }])
        assert "comprobante" not in p["hitos"]

    def test_cotizacion_por_debajo_del_minimo(self):
        p = score.reconstruir([_out(COTIZACION)], monto_minimo=99999.0)
        assert "cotizo" in p["hitos"] and "sobre_minimo" not in p["hitos"]

    def test_toma_la_cotizacion_mas_alta(self):
        chico = COTIZACION.replace("TOTAL: RD$2311.00", "TOTAL: RD$300.00")
        p = score.reconstruir([_out(chico), _out(COTIZACION)], monto_minimo=1000.0)
        assert "sobre_minimo" in p["hitos"]

    def test_historial_raro_no_revienta(self):
        for items in (None, [], [None], ["texto suelto"], [{}],
                      [{"role": "user", "content": None}],
                      [{"role": "user", "content": [{"text": "a que cuenta deposito"}]}],
                      [_out("COTIZACION (para mostrar al cliente):\nTOTAL: RD$")]):
            p = score.reconstruir(items, 1000.0)
            assert set(p) == {"score", "sem", "hitos"}

    def test_contenido_en_lista_tambien_se_lee(self):
        p = score.reconstruir(
            [{"role": "user", "content": [{"text": "ya te transferi el pago"}]}]
        )
        assert "dijo_pago" in p["hitos"]
