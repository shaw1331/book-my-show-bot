from __future__ import annotations

from abc import ABC, abstractmethod


class BaseNotifier(ABC):
    """Abstract interface for all notification channels."""

    @abstractmethod
    def send(
        self,
        title: str,
        message: str,
        url: str = "",
        priority: str = "default",
    ) -> bool:
        """Send a notification. Returns True on success."""
        ...

    @abstractmethod
    def test(self) -> bool:
        """Send a test notification to verify the channel is working."""
        ...

    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Human-readable name for logging (e.g. 'ntfy', 'telegram')."""
        ...
