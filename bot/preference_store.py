"""Persistent store for user preferences.

Saves movie, cinemas, formats, languages to a JSON file so they
survive bot restarts. Auto-saves on every change. Thread-safe.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULTS = {
    "movie_name": "",
    "movie_code": "",
    "preferred_cinemas": [],
    "formats": [],
    "languages": [],
}


class PreferenceStore:
    def __init__(self, path: str = ".preferences.json"):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    self._data = json.load(f)
                logger.info("Loaded preferences from %s", self._path)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load preferences: %s", e)
                self._data = {}

    def _save(self) -> None:
        try:
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)
        except OSError as e:
            logger.error("Failed to save preferences: %s", e)

    def get(self, key: str) -> str | list:
        with self._lock:
            return self._data.get(key, _DEFAULTS.get(key, ""))

    def set(self, key: str, value: str | list) -> None:
        with self._lock:
            self._data[key] = value
            self._save()

    def get_list(self, key: str) -> list[str]:
        with self._lock:
            val = self._data.get(key, _DEFAULTS.get(key, []))
            return list(val) if isinstance(val, list) else []

    def append_to_list(self, key: str, value: str) -> list[str]:
        with self._lock:
            items = self._data.get(key, [])
            if not isinstance(items, list):
                items = []
            items.append(value)
            self._data[key] = items
            self._save()
            return list(items)

    def remove_from_list(self, key: str, index: int) -> str | None:
        with self._lock:
            items = self._data.get(key, [])
            if not isinstance(items, list) or index < 0 or index >= len(items):
                return None
            removed = items.pop(index)
            self._data[key] = items
            self._save()
            return removed

    def clear_list(self, key: str) -> None:
        with self._lock:
            self._data[key] = []
            self._save()

    def seed_from_config(self, config) -> None:
        """Seed defaults from AppConfig if store is empty."""
        with self._lock:
            if self._data:
                logger.info("Preferences already exist, skipping seed")
                return
            self._data = {
                "movie_name": config.movie.name,
                "movie_code": config.movie.code,
                "preferred_cinemas": list(config.location.preferred_cinemas),
                "formats": list(config.movie.formats),
                "languages": list(config.movie.languages),
            }
            self._save()
            logger.info("Seeded preferences from config")
