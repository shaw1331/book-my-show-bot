from __future__ import annotations

import json
import logging

import requests

from notifier.base import BaseNotifier

logger = logging.getLogger(__name__)


class NtfyNotifier(BaseNotifier):
    """Push notifications via ntfy.sh — zero signup, zero tokens."""

    def __init__(
        self,
        topic: str,
        server: str = "https://ntfy.sh",
        priority: str = "high",
    ):
        self.topic = topic
        self.server = server.rstrip("/")
        self.priority = priority
        self.url = f"{self.server}/{self.topic}"

    @property
    def channel_name(self) -> str:
        return f"ntfy ({self.topic})"

    def send(
        self,
        title: str,
        message: str,
        url: str = "",
        priority: str = "",
    ) -> bool:
        # Use JSON body instead of headers to avoid latin-1 encoding
        # issues with Unicode characters (emojis) in the title.
        payload: dict = {
            "topic": self.topic,
            "title": title,
            "message": message,
            "priority": self._priority_to_int(priority or self.priority),
            "tags": ["movie_camera", "ticket"],
        }
        if url:
            payload["click"] = url
            payload["actions"] = [
                {"action": "view", "label": "Book Now", "url": url}
            ]

        try:
            resp = requests.post(
                self.server,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if resp.ok:
                logger.info("Notification sent via ntfy: %s", title)
                return True
            logger.warning(
                "ntfy returned %d: %s", resp.status_code, resp.text[:200]
            )
            return False
        except requests.RequestException as e:
            logger.error("Failed to send ntfy notification: %s", e)
            return False

    def test(self) -> bool:
        return self.send(
            title="BMS Bot - Test Notification",
            message="If you see this, notifications are working!",
            priority="default",
        )

    @staticmethod
    def _priority_to_int(priority: str) -> int:
        return {
            "min": 1,
            "low": 2,
            "default": 3,
            "high": 4,
            "max": 5,
        }.get(priority, 4)
