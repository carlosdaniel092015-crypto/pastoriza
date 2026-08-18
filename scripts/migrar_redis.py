"""Copia las keys `pastoriza:*` de un Redis a otro. Para mudarse de proveedor.

POR QUÉ EXISTE: en Redis no vive sólo "cache". Vive la CONFIG DE NEGOCIO (precios,
mínimos, mensajes, los dos canales), los PROMPTS editados desde el panel, las REGLAS
aprendidas, la cola de revisión y el índice de conversaciones del CRM. Levantar el bot
apuntando a un Redis vacío no rompe nada de forma visible: arranca con los defaults del
código y el panel aparece sin conversaciones, como si el negocio empezara de cero.

    # desde tu máquina, con acceso a los dos Redis
    python -m scripts.migrar_redis --origen redis://...cloud:6379/1 --destino redis://localhost:6379/1

Sin `--si` sólo MUESTRA lo que haría. El origen se lee y NUNCA se escribe.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import redis.asyncio as aioredis

PREFIJO = "pastoriza:"
# Las keys efímeras no vale la pena moverlas: el debounce y los locks son de segundos
# y copiarlos con TTL viejo sólo puede confundir. La sesión SÍ se copia: es el
# historial de la conversación, y perderlo hace que el bot le pregunte de nuevo al
# cliente lo que ya había dicho.
SALTAR = ("buffer:", "last_id:", "lock:", "bot_msg:")


def _saltar(key: str) -> bool:
    resto = key[len(PREFIJO):] if key.startswith(PREFIJO) else key
    return resto.startswith(SALTAR)


async def _copiar_por_tipo(src: aioredis.Redis, dst: aioredis.Redis, raw: bytes) -> None:
    """Fallback cuando DUMP/RESTORE falla (típico migrando desde Redis Cloud/Enterprise:
    el proxy devuelve el DUMP en un formato binario que un Redis OSS no siempre puede
    RESTORE-ar, aunque las versiones sean compatibles). Copia con comandos normales,
    tipo por tipo, en vez de binario."""
    tipo = await src.type(raw)
    tipo = tipo.decode() if isinstance(tipo, bytes) else tipo
    if tipo == "none":  # expiró entre el scan y esta lectura
        return
    if tipo != "string":
        await dst.delete(raw)  # para que --si se pueda correr dos veces sin duplicar
    if tipo == "string":
        valor = await src.get(raw)
        if valor is not None:
            await dst.set(raw, valor)
    elif tipo == "hash":
        campos = await src.hgetall(raw)
        if campos:
            await dst.hset(raw, mapping=campos)
    elif tipo == "list":
        elementos = await src.lrange(raw, 0, -1)
        if elementos:
            await dst.rpush(raw, *elementos)
    elif tipo == "set":
        miembros = await src.smembers(raw)
        if miembros:
            await dst.sadd(raw, *miembros)
    elif tipo == "zset":
        pares = await src.zrange(raw, 0, -1, withscores=True)
        if pares:
            await dst.zadd(raw, dict(pares))
    else:
        raise ValueError(f"tipo no soportado: {tipo}")


async def migrar(origen: str, destino: str, aplicar: bool, patron: str) -> int:
    try:
        src = aioredis.from_url(origen, decode_responses=False)
        dst = aioredis.from_url(destino, decode_responses=False)
    except ValueError as exc:
        # Una URL mal escrita es el error más probable acá: que se lea, no un traceback.
        print(f"ERROR en la URL: {exc}")
        print("Formato: redis://usuario:clave@host:6379/1 (o rediss:// con TLS)")
        return 2
    try:
        await src.ping()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: no pude conectar al ORIGEN: {exc}")
        return 2
    try:
        await dst.ping()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: no pude conectar al DESTINO: {exc}")
        return 2

    copiadas = saltadas = fallidas = 0
    async for raw in src.scan_iter(match=patron, count=200):
        key = raw.decode() if isinstance(raw, bytes) else str(raw)
        if _saltar(key):
            saltadas += 1
            continue

        # DUMP/RESTORE copia el valor con su tipo exacto (hash, lista, string) sin
        # que este script tenga que saber de qué tipo es cada key.
        valor = await src.dump(raw)
        if valor is None:  # expiró entre el scan y el dump
            saltadas += 1
            continue
        ttl = await src.pttl(raw)

        if not aplicar:
            resto = f"{ttl} ms" if ttl and ttl > 0 else "sin vencimiento"
            print(f"  copiaría {key}  ({resto})")
            copiadas += 1
            continue

        try:
            # replace=True: se puede correr dos veces sin duplicar ni fallar.
            await dst.restore(raw, ttl if ttl and ttl > 0 else 0, valor, replace=True)
            copiadas += 1
            continue
        except Exception as exc:  # noqa: BLE001
            if "dump payload" not in str(exc).lower():
                fallidas += 1
                print(f"  FALLÓ {key}: {exc}")
                continue
            # Típico migrando desde Redis Cloud/Enterprise: reintentar sin binario.

        try:
            await _copiar_por_tipo(src, dst, raw)
            if ttl and ttl > 0:
                await dst.pexpire(raw, ttl)
            copiadas += 1
        except Exception as exc:  # noqa: BLE001
            fallidas += 1
            print(f"  FALLÓ {key}: {exc}")

    await src.aclose()
    await dst.aclose()
    print(
        f"\n{'COPIADAS' if aplicar else 'A COPIAR'}: {copiadas} · "
        f"saltadas (efímeras): {saltadas} · fallidas: {fallidas}"
    )
    if not aplicar:
        print("\nEsto fue una PRUEBA. Agregá --si para copiar de verdad.")
    elif fallidas:
        print(
            "\nOJO: hubo fallas incluso con el copiado por tipo (fallback). Revisá el "
            "detalle de cada 'FALLÓ' arriba: puede ser un tipo de dato no soportado o "
            "un problema de conexión/permisos puntual en esa key."
        )
    return 1 if fallidas else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--origen", required=True, help="REDIS_URL del Redis actual")
    p.add_argument("--destino", required=True, help="REDIS_URL del Redis nuevo")
    p.add_argument("--patron", default=f"{PREFIJO}*", help="qué keys copiar")
    p.add_argument("--si", action="store_true", help="copiar de verdad (sin esto, prueba)")
    a = p.parse_args()
    if a.origen == a.destino:
        print("ERROR: origen y destino son el mismo Redis.")
        return 2
    return asyncio.run(migrar(a.origen, a.destino, a.si, a.patron))


if __name__ == "__main__":
    sys.exit(main())
