#!/usr/bin/env python3
"""Conversá con el bot desde la terminal, sin WhatsApp ni YCloud.

Corre el agente real, con Odoo y Redis reales, pero no envía nada al cliente.
Es la forma más barata de validar el flujo completo antes de apuntar el webhook.

Uso:
    python -m scripts.probar_conversacion
    python -m scripts.probar_conversacion --chat-id 18091234567 --anuncio 52579732276546
"""
from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from agents import Runner  # noqa: E402

from app.agents import ESPECIALISTAS, RespuestaBot, elegir_agente  # noqa: E402
from app.business_config import get_producto_de_anuncio, load_config  # noqa: E402
from app.context import ConversationContext  # noqa: E402
from app.logging_conf import setup_logging  # noqa: E402
from app.redis_client import close_redis  # noqa: E402
from app.router import respuesta_directa  # noqa: E402
from app.session import RedisSession  # noqa: E402
from app.settings import settings  # noqa: E402

VERDE = "\033[92m"
GRIS = "\033[90m"
RESET = "\033[0m"


async def main(chat_id: str, ad_id: str, limpiar: bool) -> None:
    setup_logging()
    cfg = await load_config()
    session = RedisSession(chat_id)
    if limpiar:
        await session.clear_session()
        print(f"{GRIS}(historial limpiado){RESET}")

    ad_producto = await get_producto_de_anuncio(ad_id) if ad_id else None
    print(f"{GRIS}chat_id={chat_id} | modelo={settings.model_agente}")
    if ad_id:
        print(f"anuncio={ad_id} -> {(ad_producto or {}).get('nombre', 'SIN MAPEAR')}")
    print(f"Escribí 'salir' para terminar.{RESET}\n")

    while True:
        try:
            texto = input("Vos > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if texto.lower() in {"salir", "exit", "quit"}:
            break
        if not texto:
            continue

        directa = respuesta_directa(texto, cfg, viene_de_anuncio=bool(ad_id))
        if directa:
            print(f"{GRIS}[fast-path, sin modelo]{RESET}")
            print(f"{VERDE}Bot > {directa}{RESET}\n")
            await session.add_items(
                [
                    {"role": "user", "content": texto},
                    {"role": "assistant", "content": directa},
                ]
            )
            continue

        ctx = ConversationContext(
            chat_id=chat_id,
            telefono=chat_id if chat_id.isdigit() else None,
            user_name="Cliente de Prueba",
            emisor="test",
            destino={"to": chat_id},
            cfg=cfg,
            ad_id=ad_id,
            ad_producto_tmpl_id=(ad_producto or {}).get("product_tmpl_id"),
            ad_producto_nombre=(ad_producto or {}).get("nombre", ""),
        )

        nombre = await elegir_agente(texto, ctx, session)
        ctx.agente = nombre
        print(f"{GRIS}[agente: {nombre}]{RESET}")
        result = await Runner.run(
            ESPECIALISTAS[nombre], texto, context=ctx, session=session,
            max_turns=settings.agente_max_turns,
        )
        out = result.final_output
        if not isinstance(out, RespuestaBot):
            out = RespuestaBot(mensaje=str(out))

        print(f"{VERDE}Bot > {out.mensaje}{RESET}")
        if out.mostrar_productos:
            for tid in out.mostrar_productos:
                p = ctx.productos_ofrecidos.get(int(tid))
                marca = p.resumen() if p else "!! id NO ofrecido este turno (bloqueado)"
                print(f"{GRIS}      [foto] {marca}{RESET}")
        if ctx.order_id:
            print(f"{GRIS}      [pedido creado en Odoo: {ctx.order_id}]{RESET}")
        if ctx.motivo_revision:
            print(f"{GRIS}      [revisión: {', '.join(ctx.motivo_revision)}]{RESET}")
        print()

    await close_redis()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-id", default="18090000000")
    ap.add_argument("--anuncio", default="", help="ad_id para simular Click to WhatsApp")
    ap.add_argument("--limpiar", action="store_true", help="borrar historial previo")
    args = ap.parse_args()
    asyncio.run(main(args.chat_id, args.anuncio, args.limpiar))
