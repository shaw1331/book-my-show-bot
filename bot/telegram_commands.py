"""Telegram bot command handler for managing preferences via chat.

Supported commands:
    /status          — Show current config (movie, cinemas, formats, etc.)
    /cinemas         — List preferred cinemas
    /addcinema NAME  — Add a cinema (fuzzy name)
    /removecinema N  — Remove cinema by number
    /clearcinemas    — Clear all cinema preferences (match all)
    /formats         — List preferred formats
    /addformat FMT   — Add a format (e.g. "IMAX 2D")
    /removeformat N  — Remove format by number
    /clearformats    — Clear all format preferences (match all)
    /languages       — List preferred languages
    /addlang LANG    — Add a language
    /removelang N    — Remove language by number
    /movie NAME      — Change the movie being tracked
    /check           — Trigger an immediate check
    /help            — Show available commands
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from config.schema import AppConfig

logger = logging.getLogger(__name__)


class TelegramCommandBot:
    """Polls Telegram for commands and updates AppConfig in-place.

    Runs in a background thread. The checker engine reads the same
    AppConfig object, so preference changes take effect on the next
    polling cycle without restart.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        config: AppConfig,
        on_check_now: callable = None,
    ):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self._api = f"https://api.telegram.org/bot{bot_token}"
        self.config = config
        self._on_check_now = on_check_now
        self._offset = 0
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start polling for commands in a background thread."""
        self._flush_old_updates()
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("Telegram command bot started")

    def _flush_old_updates(self) -> None:
        """Skip all pending updates so old commands don't replay on restart."""
        try:
            resp = requests.get(
                f"{self._api}/getUpdates", params={"offset": -1}, timeout=10
            )
            if resp.ok:
                results = resp.json().get("result", [])
                if results:
                    self._offset = results[-1]["update_id"] + 1
                    logger.info("Flushed %d old update(s)", len(results))
        except requests.RequestException:
            pass

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _poll_loop(self) -> None:
        while self._running:
            try:
                self._process_updates()
            except Exception as e:
                logger.warning("Telegram command poll error: %s", e)
            time.sleep(2)

    def _process_updates(self) -> None:
        resp = requests.get(
            f"{self._api}/getUpdates",
            params={"offset": self._offset, "timeout": 10},
            timeout=15,
        )
        if not resp.ok:
            return

        data = resp.json()
        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "").strip()

            if chat_id != self.chat_id or not text:
                continue

            if text.startswith("/"):
                logger.info("Telegram command received: %s", text)
                self._handle_command(text)
            else:
                logger.debug("Telegram message (not a command): %s", text)

    def _handle_command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]  # strip @botname
        arg = parts[1].strip() if len(parts) > 1 else ""

        handlers = {
            "/start": self._cmd_help,
            "/help": self._cmd_help,
            "/status": self._cmd_status,
            "/cinemas": self._cmd_cinemas,
            "/addcinema": lambda: self._cmd_add_cinema(arg),
            "/removecinema": lambda: self._cmd_remove_cinema(arg),
            "/clearcinemas": self._cmd_clear_cinemas,
            "/formats": self._cmd_formats,
            "/addformat": lambda: self._cmd_add_format(arg),
            "/removeformat": lambda: self._cmd_remove_format(arg),
            "/clearformats": self._cmd_clear_formats,
            "/languages": self._cmd_languages,
            "/addlang": lambda: self._cmd_add_lang(arg),
            "/removelang": lambda: self._cmd_remove_lang(arg),
            "/movie": lambda: self._cmd_movie(arg),
            "/check": self._cmd_check,
        }

        handler = handlers.get(cmd)
        if handler:
            handler()
        else:
            self._send(f"Unknown command: {cmd}\nType /help for available commands.")

    # --- Command implementations ---

    def _cmd_help(self) -> None:
        logger.info("/help command triggered")
        self._send(
            "<b>BMS Bot Commands</b>\n\n"
            "<b>Preferences:</b>\n"
            "/status — Show current settings\n"
            "/movie NAME — Change tracked movie\n"
            "/cinemas — List preferred cinemas\n"
            "/addcinema NAME — Add cinema (fuzzy)\n"
            "/removecinema N — Remove by number\n"
            "/clearcinemas — Watch all cinemas\n"
            "/formats — List preferred formats\n"
            "/addformat FMT — Add format\n"
            "/removeformat N — Remove by number\n"
            "/clearformats — Watch all formats\n"
            "/languages — List preferred languages\n"
            "/addlang LANG — Add language\n"
            "/removelang N — Remove by number\n\n"
            "<b>Actions:</b>\n"
            "/check — Trigger immediate check\n"
            "/help — Show this message"
        )

    def _cmd_status(self) -> None:
        c = self.config
        cinemas = ", ".join(c.location.preferred_cinemas) or "Any"
        formats = ", ".join(c.movie.formats) or "Any"
        langs = ", ".join(c.movie.languages) or "Any"
        dist = f"{c.location.max_distance_km} km" if c.location.max_distance_km else "No limit"
        self._send(
            f"<b>Current Settings</b>\n\n"
            f"🎬 <b>Movie:</b> {c.movie.name}\n"
            f"🏙 <b>City:</b> {c.location.city}\n"
            f"🎭 <b>Cinemas:</b> {cinemas}\n"
            f"📽 <b>Formats:</b> {formats}\n"
            f"🗣 <b>Languages:</b> {langs}\n"
            f"📍 <b>Max distance:</b> {dist}\n"
            f"⏱ <b>Check interval:</b> {c.checker.interval_seconds}s"
        )

    # Cinemas
    def _cmd_cinemas(self) -> None:
        items = self.config.location.preferred_cinemas
        if not items:
            self._send("No cinema preference set — watching all cinemas.\nUse /addcinema NAME to add one.")
            return
        lines = [f"{i+1}. {c}" for i, c in enumerate(items)]
        self._send("<b>Preferred Cinemas:</b>\n" + "\n".join(lines))

    def _cmd_add_cinema(self, name: str) -> None:
        if not name:
            self._send("Usage: /addcinema PVR Forum Mall")
            return
        self.config.location.preferred_cinemas.append(name)
        self._send(f"✅ Added cinema: <b>{name}</b>\n\nCurrent list:\n" + self._numbered_list(self.config.location.preferred_cinemas))

    def _cmd_remove_cinema(self, arg: str) -> None:
        items = self.config.location.preferred_cinemas
        idx = self._parse_index(arg, items)
        if idx is None:
            return
        removed = items.pop(idx)
        self._send(f"🗑 Removed: <b>{removed}</b>")

    def _cmd_clear_cinemas(self) -> None:
        self.config.location.preferred_cinemas.clear()
        self._send("Cleared all cinema preferences — now watching all cinemas.")

    # Formats
    def _cmd_formats(self) -> None:
        items = self.config.movie.formats
        if not items:
            self._send("No format preference set — watching all formats.\nUse /addformat IMAX 2D to add one.")
            return
        lines = [f"{i+1}. {f}" for i, f in enumerate(items)]
        self._send("<b>Preferred Formats:</b>\n" + "\n".join(lines))

    def _cmd_add_format(self, fmt: str) -> None:
        if not fmt:
            self._send("Usage: /addformat IMAX 2D")
            return
        self.config.movie.formats.append(fmt)
        self._send(f"✅ Added format: <b>{fmt}</b>\n\nCurrent list:\n" + self._numbered_list(self.config.movie.formats))

    def _cmd_remove_format(self, arg: str) -> None:
        items = self.config.movie.formats
        idx = self._parse_index(arg, items)
        if idx is None:
            return
        removed = items.pop(idx)
        self._send(f"🗑 Removed: <b>{removed}</b>")

    def _cmd_clear_formats(self) -> None:
        self.config.movie.formats.clear()
        self._send("Cleared all format preferences — now watching all formats.")

    # Languages
    def _cmd_languages(self) -> None:
        items = self.config.movie.languages
        if not items:
            self._send("No language preference set — watching all languages.\nUse /addlang English to add one.")
            return
        lines = [f"{i+1}. {l}" for i, l in enumerate(items)]
        self._send("<b>Preferred Languages:</b>\n" + "\n".join(lines))

    def _cmd_add_lang(self, lang: str) -> None:
        if not lang:
            self._send("Usage: /addlang English")
            return
        self.config.movie.languages.append(lang)
        self._send(f"✅ Added language: <b>{lang}</b>\n\nCurrent list:\n" + self._numbered_list(self.config.movie.languages))

    def _cmd_remove_lang(self, arg: str) -> None:
        items = self.config.movie.languages
        idx = self._parse_index(arg, items)
        if idx is None:
            return
        removed = items.pop(idx)
        self._send(f"🗑 Removed: <b>{removed}</b>")

    # Movie
    def _cmd_movie(self, name: str) -> None:
        if not name:
            self._send(f"Current movie: <b>{self.config.movie.name}</b>\n\nUsage: /movie New Movie Name")
            return
        self.config.movie.name = name
        self.config.movie.code = ""  # Force re-resolve on next check
        self._send(f"✅ Now tracking: <b>{name}</b>\nWill auto-resolve event code on next check.")

    # Check now
    def _cmd_check(self) -> None:
        if self._on_check_now:
            self._send("⏳ Running check now...")
            self._on_check_now()
        else:
            self._send("Manual check not available in this mode.")

    # --- Helpers ---

    def _send(self, text: str) -> None:
        logger.info("Sending Telegram response...")
        try:
            requests.post(
                f"{self._api}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
        except requests.RequestException as e:
            logger.error("Failed to send Telegram response: %s", e)

    def _parse_index(self, arg: str, items: list) -> int | None:
        try:
            idx = int(arg) - 1
            if 0 <= idx < len(items):
                return idx
            self._send(f"Invalid number. Valid range: 1-{len(items)}")
        except ValueError:
            self._send(f"Usage: provide a number (1-{len(items)})")
        return None

    @staticmethod
    def _numbered_list(items: list[str]) -> str:
        return "\n".join(f"{i+1}. {item}" for i, item in enumerate(items))
