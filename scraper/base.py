from __future__ import annotations

from abc import ABC, abstractmethod

from models.movie import AvailabilityResult


class ScraperError(Exception):
    """Raised when a scraper encounters a non-recoverable error."""


class BaseScraper(ABC):
    """Abstract interface for all BookMyShow scrapers.

    The checker module only interacts through this contract.
    When BMS changes, only the concrete scraper needs updating.
    """

    @abstractmethod
    def fetch_availability(
        self,
        movie_code: str,
        city: str,
        region_code: str,
        target_dates: list[str] | None = None,
        format_filter: list[str] | None = None,
        max_days: int = 0,
    ) -> AvailabilityResult:
        """Fetch current availability for a movie.

        Args:
            movie_code: BMS event code (e.g. "ET00402820")
            city: BMS city slug (e.g. "hyderabad")
            region_code: BMS region code (e.g. "HYD")
            target_dates: Optional list of dates in YYYYMMDD format.
                          None means check all available dates.

        Raises:
            ScraperError: On non-recoverable failure.
        """
        ...

    @abstractmethod
    def health_check(self, city: str) -> bool:
        """Canary check: can we fetch ANY movie data for this city?

        Used to distinguish 'movie not available' from 'scraper is broken'.
        Returns True if the scraper is functioning correctly.
        """
        ...

    def close(self) -> None:
        """Clean up resources (sessions, browser instances)."""
