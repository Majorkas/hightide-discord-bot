# Hightide Discord Events Bot

A Discord bot that pulls upcoming tabletop gaming events from Hightide Tabletop's WordPress store and maintains two persistent event boards: a current-week view (Mon–Sun, starting from today) and a current-month calendar view.

## Features

- **Automated event scraping**: Fetches events from WordPress REST API daily at 09:00 Australia/Perth
- **Two persistent boards**: 
  - `#this-week` — the current week (Mon–Sun), starting from today
  - `#this-month` — current calendar month view (auto-rotates at month boundaries)
- **Smart event management**: Deduplication by fingerprint, stale-event detection, automatic TBA fallback for missing dates
- **Recurring weekly events**: A "Every week at Hightide" section on the this-week board lists door-ticket-only recurring events (with optional end dates and an image), managed via `/config weekly_*`
- **User commands**: `/events upcoming` shows all events from today onward, plus date-TBA events, in an ephemeral message
- **Admin control**: Configure channels, timezone, refresh time, and event sources per server

## Setup

### Requirements
- **Python 3.11+** (this repo uses Python 3.12)

### Installation

1. **Clone the repository** and navigate to the repo root:
   ```bash
   git clone https://github.com/yourusername/hightide-discord-bot.git
   cd hightide-discord-bot
   ```

2. **Create a virtual environment** (choose one):

   Using `uv` (recommended for this repo):
   ```bash
   uv venv --python 3.12 .venv
   uv pip install --python .venv/bin/python -e '.[dev]'
   source .venv/bin/activate
   ```

   Using standard `venv` and `pip`:
   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -e '.[dev]'
   ```

3. **Set environment variables**:
   ```bash
   cp .env.example .env
   # Edit .env with your values (see Environment Variables below)
   ```

4. **Initialize the database**:
   The database is automatically created on first run.

## Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `DISCORD_TOKEN` | **Required.** Bot token from Discord Developer Portal | `your-bot-token` |
| `DATABASE_URL` | SQLite database path | `sqlite:///./hightide.db` |
| `DEFAULT_TIMEZONE` | IANA timezone name (server default) | `Australia/Perth` |
| `GUILD_ID` | Optional Discord server ID (instant command sync in that guild) | `123456789` |

Never commit `.env`; if the bot token ever leaks, rotate it in the Discord Developer Portal.

Setting `GUILD_ID` makes command sync instant in that guild; when unset, commands sync globally and may take up to an hour to appear.

## Running the Bot

Activate the virtual environment, then run:

```bash
python -m src.bot
```

Or with the full path:

```bash
.venv/bin/python -m src.bot
```

The bot connects to Discord and registers all commands. Check your server to see the `/config`, `/source`, `/board`, and `/events` command groups.

## Testing

From the repo root, with the virtual environment activated:

```bash
python -m pytest -v
```

Run a specific test file:
```bash
python -m pytest tests/test_board_service.py -v
```

Run a single test:
```bash
python -m pytest tests/test_normalize.py::test_parse_dot_prefix_two_digit_year -v
```

## Commands

### Admin Commands
All admin commands require the **Manage Guild** permission.

**`/config this_week_channel <#channel>`**
- Sets the Discord channel where the current-week board (Mon–Sun, starting from today) posts.

**`/config this_month_channel <#channel>`**
- Sets the Discord channel where the monthly board posts.

**`/config timezone <IANA timezone>`**
- Sets the event timezone (e.g., `Australia/Perth`, `America/New_York`).
- Affects board windows and refresh time interpretations.

**`/config daily_refresh_time <HH:MM>`**
- Sets the local time (24-hour format) for daily event refresh.
- Default: `09:00`.

**`/config include_tba <true|false>`**
- Controls whether a "Date TBA" section appears on boards for events without known dates.

**`/config weekly_list`**
- Lists recurring weekly events (including disabled ones) with their IDs.

**`/config weekly_add <title> <days> <time>`**
- Adds a recurring weekly event (title ≤200 chars, days/time ≤40), e.g. `Mon & Thu` / `6:00 PM`.

**`/config weekly_remove <row_id>`**
- Removes a recurring weekly event by ID.

**`/config weekly_image <url>`**
- Sets the http(s) image shown on the weekly section.

*Weekly changes show on the next refresh, or run `/board refresh` to apply them now.*

**`/source add url:<URL>`**
- Adds an event source URL to the sources table for future use.

**`/source list`**
- Lists all configured event sources and their IDs.

**`/source remove <id>`**
- Removes an event source by ID.

*Note: v1 fetches the built-in Hightide Events category only; custom sources are not yet consumed.*

**`/board preview <this_week|this_month>`**
- Shows a preview of the board for the chosen period (ephemeral, admin only).

**`/board refresh`**
- Force an immediate fetch and board update.

**`/board recreate <this_week|this_month>`**
- Recreates the board message for the chosen period (useful if the message is lost).

### User Commands

**`/events upcoming`**
- Shows all events from today onward, plus date-TBA events, in an ephemeral message.
- No permissions required beyond View Channels and Embed Links.

## Discord Permissions

