# Discord Events Bot v2 — Community Features Design Spec

**Date**: 2026-10-24
**Status**: Approved (design decisions confirmed with owner 2026-10-24)
**Supersedes/extends**: [2026-10-23-discord-events-bot-design.md](2026-10-23-discord-events-bot-design.md) — all v1 constraints and architecture remain in force

---

## 1. Overview

Six community features on top of the v1 events bot:

1. **Interest roles** — self-assignable game-interest roles via a button panel; new matching events ping the role once when first seen.
2. **Daily "tonight's events" post** — morning post listing today's events into the announcements channel.
3. **Event sign-ups** — ✅ button on week-board event cards; live "N going" count; admin list command.
4. **Deck sharing** — `/deck <moxfield|archidekt link>` summary embed; `/card <name>` Scryfall lookup with image + price.
5. **Card wants board** — `/want "Card Name"` posts to a wants channel; members browse/deal directly. Card names link to their Scryfall page when resolvable.
6. **New-product alerts** — watched WP categories (pre-orders/new) post to a new-stock channel, once per product.

## 2. Locked design decisions (from owner)

- Roles: **button panel** in a roles channel; event→role matching via **keyword table** (title contains keyword → role).
- Pings: **once when an event is first seen** (never on edits); role ping in the announcements channel.
- Announcements: **one channel for everything** (role pings + daily tonight post + new-stock alerts each get their own configured channel; defaults may share).
- Sign-ups: **simple count + list**; no caps/waitlists in v2.
- Trades: **wants-only** board ("cards I want to get"); no have/offers matching.
- Deck: **Moxfield + Archidekt + Scryfall `/card` lookup** (image + price).

## 3. Architecture deltas

```
src/
  discord_ui/
    role_views.py          # persistent button views (roles panel, signup button)
    commands_roles.py      # /roles panel admin, /want, /deck, /card
  services/
    role_service.py        # keyword→role matching, first-seen ping logic
    signup_service.py      # signup toggle/count/list
    deck_service.py        # moxfield/archidekt/scryfall clients
    want_service.py        # wants board
    product_service.py     # new-product watch
  db/models.py             # + interest_roles, role_keywords, event_role_pings,
                           #   event_signups, card_wants, product_alerts, guild_config additions
```

Reuse: fetcher's httpx+tenacity patterns for the three external APIs; TZDateTime; guild-scoped repositories; cached session factories; ephemeral-user-command conventions; idempotent DDL additions in `bot.py` startup.

New intents: **none privileged** — buttons use interactions; role assignment needs Manage Roles permission (documented). Role panel buttons are persistent views (`timeout=None`, custom_ids) re-registered at startup.

## 4. Data model additions

### `interest_roles`
- id PK, guild_id, role_id (unique per guild), label, emoji (nullable), sort_order, enabled

### `role_keywords`
- id PK, interest_role_id FK, keyword (e.g. "yu-gi-oh", "gundam") — matched case-insensitively against event titles

### `event_role_pings`
- id PK, guild_id, event_id (FK events), interest_role_id, pinged_at — uniqueness on (event_id, interest_role_id) enforces ping-once

### `event_signups`
- id PK, guild_id, event_id (FK events), user_id, signed_up_at — unique (event_id, user_id)

### `card_wants`
- id PK, guild_id, user_id, card_name, scryfall_uri (nullable — resolved exact-match at add time), note (nullable), created_at, active (bool)
- Auto-expire: active=false after 30 days (checked at list/refresh time; no background job needed v2)

### `product_alerts`
- id PK, guild_id, external_id (`wp:{product_id}`, unique per guild), alerted_at

### `guild_config` additions (idempotent ALTERs)
- `roles_channel_id`, `roles_message_id` (panel), `announce_channel_id`, `new_stock_channel_id`, `new_stock_cat_ids` (comma list, default the pre-orders category id resolved at runtime), `daily_post_time` (default "09:00", reuses refresh cadence), `wants_channel_id`

## 5. Feature specs

### 5.1 Interest roles
- Admin: `/roles setup` → creates the panel message (button per enabled interest role) in the configured roles channel, stores message id; `/roles add <@role> <label> [emoji]`, `/roles remove <id>`, `/roles keyword <role_id> <add|remove> <keyword>`, `/roles list`.
- Member: clicks button → bot adds/removes the role (toggle), ephemeral confirm. Role hierarchy failures (bot role below target) produce a clear ephemeral error.
- Ping flow: in `refresh_events` post-upsert, for each event with `created=True` (first seen), match title against enabled keywords → for each matched role not already in `event_role_pings`, post ONE message in the announcements channel: `@Role — New event: [Title](url) · Fri 23 Oct · 18:30`, with `allowed_mentions=discord.AllowedMentions(roles=[role])` (ONLY that role — the global none() suppression is deliberately relaxed here, scoped to the target role). Record ping rows.

