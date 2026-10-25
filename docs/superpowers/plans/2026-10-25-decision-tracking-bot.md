# Decision Tracking Bot Implementation Plan

> **For agentic workers:** Implement task-by-task (subagent-driven-development or executing-plans). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A second Discord bot in the same Fly.io container that monitors specific staff channels, uses AI (OpenAI GPT-4o-mini) to detect decisions in conversation, asks for one-click confirmation, and syncs confirmed decisions to Trello cards.

**Locked decisions (from planning):**
- **Scope:** Admins mark monitored channels with `/decision monitor #channel` (no global watching).
- **Workflow:** Confirm-first — AI flags a candidate, the bot posts a confirmation card with Confirm/Dismiss buttons; a click creates the Trello card.
- **Assignment:** Discord→Trello member map; the decider is auto-assigned on the card when mapped.
- **Build:** Full pipeline in one feature.

**Architecture:** Two `discord.Client` bots in one process (a launcher runs both event loops via `asyncio.gather`). The decision bot needs the **Message Content intent** enabled in *its own* Discord application or it cannot read message text. Shared SQLite file; three new tables (own namespace). Confirmation via persistent buttons (house DynamicItem pattern), not reactions.

**Tech stack:** existing (discord.py 2.x, SQLAlchemy 2.x/SQLite, httpx+tenacity, pytest) + `openai` (or raw httpx to the OpenAI API) + Trello REST (`https://api.trello.com/1`).

**Cost:** ~$0.02/mo OpenAI (GPT-4o-mini), Trello free, no extra hosting.

## Global Constraints

- Reuse house patterns: `_md_escape`/`_safe_url` on all user/scraped content; `allowed_mentions.none()` everywhere; per-guild config via `get_or_create_config`; idempotent DDL in setup; cached module-level session factory; `asyncio.to_thread` for blocking calls; DynamicItem for any persistent button.
- All datetimes timezone-aware UTC (TZDateTime); render in guild tz (default Australia/Perth).
- The decision bot reads message content → `discord.Intents.message_content` is mandatory for it (and only it).
- Trello + OpenAI calls are best-effort with tenacity retry on 5xx/429/transport; never block the bot loop.
- Tests run with `.venv/bin/python -m pytest` from repo root; suite must stay green with zero warnings.

## New environment variables (add to .env.example + fly secrets)

| Var | Purpose |
|---|---|
| `DECISION_BOT_TOKEN` | Discord token for the decision bot application |
| `OPENAI_API_KEY` | OpenAI key for decision detection (GPT-4o-mini) |
| `TRELLO_API_KEY` | Trello API key |
| `TRELLO_API_TOKEN` | Trello member token (read/write) |

## File Structure (new files marked +)

| File | Responsibility |
|---|---|
| `src/db/models.py` | + `Decision`, `TrelloSync`, `DecisionConfig`, `TrelloMemberMap` tables |
| `src/db/session.py` | + idempotent DDL for the new tables |
| `src/config.py` | + the four env vars |
| `src/services/decision_service.py` + | AI detection, candidate → pending decision, confirm/dismiss |
| `src/services/trello_service.py` + | Trello card create + member-map assignment |
| `src/discord_ui/decision_commands.py` + | `/decision` admin group (monitor/unmonitor/config/list/sync/create/map) |
| `src/discord_ui/decision_views.py` + | Confirm/Dismiss DynamicItem buttons |
| `src/decision_bot.py` + | decision bot client (message_content intent, on_message listener) |
| `src/__main__.py` + | launcher: run both bots via asyncio.gather |
| `Dockerfile` | run `python -m src` (the launcher) instead of `python -m src.bot` |

## Database tables

- `decisions`: `id` (autoincrement), `guild_id`, `channel_id`, `message_id`, `user_id` (decider), `content` (raw excerpt, capped), `summary` (AI one-liner), `status` (`pending`/`confirmed`/`rejected`), `created_at`, `confirmed_by`, `confirmed_at`
- `trello_sync`: `id`, `decision_id` (unique), `trello_card_id`, `trello_card_url`, `synced_at`
- `decision_config`: `guild_id` (PK), `enabled` (bool), `trello_board_id`, `trello_list_id`, plus a `monitored_channels` child table (`channel_id` per guild) **or** a JSON column — decide in Task 1 (recommend a child table `decision_channels` for clean querying)
- `trello_member_map`: `guild_id`, `discord_user_id`, `trello_member_id` (unique per guild+user)

