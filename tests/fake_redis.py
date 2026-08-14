"""Redis en memoria para los tests del panel (no es un fixture: se importa).

Sólo implementa lo que usan las rutas del panel. Se inyecta en
`app.redis_client._pool`, así que TODO el código (incluidos los módulos que
importaron `run_write`/`with_reconnect` directo) pega contra este doble sin
tocar nada más.
"""
from __future__ import annotations

from typing import Any


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.listas: dict[str, list[str]] = {}
        self.seq: dict[str, int] = {}

    # --- strings ---
    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def set(self, key: str, val: Any, nx: bool = False, ex: int | None = None):
        if nx and key in self.kv:
            return None
        self.kv[key] = str(val)
        return True

    async def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            n += int(self.kv.pop(k, None) is not None)
            n += int(self.hashes.pop(k, None) is not None)
            n += int(self.listas.pop(k, None) is not None)
        return n

    async def incr(self, key: str) -> int:
        self.seq[key] = self.seq.get(key, 0) + 1
        return self.seq[key]

    async def expire(self, *a, **kw) -> bool:
        return True

    async def ping(self) -> bool:
        return True

    async def eval(self, *a, **kw) -> int:
        return 1

    # --- hashes ---
    async def hset(self, key: str, campo: str, val: str) -> int:
        self.hashes.setdefault(key, {})[campo] = val
        return 1

    async def hget(self, key: str, campo: str) -> str | None:
        return self.hashes.get(key, {}).get(campo)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hdel(self, key: str, *campos: str) -> int:
        h = self.hashes.get(key, {})
        return sum(int(h.pop(c, None) is not None) for c in campos)

    # --- listas ---
    async def lpush(self, key: str, *vals: str) -> int:
        self.listas.setdefault(key, [])
        for v in vals:
            self.listas[key].insert(0, v)
        return len(self.listas[key])

    async def rpush(self, key: str, *vals: str) -> int:
        self.listas.setdefault(key, []).extend(vals)
        return len(self.listas[key])

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        items = self.listas.get(key, [])
        return items[start:] if stop == -1 else items[start : stop + 1]

    async def ltrim(self, key: str, start: int, stop: int) -> bool:
        if key in self.listas:
            self.listas[key] = self.listas[key][start : stop + 1]
        return True

    # --- scan ---
    async def scan_iter(self, match: str = "*", count: int = 100):
        pref = match.rstrip("*")
        for k in list(self.kv) + list(self.hashes) + list(self.listas):
            if k.startswith(pref):
                yield k

    def pipeline(self) -> "FakePipe":
        return FakePipe(self)

    async def aclose(self) -> None:
        return None


class FakePipe:
    def __init__(self, r: FakeRedis) -> None:
        self.r = r
        self.ops: list[tuple[str, tuple]] = []

    def __getattr__(self, nombre: str):
        def _encolar(*args, **kw):
            self.ops.append((nombre, args))
            return self

        return _encolar

    async def execute(self) -> list:
        out = []
        for nombre, args in self.ops:
            out.append(await getattr(self.r, nombre)(*args))
        self.ops = []
        return out