### 5.2 Daily "tonight's events" post
- Scheduler, after the daily refresh succeeds (events step only): if now's date hasn't been posted yet (track per-guild `last_daily_post_key` in memory + a `daily_posts` table row or reuse board_posts pattern — simplest: a small key-value table or a guild_config column `last_daily_post_date`), post to the announcements channel: "Today at Hightide — Fri 23 Oct" + compact list of today's events (reuse embed helpers). Nothing today → no post.
- `/config daily_post_time`, `/config announce_channel`.

### 5.3 Event sign-ups
- Week-board event cards (the per-event messages from v2 board work) each carry a persistent `discord.ui.View` with a ✅ "Going (N)" button, keyed by custom_id `signup:{event_id}`. Buttons attach at card send/edit time; views registered at startup via `bot.add_view` for persistence across restarts (the custom_id parses the event id — stateless).
- Toggle: click → insert/delete `event_signups` row → edit the card's button label count (fetch current count). Ephemeral confirmation ("You're on the list" / "Removed").
- Admin: `/board signups <event select or id>` → ephemeral list of names. Event ids surfaced via `/board preview`-style listing or autocomplete on event title.
- Stale events (deactivated) keep their button but respond "This event has ended" ephemerally.

### 5.4 Deck sharing + card lookup
- `/deck url:<moxfield|archidekt link>`: parse → fetch (Moxfield public API endpoint `/v2/decklists/{id}`; Archidekt `/api/decks/{id}/` JSON) → embed: title, format, commander/cover card image (large), card count, link. 15s timeout + retry via existing fetcher patterns; failures → ephemeral "couldn't read that deck".
- `/card name:<str>`: Scryfall `/cards/named?fuzzy=` → embed with card image (normal size), mana cost, type line, oracle text (trimmed), USD price. Fuzzy miss → ephemeral error with closest match suggestion if Scryfall offers one.
- Polite: Scryfall asks for ≤10 req/s and a UA header — we send one request per command, well within.

### 5.5 Card wants board
- `/want add card:"<name>" [note]`: insert row + update the board. At add time the card name is resolved against Scryfall with **exact** matching (`cards/named?exact=` — never fuzzy: a typo'd want must not silently link to the wrong card); on exact hit the `scryfall_uri` is stored on the row, on miss it's NULL. Board rendering: ONE persistent message (board_posts pattern, board_type="wants") listing active wants **sorted alphabetically by card name** (sellers scan for cards they hold — grouping beats recency for the seller audience), each line `[**Card**]({scryfall_uri}) — @user · note · Oct 23` when resolved (plain `**Card**` when not), edited in place on change. Max line lengths enforced; >25 wants → compact truncation like events boards.
- `/want browse` (any member, ephemeral): renders the same alphabetically-sorted, Scryfall-linked list to the caller on demand — the seller-facing "check who's looking for what" path usable from any channel.
- `/want remove <id>` (own wants only; admins can remove any), `/want list` (own, ephemeral).
- Expiry: wants older than 30 days drop off the board and deactivate.

### 5.6 New-product alerts
- Config: `new_stock_cat_ids` (default: resolve `pre-orders` category slug at runtime; admin can add `events`-adjacent categories), `new_stock_channel_id`.
- Flow: during daily refresh (same tick as events), fetch each watched category's products (reuse fetch_events with category override — fetcher gains a `cat_slug` param or a small generalization); for each product not in `product_alerts`, post `[Title](url) · $price` to the new-stock channel, then record the alert. First-ever run seeds the table WITHOUT posting (no flood of 40 alerts on day one).

## 6. Permissions additions

- v2 adds: **Manage Roles** (interest roles). README invite URL integer updated: 93184 + 268435456 (Manage Roles) = 268528640... (compute precisely in implementation and update README + tests).
- Role hierarchy note in README: the bot's role must sit above the interest roles it assigns.

## 7. Testing

Same discipline as v1: TDD per task, fixture-backed external APIs (capture small JSON fixtures for moxfield/archidekt/scryfall), AsyncMock Discord boundaries, per-milestone 4-agent council, zero-warning pristine suite, ROADMAP updated per milestone.

Coverage targets: keyword matching (case, partial-word safety — "yu" must not match "Yummy"), ping-once enforcement, signup toggle/count/idempotence, button custom_id parsing, wants expiry boundary, first-run product seeding (no flood), deck API failure paths.

## 8. Milestones (execution order)

1. **Roles foundation** — tables, DDL, role service, `/roles` admin commands, button panel, toggle assignment
2. **Event pings** — keyword matching, first-seen ping pipeline wired into refresh, announce channel config
3. **Daily tonight post** — scheduler hook + rendering + config
4. **Sign-ups** — table, persistent buttons on week cards, toggle/count, admin list
5. **Deck + card commands** — moxfield/archidekt/scryfall clients + embeds
6. **Wants board** — `/want` commands + persistent wants message + expiry
7. **New-product alerts** — category watch + first-run seeding + channel config
8. **Hardening** — README/permissions updates, full-suite pass, council + final review, deploy to Fly

## 9. Out of scope (v2)

- Trade "have" side / matching engine (explicitly descoped by owner)
- Signup caps/waitlists
- Card prices in wants board
- Role auto-assignment from signup history
- Multi-guild anything
