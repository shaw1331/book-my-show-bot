from __future__ import annotations

import logging
import time
from datetime import datetime

from bot.preference_store import PreferenceStore
from config.schema import AppConfig
from models.movie import AvailabilityResult, AvailabilityStatus, Showtime, haversine_km
from notifier.base import BaseNotifier
from scraper.base import BaseScraper, ScraperError
from scraper.health import ScraperHealthMonitor
from scraper.http_scraper import HttpScraper

from checker.state import NotificationState

logger = logging.getLogger(__name__)


class AvailabilityChecker:
    """Core orchestrator: polls the scraper and dispatches notifications."""

    def __init__(
        self,
        config: AppConfig,
        scraper: BaseScraper,
        notifiers: list[BaseNotifier],
        prefs: PreferenceStore | None = None,
    ):
        self.config = config
        self.scraper = scraper
        self.notifiers = notifiers
        self.prefs = prefs
        self.state = NotificationState()
        self.health = ScraperHealthMonitor()
        self._current_interval = config.checker.interval_seconds
        self._check_count = 0
        self._degraded_notified = False

    def _pref(self, key: str, fallback: str | list) -> str | list:
        """Read from preference store if available, else fallback to config."""
        if self.prefs:
            val = self.prefs.get(key) if isinstance(fallback, str) else self.prefs.get_list(key)
            if val:
                return val
        return fallback

    def _movie_name(self) -> str:
        return self._pref("movie_name", self.config.movie.name)

    def _movie_code(self) -> str:
        return self._pref("movie_code", self.config.movie.code)

    def _max_days(self) -> int:
        return self.config.dates.max_days

    def _format_filter(self) -> list[str] | None:
        """Get format filter from prefs, or None if empty (fetch all)."""
        fmts = self._pref("formats", self.config.movie.formats)
        return fmts if fmts else None

    def _set_movie_code(self, code: str) -> None:
        if self.prefs:
            self.prefs.set("movie_code", code)
        self.config.movie.code = code

    def run(self) -> None:
        """Main polling loop. Runs until KeyboardInterrupt."""
        logger.info(
            "Starting availability checker for '%s' in %s (checking every %ds)",
            self._movie_name(),
            self.config.location.city,
            self.config.checker.interval_seconds,
        )

        try:
            while True:
                if self._in_quiet_hours():
                    logger.debug("In quiet hours, sleeping 60s")
                    time.sleep(60)
                    continue

                self._check_once()
                time.sleep(self._current_interval)
        except KeyboardInterrupt:
            logger.info("Shutting down (Ctrl+C)")
        finally:
            self.scraper.close()

    def check_once(self) -> AvailabilityResult | None:
        """Run a single check cycle. Returns the result or None on error."""
        return self._check_once()

    def fetch_raw_shows(self) -> list[Showtime]:
        """Fetch ALL available showtimes with NO filters applied.

        Used by the /lookup command to show what BMS has for a cinema.
        """
        movie_name = self._movie_name()
        movie_code = self._movie_code()

        if not movie_code and movie_name:
            if isinstance(self.scraper, HttpScraper):
                results = self.scraper.search_movie(
                    movie_name, self.config.location.city
                )
                if results:
                    movie_code = results[0]["code"]
                    self._set_movie_code(movie_code)

        if not movie_code:
            return []

        try:
            target_dates = self._resolve_target_dates()
            result = self.scraper.fetch_availability(
                movie_code=movie_code,
                city=self.config.location.city,
                region_code=self.config.location.region_code,
                target_dates=target_dates,
                max_days=self._max_days(),
            )
            return [
                s for s in result.showtimes
                if s.status in (AvailabilityStatus.AVAILABLE, AvailabilityStatus.FILLING_FAST)
            ]
        except ScraperError as e:
            logger.warning("fetch_raw_shows failed: %s", e)
            return []

    def get_formats_by_cinema(self) -> dict[str, list[str]]:
        """Get all available formats grouped by cinema (no filters).

        Used by /whatson command.
        """
        from collections import defaultdict

        shows = self.fetch_raw_shows()
        by_cinema: dict[str, set[str]] = defaultdict(set)
        for s in shows:
            by_cinema[s.cinema.name].add(s.format)
        return {cinema: sorted(fmts) for cinema, fmts in sorted(by_cinema.items())}

    def list_shows(self) -> list[Showtime]:
        """Fetch all matching available shows (no dedup, no notifications).

        Returns filtered + sorted showtimes for display purposes.
        """
        movie_name = self._movie_name()
        movie_code = self._movie_code()

        if not movie_code and movie_name:
            if isinstance(self.scraper, HttpScraper):
                results = self.scraper.search_movie(
                    movie_name, self.config.location.city
                )
                if results:
                    movie_code = results[0]["code"]
                    self._set_movie_code(movie_code)

        if not movie_code:
            return []

        try:
            target_dates = self._resolve_target_dates()
            result = self.scraper.fetch_availability(
                movie_code=movie_code,
                city=self.config.location.city,
                region_code=self.config.location.region_code,
                target_dates=target_dates,
                format_filter=self._format_filter(),
                max_days=self._max_days(),
            )
            matching = self._filter_showtimes(result.showtimes)
            return [
                s for s in matching
                if s.status in (AvailabilityStatus.AVAILABLE, AvailabilityStatus.FILLING_FAST)
            ]
        except ScraperError as e:
            logger.warning("list_shows failed: %s", e)
            return []

    def _check_once(self) -> AvailabilityResult | None:
        self._check_count += 1

        movie_name = self._movie_name()
        movie_code = self._movie_code()

        # Auto-resolve movie code if changed via Telegram /movie command
        if not movie_code and movie_name:
            if isinstance(self.scraper, HttpScraper):
                logger.info("Resolving movie code for '%s'...", movie_name)
                results = self.scraper.search_movie(
                    movie_name, self.config.location.city
                )
                if results:
                    movie_code = results[0]["code"]
                    self._set_movie_code(movie_code)
                    logger.info("Resolved to: %s (%s)", results[0]["name"], movie_code)
                    self.state.reset()
                else:
                    logger.warning("Could not resolve movie '%s'", movie_name)
                    return None

        logger.info(
            "Check #%d — fetching availability for '%s'",
            self._check_count,
            movie_name,
        )

        try:
            target_dates = self._resolve_target_dates()
            result = self.scraper.fetch_availability(
                movie_code=movie_code,
                city=self.config.location.city,
                region_code=self.config.location.region_code,
                target_dates=target_dates,
                format_filter=self._format_filter(),
                max_days=self._max_days(),
            )
            result.movie_name = movie_name
            self.health.record(True)
            self._current_interval = self.config.checker.interval_seconds
            self._degraded_notified = False

            # Filter by user preferences
            matching = self._filter_showtimes(result.showtimes)
            available = [
                s
                for s in matching
                if s.status
                in (AvailabilityStatus.AVAILABLE, AvailabilityStatus.FILLING_FAST)
            ]

            if available:
                new = self.state.filter_new(available)
                if new:
                    logger.info("Found %d NEW available showtime(s)!", len(new))
                    self._notify(result.movie_name, new)
                    self.state.mark_notified(new)
                else:
                    logger.info(
                        "%d available (all previously notified)", len(available)
                    )
            else:
                logger.info("No available showtimes found")

            # Periodic health check
            if self._should_health_check():
                self._run_health_check()

            return result

        except ScraperError as e:
            self.health.record(False)
            self._current_interval = min(
                self._current_interval * self.config.checker.backoff_multiplier,
                self.config.checker.max_interval_seconds,
            )
            logger.warning(
                "Scraper failed (backing off to %.0fs): %s",
                self._current_interval,
                e,
            )

            if self.health.is_degraded and not self._degraded_notified:
                self._notify_degraded()
                self._degraded_notified = True

            return None

    def _resolve_target_dates(self) -> list[str] | None:
        """Convert config date preferences to YYYYMMDD date codes."""
        dates_cfg = self.config.dates

        if dates_cfg.any_date:
            return None  # Let the scraper discover all available dates

        result = []
        for d in dates_cfg.target_dates:
            # Normalize various formats to YYYYMMDD
            clean = d.replace("-", "")
            if len(clean) == 8:
                result.append(clean)

        # target_days are handled post-fetch via _filter_showtimes
        return result or None

    def _filter_showtimes(self, showtimes: list[Showtime]) -> list[Showtime]:
        """Apply user preference filters (cinemas, languages, formats, days)."""
        filtered = showtimes

        # Filter by preferred cinemas
        cinema_prefs = self._pref("preferred_cinemas", self.config.location.preferred_cinemas)
        if cinema_prefs:
            prefs_lower = [p.lower() for p in cinema_prefs]
            filtered = [
                s
                for s in filtered
                if any(p in s.cinema.name.lower() for p in prefs_lower)
            ]

        # Filter by languages
        langs = self._pref("languages", self.config.movie.languages)
        if langs:
            langs_lower = [l.lower() for l in langs]
            filtered = [
                s for s in filtered if s.language.lower() in langs_lower
            ]

        # Filter by formats
        fmts = self._pref("formats", self.config.movie.formats)
        if fmts:
            fmts_lower = [f.lower() for f in fmts]
            filtered = [
                s
                for s in filtered
                if any(f in s.format.lower() for f in fmts_lower)
            ]

        # Filter by target days
        days = self.config.dates.target_days
        if days and not self.config.dates.any_date:
            days_lower = [d.lower() for d in days]
            filtered = [
                s
                for s in filtered
                if s.show_date.strftime("%A").lower() in days_lower
            ]

        # Filter by max distance
        loc = self.config.location
        if loc.lat and loc.lng and loc.max_distance_km > 0:
            filtered = [
                s
                for s in filtered
                if s.cinema.lat
                and s.cinema.lng
                and haversine_km(loc.lat, loc.lng, s.cinema.lat, s.cinema.lng)
                <= loc.max_distance_km
            ]

        # Sort by distance if user location is set
        if loc.lat and loc.lng:
            filtered.sort(
                key=lambda s: haversine_km(
                    loc.lat, loc.lng, s.cinema.lat, s.cinema.lng
                )
                if s.cinema.lat and s.cinema.lng
                else float("inf")
            )

        return filtered

    def _notify(self, movie_name: str, showtimes: list[Showtime]) -> None:
        """Send notifications for new available showtimes."""
        title = f"🎬 {movie_name} — Tickets Available!"

        loc = self.config.location
        lines = []
        for s in showtimes[:10]:  # Cap at 10 to avoid huge messages
            status_icon = "🟢" if s.status == AvailabilityStatus.AVAILABLE else "🟡"
            dist_str = ""
            if loc.lat and loc.lng and s.cinema.lat and s.cinema.lng:
                dist = haversine_km(loc.lat, loc.lng, s.cinema.lat, s.cinema.lng)
                dist_str = f" | {dist:.1f} km away"
            lines.append(
                f"{status_icon} {s.cinema.name}\n"
                f"   {s.show_date.strftime('%a %d %b')} at {s.show_time.strftime('%I:%M %p')}\n"
                f"   {s.format} | {s.language} | {s.price_range}{dist_str}"
            )

        if len(showtimes) > 10:
            lines.append(f"\n... and {len(showtimes) - 10} more showtimes")

        message = "\n\n".join(lines)
        url = showtimes[0].booking_url if showtimes else ""

        for notifier in self.notifiers:
            try:
                notifier.send(title=title, message=message, url=url, priority="high")
            except Exception as e:
                logger.error(
                    "Failed to send via %s: %s", notifier.channel_name, e
                )

    def _notify_degraded(self) -> None:
        """Alert the user that the scraper may be broken."""
        title = "⚠️ BMS Bot — Scraper Degraded"
        message = (
            f"The scraper has failed {self.health.failure_rate:.0%} of "
            f"recent checks. It may be broken due to a BookMyShow site change.\n\n"
            f"The bot will keep retrying with backoff. Check the logs for details."
        )
        for notifier in self.notifiers:
            try:
                notifier.send(title=title, message=message, priority="max")
            except Exception as e:
                logger.error(
                    "Failed to send degraded alert via %s: %s",
                    notifier.channel_name,
                    e,
                )

    def _should_health_check(self) -> bool:
        interval = self.config.checker.health_check_interval
        return interval > 0 and self._check_count % interval == 0

    def _run_health_check(self) -> None:
        city = self.config.location.city
        logger.info("Running health check for city: %s", city)
        healthy = self.scraper.health_check(city)
        if healthy:
            logger.info("Health check passed")
        else:
            logger.warning("Health check FAILED — scraper may be broken")
            self.health.record(False)

    def _in_quiet_hours(self) -> bool:
        qh = self.config.checker.quiet_hours
        if not qh:
            return False

        now = datetime.now().time()
        try:
            start = datetime.strptime(qh.start, "%H:%M").time()
            end = datetime.strptime(qh.end, "%H:%M").time()
        except ValueError:
            return False

        if start <= end:
            return start <= now <= end
        # Wraps midnight (e.g. 23:00 - 06:00)
        return now >= start or now <= end
