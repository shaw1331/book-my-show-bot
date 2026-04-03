# BookMyShow Availability Checker Bot

Continuously monitors BookMyShow for movie ticket availability and sends instant notifications via Telegram when tickets become available.

## Features

- **Auto movie search** — just provide the movie name, no need to find event codes
- **Telegram bot interface** — manage all preferences by chatting with your bot
- **Distance-based sorting** — shows nearest cinemas first based on your location
- **Fuzzy filtering** — filter by cinema name, screening format, and language
- **Cloudflare bypass** — uses `curl_cffi` for TLS fingerprint impersonation
- **Health monitoring** — detects when the scraper breaks vs. movie genuinely unavailable
- **Dedup notifications** — won't spam you for the same showtime twice
- **Exponential backoff** — backs off on errors, respects quiet hours

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Create a Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts to create your bot
3. Copy the **API token** (e.g. `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)
4. Open your new bot in Telegram and send it any message (e.g. "hi")
5. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
6. Find your `chat_id` in the JSON response under `"chat": {"id": 123456789}`

### 3. Configure environment

Create a `.env` file in the project root:

```
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

### 4. Configure preferences

Edit `config.yaml`:

```yaml
movie:
  name: "Project Hail Mary"    # Movie name (auto-resolves to event code)
  languages: ["English"]       # Filter by language
  formats: ["IMAX"]            # Filter by format (fuzzy match)

location:
  city: "bengaluru"            # BMS city slug
  region_code: "BANG"          # BMS region code
  lat: 12.9698                 # Your latitude (for distance sorting)
  lng: 77.7338                 # Your longitude
  max_distance_km: 0           # 0 = no limit

checker:
  interval_seconds: 300        # Check every 5 minutes
```

### 5. Run

```bash
# Test notifications first
python main.py --test-notify

# Run continuous monitoring
python main.py

# Single check (for cron)
python main.py --check-once

# Search for a movie
python main.py --search "Avengers"
```

## Telegram Bot Commands

Once the bot is running, manage preferences by chatting with your Telegram bot:

| Command | Description |
|---|---|
| `/status` | Show current settings |
| `/movie NAME` | Change the tracked movie |
| `/cinemas` | List preferred cinemas |
| `/addcinema NAME` | Add a cinema (fuzzy match) |
| `/removecinema N` | Remove cinema by number |
| `/clearcinemas` | Watch all cinemas |
| `/formats` | List preferred formats |
| `/addformat FMT` | Add a format (e.g. "IMAX 2D") |
| `/removeformat N` | Remove format by number |
| `/languages` | List preferred languages |
| `/addlang LANG` | Add a language |
| `/removelang N` | Remove language by number |
| `/check` | Trigger an immediate check |
| `/help` | Show all commands |

## How It Works

```
main.py (entry point)
  |
  ├── Telegram Command Bot (background thread)
  |     Listens for /commands, updates config in memory
  |
  └── Availability Checker (main loop)
        |
        ├── Scraper (curl_cffi + BMS JSON API)
        |     1. Warms up session (gets Cloudflare cookies)
        |     2. Hits /api/movies-data/showtimes-by-event
        |     3. Parses venues, showtimes, seat availability
        |
        ├── Filters (cinemas, formats, languages, distance)
        |
        ├── Dedup State (skips already-notified showtimes)
        |
        └── Notifiers (Telegram, ntfy.sh)
              Sends alert with cinema, time, price, distance
```

### Scraper Architecture

The scraper is behind an abstract interface (`BaseScraper`). When BookMyShow changes their site, only `scraper/http_scraper.py` needs updating — the rest of the bot is untouched.

**Health checks** run periodically to distinguish "movie has no showtimes" from "scraper is broken". If the scraper degrades (>50% failure rate), you get a Telegram alert.

## Project Structure

```
book-my-show-bot/
├── main.py                 # Entry point, CLI args, factories
├── config.yaml             # User preferences (committed, no secrets)
├── .env                    # Secrets - TELEGRAM_BOT_TOKEN, CHAT_ID (gitignored)
├── requirements.txt
├── config/
│   ├── schema.py           # Pydantic config validation
│   └── loader.py           # YAML + .env loading
├── models/
│   └── movie.py            # Cinema, Showtime, AvailabilityResult
├── scraper/
│   ├── base.py             # Abstract BaseScraper interface
│   ├── http_scraper.py     # curl_cffi + BMS JSON API
│   └── health.py           # Sliding window health monitor
├── notifier/
│   ├── base.py             # Abstract BaseNotifier interface
│   ├── ntfy.py             # ntfy.sh push notifications
│   └── telegram.py         # Telegram Bot API
├── checker/
│   ├── engine.py           # Polling loop + orchestration
│   └── state.py            # Notification dedup tracker
└── bot/
    └── telegram_commands.py # Telegram command handler
```

## Finding City and Region Codes

City slugs and region codes are in the BMS URL. For example:
- `in.bookmyshow.com/explore/movies-bengaluru` → city: `bengaluru`, region: `BANG`
- `in.bookmyshow.com/explore/movies-mumbai` → city: `mumbai`, region: `MUMBAI`
- `in.bookmyshow.com/explore/movies-hyderabad` → city: `hyderabad`, region: `HYD`

You can also search movies to find the event code:
```bash
python main.py --search "Movie Name"
```

## Notifications

### Telegram (recommended)
Configured via `.env`. Sends rich HTML messages with cinema name, time, price, distance, and a "Book Now" link.

### ntfy.sh (zero-setup alternative)
No signup needed. Set a topic in `config.yaml`, install the [ntfy app](https://ntfy.sh) on your phone, and subscribe to that topic.

## Troubleshooting

**Bot not responding to Telegram commands?**
- Make sure only ONE instance of `main.py` is running. Multiple instances compete for Telegram updates.

**Getting 403 errors?**
- Cloudflare is blocking the request. The bot auto-refreshes its session, but if persistent, try restarting.

**No showtimes found for a movie you know is showing?**
- Check the event code: `python main.py --search "Movie Name"`
- The movie may not be listed in your city yet.
