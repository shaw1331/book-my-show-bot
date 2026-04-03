from __future__ import annotations

import logging

import requests

from notifier.base import BaseNotifier

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier(BaseNotifier):
    """Telegram Bot API notifications — free, reliable, personal."""

    def __init__(self, bot_token: str, chat_id: str):
        if not bot_token or not chat_id:
            raise ValueError(
                "Telegram bot_token and chat_id are required. "
                "Set the environment variables referenced in config.yaml."
            )
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._url = _TELEGRAM_API.format(token=bot_token)

    @property
    def channel_name(self) -> str:
        return "telegram"

    def send(
        self,
        title: str,
        message: str,
        url: str = "",
        priority: str = "",
    ) -> bool:
        # Format as HTML for nice rendering in Telegram
        text = f"<b>{_escape_html(title)}</b>\n\n{_escape_html(message)}"
        if url:
            text += f'\n\n<a href="{_escape_html(url)}">Book Now</a>'

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            resp = requests.post(self._url, json=payload, timeout=10)
            data = resp.json()

            if data.get("ok"):
                logger.info("Notification sent via Telegram")
                return True

            logger.warning(
                "Telegram API error: %s", data.get("description", resp.text[:200])
            )
            return False
        except requests.RequestException as e:
            logger.error("Failed to send Telegram notification: %s", e)
            return False

    def test(self) -> bool:
        return self.send(
            title="BMS Bot - Test Notification",
            message="If you see this, Telegram notifications are working!",
        )


def _escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram's HTML parse mode."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