Add the bot to your server with the following permissions:

- **View Channels** — read board and event channels
- **Send Messages** — post and update board messages
- **Embed Links** — display rich event embeds
- **Manage Messages** — update and delete board messages
- **Read Message History** — fetch stored message content for board updates

**OAuth2 scopes**: `bot` and `applications.commands`

**Invite URL template**:
```
https://discord.com/api/oauth2/authorize?client_id=1563111587500400660&permissions=93184&scope=bot%20applications.commands
```

(Adjust the `permissions` parameter if you enable additional features.)

## Deployment

### Local Deployment
Run the bot on your machine or a local server. The SQLite database is stored locally (default: `hightide.db` in the repo root).

### Deploy: Fly.io

The bot is a worker (no ports, no HTTP). `fly.toml` uses `Dockerfile`, region `syd`, a 256 MB shared-cpu-1x VM and a 1 GB volume at `/data` for SQLite; this fits Fly's free allowance, though Fly requires a card on file.

1. **Install and log in**:
   ```bash
   brew install flyctl
   fly auth signup   # or: fly auth login
   ```

2. **Create the app** (pick your own app name; `hightide-discord-bot` in `fly.toml` is a placeholder). Keep the existing `fly.toml` and answer No to any web-service, Postgres or Redis offers:
   ```bash
   fly launch --no-deploy
   ```
   Afterwards, confirm `fly.toml` still has **no** `[http_service]` section and still has the `[mounts]`, `[[restart]]`, and `[[vm]]` blocks (fly launch can inject an http_service for Dockerfile apps).

3. **Create the volume**:
   ```bash
   fly volumes create hightide_data --size 1 --region syd
   ```

4. **Set secrets** (see Environment Variables above):
   ```bash
   fly secrets set DISCORD_TOKEN=... GUILD_ID=...
   ```
   (Note: `DATABASE_URL` and `DEFAULT_TIMEZONE` are already baked in `fly.toml` as non-secret env vars.)

5. **Deploy**:
   ```bash
   fly deploy --ha=false
   ```
   **Important**: This bot must run exactly **one machine**. Two or more instances would cause the boards to be posted twice per refresh. The `--ha=false` flag ensures a single instance.

6. **First run in Discord**: set `/config this_week_channel` and `/config this_month_channel`, then `/board refresh` (see Commands above).

**Operate**: `fly logs` to tail output, `fly ssh console` for a shell. Redeploys keep the volume, so boards and settings persist.

### Other Platforms (Railway / Render)

Run as a worker (not an HTTP service) with `python -m src.bot`, a persistent volume for the SQLite database, and the environment variables above (e.g. `DATABASE_URL=sqlite:////data/hightide.db`). PostgreSQL requires a driver such as psycopg and is untested.

### Database Persistence
- **SQLite (default)**: Ensure the database file lives on a persistent volume that survives restarts.
- **PostgreSQL** (manual setup): Set `DATABASE_URL=postgresql://user:password@host:port/dbname` and initialize the schema manually.

## Architecture

```
src/
  bot.py                    # Bot entry point, intents, command sync
  config.py                 # Environment variables and defaults

  db/
    models.py               # SQLAlchemy models (events, boards, config)
    session.py              # Database engine and session factory
    repositories.py         # Data access layer

  scraping/
    fetcher.py              # WordPress REST API client
    hightide_parser.py      # WP JSON → NormalizedEvent conversion
    normalize.py            # Date/time/price parsing and fingerprinting

  services/
    board_service.py        # Board window logic and rendering
    event_service.py        # Event upsert, dedup, and stale detection
    scheduler.py            # Daily refresh and month-rollover scheduling

  discord_ui/
    commands_admin.py       # Admin slash commands
    commands_user.py        # User slash commands
    embeds.py               # Embed building

tests/                       # pytest suite with fixtures
```

## Development

- **Testing**: `pytest` with asyncio and SQLite in-memory databases for isolation
- **Code style**: Format with standard Python conventions
- **Logging**: Logging via Python's `logging` module

## Troubleshooting

**Bot doesn't respond to commands:**
- Verify the bot has **View Channels** and **Embed Links** permissions in the server.
- Check that your `DISCORD_TOKEN` is valid.

**Events not updating:**
- Run `/board refresh` to trigger an immediate fetch.
- Check bot logs for HTTP or parsing errors.
- Verify the event source URL is reachable.

**Database errors:**
- Ensure `DATABASE_URL` points to a writable location.
- On Fly.io/Railway/Render, verify the persistent volume is mounted at the correct path and the machine count is set to 1 (multiple instances would cause corruption or duplicate boards).

**Board channel moved via `/config`:**
- The previous board message stays in the old channel (delete it manually if unwanted); it is not reposted as a duplicate.

**Secret management on Fly.io**:
- `fly secrets set` puts values in your shell history. For sensitive tokens, use stdin instead:
  ```bash
  fly secrets import < .env
  ```
  (Verify with `fly secrets --help` for the exact subcommand and format in your flyctl version.)

## License

TBD — this is a private project for Hightide Tabletop LGS.