## Detection flow

1. `on_message` in `decision_bot` fires for messages in a monitored channel, not from bots, in a guild with `decision_config.enabled`.
2. Pass the message (+ small surrounding context if cheap) to GPT-4o-mini with a strict JSON prompt: `{ "is_decision": bool, "summary": str, "decider_hint": str }`.
3. If `is_decision`, insert a `pending` Decision and post a confirmation card (summary, decider, excerpt, jump link) with Confirm/Dismiss buttons in the same channel.
4. **Confirm** → set status confirmed, record confirmer, create the Trello card (summary, decider Discord name, timestamp, channel name, message jump link; assign mapped Trello member), write `trello_sync`. **Dismiss** → status rejected, edit the card to show dismissed.

## Admin commands (`/decision` group, Manage Guild)

- `monitor <#channel>` / `unmonitor <#channel>` — toggle monitoring per channel
- `config board <trello_board_id> <trello_list_id>` — set Trello target; `config on|off` — enable/disable
- `map <@user> <trello_member_id>` / `unmap <@user>` — member assignment map
- `list [status]` — view pending/confirmed/rejected decisions (ephemeral)
- `create <summary>` — manually log + sync a decision
- `sync` — retry syncing confirmed-but-unsynced decisions

## Trello card contents

Title = summary. Description = decider Discord name, timestamp (guild tz), channel name, jump link to the original message, and the raw excerpt. Assign the mapped Trello member when one exists for the decider.

## Tasks

- [ ] **Task 1: DB models + DDL + repositories.** Four tables (`decisions`, `trello_sync`, `decision_config` + `decision_channels`, `trello_member_map`), idempotent DDL, repo functions (add/pending/confirm/reject/list decisions; channel monitor set; member map get/set; trello_sync record). Tests for all repos + DDL idempotency.
- [ ] **Task 2: config + .env.example.** Four env vars; `.env.example` documents them; config guards.
- [ ] **Task 3: decision_service (AI detection).** `detect_decision(text)` → structured result via GPT-4o-mini (JSON mode, tenacity retry, budget-capped); `create_pending` + `confirm`/`dismiss` service functions. Tests: mock the OpenAI call; JSON-parse robustness; budget/timeout.
- [ ] **Task 4: trello_service.** `create_card(list_id, ...)` via Trello REST (httpx+tenacity), `lookup_member` for assignment, error isolation. Tests: mock transport; card payload shape; assignment; failure paths.
- [ ] **Task 5: decision_views (Confirm/Dismiss DynamicItem).** Persistent buttons keyed by decision id; confirm → service.confirm + trello; dismiss → reject; edit the card to reflect outcome. Tests: callback paths, restart dispatch, permission/ownership.
- [ ] **Task 6: decision_commands (`/decision` group).** All admin commands with validation + ephemeral replies. Tests: each command's happy + invalid paths.
- [ ] **Task 7: decision_bot (listener client).** Separate `discord.Client` with message_content intent; `on_message` → filters (monitored channel, not bot, enabled) → decision_service; posts confirmation card. Tests: filter logic, no-self-trigger, disabled guild.
- [ ] **Task 8: launcher + Dockerfile.** `src/__main__.py` runs both bots via `asyncio.gather`; Dockerfile CMD → `python -m src`. Tests: launcher wiring smoke (both bots constructed, neither started without its token).
- [ ] **Task 9: scheduler/council hardening + full pass.** 4-agent council (correctness/security/regressions/coverage), fix waves, README docs, full suite green.
- [ ] **Task 10: deploy.** `fly secrets` for the four new vars; `fly deploy --ha=false`; verify both bots online + a live detect→confirm→Trello round-trip in a test channel.

## Out of scope

- Editing/deleting Trello cards after creation (confirm is one-way; fix in Trello).
- Thread/forum special-casing beyond monitoring the channel they're in.
- Auto-archiving old decisions.

## Go-live gates (must verify before deploy completes)

- **Message Content intent** is enabled on the decision bot's Discord application (else it reads nothing).
- Trello key+token can create a card on the target list (probe from the Fly machine).
- OpenAI key returns a detection for a sample message (probe from the Fly machine).
