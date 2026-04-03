from __future__ import annotations

import logging

import requests

from bot.chat_store import ChatStore
from notifier.base import BaseNotifier

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier(BaseNotifier):
    """Telegram Bot API notifications — sends to all active chats."""

    def __init__(self, bot_token: str, chat_store: ChatStore):
        if not bot_token:
            raise ValueError("Telegram bot_token is required.")
        self.bot_token = bot_token
        self.chat_store = chat_store
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
        chat_ids = self.chat_store.get_all()
        if not chat_ids:
            logger.warning("No active Telegram chats — skipping notification")
            return False

        text = f"<b>{_escape_html(title)}</b>\n\n{_escape_html(message)}"
        if url:
            text += f'\n\n<a href="{_escape_html(url)}">Book Now</a>'

        all_ok = True
        for chat_id in chat_ids:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            try:
                resp = requests.post(self._url, json=payload, timeout=10)
                data = resp.json()
                if data.get("ok"):
                    logger.info("Notification sent to chat %s", chat_id)
                else:
                    logger.warning(
                        "Telegram API error for chat %s: %s",
                        chat_id,
                        data.get("description", ""),
                    )
                    all_ok = False
            except requests.RequestException as e:
                logger.error("Failed to send to chat %s: %s", chat_id, e)
                all_ok = False

        return all_ok

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
