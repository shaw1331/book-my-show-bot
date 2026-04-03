from __future__ import annotations

import logging
import time
from datetime import datetime

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
    ):
        self.config = config
        self.scraper = scraper
        self.notifiers = notifiers
        self.state = NotificationState()
        self.health = ScraperHealthMonitor()
        self._current_interval = config.checker.interval_seconds
        self._check_count = 0
        self._degraded_notified = False

    def run(self) -> None:
        """Main polling loop. Runs until KeyboardInterrupt."""
        logger.info(
            "Starting availability checker for '%s' in %s (checking every %ds)",
            self.config.movie.name,
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

    def _check_once(self) -> AvailabilityResult | None:
        self._check_count += 1

        # Auto-resolve movie code if changed via Telegram /movie command
        if not self.config.movie.code and self.config.movie.name:
            if isinstance(self.scraper, HttpScraper):
                logger.info("Resolving movie code for '%s'...", self.config.movie.name)
                results = self.scraper.search_movie(
                    self.config.movie.name, self.config.location.city
                )
                if results:
                    self.config.movie.code = results[0]["code"]
                    logger.info("Resolved to: %s (%s)", results[0]["name"], results[0]["code"])
                    self.state.reset()  # New movie, reset dedup state
                else:
                    logger.warning("Could not resolve movie '%s'", self.config.movie.name)
                    return None

        logger.info(
            "Check #%d — fetching availability for '%s'",
            self._check_count,
            self.config.movie.name,
        )

        try:
            target_dates = self._resolve_target_dates()
            result = self.scraper.fetch_availability(
                movie_code=self.config.movie.code,
                city=self.config.location.city,
                region_code=self.config.location.region_code,
                target_dates=target_dates,
            )
            result.movie_name = self.config.movie.name
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
        prefs = self.config.location.preferred_cinemas
        if prefs:
            prefs_lower = [p.lower() for p in prefs]
            filtered = [
                s
                for s in filtered
                if any(p in s.cinema.name.lower() for p in prefs_lower)
            ]

        # Filter by languages
        langs = self.config.movie.languages
        if langs:
            langs_lower = [l.lower() for l in langs]
            filtered = [
                s for s in filtered if s.language.lower() in langs_lower
            ]

        # Filter by formats
        fmts = self.config.movie.formats
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
