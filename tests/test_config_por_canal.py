"""Configuración y conocimiento POR CANAL (los dos números de YCloud).

Requisito de operación, textual: "si yo realizo un cambio en el 6701 no se debe
aplicar al 1092 a menos que yo lo coloque en ambos". Estos tests son el candado:
verifican que lo guardado dentro de un canal NO se filtre al otro, que sin valores
propios se herede la base común, y que "aplicar a ambos" sí iguale a los dos.

Corre sin Redis: se inyecta un doble en memoria.
"""
from __future__ import annotations

import json

import pytest

from app import business_config as bc
from app.panel import conocimiento

A = "18099221092"   # canal 1092
B = "+1 829 471-6701"  # canal 6701
CA, CB = "8099221092", "8294716701"


class FakeRedis:
    """Lo mínimo que usan business_config y conocimiento."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.listas: dict[str, list[str]] = {}
        self.seq = 0

    # --- strings ---
    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, val):
        self.kv[key] = val

    async def delete(self, *keys):
        for k in keys:
            self.kv.pop(k, None)
            self.listas.pop(k, None)

    async def incr(self, key):
        self.seq += 1
        return self.seq

    # --- listas ---
    async def lpush(self, key, *vals):
        self.listas.setdefault(key, [])
        for v in vals:
            self.listas[key].insert(0, v)

    async def rpush(self, key, *vals):
        self.listas.setdefault(key, []).extend(vals)

    async def lrange(self, key, start, stop):
        items = self.listas.get(key, [])
        return items if stop == -1 else items[start : stop + 1]

    def pipeline(self):
        return _Pipe(self)


class _Pipe:
    def __init__(self, r: FakeRedis) -> None:
        self.r = r
        self.ops: list = []

    def delete(self, *k):
        self.ops.append(("delete", k))
        return self

    def rpush(self, k, *v):
        self.ops.append(("rpush", (k, *v)))
        return self

    def lpush(self, k, *v):
        self.ops.append(("lpush", (k, *v)))
        return self

    def ltrim(self, *a):
        return self

    async def execute(self):
        for nombre, args in self.ops:
            await getattr(self.r, nombre)(*args)
        self.ops = []


@pytest.fixture
def redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(bc, "get_redis", lambda: r)

    async def _with_reconnect(op):
        return await op(r)

    async def _run_write(op):
        return await op(r)

    import app.redis_client as rc

    monkeypatch.setattr(rc, "with_reconnect", _with_reconnect)
    monkeypatch.setattr(rc, "run_write", _run_write)
    monkeypatch.setattr(conocimiento, "with_reconnect", _with_reconnect)
    monkeypatch.setattr(conocimiento, "run_write", _run_write)
    bc.invalidar()
    bc._ultima_buena.clear()
    # Config común de partida: los dos canales declarados.
    r.kv[bc.CONFIG_KEY] = json.dumps(
        {"precio_envio": "550", "monto_minimo": "1000",
         "canales": "18099221092 = Tienda\n18294716701 = Mayorista"}
    )
    yield r
    bc.invalidar()
    bc._ultima_buena.clear()


class TestConfigPorCanal:
    @pytest.mark.asyncio
    async def test_sin_propios_hereda_la_comun(self, redis):
        cfg = await bc.load_config(A, force=True)
        assert cfg.precio_envio == "550"

    @pytest.mark.asyncio
    async def test_un_cambio_en_un_canal_no_toca_al_otro(self, redis):
        await bc.save_config({"precio_envio": "700", "monto_minimo": "1000"}, canal=B)
        assert (await bc.load_config(B, force=True)).precio_envio == "700"
        # El 1092 sigue con lo común. Esto es EL requisito.
        assert (await bc.load_config(A, force=True)).precio_envio == "550"
        assert (await bc.load_config("", force=True)).precio_envio == "550"

    @pytest.mark.asyncio
    async def test_aplicar_a_ambos_iguala_los_dos(self, redis):
        await bc.save_config({"precio_envio": "700"}, canal=B)
        await bc.save_config(
            {"precio_envio": "900", "canales": "18099221092 = Tienda\n18294716701 = Mayorista"},
            canal=B, ambos=True,
        )
        assert (await bc.load_config(A, force=True)).precio_envio == "900"
        assert (await bc.load_config(B, force=True)).precio_envio == "900"
        # Y el canal ya no tiene valores propios pisando a la común.
        assert await bc.overrides_de_canal(B) == {}

    @pytest.mark.asyncio
    async def test_la_lista_de_canales_no_se_guarda_por_canal(self, redis):
        """Editar desde un canal no puede hacer desaparecer al otro del panel."""
        await bc.save_config(
            {"precio_envio": "700", "canales": "18099221092 = Solo yo"}, canal=B
        )
        assert "canales" not in await bc.overrides_de_canal(B)
        # La lista quedó en la común (y por eso se ve en los dos).
        assert (await bc.load_config(B, force=True)).canales == "18099221092 = Solo yo"

    @pytest.mark.asyncio
    async def test_resetear_vuelve_a_la_comun(self, redis):
        await bc.save_config({"precio_envio": "700"}, canal=A)
        assert (await bc.load_config(A, force=True)).precio_envio == "700"
        await bc.resetear_canal(A)
        assert (await bc.load_config(A, force=True)).precio_envio == "550"

    @pytest.mark.asyncio
    async def test_campos_no_tocados_siguen_a_la_comun(self, redis):
        """Guardar sólo el envío de un canal no le borra el resto."""
        await bc.save_config({"precio_envio": "700"}, canal=A)
        cfg = await bc.load_config(A, force=True)
        assert cfg.precio_envio == "700"
        assert cfg.monto_minimo == "1000"

    @pytest.mark.asyncio
    async def test_canales_configurados(self, redis):
        assert set(await bc.canales_configurados()) == {CA, CB}

    @pytest.mark.asyncio
    async def test_el_formato_del_numero_no_parte_el_canal(self, redis):
        await bc.save_config({"precio_envio": "700"}, canal="+1 809-922-1092")
        for forma in ("18099221092", "8099221092", "+1 809 922 1092"):
            assert (await bc.load_config(forma, force=True)).precio_envio == "700", forma

    @pytest.mark.asyncio
    async def test_redis_caido_no_vuelve_a_los_precios_de_fabrica(self, redis, monkeypatch):
        """Regresión: un blip de Redis NO puede resucitar precios hardcodeados."""
        await bc.save_config({"precio_envio": "700"}, canal=A)
        assert (await bc.load_config(A, force=True)).precio_envio == "700"

        async def _explota(op):
            raise RuntimeError("redis caido")

        import app.redis_client as rc
        monkeypatch.setattr(rc, "with_reconnect", _explota)
        assert (await bc.load_config(A, force=True)).precio_envio == "700"


class TestConocimientoPorCanal:
    @pytest.mark.asyncio
    async def test_una_regla_de_un_canal_no_se_inyecta_en_el_otro(self, redis):
        await conocimiento.add_regla("Al 6701 se le cobra envio aparte", canal=B)
        await conocimiento.cargar()
        assert "envio aparte" in conocimiento.get_bloque_inyeccion(B)
        assert "envio aparte" not in conocimiento.get_bloque_inyeccion(A)
        assert "envio aparte" not in conocimiento.get_bloque_inyeccion("")

    @pytest.mark.asyncio
    async def test_una_regla_comun_se_inyecta_en_los_dos(self, redis):
        await conocimiento.add_regla("Nunca prometer entrega el mismo dia", ambos=True)
        await conocimiento.cargar()
        for canal in (A, B, ""):
            assert "mismo dia" in conocimiento.get_bloque_inyeccion(canal)

    @pytest.mark.asyncio
    async def test_el_listado_del_canal_incluye_las_comunes_marcadas(self, redis):
        await conocimiento.add_regla("comun", ambos=True)
        await conocimiento.add_regla("solo del 6701", canal=B)
        reglas = await conocimiento.list_reglas(B)
        por_texto = {r["texto"]: r["canal"] for r in reglas}
        assert por_texto == {"solo del 6701": CB, "comun": ""}
        # El otro canal sólo ve la común.
        assert [r["texto"] for r in await conocimiento.list_reglas(A)] == ["comun"]

    @pytest.mark.asyncio
    async def test_borrar_una_regla_del_canal_no_toca_la_comun(self, redis):
        comun = await conocimiento.add_regla("comun", ambos=True)
        propia = await conocimiento.add_regla("del 6701", canal=B)
        await conocimiento.del_regla(propia["id"], canal=B)
        assert [r["texto"] for r in await conocimiento.list_reglas(B)] == ["comun"]
        await conocimiento.del_regla(comun["id"], canal=B)
        assert await conocimiento.list_reglas(B) == []

    @pytest.mark.asyncio
    async def test_correcciones_por_canal(self, redis):
        await conocimiento.add_correccion("pide descuento", "pasalo a un asesor", canal=B)
        await conocimiento.cargar()
        assert "pide descuento" in conocimiento.get_bloque_inyeccion(B)
        assert "pide descuento" not in conocimiento.get_bloque_inyeccion(A)

    @pytest.mark.asyncio
    async def test_sugerencia_aprobada_va_al_canal_de_donde_salio(self, redis):
        s = await conocimiento.add_sugerencia("regla", "avisar del minimo", canal=B)
        await conocimiento.aprobar_sugerencia(s["id"])
        await conocimiento.cargar()
        assert "avisar del minimo" in conocimiento.get_bloque_inyeccion(B)
        assert "avisar del minimo" not in conocimiento.get_bloque_inyeccion(A)

    @pytest.mark.asyncio
    async def test_las_sugerencias_se_ven_en_su_canal_y_las_comunes_en_todos(self, redis):
        await conocimiento.add_sugerencia("regla", "de B", canal=B)
        await conocimiento.add_sugerencia("regla", "comun")
        de_a = {s["contenido"] for s in await conocimiento.list_sugerencias(canal=A)}
        de_b = {s["contenido"] for s in await conocimiento.list_sugerencias(canal=B)}
        assert de_a == {"comun"}
        assert de_b == {"de B", "comun"}
