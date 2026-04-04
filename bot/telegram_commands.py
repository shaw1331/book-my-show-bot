"""Telegram bot command handler for managing preferences via chat.

Supports dynamic multi-user chat management with password auth.
Users send /start <password> to connect, /close to disconnect.

Supported commands:
    /start PASSWORD  — Authenticate and start receiving notifications
    /close           — Stop receiving notifications, disconnect
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

from bot.chat_store import ChatStore
from bot.preference_store import PreferenceStore
from config.schema import AppConfig

logger = logging.getLogger(__name__)


class TelegramCommandBot:
    """Polls Telegram for commands and updates AppConfig in-place.

    Supports multiple users via ChatStore. Only authenticated users
    (who have sent /start <password>) can use commands.
    """

    def __init__(
        self,
        bot_token: str,
        chat_store: ChatStore,
        auth_password: str,
        config: AppConfig,
        prefs: PreferenceStore,
        on_check_now: callable = None,
        on_list_shows: callable = None,
        on_fetch_raw: callable = None,
        on_whatson: callable = None,
    ):
        self.bot_token = bot_token
        self.chat_store = chat_store
        self.auth_password = auth_password
        self._api = f"https://api.telegram.org/bot{bot_token}"
        self.config = config
        self.prefs = prefs
        self._on_check_now = on_check_now
        self._on_list_shows = on_list_shows
        self._on_fetch_raw = on_fetch_raw
        self._on_whatson = on_whatson
        self._offset = 0
        self._running = False
        self._thread: threading.Thread | None = None
        # Per-user pending state for commands awaiting arguments
        # Maps chat_id -> command name (e.g. "addcinema")
        self._pending: dict[str, str] = {}

    def start(self) -> None:
        """Start polling for commands in a background thread."""
        self._flush_old_updates()
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info(
            "Telegram command bot started (%d active chat(s))",
            self.chat_store.count,
        )

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

            if not chat_id:
                continue

            logger.info("Telegram [chat %s]: %s", chat_id, text or "<empty>")

            if not text:
                continue

            # Strip leading / if present (both "help" and "/help" work)
            clean = text.lstrip("/")
            parts = clean.split(maxsplit=1)
            cmd = parts[0].lower().split("@")[0]
            arg = parts[1].strip() if len(parts) > 1 else ""

            # "start" is always allowed (for auth)
            if cmd == "start":
                self._pending.pop(chat_id, None)
                self._cmd_start(chat_id, arg)
                continue

            # Silently ignore messages from non-active chats
            if not self.chat_store.is_active(chat_id):
                logger.debug("Ignoring message from inactive chat %s", chat_id)
                continue

            # If user has a pending command and this isn't a known command,
            # treat the entire text as the argument for the pending command
            if chat_id in self._pending and cmd not in self._all_commands():
                pending_cmd = self._pending.pop(chat_id)
                logger.info("Resolving pending '%s' with arg: %s", pending_cmd, text)
                self._handle_command(chat_id, pending_cmd, text)
                continue

            # Clear any pending state if user sends a new command
            self._pending.pop(chat_id, None)
            self._handle_command(chat_id, cmd, arg)

    def _handle_command(self, chat_id: str, cmd: str, arg: str) -> None:
        handlers = {
            "help": lambda: self._cmd_help(chat_id),
            "status": lambda: self._cmd_status(chat_id),
            "close": lambda: self._cmd_close(chat_id),
            "cinemas": lambda: self._cmd_cinemas(chat_id),
            "addcinema": lambda: self._cmd_add_cinema(chat_id, arg),
            "removecinema": lambda: self._cmd_remove_cinema(chat_id, arg),
            "clearcinemas": lambda: self._cmd_clear_cinemas(chat_id),
            "formats": lambda: self._cmd_formats(chat_id),
            "addformat": lambda: self._cmd_add_format(chat_id, arg),
            "removeformat": lambda: self._cmd_remove_format(chat_id, arg),
            "clearformats": lambda: self._cmd_clear_formats(chat_id),
            "languages": lambda: self._cmd_languages(chat_id),
            "addlang": lambda: self._cmd_add_lang(chat_id, arg),
            "removelang": lambda: self._cmd_remove_lang(chat_id, arg),
            "movie": lambda: self._cmd_movie(chat_id, arg),
            "check": lambda: self._cmd_check(chat_id),
            "list": lambda: self._cmd_list(chat_id),
            "shows": lambda: self._cmd_list(chat_id),
            "lookup": lambda: self._cmd_lookup(chat_id, arg),
            "whatson": lambda: self._cmd_whatson(chat_id),
        }

        handler = handlers.get(cmd)
        if handler:
            handler()
        else:
            self._send(chat_id, f"Unknown command: {cmd}\nType help for available commands.")

    # --- Auth commands ---

    def _cmd_start(self, chat_id: str, password: str) -> None:
        if self.chat_store.is_active(chat_id):
            self._send(chat_id, "You're already connected! Send <b>help</b> for commands.")
            return

        if not password:
            self._send(chat_id, "Welcome! Send <b>start &lt;password&gt;</b> to authenticate.")
            return

        if password != self.auth_password:
            logger.warning("Failed auth attempt from chat %s", chat_id)
            self._send(chat_id, "Wrong password. Try again with <b>start &lt;password&gt;</b>")
            return

        self.chat_store.add(chat_id)
        self._send(
            chat_id,
            "✅ <b>Connected!</b>\n\n"
            "You'll now receive movie availability notifications.\n"
            "Send <b>help</b> to see available commands.\n"
            "Send <b>close</b> to disconnect.",
        )

    def _cmd_close(self, chat_id: str) -> None:
        self.chat_store.remove(chat_id)
        self._send(
            chat_id,
            "👋 <b>Disconnected.</b>\n\n"
            "You'll no longer receive notifications.\n"
            "Send <b>start &lt;password&gt;</b> to reconnect.",
        )

    # --- Preference commands ---

    def _cmd_help(self, chat_id: str) -> None:
        self._send(
            chat_id,
            "<b>BMS Bot Commands</b>\n"
            "<i>Tap a command or just type without /</i>\n\n"
            "<b>Session:</b>\n"
            "/close — Disconnect from bot\n\n"
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
            "/whatson — Formats available by cinema\n"
            "/lookup CINEMA — All shows at a cinema (no filters)\n"
            "/list — Show all matching shows now\n"
            "/check — Trigger immediate check\n"
            "/help — Show this message",
        )

    def _cmd_status(self, chat_id: str) -> None:
        c = self.config
        cinemas = ", ".join(self.prefs.get_list("preferred_cinemas")) or "Any"
        formats = ", ".join(self.prefs.get_list("formats")) or "Any"
        langs = ", ".join(self.prefs.get_list("languages")) or "Any"
        dist = f"{c.location.max_distance_km} km" if c.location.max_distance_km else "No limit"
        movie_name = self.prefs.get("movie_name") or c.movie.name
        self._send(
            chat_id,
            f"<b>Current Settings</b>\n\n"
            f"🎬 <b>Movie:</b> {movie_name}\n"
            f"🏙 <b>City:</b> {c.location.city}\n"
            f"🎭 <b>Cinemas:</b> {cinemas}\n"
            f"📽 <b>Formats:</b> {formats}\n"
            f"🗣 <b>Languages:</b> {langs}\n"
            f"📍 <b>Max distance:</b> {dist}\n"
            f"⏱ <b>Check interval:</b> {c.checker.interval_seconds}s\n"
            f"👥 <b>Active users:</b> {self.chat_store.count}",
        )

    # Cinemas
    def _cmd_cinemas(self, chat_id: str) -> None:
        items = self.prefs.get_list("preferred_cinemas")
        if not items:
            self._send(chat_id, "No cinema preference set — watching all cinemas.\nUse /addcinema NAME to add one.")
            return
        lines = [f"{i+1}. {c}" for i, c in enumerate(items)]
        self._send(chat_id, "<b>Preferred Cinemas:</b>\n" + "\n".join(lines))

    def _cmd_add_cinema(self, chat_id: str, name: str) -> None:
        if not name:
            self._pending[chat_id] = "addcinema"
            self._send_force_reply(chat_id, "Which cinema do you want to add?", "e.g. PVR Forum Mall")
            return
        items = self.prefs.append_to_list("preferred_cinemas", name)
        self._send(chat_id, f"✅ Added cinema: <b>{name}</b>\n\nCurrent list:\n" + self._numbered_list(items))

    def _cmd_remove_cinema(self, chat_id: str, arg: str) -> None:
        items = self.prefs.get_list("preferred_cinemas")
        if not arg:
            if not items:
                self._send(chat_id, "No cinemas to remove.")
                return
            self._pending[chat_id] = "removecinema"
            lines = [f"{i+1}. {c}" for i, c in enumerate(items)]
            self._send_force_reply(chat_id, "Which number to remove?\n" + "\n".join(lines), "e.g. 1")
            return
        idx = self._parse_index(chat_id, arg, items)
        if idx is None:
            return
        removed = self.prefs.remove_from_list("preferred_cinemas", idx)
        self._send(chat_id, f"🗑 Removed: <b>{removed}</b>")

    def _cmd_clear_cinemas(self, chat_id: str) -> None:
        self.prefs.clear_list("preferred_cinemas")
        self._send(chat_id, "Cleared all cinema preferences — now watching all cinemas.")

    # Formats
    def _cmd_formats(self, chat_id: str) -> None:
        items = self.prefs.get_list("formats")
        if not items:
            self._send(chat_id, "No format preference set — watching all formats.\nUse /addformat IMAX 2D to add one.")
            return
        lines = [f"{i+1}. {f}" for i, f in enumerate(items)]
        self._send(chat_id, "<b>Preferred Formats:</b>\n" + "\n".join(lines))

    def _cmd_add_format(self, chat_id: str, fmt: str) -> None:
        if not fmt:
            self._pending[chat_id] = "addformat"
            self._send_force_reply(chat_id, "Which format do you want to add?", "e.g. IMAX 2D")
            return
        items = self.prefs.append_to_list("formats", fmt)
        self._send(chat_id, f"✅ Added format: <b>{fmt}</b>\n\nCurrent list:\n" + self._numbered_list(items))

    def _cmd_remove_format(self, chat_id: str, arg: str) -> None:
        items = self.prefs.get_list("formats")
        if not arg:
            if not items:
                self._send(chat_id, "No formats to remove.")
                return
            self._pending[chat_id] = "removeformat"
            lines = [f"{i+1}. {f}" for i, f in enumerate(items)]
            self._send_force_reply(chat_id, "Which number to remove?\n" + "\n".join(lines), "e.g. 1")
            return
        idx = self._parse_index(chat_id, arg, items)
        if idx is None:
            return
        removed = self.prefs.remove_from_list("formats", idx)
        self._send(chat_id, f"🗑 Removed: <b>{removed}</b>")

    def _cmd_clear_formats(self, chat_id: str) -> None:
        self.prefs.clear_list("formats")
        self._send(chat_id, "Cleared all format preferences — now watching all formats.")

    # Languages
    def _cmd_languages(self, chat_id: str) -> None:
        items = self.prefs.get_list("languages")
        if not items:
            self._send(chat_id, "No language preference set — watching all languages.\nUse /addlang English to add one.")
            return
        lines = [f"{i+1}. {l}" for i, l in enumerate(items)]
        self._send(chat_id, "<b>Preferred Languages:</b>\n" + "\n".join(lines))

    def _cmd_add_lang(self, chat_id: str, lang: str) -> None:
        if not lang:
            self._pending[chat_id] = "addlang"
            self._send_force_reply(chat_id, "Which language do you want to add?", "e.g. English")
            return
        items = self.prefs.append_to_list("languages", lang)
        self._send(chat_id, f"✅ Added language: <b>{lang}</b>\n\nCurrent list:\n" + self._numbered_list(items))

    def _cmd_remove_lang(self, chat_id: str, arg: str) -> None:
        items = self.prefs.get_list("languages")
        if not arg:
            if not items:
                self._send(chat_id, "No languages to remove.")
                return
            self._pending[chat_id] = "removelang"
            lines = [f"{i+1}. {l}" for i, l in enumerate(items)]
            self._send_force_reply(chat_id, "Which number to remove?\n" + "\n".join(lines), "e.g. 1")
            return
        idx = self._parse_index(chat_id, arg, items)
        if idx is None:
            return
        removed = self.prefs.remove_from_list("languages", idx)
        self._send(chat_id, f"🗑 Removed: <b>{removed}</b>")

    # Movie
    def _cmd_movie(self, chat_id: str, name: str) -> None:
        if not name:
            self._pending[chat_id] = "movie"
            movie_name = self.prefs.get("movie_name") or self.config.movie.name
            self._send_force_reply(
                chat_id,
                f"Currently tracking: <b>{movie_name}</b>\n\nWhich movie do you want to track?",
                "e.g. Project Hail Mary",
            )
            return
        self.prefs.set("movie_name", name)
        self.prefs.set("movie_code", "")
        self._send(chat_id, f"✅ Now tracking: <b>{name}</b>\nWill auto-resolve event code on next check.")

    # Check now
    def _cmd_check(self, chat_id: str) -> None:
        if not self._on_check_now:
            self._send(chat_id, "Manual check not available in this mode.")
            return

        self._send(chat_id, "⏳ Running check now...")
        result = self._on_check_now()
        movie_name = self.prefs.get("movie_name") or self.config.movie.name

        if result is None:
            self._send(chat_id, "❌ Check failed. See logs for details.")
            return

        if not result.showtimes:
            self._send(chat_id, f"No shows found for <b>{movie_name}</b> in {self.config.location.city}.")
            return

        filtered = self._on_list_shows() if self._on_list_shows else []
        if not filtered:
            fmts = self.prefs.get_list("formats")
            cinemas = self.prefs.get_list("preferred_cinemas")
            hints = []
            if fmts:
                hints.append(f"formats: {', '.join(fmts)}")
            if cinemas:
                hints.append(f"cinemas: {', '.join(cinemas)}")
            hint_str = f"\n\nFilters: {' | '.join(hints)}" if hints else ""
            self._send(
                chat_id,
                f"Shows exist for <b>{movie_name}</b> but none match your filters.{hint_str}\n\n"
                f"Try /clearformats or /clearcinemas to broaden search.",
            )
            return

        # Grouped display: Cinema → Format+Language → Date → Timings
        self._send_grouped_shows(chat_id, movie_name, filtered)

    # List all matching shows
    def _cmd_list(self, chat_id: str) -> None:
        if not self._on_list_shows:
            self._send(chat_id, "List not available in this mode.")
            return

        self._send(chat_id, "⏳ Fetching shows...")
        shows = self._on_list_shows()

        if not shows:
            movie_name = self.prefs.get("movie_name") or self.config.movie.name
            self._send(chat_id, f"No matching shows found for <b>{movie_name}</b>.")
            return

        movie_name = self.prefs.get("movie_name") or self.config.movie.name
        self._send_grouped_shows(chat_id, movie_name, shows)

    # Lookup: movie x cinema with all formats (no filters)
    def _cmd_lookup(self, chat_id: str, cinema_query: str) -> None:
        if not self._on_fetch_raw:
            self._send(chat_id, "Lookup not available in this mode.")
            return

        if not cinema_query:
            self._pending[chat_id] = "lookup"
            self._send_force_reply(chat_id, "Which cinema do you want to look up?", "e.g. Nexus Shantiniketan")
            return

        self._send(chat_id, f"⏳ Looking up shows at <b>{cinema_query}</b>...")
        all_shows = self._on_fetch_raw()

        if not all_shows:
            movie_name = self.prefs.get("movie_name") or self.config.movie.name
            self._send(chat_id, f"No shows found for <b>{movie_name}</b>.")
            return

        # Fuzzy match cinema name
        query_lower = cinema_query.lower()
        matched = [s for s in all_shows if query_lower in s.cinema.name.lower()]

        movie_name = self.prefs.get("movie_name") or self.config.movie.name

        if not matched:
            # Show available cinemas as suggestions
            cinema_names = sorted(set(s.cinema.name for s in all_shows))
            suggestions = "\n".join(f"• {c}" for c in cinema_names[:15])
            more = f"\n<i>...and {len(cinema_names) - 15} more</i>" if len(cinema_names) > 15 else ""
            self._send(
                chat_id,
                f"No cinema matching <b>{cinema_query}</b> found.\n\n"
                f"<b>Available cinemas for {movie_name}:</b>\n{suggestions}{more}",
            )
            return

        self._send_grouped_shows(chat_id, movie_name, matched, subtitle=f"at <b>{cinema_query}</b>")

    # What's on: formats grouped by cinema (no filters)
    def _cmd_whatson(self, chat_id: str) -> None:
        if not self._on_whatson:
            self._send(chat_id, "Not available in this mode.")
            return

        self._send(chat_id, "⏳ Fetching available formats...")
        formats_by_cinema = self._on_whatson()

        if not formats_by_cinema:
            movie_name = self.prefs.get("movie_name") or self.config.movie.name
            self._send(chat_id, f"No shows found for <b>{movie_name}</b>.")
            return

        movie_name = self.prefs.get("movie_name") or self.config.movie.name
        lines = [f"🎬 <b>{movie_name}</b> — Formats by Cinema\n"]

        for cinema_name, formats in formats_by_cinema.items():
            lines.append(f"🏢 <b>{cinema_name}</b>")
            lines.append(f"  📽 {', '.join(formats)}")
            lines.append("")

        self._send(chat_id, "\n".join(lines))

    # --- Helpers ---

    def _send_grouped_shows(
        self, chat_id: str, movie_name: str, shows: list, subtitle: str = ""
    ) -> None:
        """Send shows grouped by Cinema → Format+Language → Date → Timings."""
        from collections import defaultdict
        from models.movie import AvailabilityStatus, haversine_km

        loc = self.config.location

        by_cinema: dict[str, list] = defaultdict(list)
        for s in shows:
            by_cinema[s.cinema.name].append(s)

        lines = []
        for cinema_name, cinema_shows in by_cinema.items():
            dist_str = ""
            if loc.lat and loc.lng and cinema_shows[0].cinema.lat and cinema_shows[0].cinema.lng:
                dist = haversine_km(loc.lat, loc.lng, cinema_shows[0].cinema.lat, cinema_shows[0].cinema.lng)
                dist_str = f" ({dist:.1f}km)"

            lines.append(f"🏢 <b>{cinema_name}</b>{dist_str}")

            by_fmt_lang: dict[tuple, list] = defaultdict(list)
            for s in cinema_shows:
                by_fmt_lang[(s.format, s.language)].append(s)

            for (fmt, lang), fmt_shows in by_fmt_lang.items():
                lines.append(f"\n  📽 <b>{fmt}</b> | {lang}")

                by_date: dict[str, list] = defaultdict(list)
                for s in fmt_shows:
                    by_date[s.show_date.strftime("%a %d %b")].append(s)

                for date_str, date_shows in by_date.items():
                    timings = []
                    for s in sorted(date_shows, key=lambda x: x.show_time):
                        icon = "\n    🟢" if s.status == AvailabilityStatus.AVAILABLE else "\n    🟡"
                        timings.append(f"{icon}{s.show_time.strftime('%I:%M %p')} ({s.price_range})")
                    lines.append(f"    {date_str}:")
                    lines.append(f"    {''.join(timings)}")

            lines.append("\n")

        sub = f" {subtitle}" if subtitle else ""
        header = f"🎬 <b>{movie_name}</b>{sub}\n{len(shows)} show(s)\n\n"
        self._send(chat_id, header + "\n".join(lines))

    def _all_commands(self) -> set[str]:
        """All known command names (without /)."""
        return {
            "help", "status", "close", "start", "check", "list", "shows", "lookup", "whatson",
            "cinemas", "addcinema", "removecinema", "clearcinemas",
            "formats", "addformat", "removeformat", "clearformats",
            "languages", "addlang", "removelang",
            "movie",
        }

    def _send_force_reply(self, chat_id: str, text: str, placeholder: str = "") -> None:
        """Send a message that forces the user to reply (shows input field)."""
        logger.info("Sending force reply to chat %s", chat_id)
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {
                "force_reply": True,
                "input_field_placeholder": placeholder,
            },
        }
        try:
            requests.post(f"{self._api}/sendMessage", json=payload, timeout=10)
        except requests.RequestException as e:
            logger.error("Failed to send force reply: %s", e)

    def _send(self, chat_id: str, text: str) -> None:
        logger.info("Sending response to chat %s", chat_id)
        try:
            requests.post(
                f"{self._api}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
        except requests.RequestException as e:
            logger.error("Failed to send Telegram response: %s", e)

    def _parse_index(self, chat_id: str, arg: str, items: list) -> int | None:
        try:
            idx = int(arg) - 1
            if 0 <= idx < len(items):
                return idx
            self._send(chat_id, f"Invalid number. Valid range: 1-{len(items)}")
        except ValueError:
            self._send(chat_id, f"Usage: provide a number (1-{len(items)})")
        return None

    @staticmethod
    def _numbered_list(items: list[str]) -> str:
        return "\n".join(f"{i+1}. {item}" for i, item in enumerate(items))
