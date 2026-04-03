from __future__ import annotations

from collections import deque


class ScraperHealthMonitor:
    """Tracks scraper success/failure over a sliding window.

    Used by the checker to detect when scraping is systematically broken
    (as opposed to a single transient failure).
    """

    def __init__(self, window_size: int = 10):
        self._results: deque[bool] = deque(maxlen=window_size)

    def record(self, success: bool) -> None:
        self._results.append(success)

    @property
    def failure_rate(self) -> float:
        if not self._results:
            return 0.0
        failures = sum(1 for r in self._results if not r)
        return failures / len(self._results)

    @property
    def is_degraded(self) -> bool:
        """True if >50% of recent checks failed."""
        return len(self._results) >= 3 and self.failure_rate > 0.5

    @property
    def total_checks(self) -> int:
        return len(self._results)

    def reset(self) -> None:
        self._results.clear()
