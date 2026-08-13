"""Envío de mensajes por YCloud.

Reemplaza `Enviar como Humano`, `Enviar Imagenes en Serie`, `Enviar texto al
cliente`, `Humano Notificar Admin` y todos los nodos httpRequest de salida.
"""
from __future__ import annotations

import asyncio
import random
import re
import urllib.parse

import httpx

from app.logging_conf import get_logger
from app.media import descargar, es_imagen_valida
from app.settings import settings

log = get_logger(__name__)

_MSG_URL = f"{settings.ycloud_base_url.rstrip('/')}/whatsapp/messages"
_RE_LISTA = re.compile(r"^\s*\d+\.\s", re.MULTILINE)


class YCloud:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def _c(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": settings.ycloud_api_key,
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _post(self, body: dict, intentos: int = 3) -> dict | None:
        ultimo: Exception | None = None
        for i in range(1, intentos + 1):
            try:
                r = await self._c().post(_MSG_URL, json=body)
                if r.status_code >= 500 or r.status_code in (408, 429):
                    raise httpx.HTTPStatusError(
                        f"status {r.status_code}", request=r.request, response=r
                    )
                if r.status_code >= 400:
                    # 4xx permanente (401/403/400…): reintentar no cambia nada.
                    log.error(
                        "ycloud_envio_rechazado",
                        status=r.status_code,
                        tipo=body.get("type"),
                    )
                    return None
                r.raise_for_status()
                data = r.json()
                # Registrar el id como "enviado por el bot", para distinguirlo de
                # un mensaje que el supervisor escriba a mano desde YCloud.
                try:
                    mid = data.get("id") if isinstance(data, dict) else None
                    if mid:
                        from app.estado import registrar_msg_bot

                        await registrar_msg_bot(str(mid))
                except Exception:  # noqa: BLE001
                    pass
                return data
            except Exception as exc:  # noqa: BLE001
                ultimo = exc
                if i == intentos:
                    break
                await asyncio.sleep(2 * i)
        log.error("ycloud_envio_fallo", error=str(ultimo), tipo=body.get("type"))
        return None

    # ------------------------------------------------------------- texto ---
    @staticmethod
    def trocear(texto: str) -> list[str]:
        """Parte la respuesta en mensajes cortos, como escribiría una persona.

        Port de la función `trocear` del nodo `Enviar como Humano`.
        """
        texto = (texto or "").strip()
        if not texto:
            return []

        es_lista = len(_RE_LISTA.findall(texto)) >= 3
        if es_lista or len(texto) > 1000:
            MAXM = 3500
            out: list[str] = []
            buf = ""
            for ln in texto.split("\n"):
                if buf and len(buf) + 1 + len(ln) > MAXM:
                    out.append(buf)
                    buf = ln
                else:
                    buf = f"{buf}\n{ln}" if buf else ln
            if buf:
                out.append(buf)
            return out

        MAX = 350
        out = []
        for parrafo in [p.strip() for p in re.split(r"\n{2,}", texto) if p.strip()]:
            if len(parrafo) <= MAX:
                out.append(parrafo)
                continue
            buf = ""
            for sent in re.split(r"(?<=[.!?])\s+", parrafo):
                if len(buf) + 1 + len(sent) > MAX:
                    if buf:
                        out.append(buf.strip())
                    buf = sent
                else:
                    buf = f"{buf} {sent}" if buf else sent
            if buf:
                out.append(buf.strip())
        return out

    async def enviar_texto(
        self,
        destino: dict,
        emisor: str,
        texto: str,
        simular_tipeo: bool = True,
    ) -> None:
        for i, chunk in enumerate(self.trocear(texto)):
            if simular_tipeo:
                ms = min(max(1000 + len(chunk) * 12, 1200), 5000)
                await asyncio.sleep((ms + random.randint(0, 600)) / 1000)
            await self._post(
                {
                    "from": emisor,
                    **destino,
                    "type": "text",
                    "text": {"body": chunk[:4000]},
                }
            )

    # ----------------------------------------------------------- imagen ---
    @staticmethod
    def _variantes_proxy(url: str) -> list[str]:
        """Odoo a veces sirve WebP o tarda; weserv normaliza a JPG.

        Port de la función `variantes` del nodo `Enviar Imagenes en Serie`.
        """
        sin_proto = re.sub(r"^https?://", "", (url or "").split("?")[0])
        enc = urllib.parse.quote(sin_proto, safe="")
        bust = f"cb={random.randint(100000, 999999)}"
        return [
            f"https://images.weserv.nl/?url={enc}&output=jpg&q=85&we=1&bg=white&maxage=1d",
            f"https://wsrv.nl/?url={enc}&output=jpg&q=85&we=1&bg=white&{bust}",
            f"https://images.weserv.nl/?url={enc}&output=jpg&q=80&w=1024&we=1&bg=white&{bust}",
        ]

    async def enviar_imagen(
        self, destino: dict, emisor: str, url: str, caption: str = ""
    ) -> bool:
        # Si la foto la servimos nosotros (/img, ya en JPG confiable), se manda
        # directo sin pasar por el proxy externo weserv.
        if settings.base_url and url.startswith(settings.base_url):
            ok = await self._post(
                {
                    "from": emisor,
                    **destino,
                    "type": "image",
                    "image": {"link": url, "caption": (caption or "")[:1024]},
                },
                intentos=3,
            )
            return ok is not None
        for cand in self._variantes_proxy(url):
            try:
                data = await descargar(cand, timeout=15.0)
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.8)
                continue
            if not es_imagen_valida(data):
                await asyncio.sleep(0.8)
                continue
            ok = await self._post(
                {
                    "from": emisor,
                    **destino,
                    "type": "image",
                    "image": {"link": cand, "caption": (caption or "")[:1024]},
                },
                intentos=2,
            )
            if ok is not None:
                return True
            await asyncio.sleep(1.5)
        return False

    async def enviar_imagenes(
        self, destino: dict, emisor: str, items: list[tuple[str, str]]
    ) -> None:
        """items = [(url, caption), ...]. Si una imagen falla, manda el caption como texto."""
        for i, (url, caption) in enumerate(items[: settings.max_imagenes_por_mensaje]):
            ok = await self.enviar_imagen(destino, emisor, url, caption)
            if not ok and caption:
                await self.enviar_texto(destino, emisor, caption, simular_tipeo=False)
            if i < len(items) - 1:
                await asyncio.sleep(2 + random.random() * 0.5)

    # ----------------------------------------------------------- audio ---
    async def enviar_audio(self, destino: dict, emisor: str, url: str) -> bool:
        """Manda una nota de voz. `url` debe ser pública (YCloud la fetchea)."""
        ok = await self._post(
            {
                "from": emisor,
                **destino,
                "type": "audio",
                "audio": {"link": url},
            },
            intentos=3,
        )
        return ok is not None

    # --------------------------------------------------------- plantilla ---
    async def enviar_plantilla(
        self, telefono: str, emisor: str, nombre: str, parametros: list[str]
    ) -> None:
        await self._post(
            {
                "from": emisor,
                "to": telefono,
                "type": "template",
                "template": {
                    "name": nombre,
                    "language": {"code": settings.template_lang},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [
                                {
                                    "type": "text",
                                    "text": re.sub(r"[\r\n\t]", " ", str(p)).strip()[:200]
                                    or "-",
                                }
                                for p in parametros
                            ],
                        }
                    ],
                },
            }
        )

    async def avisar_admin(self, emisor: str, texto: str) -> None:
        await self._post(
            {
                "from": emisor,
                "to": settings.admin_phone,
                "type": "text",
                "text": {"body": texto[:4000]},
            }
        )


ycloud = YCloud()
