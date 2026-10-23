# Discord Events Bot — Design Spec

**Date**: 2026-10-23
**Status**: Approved
**Source brief**: User-provided "Discord Events Bot — Final Project Brief (This Week / This Month)"

---

## 1. Overview

A Python Discord bot for the Hightide Tabletop LGS server that:

1. Pulls upcoming events from `hightidetabletop.com.au` via the **WordPress REST API** (not HTML scraping — see §2)
2. Maintains a single unified event dataset in SQLite
3. Publishes two persistent board messages:
   - `#this-week` — rolling 7-day view, edited in place
   - `#this-month` — current calendar month view, edited in place, recreated at month rollover
4. Refreshes both boards daily at 09:00 Australia/Perth
5. Self-assignable interest roles: **deferred to v2** (user decision)

Single-guild, self-hosted deployment (user decision).

---

## 2. Data Source (key deviation from brief)

The store runs **WooCommerce on WordPress**. The public WP REST API is enabled and returns events as structured JSON — verified against the live site on 2026-10-23.

- Endpoint: `GET /wp-json/wp/v2/product?product_cat={events_cat_id}&per_page=100&_embed=1&page=N`
- `product_cat` ID resolved at runtime via `GET /wp-json/wp/v2/product_cat?slug=events`
- `_embed=1` inlines featured media (images) and category terms in the same request
- Rich event details (time, ticket price, structure) live in `content.rendered` as HTML
- Observed live date formats in titles: `DD.MM.YY` prefix, `DD.MM.YYYY` prefix, natural-language suffix ("Friday October 23rd 2026")
- Observed live detail format: `→ Time: 6:30pm`, `→ Ticket: $10` inside content paragraphs

**Consequences:**

- `httpx` + `tenacity` remain (HTTP + retry)
- `beautifulsoup4` retained for HTML→text of descriptions only; `lxml` dropped
- No pagination-by-HTML; use `X-WP-TotalPages` header
- The brief's `/shop/` source URL is replaced by the Events category (`/product-category/events/`) — scraping `/shop/` would ingest pre-orders and merch

## 3. Architecture

```text
src/
  bot.py                    # entry point, intents, command sync
  config.py                 # env vars, constants

  db/
    models.py               # SQLAlchemy models
    session.py              # engine + session factory
    repositories.py         # event/board/config queries

  scraping/
    fetcher.py              # WP REST client (httpx + tenacity)
    hightide_parser.py      # JSON → NormalizedEvent
    normalize.py            # date/time/price parsing, fingerprints

  services/
    board_service.py        # builds week/month board payloads
    scheduler.py            # daily refresh + month rollover loop
    event_service.py        # upsert/dedupe/fingerprints/stale detection

  discord_ui/
    commands_admin.py       # /config, /source, /board, admin-only
    commands_user.py        # /events upcoming (ephemeral)
    embeds.py               # board + preview embeds

tests/
  test_parser.py
  test_normalize.py
  test_board_render.py
  test_event_dedupe.py
  fixtures/
    wp_products_events.json # captured real API payload

docs/
  superpowers/specs/        # this spec
ROADMAP.md                  # milestone working memory
```

Deferred to v2: `services/role_service.py`, `discord_ui/role_views.py`, `interest_roles` table, all `/roles` commands.

## 4. Data Model

SQLite via SQLAlchemy. All datetimes stored timezone-aware (UTC internally, rendered in configured timezone).

### `guild_config`
- `guild_id` (PK)
- `timezone` (default `Australia/Perth`)
- `this_week_channel_id` (nullable)
- `this_month_channel_id` (nullable)
- `daily_refresh_time` (default `09:00`)
- `week_start_mode` (`now` | `today_start`; default `today_start`)
- `include_tba_section` (bool, default true)
- `delete_previous_month_message` (bool, default true)

### `event_sources`
- `id` (PK), `guild_id`, `url`, `enabled` (bool)
- Default row seeded: the Events category base URL

### `events`
- `id` (PK), `guild_id`
- `external_id` (unique per guild) — `wp:{post_id}`
- `source_url`, `title`, `url`
- `starts_at` (nullable, tz-aware)
- `time_known` (bool, default false) — distinguishes real times from date-only events (**schema addition vs brief**)
- `price_text` (nullable), `summary` (nullable), `image_url` (nullable)
- `fingerprint` — sha1(title | starts_at | price_text | summary)
- `first_seen_at`, `last_seen_at`, `missed_refreshes` (int), `is_active` (bool)

### `board_posts`
- `id` (PK), `guild_id`
- `board_type` (`this_week` | `this_month`)
- `period_key` — week: ISO `YYYY-Www`; month: `YYYY-MM`
- `channel_id`, `message_id`, `render_hash`, `updated_at`

## 5. Normalization Pipeline

