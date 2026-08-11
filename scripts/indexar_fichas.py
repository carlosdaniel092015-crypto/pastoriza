#!/usr/bin/env python3
"""Indexa las 'fichas visuales' del catálogo para que funcione buscar_por_foto.

Esto es el workflow "Indexar Fichas" que en n8n estaba aparte y sin el cual
BuscarPorFoto no devuelve nada. Descarga la foto de cada producto de Odoo, le
pide a GPT una ficha estructurada del envase y guarda todo en
ir.config_parameter('pastoriza.imgsigs').

Uso:
    python -m scripts.indexar_fichas            # indexa lo que falta
    python -m scripts.indexar_fichas --todo     # reindexa todo desde cero
    python -m scripts.indexar_fichas --limite 20

Corrélo una vez al principio y después cada vez que agreguen productos nuevos.
Cuesta ~1 llamada de visión por producto.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys

sys.path.insert(0, ".")

from app.catalogo import catalogo  # noqa: E402
from app.logging_conf import get_logger, setup_logging  # noqa: E402
from app.media import ficha_visual  # noqa: E402
from app.odoo import odoo  # noqa: E402
from app.redis_client import close_redis  # noqa: E402

PARAM_KEY = "pastoriza.imgsigs"
log = get_logger("indexar")


async def imagen_de_template(tmpl_id: int) -> bytes | None:
    """Lee image_1024 directo de Odoo (base64), sin pasar por HTTP público."""
    try:
        res = await odoo.read("product.template", [tmpl_id], ["image_1024"])
    except Exception as exc:  # noqa: BLE001
        log.warning("imagen_lectura_fallo", tmpl_id=tmpl_id, error=str(exc))
        return None
    if not res:
        return None
    b64 = res[0].get("image_1024")
    if not b64 or b64 is False:
        return None
    try:
        return base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return None


async def main(reindexar_todo: bool, limite: int | None) -> None:
    setup_logging()

    store: dict = {}
    if not reindexar_todo:
        raw = await odoo.get_param(PARAM_KEY)
        if raw:
            try:
                store = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("store_corrupto_reiniciando")
                store = {}

    productos = await catalogo.todos(force=True)
    pendientes = [p for p in productos if str(p.tmpl_id) not in store]
    if limite:
        pendientes = pendientes[:limite]

    print(f"Catálogo: {len(productos)} productos. A indexar: {len(pendientes)}.")
    if not pendientes:
        print("Nada que hacer.")
        return

    ok = fallos = 0
    for i, p in enumerate(pendientes, 1):
        data = await imagen_de_template(p.tmpl_id)
        if not data:
            print(f"  [{i}/{len(pendientes)}] {p.nombre}: SIN FOTO en Odoo")
            fallos += 1
            continue

        ficha = await ficha_visual(data)
        if not ficha or not ficha.get("tipo"):
            print(f"  [{i}/{len(pendientes)}] {p.nombre}: no pude leer la foto")
            fallos += 1
            continue

        store[str(p.tmpl_id)] = {
            "name": p.nombre,
            "price_con": p.precio_con_itbis,
            "ficha": ficha,
        }
        ok += 1
        print(
            f"  [{i}/{len(pendientes)}] {p.nombre}: "
            f"{ficha.get('tipo')}/{ficha.get('forma')}/{ficha.get('capacidad') or '?'}"
        )

        # Guardado incremental cada 10, por si se corta a mitad de camino.
        if ok % 10 == 0:
            await odoo.set_param(PARAM_KEY, json.dumps(store, ensure_ascii=False))

    await odoo.set_param(PARAM_KEY, json.dumps(store, ensure_ascii=False))
    print(f"\nListo. Indexados: {ok} | Fallos: {fallos} | Total en índice: {len(store)}")
    await close_redis()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--todo", action="store_true", help="reindexar todo desde cero")
    ap.add_argument("--limite", type=int, default=None)
    args = ap.parse_args()
    asyncio.run(main(args.todo, args.limite))
