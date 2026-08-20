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


def _solo_digitos(n: str) -> str:
    return re.sub(r"\D", "", str(n or ""))[-10:]


# Qué plantilla es cuál, para que el panel no muestre un nombre técnico suelto. Las
# etiquetas de las variables dependen de la plantilla: vive en Meta, el bot sólo manda
# los valores (ver PLANTILLA_META.md).
_ETIQUETAS_PLANTILLA: dict[str, tuple[str, ...]] = {
    "alerta_supervisor_cliente": ("Cliente", "Número", "Lo que pidió"),
    "notificar_pedido_creado": ("Cliente", "Número", "Detalle"),
}


async def _registrar_si_es_al_supervisor(
    telefono: str, emisor: str, plantilla: str, parametros: list[str], enviado: bool
) -> None:
    """Anota en el panel las plantillas que van al ADMIN_PHONE. Nunca puede impedir el
    envío ni propagar: el registro vale menos que el aviso."""
    if _solo_digitos(telefono) != _solo_digitos(settings.admin_phone):
        return
    try:
        # Import perezoso: `panel` es capa de arriba, no una dependencia de ycloud.
        from app.panel import supervisor_log

        etiquetas = _ETIQUETAS_PLANTILLA.get(plantilla, ())
        texto = "\n".join(
            f"{etiquetas[i]}: {p}" if i < len(etiquetas) else str(p)
            for i, p in enumerate(parametros or [])
        )
        await supervisor_log.registrar(
            "aviso" if plantilla != settings.template_alerta_supervisor else "escalamiento",
            emisor=emisor,
            plantilla=plantilla,
            texto=texto,
            enviado=enviado,
            cliente=str(parametros[0]) if parametros else "",
            # El panel enlaza a la conversación con esto. En las dos plantillas de
            # arriba el teléfono del cliente es la variable 2; si mañana hay otra con
            # otro orden, queda vacío y el aviso igual se ve (sólo sin el enlace).
            chat_id=(
                str(parametros[1]).lstrip("+")
                if len(parametros or []) > 1 and etiquetas[1:2] == ("Número",)
                else ""
            ),
            detalle="" if enviado else (
                "YCloud/Meta rechazó el envío: el supervisor NO se enteró. Revisá que la "
                "plantilla esté aprobada y que el nombre coincida (ver PLANTILLA_META.md)."
            ),
        )
    except Exception:  # noqa: BLE001
        pass


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

        # UN turno = UN mensaje de WhatsApp. Antes se partía por párrafos (MAX 350),
        # así que una sola respuesta llegaba como 3-4 mensajes seguidos y el cliente
        # veía un chorro de burbujas. Sólo se parte si excede el límite del canal.
        MAXM = 3500
        if len(texto) <= MAXM:
            return [texto]

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

    async def enviar_texto(
        self,
        destino: dict,
        emisor: str,
        texto: str,
        simular_tipeo: bool = True,
    ) -> bool:
        """Manda el texto (troceado). Devuelve True si TODOS los chunks salieron.

        Devuelve bool porque `_post` nunca lanza (se traga 4xx/timeouts y loguea): sin
        esto, quien llama no puede distinguir enviado de fallado. El panel lo usa para
        no decirle al supervisor "enviado" cuando YCloud rechazó el mensaje.
        """
        ok_total = True
        for i, chunk in enumerate(self.trocear(texto)):
            if simular_tipeo:
                ms = min(max(1000 + len(chunk) * 12, 1200), 5000)
                await asyncio.sleep((ms + random.randint(0, 600)) / 1000)
            ok = await self._post(
                {
                    "from": emisor,
                    **destino,
                    "type": "text",
                    "text": {"body": chunk[:4000]},
                }
            )
            if ok is None:
                ok_total = False
        return ok_total

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
        enviado = True
        try:
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
        except Exception:
            enviado = False
            raise
        finally:
            # Acá y no en cada llamada: así TODA plantilla al supervisor queda en el
            # módulo "Al supervisor" del panel, incluidas las que se agreguen después.
            # Es el único lugar donde se puede ver qué se le mandó y si llegó.
            await _registrar_si_es_al_supervisor(telefono, emisor, nombre, parametros, enviado)

    async def enviar_plantilla_botones(
        self,
        telefono: str,
        emisor: str,
        nombre: str,
        parametros: list[str],
        imagen_url: str = "",
        botones: list[str] | None = None,
    ) -> bool:
        """Plantilla con foto de cabecera y botones de respuesta rápida.

        Es la que aprueba el supervisor: cabecera = comprobante, cuerpo = resumen del
        pedido, botones = aprobar / no aprobar. Cuando toca uno, WhatsApp nos devuelve
        su `payload` como mensaje entrante (ver `app/aprobacion.py`).

        Devuelve True sólo si YCloud aceptó el envío: el que llama necesita saberlo
        para no dar por avisado al supervisor cuando no se avisó.
        """
        componentes: list[dict] = []
        if imagen_url:
            componentes.append({
                "type": "header",
                "parameters": [{"type": "image", "image": {"link": imagen_url}}],
            })
        componentes.append({
            "type": "body",
            "parameters": [
                {
                    "type": "text",
                    # Meta RECHAZA variables con saltos de línea o tabs.
                    "text": re.sub(r"[\r\n\t]", " ", str(p)).strip()[:300] or "-",
                }
                for p in parametros
            ],
        })
        for i, pay in enumerate(botones or []):
            componentes.append({
                "type": "button",
                "sub_type": "quick_reply",
                "index": str(i),
                "parameters": [{"type": "payload", "payload": str(pay)[:128]}],
            })

        data = await self._post({
            "from": emisor,
            "to": telefono,
            "type": "template",
            "template": {
                "name": nombre,
                "language": {"code": settings.template_lang},
                "components": componentes,
            },
        })
        return bool(data)

    async def avisar_admin(self, emisor: str, texto: str) -> None:
        enviado = True
        try:
            await self._post(
                {
                    "from": emisor,
                    "to": settings.admin_phone,
                    "type": "text",
                    "text": {"body": texto[:4000]},
                }
            )
        except Exception:
            enviado = False
            raise
        finally:
            # Import perezoso: `panel` es una capa de arriba, no una dependencia de
            # ycloud. Y el registro no puede impedir el aviso, así que se traga el fallo.
            try:
                from app.panel import supervisor_log

                await supervisor_log.registrar(
                    "aviso", emisor=emisor, texto=texto, enviado=enviado
                )
            except Exception:  # noqa: BLE001
                pass


ycloud = YCloud()