1. Fetch all pages of Events-category products
2. For each product:
   - `external_id = wp:{id}`
   - Title cleaned of HTML entities (`&#8211;` → `–`)
   - Date strategies, in order:
     a. Title prefix `DD.MM.YY` or `DD.MM.YYYY`
     b. dateutil parse of trailing natural date in title
     c. `starts_at = None` (TBA)
   - Time: regex `→\s*Time:\s*(.+)` on content → dateutil time parse → combine with date; sets `time_known = true`
   - Price: regex `→\s*Ticket:\s*(.+)`; detect "free"; else null
   - Summary: first non-arrow paragraph of content, stripped to plain text, truncated
   - Image: `_embedded['wp:featuredmedia'][0].source_url`
3. Compute `fingerprint`; upsert by `(guild_id, external_id)`
4. Stale handling: increment `missed_refreshes` for absent actives; mark `is_active = false` after 2 misses

## 6. Board Rendering

- Group by local (AWST) date, ascending; events sorted by datetime within a group
- Per-event line: `HH:MM` (if `time_known`) · [Title](url) · price
- Date-only events show "time TBC"
- `Date TBA` section last, only when `include_tba_section = true`
- Empty state: "No events found for this period."
- Footer: `Last updated <HH:MM> AWST`
- Windows:
  - **this_week**: `start = today 00:00 local` (default; `now` when `week_start_mode = now`), `end = start + 7 days` exclusive
  - **this_month**: `[first of month 00:00, first of next month 00:00)` local
- Output hashed → board edit skipped when unchanged

## 7. Update Logic

- **Scheduler**: `discord.ext.tasks` 60-second loop; triggers when local time crosses configured daily refresh time
- **Daily refresh**: fetch → normalize → upsert → recompute both boards → edit in place if hash changed
- **Month rollover**: when current month `period_key` differs from stored, create new message in `#this-month`, store new `message_id`, delete previous month's message when `delete_previous_month_message = true`
- **Recovery**: if stored `message_id` no longer exists (manually deleted), recreate the board message on next refresh
- **Idempotency**: all posting keyed off `board_posts` rows; safe to re-run

## 8. Commands

### Admin (default member permission: Manage Guild)
- `/config this_week_channel <#channel>`
- `/config this_month_channel <#channel>`
- `/config timezone <IANA tz>`
- `/config daily_refresh_time <HH:MM>`
- `/config include_tba <bool>`
- `/source add url:<...>` / `/source list` / `/source remove <id>`
- `/board preview this_week|this_month` (ephemeral)
- `/board refresh` (force fetch + update now)
- `/board recreate this_week|this_month`

### User
- `/events upcoming` (ephemeral, next 7 days)

## 9. Reliability

- HTTP: 15s timeout, tenacity exponential backoff (3 attempts), polite User-Agent identifying the bot
- Structured `logging` for fetch/parse/post failures; admin refresh command reports summary
- Admin commands gated by Manage Guild
- Discord rate limits: edits only on hash change; single message per board

## 10. Discord Permissions

- View Channels, Send Messages, Embed Links, Manage Messages (recommended)
- (v2 roles) Manage Roles

## 11. Deployment

- Railway/Render worker; SQLite on a persistent volume
- Env vars: `DISCORD_TOKEN`, `DATABASE_URL` (defaults to local sqlite file), `DEFAULT_TIMEZONE=Australia/Perth`
- Optional: `GUILD_ID` for fast dev command sync

## 12. Testing

- `pytest`, run from repo root
- Fixtures: real captured API payload (`tests/fixtures/wp_products_events.json`)
- Coverage targets:
  - Parser: all three observed live date formats, TBA fallback, time/price extraction, entity cleanup
  - Dedupe: upsert by external_id, fingerprint change detection, stale deactivation
  - Board windows: today_start vs now, month boundaries, TBA section, empty state
  - Board hashing: identical render → no edit

## 13. Milestones (execution order)

1. **Foundation** — project scaffold, bot online, DB + models, `/config` and `/source` commands
2. **Scraper** — WP REST fetcher, parser, normalization, `/board preview`
3. **Board system** — `board_posts` tracking, create/edit logic, `/board refresh|recreate`, daily scheduled refresh
4. **Month rollover** — new monthly post on period change, previous month deletion
5. **Hardening** — recovery from manual deletion, logging polish, full test pass, deploy notes

Each milestone: implement → focused tests → 4-agent review council → fixes → ROADMAP.md update → commit. The next milestone does not start until all of that is done.

## 14. Definition of Done

- `#this-week` board updates daily with next 7 days of events
- `#this-month` board covers the current calendar month and refreshes daily
- New monthly post created at rollover; previous monthly post removed
- No duplicate spam posts (edit-in-place + hash comparison)
- Bot recovers automatically if a board message is manually deleted
- Test suite green

## 15. Explicitly Out of Scope (v1)

- Self-assignable interest roles and `/roles` commands (v2)
- Multi-guild distribution
- HTML scraping fallback (can be added if the WP API is ever disabled)
- Alembic migrations (v1 schema is created fresh; SQLite single deployment)
