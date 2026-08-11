#!/usr/bin/env python3
"""Mapea anuncios de Facebook (ad_id) a productos del catálogo.

El referral de Meta trae el `ad_id` pero NO un SKU. Este mapa es lo que hace
que el bot sepa de qué producto habla el cliente antes de que escriba una
palabra.

Uso:
    python -m scripts.mapear_anuncios --listar
    python -m scripts.mapear_anuncios --buscar botella
    python -m scripts.mapear_anuncios --ad 52579732276546 --producto 42
    python -m scripts.mapear_anuncios              # modo interactivo

Los anuncios que llegan sin mapear entran solos a la cola de revisión
(GET /admin/revision), así te enterás de cuáles faltan.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from app.business_config import listar_anuncios, set_producto_de_anuncio  # noqa: E402
from app.catalogo import catalogo  # noqa: E402
from app.estado import listar_revision  # noqa: E402
from app.logging_conf import setup_logging  # noqa: E402
from app.redis_client import close_redis  # noqa: E402

GRIS = "\033[90m"
VERDE = "\033[92m"
AMARILLO = "\033[93m"
RESET = "\033[0m"


async def mostrar_mapa() -> None:
    mapa = await listar_anuncios()
    if not mapa:
        print("No hay anuncios mapeados todavía.")
        return
    print(f"\n{len(mapa)} anuncio(s) mapeado(s):")
    for ad_id, info in sorted(mapa.items()):
        print(f"  {ad_id} -> [{info.get('product_tmpl_id')}] {info.get('nombre')}")


async def mostrar_pendientes() -> None:
    """Anuncios que llegaron y todavía no tienen producto asignado."""
    items = await listar_revision(200)
    pendientes = sorted(
        {
            m.split(":", 1)[1]
            for it in items
            for m in it.get("motivos", [])
            if m.startswith("anuncio_sin_mapear:")
        }
    )
    if pendientes:
        print(f"\n{AMARILLO}Anuncios SIN MAPEAR que ya recibieron mensajes:{RESET}")
        for ad in pendientes:
            print(f"  {ad}")


async def buscar(texto: str) -> None:
    productos = await catalogo.todos()
    if texto:
        veredicto, encontrados = await catalogo.buscar(texto, limite=20)
        print(f"{GRIS}({veredicto}){RESET}")
    else:
        encontrados = productos
    for p in encontrados:
        print(f"  [{p.tmpl_id:>5}] {p.nombre} - RD${p.precio_con_itbis:.2f}")
    print(f"\n{len(encontrados)} de {len(productos)} productos.")


async def asignar(ad_id: str, tmpl_id: int) -> bool:
    p = await catalogo.por_tmpl_id(tmpl_id)
    if not p:
        print(f"ERROR: no existe el producto {tmpl_id}.")
        return False
    await set_producto_de_anuncio(ad_id, tmpl_id, p.nombre)
    print(f"{VERDE}OK: anuncio {ad_id} -> {p.nombre}{RESET}")
    return True


async def interactivo() -> None:
    await mostrar_mapa()
    await mostrar_pendientes()
    print(
        f"\n{GRIS}Comandos: buscar <texto> | mapa | <ad_id> <product_id> | "
        f"salir{RESET}\n"
    )
    while True:
        try:
            linea = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not linea or linea.lower() in {"salir", "exit", "quit"}:
            break
        if linea.lower() == "mapa":
            await mostrar_mapa()
            continue
        if linea.lower().startswith("buscar"):
            await buscar(linea[6:].strip())
            continue
        partes = linea.split()
        if len(partes) == 2 and partes[1].isdigit():
            await asignar(partes[0], int(partes[1]))
        else:
            print("Formato: <ad_id> <product_id>. Ej: 52579732276546 42")


async def main(args: argparse.Namespace) -> None:
    setup_logging()
    if args.listar:
        await mostrar_mapa()
        await mostrar_pendientes()
    elif args.buscar is not None:
        await buscar(args.buscar)
    elif args.ad and args.producto:
        await asignar(args.ad, args.producto)
    else:
        await interactivo()
    await close_redis()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--listar", action="store_true", help="ver el mapa actual")
    ap.add_argument("--buscar", nargs="?", const="", help="buscar productos e ids")
    ap.add_argument("--ad", help="ad_id de Facebook")
    ap.add_argument("--producto", type=int, help="product_tmpl_id de Odoo")
    asyncio.run(main(ap.parse_args()))
