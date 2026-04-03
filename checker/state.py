from __future__ import annotations

from models.movie import Showtime


class NotificationState:
    """Tracks which showtimes have already been notified about.

    Prevents duplicate alerts across polling cycles. Uses a set of
    (cinema_code, date, time, format, language) tuples for dedup.
    """

    def __init__(self) -> None:
        self._notified: set[tuple] = set()

    def filter_new(self, showtimes: list[Showtime]) -> list[Showtime]:
        """Return only showtimes that haven't been notified yet."""
        return [s for s in showtimes if self._key(s) not in self._notified]

    def mark_notified(self, showtimes: list[Showtime]) -> None:
        """Mark showtimes as already notified."""
        for s in showtimes:
            self._notified.add(self._key(s))

    @property
    def notified_count(self) -> int:
        return len(self._notified)

    def reset(self) -> None:
        self._notified.clear()

    @staticmethod
    def _key(s: Showtime) -> tuple:
        return (s.cinema.code, s.show_date, s.show_time, s.format, s.language)
