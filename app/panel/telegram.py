"""Notificador de errores por Telegram (opcional).

Si TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID no están configurados, no hace nada
(el panel sigue mostrando el error igual). Nunca lanza excepción hacia arriba:
un fallo notificando no debe tumbar el turno del bot.
"""
from __future__ import annotations

import httpx

from app.logging_conf import get_logger
from app.settings import settings

log = get_logger(__name__)

_client: httpx.AsyncClient | None = None


def _c() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
    return _client


def configurado() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


async def enviar(texto: str) -> bool:
    """Manda un mensaje a Telegram. Devuelve True si salió, False si no."""
    if not configurado():
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        r = await _c().post(
            url,
            json={
                "chat_id": settings.telegram_chat_id,
                "text": texto[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        r.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram_envio_fallo", error=str(exc))
        return False


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        finally:
            _client = None
