from __future__ import annotations

from pydantic import BaseModel, Field


class MovieConfig(BaseModel):
    name: str
    code: str = ""
    url: str = ""
    languages: list[str] = []
    formats: list[str] = []


class LocationConfig(BaseModel):
    city: str
    region_code: str = ""
    preferred_cinemas: list[str] = []
    nearby_cinemas: bool = True
    lat: float = 0.0
    lng: float = 0.0
    max_distance_km: float = 0.0  # 0 = no limit


class QuietHours(BaseModel):
    start: str = "01:00"
    end: str = "06:00"


class DatesConfig(BaseModel):
    target_dates: list[str] = []
    target_days: list[str] = []
    any_date: bool = True
    max_days: int = 0  # 0 = all available dates from BMS


class ScraperConfig(BaseModel):
    strategy: str = "http"
    request_timeout: int = 15
    user_agent: str = ""
    max_retries: int = 3
    headers: dict[str, str] = {}


class CheckerConfig(BaseModel):
    interval_seconds: int = 120
    backoff_multiplier: float = 2.0
    max_interval_seconds: int = 900
    health_check_interval: int = 10
    quiet_hours: QuietHours | None = None


class NotificationChannelConfig(BaseModel):
    type: str
    enabled: bool = True
    # ntfy
    topic: str = ""
    server: str = "https://ntfy.sh"
    priority: str = "high"
    # telegram
    bot_token_env: str = ""
    chat_id_env: str = ""
    auth_password_env: str = ""
    # email
    smtp_server: str = ""
    smtp_port: int = 587
    username_env: str = ""
    password_env: str = ""
    to_address: str = ""


class NotificationsConfig(BaseModel):
    channels: list[NotificationChannelConfig] = []


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = ""
    max_file_size_mb: int = 10


class AppConfig(BaseModel):
    movie: MovieConfig
    location: LocationConfig
    dates: DatesConfig = Field(default_factory=DatesConfig)
    scraper: ScraperConfig = Field(default_factory=ScraperConfig)
    checker: CheckerConfig = Field(default_factory=CheckerConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
