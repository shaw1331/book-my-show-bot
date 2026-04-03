from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, time
from enum import Enum


class AvailabilityStatus(Enum):
    AVAILABLE = "available"
    FILLING_FAST = "filling_fast"
    SOLD_OUT = "sold_out"
    NOT_YET_OPEN = "not_yet_open"


@dataclass(frozen=True)
class Cinema:
    name: str
    code: str
    address: str = ""
    lat: float = 0.0
    lng: float = 0.0


@dataclass(frozen=True)
class Showtime:
    cinema: Cinema
    show_date: date
    show_time: time
    format: str  # "2D", "IMAX", "3D", etc.
    language: str
    status: AvailabilityStatus
    booking_url: str = ""
    price_range: str = ""


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate distance in km between two lat/lng points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@dataclass
class AvailabilityResult:
    movie_name: str
    movie_code: str
    city: str
    showtimes: list[Showtime] = field(default_factory=list)
    scraped_at: str = ""
    scraper_used: str = ""
    is_healthy: bool = True
