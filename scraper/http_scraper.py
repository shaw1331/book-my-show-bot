from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import date, datetime, time as dt_time

from curl_cffi import requests as cffi_requests
from curl_cffi.requests.exceptions import RequestException

from config.schema import ScraperConfig
from models.movie import (
    AvailabilityResult,
    AvailabilityStatus,
    Cinema,
    Showtime,
)
from scraper.base import BaseScraper, ScraperError

logger = logging.getLogger(__name__)

_SHOWTIMES_API = "https://in.bookmyshow.com/api/movies-data/showtimes-by-event"
_MOVIES_PAGE = "https://in.bookmyshow.com/explore/movies-{city}"

# Static token used by the BMS mobile app
_DEFAULT_TOKEN = "67x1xa33b4x422b361ba"
_DEFAULT_APP_CODE = "MOBAND2"
_DEFAULT_APP_VERSION = "14304"

_AVAILABILITY_MAP = {
    "A": AvailabilityStatus.AVAILABLE,
    "F": AvailabilityStatus.FILLING_FAST,
    "N": AvailabilityStatus.SOLD_OUT,
}

# Browser impersonation targets for curl_cffi
_IMPERSONATE_TARGETS = ["chrome", "chrome110", "chrome120"]


class HttpScraper(BaseScraper):
    """HTTP-based scraper using the BMS showtimes JSON API.

    Uses curl_cffi to impersonate a real browser (TLS fingerprint),
    which bypasses Cloudflare's bot detection. First visits the BMS
    website to acquire session cookies, then hits the JSON API.
    """

    def __init__(self, config: ScraperConfig, city: str = ""):
        self.config = config
        self._city = city
        self._session = self._build_session()
        self._warmed_up = False
        # Cache: movie_code -> {dimension: event_code}
        self._dimensions_cache: dict[str, dict[str, str]] = {}

    def _build_session(self) -> cffi_requests.Session:
        target = random.choice(_IMPERSONATE_TARGETS)
        session = cffi_requests.Session(impersonate=target)
        return session

    def _warm_up(self) -> None:
        """Visit the BMS website once to acquire Cloudflare cookies."""
        if self._warmed_up:
            return
        city = self._city or "hyderabad"
        url = _MOVIES_PAGE.format(city=city)
        try:
            logger.info("Warming up session (visiting %s)...", url)
            resp = self._session.get(url, timeout=self.config.request_timeout)
            if resp.status_code == 200:
                self._warmed_up = True
                logger.info("Session warmed up (got Cloudflare cookies)")
            else:
                logger.warning("Warm-up got status %d", resp.status_code)
        except RequestException as e:
            logger.warning("Warm-up failed: %s", e)

    def _refresh_session(self) -> None:
        """Recreate the session with a fresh browser fingerprint."""
        logger.info("Refreshing session (new browser fingerprint)")
        self._session.close()
        self._session = self._build_session()
        self._warmed_up = False

    def _get_available_dates(self, show_data: dict) -> list[dict]:
        """Extract available dates from the showtimes response."""
        return [
            d
            for d in show_data.get("ShowDatesArray", [])
            if not d.get("isDisabled", False)
        ]

    def discover_dimensions(self, movie_code: str, city: str) -> dict[str, str]:
        """Discover all dimension variants (IMAX 2D, 4DX, etc.) for a movie.

        BMS uses separate event codes per dimension. This fetches the movie
        detail page and extracts the dimension -> event code mapping.
        Results are cached per movie_code.

        Returns: {"2D": "ET00451760", "IMAX 2D": "ET00481564", ...}
        """
        if movie_code in self._dimensions_cache:
            return self._dimensions_cache[movie_code]

        self._warm_up()
        detail_url = f"https://in.bookmyshow.com/movies/{city}/-/{movie_code}"
        try:
            resp = self._session.get(detail_url, timeout=self.config.request_timeout)
            if resp.status_code != 200:
                return {"2D": movie_code}

            mappings = re.findall(
                r'\{"dimension":"([^"]+)","eventCode":"(ET\d{8})"',
                resp.text,
            )
            if mappings:
                result = {dim: code for dim, code in mappings}
                self._dimensions_cache[movie_code] = result
                logger.info("Discovered %d dimension(s): %s", len(result), list(result.keys()))
                return result
        except Exception as e:
            logger.warning("Failed to discover dimensions: %s", e)

        return {"2D": movie_code}

    @staticmethod
    def _slugify(code: str) -> str:
        """Placeholder slug — BMS redirects to the correct URL anyway."""
        return "-"

    def fetch_availability(
        self,
        movie_code: str,
        city: str,
        region_code: str,
        target_dates: list[str] | None = None,
        format_filter: list[str] | None = None,
        max_days: int = 0,
    ) -> AvailabilityResult:
        self._city = city
        self._warm_up()

        # Discover all dimension variants (IMAX, 4DX, etc.)
        dimensions = self.discover_dimensions(movie_code, city)

        # Filter dimensions by user's format preferences
        if format_filter:
            filter_lower = [f.lower() for f in format_filter]
            filtered = {
                dim: code
                for dim, code in dimensions.items()
                if any(f in dim.lower() for f in filter_lower)
            }
            if filtered:
                dimensions = filtered
            else:
                logger.warning(
                    "No dimensions match format filter %s — fetching all",
                    format_filter,
                )

        all_codes = list(dimensions.values())
        logger.info("Checking %d event code(s): %s", len(all_codes), list(dimensions.keys()))

        if not target_dates:
            today = datetime.now().strftime("%Y%m%d")
            target_dates = [today]
            try:
                initial = self._fetch_showtimes(all_codes[0], region_code, today)
                available = self._get_available_dates(initial)
                if available:
                    target_dates = [d["DateCode"] for d in available]
            except ScraperError:
                pass

        # Limit number of dates if max_days is set
        if max_days > 0 and len(target_dates) > max_days:
            target_dates = target_dates[:max_days]
            logger.info("Limited to %d day(s)", max_days)

        all_showtimes: list[Showtime] = []
        call_count = 0
        for code in all_codes:
            for date_code in target_dates:
                # Throttle: small delay between calls to avoid 429 rate limits
                if call_count > 0:
                    time.sleep(1.0 + random.uniform(0, 0.5))
                call_count += 1
                try:
                    data = self._fetch_showtimes(code, region_code, date_code)
                    showtimes = self._parse_showtimes(data, code, city)
                    all_showtimes.extend(showtimes)
                except ScraperError as e:
                    logger.warning("Failed to fetch %s date %s: %s", code, date_code, e)

        return AvailabilityResult(
            movie_name="",
            movie_code=movie_code,
            city=city,
            showtimes=all_showtimes,
            scraped_at=datetime.now().isoformat(),
            scraper_used="http",
        )

    def _fetch_showtimes(
        self, movie_code: str, region_code: str, date_code: str
    ) -> dict:
        """Fetch raw showtimes JSON from the BMS API."""
        bms_id = self._session.cookies.get("bmsId", "1.0.0")
        params = {
            "appCode": _DEFAULT_APP_CODE,
            "appVersion": _DEFAULT_APP_VERSION,
            "language": "en",
            "eventCode": movie_code,
            "regionCode": region_code,
            "subRegion": region_code,
            "bmsId": bms_id,
            "token": _DEFAULT_TOKEN,
            "date": date_code,
        }

        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = self._session.get(
                    _SHOWTIMES_API,
                    params=params,
                    timeout=self.config.request_timeout,
                )

                if resp.status_code == 403:
                    logger.warning(
                        "Got 403, refreshing session (attempt %d)", attempt
                    )
                    self._refresh_session()
                    self._warm_up()
                    bms_id = self._session.cookies.get("bmsId", "1.0.0")
                    params["bmsId"] = bms_id
                    continue

                if resp.status_code == 429:
                    wait = min(2**attempt + random.uniform(0, 1), 30)
                    logger.warning(
                        "Rate limited (429), waiting %.1fs (attempt %d)",
                        wait,
                        attempt,
                    )
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                return resp.json()

            except RequestException as e:
                last_error = e
                if attempt < self.config.max_retries:
                    wait = 2**attempt + random.uniform(0, 1)
                    logger.warning(
                        "Request failed (attempt %d/%d): %s — retrying in %.1fs",
                        attempt,
                        self.config.max_retries,
                        e,
                        wait,
                    )
                    time.sleep(wait)

        raise ScraperError(
            f"Failed to fetch showtimes after {self.config.max_retries} "
            f"attempts: {last_error}"
        )

    def _parse_showtimes(
        self, data: dict, movie_code: str, city: str
    ) -> list[Showtime]:
        """Parse the BMS showtimes JSON into Showtime objects."""
        showtimes: list[Showtime] = []

        for show_detail in data.get("ShowDetails", []):
            # Build a lookup for child event metadata (language, dimension)
            event_data = show_detail.get("Event", {})
            child_events = {
                ce["EventCode"]: ce
                for ce in event_data.get("ChildEvents", [])
                if "EventCode" in ce
            }

            for venue in show_detail.get("Venues", []):
                cinema = Cinema(
                    name=venue.get("VenueName", ""),
                    code=venue.get("VenueCode", ""),
                    address=venue.get("VenueAdd", ""),
                    lat=float(venue.get("Lat") or 0),
                    lng=float(venue.get("Lng") or 0),
                )

                for st in venue.get("ShowTimes", []):
                    avail_code = st.get("Availability", "N")
                    status = _AVAILABILITY_MAP.get(
                        avail_code, AvailabilityStatus.SOLD_OUT
                    )

                    show_date = self._parse_date(st.get("ShowDateCode", ""))
                    show_time = self._parse_time(st.get("ShowTime", ""))
                    if show_date is None or show_time is None:
                        continue

                    price_min = st.get("MinPrice", "")
                    price_max = st.get("MaxPrice", "")
                    price_range = (
                        f"₹{price_min}-{price_max}"
                        if price_min and price_max
                        else ""
                    )

                    event_code = st.get("EventCode", movie_code)
                    ce = child_events.get(event_code, {})
                    language = ce.get("EventLang", "")
                    dimension = ce.get("EventDimension", "")
                    attributes = st.get("Attributes", "")
                    # Avoid duplication like "IMAX 2D IMAX"
                    if attributes and attributes.lower() in dimension.lower():
                        fmt = dimension
                    else:
                        fmt = " ".join(filter(None, [dimension, attributes])).strip()

                    booking_url = (
                        f"https://in.bookmyshow.com/buytickets/"
                        f"{venue.get('VenueCode', '')}/movie-"
                        f"{city}-{event_code}-MT/"
                        f"{st.get('ShowDateCode', '')}"
                    )

                    showtimes.append(
                        Showtime(
                            cinema=cinema,
                            show_date=show_date,
                            show_time=show_time,
                            format=fmt or "2D",
                            language=language,
                            status=status,
                            booking_url=booking_url,
                            price_range=price_range,
                        )
                    )

        return showtimes

    def health_check(self, city: str) -> bool:
        """Fetch the movies listing page and verify we can extract data."""
        try:
            url = _MOVIES_PAGE.format(city=city)
            resp = self._session.get(url, timeout=self.config.request_timeout)
            resp.raise_for_status()

            match = re.search(
                r"window\.__INITIAL_STATE__\s*=\s*({.+?})\s*;?\s*</script>",
                resp.text,
                re.DOTALL,
            )
            if not match:
                logger.warning("Health check: __INITIAL_STATE__ not found")
                return False

            state = json.loads(match.group(1))
            listings = (
                state.get("explore", {}).get("movies", {}).get("listings", [])
            )
            card_count = sum(
                len(listing.get("cards", [])) for listing in listings
            )
            if card_count > 0:
                logger.debug("Health check passed: %d movies found", card_count)
                return True

            logger.warning("Health check: no movie cards found")
            return False

        except Exception as e:
            logger.warning("Health check failed: %s", e)
            return False

    def search_movie(self, query: str, city: str) -> list[dict]:
        """Search for a movie by name on the BMS listings page.

        Returns a list of matches: [{"name": ..., "code": ..., "languages": ...}]
        sorted by relevance (best match first).
        """
        self._city = city
        self._warm_up()
        url = _MOVIES_PAGE.format(city=city)

        try:
            resp = self._session.get(url, timeout=self.config.request_timeout)
            resp.raise_for_status()
        except Exception as e:
            raise ScraperError(f"Failed to fetch movie listings: {e}")

        match = re.search(
            r"window\.__INITIAL_STATE__\s*=\s*({.+?})\s*;?\s*</script>",
            resp.text,
            re.DOTALL,
        )
        if not match:
            raise ScraperError("Could not parse movie listings page")

        state = json.loads(match.group(1))
        listings = state.get("explore", {}).get("movies", {}).get("listings", [])

        query_lower = query.lower()
        query_words = set(query_lower.split())
        results = []

        for listing in listings:
            for card in listing.get("cards", []):
                analytics = card.get("analytics", {})
                text_parts = [
                    c.get("text", "")
                    for t in card.get("text", [])
                    for c in t.get("components", [])
                ]
                name = text_parts[0] if text_parts else ""
                if not name:
                    continue

                name_lower = name.lower()
                name_words = set(name_lower.split())

                # Score: exact substring > word overlap > partial
                if query_lower == name_lower:
                    score = 100
                elif query_lower in name_lower:
                    score = 80
                elif query_words & name_words:
                    overlap = len(query_words & name_words) / len(query_words)
                    score = int(60 * overlap)
                else:
                    continue

                results.append({
                    "name": name,
                    "code": analytics.get("event_code", ""),
                    "languages": text_parts[2] if len(text_parts) > 2 else "",
                    "score": score,
                })

        results.sort(key=lambda r: r["score"], reverse=True)
        return results

    def close(self) -> None:
        self._session.close()

    @staticmethod
    def _parse_date(date_code: str) -> date | None:
        try:
            return datetime.strptime(date_code, "%Y%m%d").date()
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_time(time_str: str) -> dt_time | None:
        if not time_str:
            return None
        for fmt in ("%I:%M %p", "%H%M", "%H:%M"):
            try:
                return datetime.strptime(time_str.strip(), fmt).time()
            except ValueError:
                continue
        return None
