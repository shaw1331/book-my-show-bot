"""BookMyShow Availability Checker Bot.

Continuously monitors BookMyShow for movie ticket availability
and sends notifications when tickets become available.

Usage:
    python main.py                    # Run continuous polling
    python main.py --check-once       # Single check and exit
    python main.py --test-notify      # Send test notification and exit
    python main.py -c my_config.yaml  # Use custom config file
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys

from bot.chat_store import ChatStore
from bot.preference_store import PreferenceStore
from bot.telegram_commands import TelegramCommandBot
from checker.engine import AvailabilityChecker
from config.loader import load_config
from config.schema import AppConfig, NotificationChannelConfig, ScraperConfig
from notifier.base import BaseNotifier
from notifier.ntfy import NtfyNotifier
from notifier.telegram import TelegramNotifier
from scraper.base import BaseScraper
from scraper.http_scraper import HttpScraper


def create_scraper(config: ScraperConfig, city: str = "") -> BaseScraper:
    """Factory: create a scraper based on the config strategy."""
    if config.strategy == "http":
        return HttpScraper(config, city=city)
    # Phase 2: "playwright" and "auto" strategies
    raise ValueError(f"Unknown scraper strategy: {config.strategy!r}")


def create_notifiers(
    channels: list[NotificationChannelConfig],
    chat_store: ChatStore | None = None,
) -> list[BaseNotifier]:
    """Factory: create notifiers for all enabled channels."""
    notifiers: list[BaseNotifier] = []
    for ch in channels:
        if not ch.enabled:
            continue
        if ch.type == "ntfy":
            notifiers.append(
                NtfyNotifier(topic=ch.topic, server=ch.server, priority=ch.priority)
            )
        elif ch.type == "telegram":
            if not chat_store:
                logging.warning("Telegram notifier requires ChatStore — skipping")
                continue
            notifiers.append(
                TelegramNotifier(bot_token=ch.bot_token_env, chat_store=chat_store)
            )
        else:
            logging.warning("Unknown notification channel type: %s", ch.type)
    return notifiers


def setup_logging(config: AppConfig) -> None:
    """Configure logging from the app config."""
    log_cfg = config.logging
    level = getattr(logging, log_cfg.level.upper(), logging.INFO)

    fmt = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_cfg.file:
        file_handler = logging.handlers.RotatingFileHandler(
            log_cfg.file,
            maxBytes=log_cfg.max_file_size_mb * 1024 * 1024,
            backupCount=3,
        )
        handlers.append(file_handler)

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BookMyShow Availability Checker Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py                    # Continuous monitoring\n"
            "  python main.py --check-once       # Single check\n"
            "  python main.py --test-notify      # Test notifications\n"
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--test-notify",
        action="store_true",
        help="Send a test notification and exit",
    )
    parser.add_argument(
        "--check-once",
        action="store_true",
        help="Run a single check cycle and exit",
    )
    parser.add_argument(
        "--search",
        type=str,
        metavar="MOVIE_NAME",
        help="Search for a movie by name and show matching event codes",
    )
    args = parser.parse_args()

    # Load and validate config
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config)
    logger = logging.getLogger(__name__)

    # Create scraper early (needed for search and auto-resolve)
    scraper = create_scraper(config.scraper, city=config.location.city)

    # Search mode
    if args.search:
        city = config.location.city
        logger.info("Searching for '%s' in %s...", args.search, city)
        if isinstance(scraper, HttpScraper):
            results = scraper.search_movie(args.search, city)
            if results:
                for r in results:
                    print(f"  {r['code']}  {r['name']}  ({r['languages']})")
            else:
                print(f"No movies matching '{args.search}' found in {city}")
        return

    # Auto-resolve movie name to code if code is not set
    if not config.movie.code or config.movie.code.startswith("ET00XXXX"):
        if config.movie.name and isinstance(scraper, HttpScraper):
            logger.info(
                "No event code set — searching for '%s'...", config.movie.name
            )
            results = scraper.search_movie(
                config.movie.name, config.location.city
            )
            if results:
                best = results[0]
                config.movie.code = best["code"]
                logger.info(
                    "Auto-resolved to: %s (%s)", best["name"], best["code"]
                )
            else:
                logger.error(
                    "Could not find movie '%s'. Use --search to find the "
                    "correct name, or set movie.code in config.yaml",
                    config.movie.name,
                )
                sys.exit(1)
        else:
            logger.error("Config error: movie.code or movie.name is required")
            sys.exit(1)
    # Create persistent stores
    chat_store = ChatStore()
    prefs = PreferenceStore()
    prefs.seed_from_config(config)
    telegram_config = None
    for ch in config.notifications.channels:
        if ch.type == "telegram" and ch.enabled and ch.bot_token_env:
            telegram_config = ch
            # Seed existing chat_id so current users don't need to re-auth
            if ch.chat_id_env and not chat_store.is_active(ch.chat_id_env):
                chat_store.add(ch.chat_id_env)
                logger.info("Seeded chat %s from config", ch.chat_id_env)
            break

    notifiers = create_notifiers(config.notifications.channels, chat_store=chat_store)

    if not notifiers:
        logger.warning(
            "No notification channels enabled! "
            "Results will only appear in logs."
        )

    # Test notifications mode
    if args.test_notify:
        if not notifiers:
            logger.error("No notification channels to test")
            sys.exit(1)
        for n in notifiers:
            logger.info("Testing %s...", n.channel_name)
            if n.test():
                logger.info("  ✓ %s working", n.channel_name)
            else:
                logger.error("  ✗ %s failed", n.channel_name)
        return

    # Create checker
    checker = AvailabilityChecker(config, scraper, notifiers, prefs=prefs)

    if args.check_once:
        result = checker.check_once()
        if result and result.showtimes:
            logger.info("Found %d total showtime(s)", len(result.showtimes))
        elif result:
            logger.info("No showtimes found")
        else:
            logger.error("Check failed")
            sys.exit(1)
        return

    # Start Telegram command bot if telegram is configured
    cmd_bot = None
    if telegram_config:
        auth_password = telegram_config.auth_password_env
        if not auth_password:
            logger.warning("No BOT_AUTH_PASSWORD set — users cannot /start")
        cmd_bot = TelegramCommandBot(
            bot_token=telegram_config.bot_token_env,
            chat_store=chat_store,
            auth_password=auth_password,
            config=config,
            prefs=prefs,
            on_check_now=checker.check_once,
            on_list_shows=checker.list_shows,
            on_fetch_raw=checker.fetch_raw_shows,
        )
        cmd_bot.start()
        logger.info(
            "Telegram command bot active — send /start <password> to connect"
        )

    try:
        checker.run()
    finally:
        if cmd_bot:
            cmd_bot.stop()


if __name__ == "__main__":
    main()
