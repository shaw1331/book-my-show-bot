"""Persistent store for active Telegram chat IDs.

Stores chat IDs in a JSON file so they survive bot restarts.
Thread-safe for concurrent access from the command bot (background thread)
and the main checker loop.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class ChatStore:
    def __init__(self, path: str = ".chat_ids.json"):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._chat_ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    data = json.load(f)
                self._chat_ids = set(str(cid) for cid in data.get("chat_ids", []))
                logger.info("Loaded %d active chat(s) from %s", len(self._chat_ids), self._path)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load chat store: %s", e)
                self._chat_ids = set()

    def _save(self) -> None:
        try:
            with open(self._path, "w") as f:
                json.dump({"chat_ids": sorted(self._chat_ids)}, f, indent=2)
        except OSError as e:
            logger.error("Failed to save chat store: %s", e)

    def add(self, chat_id: str) -> None:
        with self._lock:
            self._chat_ids.add(str(chat_id))
            self._save()
        logger.info("Added chat %s (total: %d)", chat_id, len(self._chat_ids))

    def remove(self, chat_id: str) -> None:
        with self._lock:
            self._chat_ids.discard(str(chat_id))
            self._save()
        logger.info("Removed chat %s (total: %d)", chat_id, len(self._chat_ids))

    def get_all(self) -> list[str]:
        with self._lock:
            return list(self._chat_ids)

    def is_active(self, chat_id: str) -> bool:
        with self._lock:
            return str(chat_id) in self._chat_ids

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._chat_ids)
