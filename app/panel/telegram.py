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


async def enviar_con_botones(texto: str, botones: list[list[dict]]) -> dict | None:
    """Manda un mensaje con inline keyboard. `botones` = filas de {text, callback_data}.

    Devuelve el JSON de Telegram (para conocer message_id) o None si no salió.
    """
    if not configurado():
        return None
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        r = await _c().post(
            url,
            json={
                "chat_id": settings.telegram_chat_id,
                "text": texto[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": botones},
            },
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram_botones_fallo", error=str(exc))
        return None


async def enviar_sugerencia(s: dict) -> None:
    """Envía una sugerencia del analista con botones Aprobar/Rechazar."""
    sid = s.get("id")
    riesgo = str(s.get("riesgo", "bajo"))
    contenido = str(s.get("contenido", "")).strip()
    emoji = "🔴" if riesgo == "alto" else "🟢"
    texto = f"{emoji} <b>Sugerencia #{sid}</b> (riesgo {riesgo})\n\n{contenido}"
    botones = [[
        {"text": "✅ Aprobar", "callback_data": f"sug:aprobar:{sid}"},
        {"text": "❌ Rechazar", "callback_data": f"sug:rechazar:{sid}"},
    ]]
    await enviar_con_botones(texto, botones)


async def responder_callback(callback_id: str, texto: str = "") -> None:
    """Cierra el 'reloj' del botón en Telegram con un toast opcional."""
    if not configurado() or not callback_id:
        return
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/answerCallbackQuery"
    try:
        await _c().post(url, json={"callback_query_id": callback_id, "text": texto[:200]})
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram_answer_fallo", error=str(exc))


async def editar_texto(chat_id: str, message_id: int, texto: str) -> None:
    """Reescribe el mensaje de la sugerencia para reflejar el resultado (quita botones)."""
    if not configurado() or not message_id:
        return
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/editMessageText"
    try:
        await _c().post(
            url,
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": texto[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram_edit_fallo", error=str(exc))


async def set_webhook(url_webhook: str, secret: str) -> bool:
    """Registra el webhook en Telegram para recibir los clics de los botones."""
    if not configurado():
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook"
    try:
        r = await _c().post(
            url,
            json={
                "url": url_webhook,
                "secret_token": secret,
                "allowed_updates": ["callback_query"],
            },
        )
        r.raise_for_status()
        log.info("telegram_webhook_registrado", url=url_webhook)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram_setwebhook_fallo", error=str(exc))
        return False


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        finally:
            _client = None
