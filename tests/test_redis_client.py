"""Tests de la capa Redis: liberación del lock de conversación y política de
resiliencia por idempotencia (ADR-007).

El lock de conversación NO estaba cubierto por ningún test; por eso el bug de la
variable `r` inexistente en el `finally` (el lock nunca se liberaba, sobrevivía
120s por TTL) pasó desapercibido. Estos tests son la red que faltaba.

Usan un fake de Redis autocontenido (sin dependencias) inyectado por monkeypatch.
"""
from __future__ import annotations

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app import redis_client as rc


class FakeRedis:
    """Emula lo mínimo que usa conversation_lock: SET NX EX y el EVAL de release."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.eval_calls = 0

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def eval(self, script, numkeys, key, arg):
        self.eval_calls += 1
        # Compare-and-delete: borra solo si el token sigue siendo el nuestro.
        if self.store.get(key) == arg:
            del self.store[key]
            return 1
        return 0


async def test_lock_se_libera_al_salir(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(rc, "get_redis", lambda: fake)
    key = rc.settings.key("lock", "c1")

    async with rc.conversation_lock("c1") as got:
        assert got is True
        assert key in fake.store  # tomado durante el turno

    # Tras salir del `with`, el lock DEBE estar liberado (antes no lo estaba).
    assert key not in fake.store
    assert fake.eval_calls == 1  # el release corrió de verdad


async def test_lock_readquirible_de_inmediato(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(rc, "get_redis", lambda: fake)

    async with rc.conversation_lock("c1") as got:
        assert got is True
    # El follow-up del cliente dentro de los 120s debe poder procesarse.
    async with rc.conversation_lock("c1") as got2:
        assert got2 is True


async def test_lock_ocupado_devuelve_false(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(rc, "get_redis", lambda: fake)

    async with rc.conversation_lock("c1") as primero:
        assert primero is True
        async with rc.conversation_lock("c1") as segundo:
            assert segundo is False  # ya hay un turno corriendo


async def test_lock_degradado_no_intenta_liberar(monkeypatch):
    """Si Redis falla al tomar el lock, degradamos (got=True) pero NO hay lock
    real que soltar: el release no debe ejecutarse."""
    fake = FakeRedis()

    async def set_falla(*a, **k):
        raise RedisConnectionError("blip")

    fake.set = set_falla  # type: ignore[assignment]
    monkeypatch.setattr(rc, "get_redis", lambda: fake)

    async def noop():
        return None

    monkeypatch.setattr(rc, "close_redis", noop)

    async with rc.conversation_lock("c1") as got:
        assert got is True  # degradado: procesamos igual
    assert fake.eval_calls == 0  # no había lock que liberar


async def test_run_write_no_reintenta(monkeypatch):
    """Escritura no idempotente: 1 solo intento, devuelve None y descarta el pool."""
    llamadas = {"n": 0}

    async def op(r):
        llamadas["n"] += 1
        raise RedisConnectionError("boom")

    cerrados = {"n": 0}

    async def fake_close():
        cerrados["n"] += 1

    monkeypatch.setattr(rc, "get_redis", lambda: object())
    monkeypatch.setattr(rc, "close_redis", fake_close)

    res = await rc.run_write(op)
    assert res is None
    assert llamadas["n"] == 1  # NO reintenta (evitaría duplicar historial/eventos)
    assert cerrados["n"] == 1


async def test_with_reconnect_reintenta_y_recrea_pool(monkeypatch):
    """Lectura: reintenta recreando el pool hasta que una vez funciona."""
    llamadas = {"n": 0}

    async def op(r):
        llamadas["n"] += 1
        if llamadas["n"] < 3:
            raise RedisTimeoutError("blip")
        return "ok"

    cerrados = {"n": 0}

    async def fake_close():
        cerrados["n"] += 1

    monkeypatch.setattr(rc, "get_redis", lambda: object())
    monkeypatch.setattr(rc, "close_redis", fake_close)

    res = await rc.with_reconnect(op, attempts=3)
    assert res == "ok"
    assert llamadas["n"] == 3
    assert cerrados["n"] == 2  # recreó el pool tras cada uno de los 2 fallos
