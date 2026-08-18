"""El PANEL responde por canal: elegir un número cambia TODO lo que se ve y edita.

Requisito de operación, textual: "si le doy al 1092 se debe cambiar a todas las
configuraciones y conversaciones de este, si le doy al 6701 se debe cambiar a todas
las configuraciones de ese, y las conversaciones no se deben juntar; cada uno estará
con todo individual".

Se prueba contra la app real (TestClient) con un Redis de mentira: así se verifica el
contrato HTTP completo que consume el panel, no sólo las funciones internas.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.fake_redis import FakeRedis

A, B = "18099221092", "18294716701"
CA, CB = "8099221092", "8294716701"
CANALES = f"{A} = Tienda\n{B} = Mayorista"


@pytest.fixture
def cliente(monkeypatch):
    import app.redis_client as rc
    from app import business_config as bc
    from app.main import app
    from app.panel import agentes_custom, conocimiento, prompt_store

    from app.panel import router as panel_router_mod

    fake = FakeRedis()
    monkeypatch.setattr(rc, "_pool", fake)
    # Caches de proceso del panel (scan de sesiones y último mensaje derivado): si se
    # arrastran entre tests, uno ve los datos del anterior.
    monkeypatch.setattr(panel_router_mod, "_cache_scan", None)
    panel_router_mod._cache_ultimo.clear()
    fake.kv[bc.CONFIG_KEY] = json.dumps(
        {"precio_envio": "550", "monto_minimo": "1000", "canales": CANALES}
    )
    bc.invalidar()
    bc._ultima_buena.clear()
    # Caches de proceso limpios y con los dos canales cargados.
    prompt_store._override.clear()
    prompt_store._override[""] = {}
    conocimiento._reglas.clear()
    conocimiento._correc.clear()
    agentes_custom._cache.clear()
    agentes_custom._cache[""] = {}

    with TestClient(app) as c:  # el lifespan carga prompts/conocimiento/agentes
        yield c

    bc.invalidar()
    bc._ultima_buena.clear()
    rc._pool = None


def _get(c, path, canal=""):
    r = c.get(path + (f"?canal={canal}" if canal else ""))
    assert r.status_code == 200, r.text
    return r.json()


def _post(c, path, body, canal=""):
    r = c.post(path + (f"?canal={canal}" if canal else ""), json=body)
    assert r.status_code == 200, r.text
    return r.json()


class TestPestanas:
    def test_los_dos_numeros_aparecen_aunque_no_tengan_conversaciones(self, cliente):
        """Un número recién dado de alta debe verse: si no, no hay dónde configurarlo."""
        d = _get(cliente, "/panel/api/chats")
        canales = {c["canal"]: c["nombre"] for c in d["canales"]}
        assert canales == {CA: "Tienda", CB: "Mayorista"}

    def test_muchas_conversaciones_y_sin_ultimo_de(self, cliente):
        """Con muchos chats viejos (sin `ultimo_de`) la lista tiene que responder igual.

        Esos chats obligan a reconstruir quién habló último leyendo el historial: en
        serie, con Redis remoto, el panel se quedaba en "Cargando…" y las pestañas no
        aparecían nunca (se pintan cuando termina esta llamada).
        """
        import json as _json

        from app.panel import events
        from app.settings import settings

        import app.redis_client as rc
        fake = rc._pool
        for i in range(40):
            chat = f"1809555{i:04d}"
            emisor = A if i % 2 else B
            fake.hashes.setdefault(events.CHATMETA_KEY, {})[chat] = _json.dumps(
                {"chat_id": chat, "emisor": emisor, "user_name": f"Cliente {i}",
                 "ultimo": "hola", "ultimo_ts": 1000 + i}  # sin ultimo_de: caso viejo
            )
            fake.listas[settings.key("session", chat)] = [
                _json.dumps({"role": "user", "content": "precio de botella 8 oz"})
            ]
        d = _get(cliente, "/panel/api/chats")
        assert d["total"] == 40
        por_canal = {c["canal"]: c["total"] for c in d["canales"]}
        assert por_canal == {CA: 20, CB: 20}
        # Se dedujo del historial quién habló último.
        assert {c["ultimo_de"] for c in d["chats"]} == {"cliente"}
        # Orden estable de las pestañas (por número), no por cantidad.
        assert [c["canal"] for c in d["canales"]] == sorted([CA, CB])


class TestSinCanal:
    """Conversaciones anteriores a que se guardara el emisor: no tienen número.

    Ojo con el id: si "Sin canal" usara "" (como "Todos"), hacerle clic mostraría
    TODAS las conversaciones y el panel diría que estás configurando un número.
    """

    def _viejas(self, cliente, n=3):
        import json as _json

        from app.panel import events

        import app.redis_client as rc
        fake = rc._pool
        for i in range(n):
            chat = f"1809888{i:04d}"
            fake.hashes.setdefault(events.CHATMETA_KEY, {})[chat] = _json.dumps(
                {"chat_id": chat, "user_name": f"Viejo {i}", "ultimo": "hola",
                 "ultimo_de": "cliente", "ultimo_ts": 500 + i}  # sin emisor
            )

    def _viejisima_sin_indice(self, cliente, chat="18098880999"):
        """Tan vieja que ni siquiera llegó a tener entrada en el índice del panel:
        sólo sobrevive su `pastoriza:session:*` (caso real tras migrar de otro Redis)."""
        import json as _json

        from app.settings import settings

        import app.redis_client as rc
        rc._pool.listas[settings.key("session", chat)] = [
            _json.dumps({"role": "user", "content": "precio de botella 8 oz"})
        ]
        return chat

    def test_van_a_su_propia_pestana_no_a_todos(self, cliente):
        self._viejas(cliente)
        d = _get(cliente, "/panel/api/chats")
        sin = [c for c in d["canales"] if c["canal"] == "-"]
        assert sin and sin[0]["nombre"] == "Sin canal" and sin[0]["total"] == 3
        assert all(c["canal"] == "-" for c in d["chats"])
        # Y NO se cuentan como de ninguno de los dos números.
        assert {c["canal"]: c["total"] for c in d["canales"] if c["canal"] != "-"} == {
            CA: 0, CB: 0
        }

    def test_asignarlas_las_mueve_al_numero_elegido(self, cliente):
        self._viejas(cliente)
        r = cliente.post(f"/panel/api/chats/asignar-canal?canal={CB}")
        assert r.status_code == 200, r.text
        assert r.json()["asignadas"] == 3
        d = _get(cliente, "/panel/api/chats")
        assert {c["canal"] for c in d["chats"]} == {CB}
        assert [c["total"] for c in d["canales"] if c["canal"] == CB] == [3]
        assert not [c for c in d["canales"] if c["canal"] == "-"]

    def test_no_toca_las_que_ya_tienen_numero(self, cliente):
        import json as _json

        from app.panel import events

        import app.redis_client as rc
        rc._pool.hashes.setdefault(events.CHATMETA_KEY, {})["18091112222"] = _json.dumps(
            {"chat_id": "18091112222", "emisor": A, "ultimo": "hola", "ultimo_de": "bot",
             "ultimo_ts": 900}
        )
        self._viejas(cliente, 2)
        assert cliente.post(f"/panel/api/chats/asignar-canal?canal={CB}").json()[
            "asignadas"
        ] == 2
        d = _get(cliente, "/panel/api/chats")
        por_chat = {c["chat_id"]: c["canal"] for c in d["chats"]}
        assert por_chat["18091112222"] == CA  # el que ya tenía número, intacto

    def test_sin_canal_no_se_puede_asignar(self, cliente):
        assert cliente.post("/panel/api/chats/asignar-canal").status_code == 400

    def test_asignar_una_por_una_al_numero_que_se_quiera(self, cliente):
        self._viejas(cliente, 3)
        d = _get(cliente, "/panel/api/chats")
        chats = sorted(c["chat_id"] for c in d["chats"])
        r = cliente.post(f"/panel/api/chats/{chats[0]}/asignar-canal?canal={CA}")
        assert r.status_code == 200, r.text
        assert r.json()["canal"] == CA
        r2 = cliente.post(f"/panel/api/chats/{chats[1]}/asignar-canal?canal={CB}")
        assert r2.status_code == 200, r2.text
        assert r2.json()["canal"] == CB

        d2 = _get(cliente, "/panel/api/chats")
        por_chat = {c["chat_id"]: c["canal"] for c in d2["chats"]}
        assert por_chat[chats[0]] == CA
        assert por_chat[chats[1]] == CB
        assert por_chat[chats[2]] == "-"  # la tercera, sin tocar

    def test_asignar_una_sin_numero_falla(self, cliente):
        self._viejas(cliente, 1)
        d = _get(cliente, "/panel/api/chats")
        chat_id = d["chats"][0]["chat_id"]
        assert cliente.post(f"/panel/api/chats/{chat_id}/asignar-canal").status_code == 400

    def test_asignar_una_inexistente_da_404(self, cliente):
        assert cliente.post(f"/panel/api/chats/no-existe/asignar-canal?canal={CA}").status_code == 404

    def test_asignar_una_sin_indice_pero_con_sesion(self, cliente):
        """Tan vieja que no llegó a tener entrada en el CRM: sólo su sesión. Antes del
        fix, ni el botón individual ni el masivo la encontraban (404 / se saltaba)."""
        chat = self._viejisima_sin_indice(cliente)
        d = _get(cliente, "/panel/api/chats")
        assert chat in [c["chat_id"] for c in d["chats"]]  # /api/chats ya la mostraba

        r = cliente.post(f"/panel/api/chats/{chat}/asignar-canal?canal={CA}")
        assert r.status_code == 200, r.text
        assert r.json()["canal"] == CA

        d2 = _get(cliente, "/panel/api/chats")
        por_chat = {c["chat_id"]: c["canal"] for c in d2["chats"]}
        assert por_chat[chat] == CA

    def test_asignar_masivo_incluye_las_sin_indice(self, cliente):
        self._viejas(cliente, 1)
        chat_solo_sesion = self._viejisima_sin_indice(cliente, "18098880998")
        r = cliente.post(f"/panel/api/chats/asignar-canal?canal={CB}")
        assert r.status_code == 200, r.text
        assert r.json()["asignadas"] == 2  # la vieja con índice + la de sólo sesión
        d = _get(cliente, "/panel/api/chats")
        por_chat = {c["chat_id"]: c["canal"] for c in d["chats"]}
        assert por_chat[chat_solo_sesion] == CB


class TestSemaforoEnLaLista:
    """El semáforo de cierre viaja en la lista, con su motivo y sin costo extra."""

    def _chat(self, cliente, chat, **extra):
        import json as _json

        from app.panel import events

        import app.redis_client as rc
        rc._pool.hashes.setdefault(events.CHATMETA_KEY, {})[chat] = _json.dumps(
            {"chat_id": chat, "emisor": A, "ultimo": "hola", "ultimo_de": "cliente",
             "ultimo_ts": 1000, **extra}
        )

    def test_llega_con_el_motivo_en_texto(self, cliente):
        self._chat(cliente, "18091110001", score=45, score_sem="verde",
                   score_hitos=["pidio_cuentas", "cotizo"])
        fila = _get(cliente, "/panel/api/chats")["chats"][0]
        assert fila["score"] == 45 and fila["sem"] == "verde"
        # Traducido para el operador, no el nombre interno del hito.
        assert fila["hitos"] == ["Pidió las cuentas", "Cotizó"]

    def test_un_chat_sin_semaforo_no_se_marca_como_frio(self, cliente):
        self._chat(cliente, "18091110002")
        fila = _get(cliente, "/panel/api/chats")["chats"][0]
        assert fila["score"] is None and fila["sem"] == "" and fila["hitos"] == []

    def test_avisa_cuando_un_ENVIO_no_tiene_comprobante(self, cliente):
        self._chat(cliente, "18091110007", score=100, score_sem="cerrado",
                   score_hitos=["pedido", "lineas", "entrega_envio"])
        assert _get(cliente, "/panel/api/chats")["chats"][0]["falta_pago"] is True
        self._chat(cliente, "18091110007", score=100, score_sem="cerrado",
                   score_hitos=["pedido", "entrega_envio", "comprobante"])
        assert _get(cliente, "/panel/api/chats")["chats"][0]["falta_pago"] is False

    def test_en_retiro_no_avisa_nada(self, cliente):
        """En retiro en tienda no se pide comprobante: avisar seria ruido."""
        self._chat(cliente, "18091110008", score=100, score_sem="cerrado",
                   score_hitos=["pedido", "lineas", "entrega_retiro"])
        fila = _get(cliente, "/panel/api/chats")["chats"][0]
        assert fila["falta_pago"] is False
        assert "Retiro en tienda" in fila["hitos"]

    def test_cuenta_los_que_estan_por_cerrar_por_canal(self, cliente):
        self._chat(cliente, "18091110003", score=60, score_sem="verde")
        self._chat(cliente, "18091110004", score=15, score_sem="amarillo")
        self._chat(cliente, "18091110005", score=100, score_sem="cerrado")
        por_canal = {c["canal"]: c for c in _get(cliente, "/panel/api/chats")["canales"]}
        assert por_canal[CA]["por_cerrar"] == 1
        assert por_canal[CA]["total"] == 3
        assert por_canal[CB]["por_cerrar"] == 0

    def test_hitos_viejos_o_desconocidos_no_rompen_la_lista(self, cliente):
        self._chat(cliente, "18091110006", score=10, score_sem="amarillo",
                   score_hitos=["hito_que_ya_no_existe"])
        assert _get(cliente, "/panel/api/chats")["chats"][0]["hitos"] == []


class TestCalcularSemaforoDeLosViejos:
    """Pintar las conversaciones que ya existían, leyendo su historial UNA vez."""

    def _chat_con_historial(self, chat, items, **extra):
        import json as _json

        from app.panel import events
        from app.settings import settings

        import app.redis_client as rc
        rc._pool.hashes.setdefault(events.CHATMETA_KEY, {})[chat] = _json.dumps(
            {"chat_id": chat, "emisor": A, "ultimo": "hola", "ultimo_de": "cliente",
             "ultimo_ts": 1234.0, **extra}
        )
        rc._pool.listas[settings.key("session", chat)] = [
            _json.dumps(i) for i in items
        ]

    def test_calcula_y_persiste(self, cliente):
        # Historial REALISTA: los tool-calls van en PAR (call + output), porque
        # `RedisSession.get_items` descarta los huérfanos (saneo del 400 de OpenAI).
        self._chat_con_historial("18091110010", [
            {"role": "user", "content": "a que cuenta deposito?"},
            {"type": "function_call", "call_id": "c1", "name": "cotizar", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1",
             "output": "COTIZACION (para mostrar al cliente):\nTOTAL: RD$5550.00"},
        ])
        r = cliente.post(f"/panel/api/chats/calcular-semaforo?canal={CA}")
        assert r.status_code == 200, r.text
        assert r.json()["calculadas"] == 1 and r.json()["con_senales"] == 1

        fila = _get(cliente, "/panel/api/chats")["chats"][0]
        assert fila["sem"] == "verde"
        assert "Pidió las cuentas" in fila["hitos"]
        # Y no volvió a la cola: ya no está pendiente.
        assert cliente.post(
            f"/panel/api/chats/calcular-semaforo?canal={CA}"
        ).json()["pendientes"] == 0

    def test_no_toca_el_ultimo_mensaje_ni_la_hora(self, cliente):
        """Si moviera `ultimo_ts`, todas las conversaciones viejas saltarían a 'ahora'."""
        self._chat_con_historial("18091110011", [
            {"role": "user", "content": "ya te transferi"},
        ], ultimo="ya te transferi", ultimo_de="cliente")
        cliente.post(f"/panel/api/chats/calcular-semaforo?canal={CA}")
        fila = _get(cliente, "/panel/api/chats")["chats"][0]
        assert fila["ultimo_ts"] == 1234.0
        assert fila["ultimo"] == "ya te transferi"
        assert fila["ultimo_de"] == "cliente"

    def test_no_recalcula_lo_que_ya_tiene_salvo_que_se_pida(self, cliente):
        self._chat_con_historial("18091110012", [
            {"role": "user", "content": "a que cuenta deposito?"},
        ], score=0, score_sem="gris", score_hitos=[])
        assert cliente.post(
            f"/panel/api/chats/calcular-semaforo?canal={CA}"
        ).json()["calculadas"] == 0
        # Con `rehacer` sí, y ahí corrige lo que estaba mal.
        assert cliente.post(
            f"/panel/api/chats/calcular-semaforo?canal={CA}&rehacer=true"
        ).json()["calculadas"] == 1
        assert _get(cliente, "/panel/api/chats")["chats"][0]["sem"] == "amarillo"

    def test_solo_el_canal_pedido(self, cliente):
        self._chat_con_historial("18091110013", [
            {"role": "user", "content": "a que cuenta deposito?"},
        ])
        assert cliente.post(
            f"/panel/api/chats/calcular-semaforo?canal={CB}"
        ).json()["calculadas"] == 0


class TestRendimientoYFallas:
    """La lista de chats es lo que más se refresca: no puede costar una ida a Redis
    por conversación, ni mentir cuando Redis no responde."""

    def test_una_sola_lectura_para_saber_quien_esta_pausado(self, cliente):
        import json as _json

        from app.panel import events

        import app.redis_client as rc
        fake = rc._pool
        for i in range(30):
            chat = f"1809666{i:04d}"
            fake.hashes.setdefault(events.CHATMETA_KEY, {})[chat] = _json.dumps(
                {"chat_id": chat, "emisor": A, "ultimo": "hola", "ultimo_de": "bot",
                 "ultimo_ts": 1000 + i}
            )
        fake.contar = True
        fake.ops.clear()
        d = _get(cliente, "/panel/api/chats")
        assert d["total"] == 30
        # Un `get` por chat serían 30+ lecturas; con MGET es una sola.
        assert fake.ops.count("mget") == 1
        assert fake.ops.count("get") == 0

    def test_no_dispara_una_conexion_por_conversacion(self, cliente):
        """Regresión de producción: `ConnectionError: max number of clients reached`.

        Paralelizar la lista sin tope pedía una conexión por chat (cientos a la vez);
        Redis empezaba a rechazar TODO, incluido el bot atendiendo clientes.
        """
        import json as _json

        from app.panel import events, router as pr
        from app.settings import settings

        import app.redis_client as rc
        fake = rc._pool
        for i in range(200):
            chat = f"1809777{i:04d}"
            fake.hashes.setdefault(events.CHATMETA_KEY, {})[chat] = _json.dumps(
                {"chat_id": chat, "emisor": A, "ultimo": "hola", "ultimo_ts": 1000 + i}
            )  # sin `ultimo_de`: obliga a leer el historial de cada uno
            fake.listas[settings.key("session", chat)] = [
                _json.dumps({"role": "user", "content": "hola"})
            ]
        fake.max_en_vuelo = 0
        d = _get(cliente, "/panel/api/chats")
        assert d["total"] == 200
        assert 0 < fake.max_en_vuelo <= pr._CONCURRENCIA, (
            f"{fake.max_en_vuelo} lecturas a la vez: tiene que estar acotado"
        )

    def test_si_redis_falla_lo_dice_en_vez_de_mostrar_cero(self, cliente, monkeypatch):
        """Regresión: "no hay conversaciones" y "Redis caído" se veían IGUAL."""
        from app.panel import events

        async def _explota(estricto: bool = False):
            raise ConnectionError("Error 111 connecting to redis: rechazado")

        monkeypatch.setattr(events, "todos_chatmeta", _explota)
        d = _get(cliente, "/panel/api/chats")
        assert d["chats"] == []
        assert "ConnectionError" in d["degradado"]
        # Las pestañas siguen saliendo (de la config), para poder operar.
        assert {c["canal"] for c in d["canales"]} == {CA, CB}

    def test_sin_fallas_no_marca_degradado(self, cliente):
        assert _get(cliente, "/panel/api/chats")["degradado"] == ""


class TestConfig:
    def test_guardar_en_un_canal_no_toca_el_otro(self, cliente):
        _post(cliente, "/panel/api/config",
              {"precio_envio": "700", "monto_minimo": "1000"}, canal=CB)
        assert _get(cliente, "/panel/api/config", CB)["precio_envio"] == "700"
        assert _get(cliente, "/panel/api/config", CA)["precio_envio"] == "550"
        assert _get(cliente, "/panel/api/config")["precio_envio"] == "550"

    def test_el_panel_sabe_que_campos_son_propios(self, cliente):
        _post(cliente, "/panel/api/config", {"precio_envio": "700"}, canal=CB)
        assert "precio_envio" in _get(cliente, "/panel/api/config", CB)["_propios"]
        assert _get(cliente, "/panel/api/config", CA)["_propios"] == []

    def test_aplicar_a_los_dos(self, cliente):
        _post(cliente, "/panel/api/config", {"precio_envio": "700"}, canal=CB)
        _post(cliente, "/panel/api/config",
              {"precio_envio": "900", "canales": CANALES, "_ambos": True}, canal=CB)
        assert _get(cliente, "/panel/api/config", CA)["precio_envio"] == "900"
        assert _get(cliente, "/panel/api/config", CB)["precio_envio"] == "900"

    def test_volver_a_la_comun(self, cliente):
        _post(cliente, "/panel/api/config", {"precio_envio": "700"}, canal=CB)
        r = cliente.request("DELETE", f"/panel/api/config?canal={CB}")
        assert r.status_code == 200, r.text
        assert _get(cliente, "/panel/api/config", CB)["precio_envio"] == "550"

    def test_sin_canal_no_se_puede_resetear(self, cliente):
        assert cliente.request("DELETE", "/panel/api/config").status_code == 400


class TestPrompts:
    LARGO = "Sos Michelle y atendes SOLO al mayorista del 6701, con precios por fardo."

    def test_el_prompt_guardado_en_un_canal_no_aplica_al_otro(self, cliente):
        d = _post(cliente, "/panel/api/prompts/ventas", {"override": self.LARGO}, canal=CB)
        assert d["origen"] == "canal"
        de_b = _get(cliente, "/panel/api/prompts", CB)["prompts"]["ventas"]
        de_a = _get(cliente, "/panel/api/prompts", CA)["prompts"]["ventas"]
        assert de_b["override"] == self.LARGO and de_b["usando_override"] is True
        assert de_a["usando_override"] is False and de_a["origen"] == "base"

    def test_aplicar_a_los_dos(self, cliente):
        _post(cliente, "/panel/api/prompts/ventas",
              {"override": self.LARGO, "ambos": True}, canal=CB)
        for canal in (CA, CB):
            p = _get(cliente, "/panel/api/prompts", canal)["prompts"]["ventas"]
            assert p["override"] == self.LARGO, canal
            assert p["origen"] == "comun"

    def test_borrar_el_propio_vuelve_al_heredado(self, cliente):
        _post(cliente, "/panel/api/prompts/ventas",
              {"override": self.LARGO, "ambos": True}, canal=CB)
        otro = "Sos Michelle del 6701 y solo hablas de envases para exportacion."
        _post(cliente, "/panel/api/prompts/ventas", {"override": otro}, canal=CB)
        assert _get(cliente, "/panel/api/prompts", CB)["prompts"]["ventas"]["override"] == otro
        _post(cliente, "/panel/api/prompts/ventas", {"override": ""}, canal=CB)
        p = _get(cliente, "/panel/api/prompts", CB)["prompts"]["ventas"]
        assert p["override"] == self.LARGO and p["origen"] == "comun"

    def test_prompt_corto_se_rechaza(self, cliente):
        r = cliente.post(f"/panel/api/prompts/ventas?canal={CB}", json={"override": "corto"})
        assert r.status_code == 400


class TestAprendizaje:
    def test_una_regla_del_canal_no_se_ve_en_el_otro(self, cliente):
        _post(cliente, "/panel/api/reglas", {"texto": "Al 6701 se cobra envio aparte"}, canal=CB)
        de_b = [r["texto"] for r in _get(cliente, "/panel/api/aprendizaje", CB)["reglas"]]
        de_a = [r["texto"] for r in _get(cliente, "/panel/api/aprendizaje", CA)["reglas"]]
        assert de_b == ["Al 6701 se cobra envio aparte"]
        assert de_a == []

    def test_una_regla_para_los_dos(self, cliente):
        _post(cliente, "/panel/api/reglas",
              {"texto": "Nunca prometer entrega el mismo dia", "ambos": True}, canal=CB)
        for canal in (CA, CB):
            reglas = _get(cliente, "/panel/api/aprendizaje", canal)["reglas"]
            assert [r["canal"] for r in reglas] == [""], canal

    def test_correccion_por_canal(self, cliente):
        _post(cliente, "/panel/api/correcciones",
              {"situacion": "pide descuento", "respuesta_correcta": "pasalo a un asesor"},
              canal=CB)
        assert len(_get(cliente, "/panel/api/aprendizaje", CB)["correcciones"]) == 1
        assert _get(cliente, "/panel/api/aprendizaje", CA)["correcciones"] == []


class TestAgentes:
    PROMPT = "Sos Michelle y atendes compras al por mayor con precios por fardo cerrado."

    def test_un_agente_creado_en_un_canal_no_existe_en_el_otro(self, cliente):
        _post(cliente, "/panel/api/agentes", {
            "nombre": "mayorista", "descripcion": "al por mayor",
            "herramientas": ["catalogo"], "palabras": ["al por mayor"],
            "modelo": "mini", "prompt": self.PROMPT,
        }, canal=CB)
        en_b = [a["nombre"] for a in _get(cliente, "/panel/api/prompts", CB)["personalizados"]]
        en_a = [a["nombre"] for a in _get(cliente, "/panel/api/prompts", CA)["personalizados"]]
        assert en_b == ["mayorista"]
        assert en_a == []

    def test_un_agente_para_los_dos(self, cliente):
        _post(cliente, "/panel/api/agentes", {
            "nombre": "mayorista", "descripcion": "al por mayor",
            "herramientas": ["catalogo"], "palabras": ["al por mayor"],
            "modelo": "mini", "prompt": self.PROMPT, "ambos": True,
        }, canal=CB)
        for canal in (CA, CB):
            en = _get(cliente, "/panel/api/prompts", canal)["personalizados"]
            assert [a["nombre"] for a in en] == ["mayorista"], canal
            assert en[0]["canal"] == ""


class TestMoverElSemaforoAMano:
    """El supervisor puede mover una conversación a la columna que quiera.

    Pedido de la operación: "permíteme en semáforo poder cambiar los estados de crear
    pedido y así, o sea yo poder moverlo a donde quiera, también aparte de que tú lo
    haces". Lo manual GANA, pero el cálculo NO se pisa: se guarda aparte, así que
    volver al automático no perdió nada.
    """

    CHAT = "18091110050"

    def _chat(self, **extra) -> None:
        import json as _json

        from app.panel import events

        import app.redis_client as rc
        rc._pool.hashes.setdefault(events.CHATMETA_KEY, {})[self.CHAT] = _json.dumps({
            "chat_id": self.CHAT, "emisor": A, "ultimo": "hola",
            "ultimo_de": "cliente", "ultimo_ts": 1234.0,
            "score": 100, "score_sem": "cerrado", "score_hitos": ["pedido"],
            **extra,
        })

    def _fila(self, cliente) -> dict:
        return _get(cliente, f"/panel/api/chats?canal={CA}")["chats"][0]

    def test_lo_movido_a_mano_gana(self, cliente):
        self._chat()
        assert self._fila(cliente)["sem"] == "cerrado"

        r = cliente.post(f"/panel/api/chats/{self.CHAT}/semaforo",
                         json={"sem": "gris"})
        assert r.status_code == 200, r.text

        fila = self._fila(cliente)
        assert fila["sem"] == "gris"
        assert fila["sem_manual"] == "gris"

    def test_el_calculo_automatico_NO_se_pierde(self, cliente):
        """Es lo que hace reversible el movimiento: el panel puede volver a él."""
        self._chat()
        cliente.post(f"/panel/api/chats/{self.CHAT}/semaforo", json={"sem": "gris"})
        fila = self._fila(cliente)
        assert fila["sem_auto"] == "cerrado"
        assert fila["score"] == 100
        assert "Pedido creado" in fila["hitos"]

    def test_volver_al_automatico(self, cliente):
        self._chat()
        cliente.post(f"/panel/api/chats/{self.CHAT}/semaforo", json={"sem": "verde"})
        r = cliente.post(f"/panel/api/chats/{self.CHAT}/semaforo", json={"sem": ""})
        assert r.status_code == 200

        fila = self._fila(cliente)
        assert fila["sem"] == "cerrado", "vuelve a lo que dice el cálculo"
        assert fila["sem_manual"] == ""

    def test_recalcular_no_borra_lo_que_movio_la_persona(self, cliente):
        """Si «Recalcular todo» pisara lo manual, el trabajo del supervisor se perdería."""
        self._chat()
        cliente.post(f"/panel/api/chats/{self.CHAT}/semaforo", json={"sem": "amarillo"})
        cliente.post(f"/panel/api/chats/calcular-semaforo?canal={CA}&rehacer=true")
        assert self._fila(cliente)["sem_manual"] == "amarillo"

    def test_un_color_inventado_se_rechaza(self, cliente):
        self._chat()
        r = cliente.post(f"/panel/api/chats/{self.CHAT}/semaforo",
                         json={"sem": "azul"})
        assert r.status_code == 400
        assert self._fila(cliente)["sem"] == "cerrado"

    def test_mover_no_cambia_el_ultimo_mensaje_ni_la_hora(self, cliente):
        """Mover de columna no es actividad de la conversación: no debe reordenarla."""
        self._chat()
        cliente.post(f"/panel/api/chats/{self.CHAT}/semaforo", json={"sem": "verde"})
        fila = self._fila(cliente)
        assert fila["ultimo_ts"] == 1234.0 and fila["ultimo"] == "hola"
        assert fila["ultimo_de"] == "cliente"

    def test_pide_token(self, cliente, monkeypatch):
        from app.settings import settings

        monkeypatch.setattr(settings, "panel_token", "secreto")
        self._chat()
        r = cliente.post(f"/panel/api/chats/{self.CHAT}/semaforo", json={"sem": "verde"})
        assert r.status_code == 401
