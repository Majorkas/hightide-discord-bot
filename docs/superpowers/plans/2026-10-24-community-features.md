# Community Features (v2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add six community features to the events bot: interest roles with first-seen event pings, a daily "tonight's events" post, card sign-up buttons on event cards, deck/card lookup commands, a card-wants board, and new-product alerts.

**Architecture:** Extends the v1 layered design in place — new SQLAlchemy tables via the existing idempotent-DDL pattern, new services following the existing service/repository split, persistent discord.py views for buttons, and reuse of the fetcher/embed/session patterns. All v1 interfaces (`upsert_board`, `run_daily_refresh`, `refresh_events` module-level names; handler lazy imports; `should_run_now`) MUST remain intact.

**Tech Stack:** discord.py 2.x (app_commands + ui.View persistent buttons), httpx + tenacity (external APIs), SQLAlchemy 2.x/SQLite, pytest + AsyncMock.

**Spec:** [docs/superpowers/specs/2026-10-24-community-features-design.md](../specs/2026-10-24-community-features-design.md)

## Global Constraints

- Python 3.11+; all datetimes tz-aware; default tz `Australia/Perth`
- Existing patterns are binding: cached module-level `_session_factory` per module, context-managed sessions, ephemeral user replies, Manage-Guild + guild_only admin groups, `allowed_mentions` discipline (none() everywhere EXCEPT scoped role pings, spec §5.1), `_md_escape`/`_safe_url` on ALL scraped or user-provided rendered text, idempotent ALTER/DDL in `src/bot.py` setup_hook catching duplicate-column only
- Role pings: `allowed_mentions=discord.AllowedMentions(roles=[<role>])` — ping exactly the target role, nothing else
- New Discord permission: Manage Roles (README invite integer recomputed in Task 15)
- Tests run with `python3 -m pytest` (`.venv/bin/python -m pytest` here); suite must stay green with ZERO warnings at every task boundary
- No new privileged intents (buttons are interactions; role assignment is a bot permission, not an intent)
- Buttons: persistent views with `timeout=None`, custom_ids prefixed (`role_toggle:{interest_role_id}`, `signup:{event_id}`), re-registered in setup_hook
- Single-guild v2 (config is per-guild in the schema but only one guild is exercised)
- Suites named per feature; fixtures for external APIs are small captured JSON files under tests/fixtures/

## File Structure (new/changed)

| File | Responsibility |
|---|---|
| `src/db/models.py` | + interest_roles, role_keywords, event_role_pings, event_signups, card_wants, product_alerts; guild_config new columns |
| `src/db/session.py` | + DDL helpers for new tables/columns (existing pattern) |
| `src/services/weekly_service.py` | unchanged — reference pattern for new services |
| `src/services/role_service.py` | role CRUD, keyword matching, ping-once logic |
| `src/services/signup_service.py` | toggle/count/list signups |
| `src/services/deck_service.py` | moxfield/archidekt/scryfall clients |
| `src/services/want_service.py` | wants CRUD + expiry |
| `src/services/product_service.py` | new-product watch + first-run seeding |
| `src/discord_ui/role_views.py` | persistent Views: RolesPanelView, SignupView |
| `src/discord_ui/commands_roles.py` | /roles group, /want group, /deck, /card |
| `src/services/scheduler.py` | + daily tonight post + product watch hooks |
| `src/services/event_service.py` | + first-seen ping hook after upserts |
| `src/discord_ui/embeds.py` | + announcement/tonight/deck/card/wants embeds |
| `src/bot.py` | + DDL calls, view registration, cog wiring |
| `src/discord_ui/commands_admin.py` | + /config announce/roles/new_stock/wants channel setters |

---

## Milestone 1 — Roles foundation

### Task 1: Role tables + DDL + repositories

**Files:**
- Modify: `src/db/models.py` (append new models + guild_config columns)
- Modify: `src/db/session.py` (DDL helper for the new columns/tables — follow the existing `ensure_*` pattern)
- Modify: `src/bot.py` (call new DDL helper in setup_hook)
- Modify: `src/db/repositories.py` (role repo functions)
- Test: `tests/test_roles_repo.py`

**Interfaces:**
- Produces: `InterestRole(id, guild_id, role_id, label, emoji, sort_order, enabled)`, `RoleKeyword(id, interest_role_id, keyword)`, `EventRolePing(id, guild_id, event_id, interest_role_id, pinged_at)`; `GuildConfig.roles_channel_id`, `.roles_message_id`, `.announce_channel_id` (all nullable)
- Produces repo functions: `add_interest_role(session, guild_id, role_id, label, emoji=None) -> InterestRole`; `remove_interest_role(session, guild_id, row_id) -> bool`; `list_interest_roles(session, guild_id) -> list[InterestRole]` (ordered by sort_order); `add_keyword(session, interest_role_id, keyword) -> RoleKeyword`; `remove_keyword(session, interest_role_id, keyword) -> bool`; `get_keywords_by_role(session, guild_id) -> dict[int, list[str]]`; `has_ping(session, event_id, interest_role_id) -> bool`; `record_ping(session, guild_id, event_id, interest_role_id) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_roles_repo.py
from src.db.models import Base
from src.db.repositories import (
    add_interest_role, add_keyword, get_keywords_by_role, has_ping,
    list_interest_roles, record_ping, remove_interest_role, remove_keyword,
)
from src.db.session import get_engine, get_session_factory


def _session():
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = get_session_factory("sqlite:///:memory:")
    factory.configure(bind=engine)
    return factory()


def test_add_list_remove_role():
    s = _session()
    r = add_interest_role(s, 1, 111, "Yu-Gi-Oh", emoji="🎴")
    assert r.id is not None
    roles = list_interest_roles(s, 1)
    assert [x.label for x in roles] == ["Yu-Gi-Oh"]
    assert roles[0].emoji == "🎴"
    assert remove_interest_role(s, 1, r.id) is True
    assert list_interest_roles(s, 1) == []
    assert remove_interest_role(s, 1, 999) is False


def test_keywords_and_ping_once():
    s = _session()
    r = add_interest_role(s, 1, 111, "MTG")
    add_keyword(s, r.id, "mtg")
    add_keyword(s, r.id, "magic")
    kw = get_keywords_by_role(s, 1)
    assert kw[r.id] == ["mtg", "magic"]
    assert remove_keyword(s, r.id, "magic") is True
    assert get_keywords_by_role(s, 1)[r.id] == ["mtg"]
    assert has_ping(s, 42, r.id) is False
    record_ping(s, 1, 42, r.id)
    assert has_ping(s, 42, r.id) is True


def test_guild_isolation():
    s = _session()
    add_interest_role(s, 1, 111, "A")
    add_interest_role(s, 2, 222, "B")
    assert [x.label for x in list_interest_roles(s, 1)] == ["A"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_roles_repo.py -v`
Expected: FAIL — ImportError (functions don't exist)

- [ ] **Step 3: Write minimal implementation**

Append to `src/db/models.py` (follow the existing model style; all id columns BigInteger for Discord snowflakes):

```python
class InterestRole(Base):
    __tablename__ = "interest_roles"
    __table_args__ = (UniqueConstraint("guild_id", "role_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    role_id: Mapped[int] = mapped_column(BigInteger)
    label: Mapped[str] = mapped_column(String)
    emoji: Mapped[str | None] = mapped_column(String, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class RoleKeyword(Base):
    __tablename__ = "role_keywords"
    __table_args__ = (UniqueConstraint("interest_role_id", "keyword"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    interest_role_id: Mapped[int] = mapped_column(Integer)
    keyword: Mapped[str] = mapped_column(String)


class EventRolePing(Base):
    __tablename__ = "event_role_pings"
    __table_args__ = (UniqueConstraint("event_id", "interest_role_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    event_id: Mapped[int] = mapped_column(Integer)
    interest_role_id: Mapped[int] = mapped_column(Integer)
    pinged_at: Mapped[datetime] = mapped_column(TZDateTime, default=_utcnow)
```

Add to `GuildConfig` (plus DDL in `src/db/session.py` following `ensure_board_post_message_ids`, one `ensure_guild_config_v2_columns` doing three ALTERs + `CREATE TABLE IF NOT EXISTS` fallback via create_all; wired in `src/bot.py` setup_hook after the existing calls):

```python
    roles_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    roles_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    announce_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
```

Repo functions in `src/db/repositories.py` (each commits; role removal cascades keyword/ping rows for that role id):

```python
def add_interest_role(session, guild_id, role_id, label, emoji=None):
    row = InterestRole(guild_id=guild_id, role_id=role_id, label=label, emoji=emoji)
    # next sort order
    existing = list_interest_roles(session, guild_id)
    row.sort_order = (max((x.sort_order for x in existing), default=-1) + 1)
    session.add(row)
    session.commit()
    return row


def remove_interest_role(session, guild_id, row_id) -> bool:
    row = session.get(InterestRole, row_id)
    if row is None or row.guild_id != guild_id:
        return False
    session.query(RoleKeyword).filter(RoleKeyword.interest_role_id == row.id).delete()
    session.query(EventRolePing).filter(EventRolePing.interest_role_id == row.id).delete()
    session.delete(row)
    session.commit()
    return True


def list_interest_roles(session, guild_id) -> list[InterestRole]:
    stmt = select(InterestRole).where(InterestRole.guild_id == guild_id).order_by(InterestRole.sort_order)
    return list(session.execute(stmt).scalars())


def add_keyword(session, interest_role_id, keyword):
    row = RoleKeyword(interest_role_id=interest_role_id, keyword=keyword.strip().lower())
    session.add(row)
    session.commit()
    return row


def remove_keyword(session, interest_role_id, keyword) -> bool:
    stmt = select(RoleKeyword).where(
        RoleKeyword.interest_role_id == interest_role_id,
        RoleKeyword.keyword == keyword.strip().lower())
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def get_keywords_by_role(session, guild_id) -> dict[int, list[str]]:
    roles = list_interest_roles(session, guild_id)
    out: dict[int, list[str]] = {r.id: [] for r in roles}
    if not roles:
        return out
    stmt = select(RoleKeyword).where(
        RoleKeyword.interest_role_id.in_([r.id for r in roles]))
    for k in session.execute(stmt).scalars():
        out[k.interest_role_id].append(k.keyword)
    return out


def has_ping(session, event_id, interest_role_id) -> bool:
    stmt = select(EventRolePing).where(
        EventRolePing.event_id == event_id,
        EventRolePing.interest_role_id == interest_role_id)
    return session.execute(stmt).scalar_one_or_none() is not None


def record_ping(session, guild_id, event_id, interest_role_id) -> None:
    if has_ping(session, event_id, interest_role_id):
        return
    session.add(EventRolePing(guild_id=guild_id, event_id=event_id,
                              interest_role_id=interest_role_id))
    session.commit()
```

- [ ] **Step 4: Run test to verify it passes** — 3 tests PASS, zero warnings
- [ ] **Step 5: Commit** — `feat: interest-role tables, DDL, repositories`

---

### Task 2: `/roles` admin commands + roles panel + toggle view

**Files:**
- Create: `src/discord_ui/role_views.py`
- Create: `src/discord_ui/commands_roles.py`
- Create: `src/services/role_service.py`
- Modify: `src/bot.py` (register RolesCog groups on tree like AdminCog pattern; register persistent views in setup_hook)
- Test: `tests/test_roles_panel.py`

**Interfaces:**
- Consumes: Task 1 repos; AdminCog registration pattern (tree.add_command at create_bot; comment explains no add_cog)
- Produces: `RolesPanelView(timeout=None)` with dynamic buttons custom_id `role_toggle:{interest_role_id}`; `RolesCog` with `/roles setup|add|remove|keyword|list`; `build_roles_panel_embed(roles) -> discord.Embed`; `async def handle_role_toggle(interaction, interest_role_id) -> None` (in role_service or the view module — the callback entry point the bot wires)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_roles_panel.py
import discord

from src.bot import create_bot
from src.discord_ui.role_views import RolesPanelView
from src.discord_ui.role_views import build_roles_panel_embed
from src.db.models import InterestRole


def test_roles_commands_registered():
    bot = create_bot()
    names = {c.name for c in bot.tree.get_commands()}
    assert "roles" in names


def test_panel_embed_lists_roles():
    roles = [InterestRole(id=1, guild_id=1, role_id=11, label="Yu-Gi-Oh", emoji="🎴"),
             InterestRole(id=2, guild_id=1, role_id=22, label="40k", emoji=None)]
    embed = build_roles_panel_embed(roles)
    assert "Yu-Gi-Oh" in embed.description
    assert "40k" in embed.description
    assert "<@&11>" in embed.description


def test_panel_view_buttons_have_persistent_custom_ids():
    roles = [InterestRole(id=7, guild_id=1, role_id=11, label="Yu-Gi-Oh")]
    view = RolesPanelView(roles)
    assert view.timeout is None
    customs = [c.custom_id for c in view.children]
    assert customs == ["role_toggle:7"]
```

- [ ] **Step 2: Run to verify FAIL** (ModuleNotFoundError)
- [ ] **Step 3: Implement**

`src/discord_ui/role_views.py`:

```python
import discord

from src.db.models import InterestRole


def build_roles_panel_embed(roles: list[InterestRole]) -> discord.Embed:
    lines = [f"{(r.emoji + ' ') if r.emoji else ''}**{r.label}** — <@&{r.role_id}>"
             for r in roles]
    embed = discord.Embed(
        title="Get event pings",
        description=("Click a button to add or remove that game's role. "
                     "You'll be pinged when a new event in that game appears.\n\n"
                     + ("\n".join(lines) if lines else "No roles configured yet.")),
        color=0x1F8B4C)
    return embed


class RolesPanelView(discord.ui.View):
    def __init__(self, roles: list[InterestRole]):
        super().__init__(timeout=None)
        for r in roles:
            style = discord.ButtonStyle.secondary
            btn = discord.ui.Button(
                label=r.label, emoji=r.emoji or None, style=style,
                custom_id=f"role_toggle:{r.id}")
            btn.callback = self._make_callback(r.id)
            self.add_item(btn)

    def _make_callback(self, role_row_id: int):
        async def _cb(interaction: discord.Interaction):
            from src.services.role_service import handle_role_toggle
            await handle_role_toggle(interaction, role_row_id)
        return _cb
```

`src/services/role_service.py` (cached `_session_factory` like other modules):

```python
import logging

import discord

from src import config
from src.db.repositories import get_or_create_config
from src.db.session import get_session_factory

log = logging.getLogger("hightide.roles")

_session_factory = None


def _session():
    global _session_factory
    if _session_factory is None:
        _session_factory = get_session_factory(config.DATABASE_URL)
    return _session_factory()


async def handle_role_toggle(interaction: discord.Interaction, role_row_id: int) -> None:
    from src.db.models import InterestRole
    with _session() as session:
        row = session.get(InterestRole, role_row_id)
        if row is None or not row.enabled or row.guild_id != interaction.guild_id:
            await interaction.response.send_message(
                "That role isn't available any more.", ephemeral=True)
            return
        role_id, label = row.role_id, row.label
    role = interaction.guild.get_role(role_id)
    member = interaction.user
    if role is None:
        await interaction.response.send_message(
            f"The **{label}** role no longer exists — tell an admin.", ephemeral=True)
        return
    try:
        if role in member.roles:
            await member.remove_roles(role, reason="interest role toggle")
            await interaction.response.send_message(f"Removed **{label}**.", ephemeral=True)
        else:
            await member.add_roles(role, reason="interest role toggle")
            await interaction.response.send_message(f"Added **{label}** — you'll be pinged for new events.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I can't manage that role (check my role sits above it). Tell an admin.",
            ephemeral=True)
```

`src/discord_ui/commands_roles.py`: `RolesCog` with a `roles_group = app_commands.Group(name="roles", default_permissions=manage_guild, guild_only=True)` and commands:
- `setup(channel: discord.TextChannel)`: build embed + view from current roles, send panel message, store `roles_channel_id` + `roles_message_id` on the guild config. Re-send edits the stored panel in place if it exists (fetch + edit; NotFound → send new).
- `add(role: discord.Role, label: str, emoji: str | None)`: validate label ≤ 60, not duplicate role_id; repo add; refresh the panel message (edit with rebuilt embed + view).
- `remove(row_id: int)`: repo remove (guild-scoped); refresh panel.
- `keyword(row_id: int, action: Literal["add", "remove"], keyword: str)`: repo add/remove; ephemeral result.
- `list`: ephemeral table of id/label/role/keywords/enabled.

`src/bot.py`: in `create_bot`, after AdminCog registration, `from src.discord_ui.commands_roles import RolesCog` then `bot.tree.add_command(RolesCog(bot).roles_group)` with the same comment pattern. In `setup_hook`, after DDL: register the persistent view for existing panels — `bot.add_view(RolesPanelView(list_interest_roles(session, guild_id)))` per guild with a roles_message_id. (SignupView wiring arrives in Task 8.)

- [ ] **Step 4: Tests pass** — 3 new tests + full suite green, zero warnings
- [ ] **Step 5: Commit** — `feat: /roles commands + persistent roles panel + toggle`

---

## Milestone 2 — Event pings

### Task 3: Keyword matching + first-seen ping pipeline

**Files:**
- Modify: `src/services/role_service.py` (matching + ping functions)
- Modify: `src/services/event_service.py` (call ping hook with created events)
- Modify: `src/discord_ui/embeds.py` (`build_ping_message(event, role) -> str`)
- Test: `tests/test_role_pings.py`

**Interfaces:**
- Consumes: Task 1/2 repos; `upsert_products` return/created events
- Produces: `match_roles_for_title(title, keywords_by_role, roles_by_id) -> list[InterestRole]` (whole-phrase case-insensitive matching: keyword "yu" must NOT match "Yummy" — use word-boundary regex `re.search(r"\b" + re.escape(kw) + r"\b", title, re.I)`); `async def ping_new_events(bot, session, guild_id, created_events, tz) -> int` (returns pings sent); embeds: `build_ping_message(event, role_label) -> str` → `"<@&{role_id}> — New event: [{title}]({url}) · {Fri 23 Oct} · {18:30 or 'time TBC'}"` (escaped title/url via _md_escape/_safe_url)

**Ping semantics (binding):**
- Only events with `created=True` from the upsert stats participate. `upsert_products` currently returns only counts — change it to also return `created_events: list[Event]` in the stats dict (additive key; update the two tests asserting the full dict shape).
- Pings send only when `announce_channel_id` is configured; each ping recorded via `record_ping`; a ping send failure (Forbidden/NotFound) is logged and does not break the refresh.
- allowed_mentions scoped: `discord.AllowedMentions(roles=[role_obj])` where role_obj comes from `guild.get_role(role_id)`; if the role no longer exists → skip ping, log.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_role_pings.py
import re
from types import SimpleNamespace

from src.services.role_service import match_roles_for_title


def _roles():
    r1 = SimpleNamespace(id=1, label="Yu-Gi-Oh", role_id=11, enabled=True)
    r2 = SimpleNamespace(id=2, label="MTG", role_id=22, enabled=True)
    return {1: r1, 2: r2}, {1: ["yu-gi-oh", "yugioh"], 2: ["mtg", "magic the gathering"]}


def test_keyword_match_basic():
    roles_by_id, kw = _roles()
    got = match_roles_for_title("05.11.26 – Weekly Duels – Yu-Gi-Oh!", kw, roles_by_id)
    assert [r.id for r in got] == [1]


def test_no_partial_word_match():
    roles_by_id, kw = _roles()
    # 'mtg' must not match 'scmtg' or 'mtgx'
    assert match_roles_for_title("Scmtg night", kw, roles_by_id) == []
    assert match_roles_for_title("MTGX open", kw, roles_by_id) == []
    # but standalone works
    assert [r.id for r in match_roles_for_title("FNM – MTG draft", kw, roles_by_id)] == [2]


def test_case_insensitive():
    roles_by_id, kw = _roles()
    assert [r.id for r in match_roles_for_title("YUGIOH locals", kw, roles_by_id)] == [1]
```

And ping-level tests (AsyncMock bot/guild/channel; real in-memory session): created event with matching keyword → exactly one channel.send with the role mention string and `allowed_mentions` scoping that role; second refresh with the same event (unchanged) → no ping; announce channel unset → no send; role deleted from guild → skip, no crash.

- [ ] **Step 2-4: TDD cycle to green.** In `run_daily_refresh` (scheduler.py) and nowhere else, call `ping_new_events` after the events step succeeds with the created events (needs the bot object — run_daily_refresh already receives it; pass created events from `refresh_events` → `upsert_products` stats["created_events"]).
- [ ] **Step 5: Commit** — `feat: keyword role matching + first-seen event pings`

---

## Milestone 3 — Daily tonight post

### Task 4: Daily "Today at Hightide" post

**Files:**
- Modify: `src/db/models.py` (guild_config `daily_post_time` default "09:30", `last_daily_post_date` nullable String) + DDL
- Modify: `src/services/scheduler.py` (post hook after successful events step)
- Modify: `src/discord_ui/embeds.py` (`build_tonight_embed(events_today, tz) -> discord.Embed`)
- Modify: `src/discord_ui/commands_admin.py` (`/config announce_channel`, `/config daily_post_time` — both validated like existing setters)
- Test: `tests/test_tonight_post.py`

**Interfaces:**
- Produces: `build_tonight_embed(events, tz)` → title "Today at Hightide — Fri 23 Oct", one line per event (`HH:MM`/`time TBC` · linked title · price, escaped, sorted by time); empty list → returns None (no post). `async def maybe_post_tonight(bot, session, cfg, tz, events, now) -> bool` in scheduler (skips when announce channel unset, already posted today, or no events today).

- [ ] **Step 1: Failing tests** — embed rendering (sorted, escaped, empty→None); maybe_post_tonight: posts once, second same-day call no-op, unset channel no-op, zero events no-op, date boundary via injected now.
- [ ] **Step 2-4: TDD to green.** Hook: in the scheduler tick after a successful `run_daily_refresh`, call `maybe_post_tonight`. The daily refresh time stays 09:00; the tonight post fires at the same tick (single daily run) — `daily_post_time` exists for future decoupling; v2 uses the refresh time. (Document this in the command description.)
- [ ] **Step 5: Commit** — `feat: daily tonight-events post`

---

## Milestone 4 — Sign-ups

### Task 5: Signup table + service

**Files:**
- Modify: `src/db/models.py` (+ `event_signups`; guild_config unchanged) + DDL
- Create: `src/services/signup_service.py`
- Test: `tests/test_signups.py`

**Interfaces:**
- Produces: `EventSignup(id, guild_id, event_id, user_id, signed_up_at)` unique (event_id, user_id); `toggle_signup(session, guild_id, event_id, user_id) -> tuple[bool, int]` (added?, new count); `count_signups(session, event_id) -> int`; `list_signups(session, guild_id, event_id) -> list[EventSignup]`; `get_event_by_id(session, guild_id, event_id) -> Event | None` (repository addition)

- [ ] **Steps 1-5 (TDD)**: toggle add → (True, 1); toggle again → (False, 0); duplicate inserts impossible (second add path is the toggle); counts per event isolated; list returns user_ids; unknown event id → service returns None for the event lookup (button handler will message "event ended"). Commit: `feat: signup table + service`

### Task 6: Signup buttons on week cards + admin list

**Files:**
- Modify: `src/discord_ui/role_views.py` (+ `SignupView` persistent view, custom_id `signup:{event_id}`, label "✅ Going (N)")
- Modify: `src/services/board_service.py` (week card messages get the view attached on send/edit)
- Modify: `src/bot.py` (register a persistent SignupView at startup: `bot.add_view(SignupView())` — the view is stateless; custom_id carries the event id)
- Modify: `src/discord_ui/commands_admin.py` (`/board signups` with event autocomplete from active events; ephemeral name list)
- Test: `tests/test_signup_buttons.py`

**Interfaces:**
- Produces: `SignupView(discord.ui.View, timeout=None)` whose button has `custom_id="signup:0"` placeholder overridden per card (`signup:{event_id}`); callback → `handle_signup_toggle(interaction, event_id)` in signup_service: event missing/inactive → ephemeral "This event has ended."; else toggle + edit the ORIGINAL message's view label with the new count (`interaction.response.edit_message(view=...)` is NOT enough — the count lives on the button label: rebuild a fresh SignupView with the new count and `await interaction.response.edit_message(view=new_view)`).
- Board integration: when board_service sends/edits a week card for event id X, attach `SignupView(count)` (count from the DB). The per-card content hash gains the signup count so count changes from OTHER users also refresh labels on the next board edit — but the immediate edit happens via the button callback itself.

- [ ] **Steps 1-5 (TDD)**: button custom_id format; toggle via callback edits message label (AsyncMock interaction + real session); ended-event path; `/board signups` lists names (fetch users via `interaction.guild.get_member` — mock); empty list → "No signups yet."; autocomplete returns active event choices ≤25. Full suite green, zero warnings. Commit: `feat: signup buttons on event cards + /board signups`

---

## Milestone 5 — Deck + card commands

### Task 7: Scryfall `/card` lookup

**Files:**
- Create: `src/services/deck_service.py`
- Modify: `src/discord_ui/commands_roles.py` (+ `/card` on a new public group or top-level command — top-level `card` command, ephemeral=False (public: people share cards openly))
- Test: `tests/test_card.py` + fixture `tests/fixtures/scryfall_card.json`

**Interfaces:**
- Produces: `async def scryfall_card(name: str) -> dict | None` (httpx.AsyncClient, 15s timeout, tenacity retry on TransportError/5xx, UA header `hightide-events-bot/0.2`); `build_card_embed(card: dict) -> discord.Embed` — title=card name, url=scryfall_uri, image=image_uris.normal, description = mana_cost + type_line + oracle_text (trimmed to 400 chars, escaped) + `**USD $X.XX**` when prices.usd present; fuzzy miss (404) → None → ephemeral "No card found for '<name>'" (with Scryfall's suggestion if the error body has one).

- [ ] **Steps 1-5 (TDD)**: fixture-backed embed test (real captured Scryfall JSON for "Lightning Bolt"); miss path; price absent path; description trimming. Commands use the cached-session-free async client directly (no DB). Commit: `feat: /card scryfall lookup`

### Task 8: `/deck` moxfield + archidekt

**Files:**
- Modify: `src/services/deck_service.py`
- Modify: `src/discord_ui/commands_roles.py` (+ `/deck url:<str>`, public non-ephemeral)
- Test: `tests/test_deck.py` + fixtures `tests/fixtures/moxfield_deck.json`, `tests/fixtures/archidekt_deck.json`

**Interfaces:**
- Produces: `async def fetch_deck(url: str) -> DeckSummary | None` where `DeckSummary` = dataclass(title, fmt, card_count, image_url, url, source); URL parsing accepts `moxfield.com/decks/<id>` and `archidekt.com/decks/<id>` (any path variant with the numeric/slug id); fetch Moxfield `https://api2.moxfield.com/v2/decklists/<id>` and Archidekt `https://archidekt.com/api/decks/<id>/`; image: Moxfield → the commander's card image via Scryfall `cards/<sfid>` when present else None; Archidekt → `customImage` else first featured card. Unknown host / fetch failure / non-200 → None → ephemeral error. `build_deck_embed(d: DeckSummary) -> discord.Embed` — title, url, image (large), description: `{source} · {format} · {count} cards`.

- [ ] **Steps 1-5 (TDD)**: URL parsing table (valid moxfield/archidekt variants, invalid hosts, garbage); fixture-backed embeds for both sources; failure paths. Commit: `feat: /deck moxfield + archidekt embeds`

---

## Milestone 6 — Card wants board

### Task 9: `/want` commands + persistent wants board

**Files:**
- Modify: `src/db/models.py` (+ `card_wants`; guild_config `wants_channel_id`) + DDL
- Create: `src/services/want_service.py`
- Modify: `src/discord_ui/commands_roles.py` (`/want add|browse|remove|list` — public, guild_only; add confirm ephemeral; browse ephemeral)
- Modify: `src/discord_ui/embeds.py` (`build_wants_embed(wants, tz) -> discord.Embed`)
- Modify: `src/discord_ui/commands_admin.py` (`/config wants_channel`)
- Test: `tests/test_wants.py`

**Interfaces:**
- Consumes: `scryfall_card_exact(name) -> dict | None` (added in Task 7 — exact-match variant of the Scryfall client; `/card` uses fuzzy, wants resolution uses exact)
- Produces: `async add_want(session, guild_id, user_id, card_name, note=None) -> CardWant` (card_name ≤200, note ≤120, both escaped at render; per-user cap 10 active wants; resolves `scryfall_card_exact(card_name)` at add time and stores `scryfall_uri` when found, NULL otherwise — a failed/unknown card NEVER blocks the want); `remove_want(session, guild_id, want_id, user_id, is_admin) -> bool` (owner or admin); `list_wants(session, guild_id, user_id=None) -> list[CardWant]`; `expire_wants(session, guild_id, now) -> int` (active=False past 30 days); `async def update_wants_board(bot, session, guild_id) -> str` reusing the board_posts single-message pattern with board_type="wants" (create/edit/skip via render hash of active wants).
- Board rendering: title "Card Wants", description lines `[**{card}**]({scryfall_uri}) — <@{user_id}> · {note?} · {Mon DD}` when resolved (plain `**{card}**` when scryfall_uri is NULL), **sorted alphabetically by card name (case-insensitive)** — sellers scan for cards they hold, so grouping beats recency; empty → "No active wants."; cap 25 lines with `… +N more`. The link URL is validated via `_safe_url` like all rendered URLs.
- `/want browse` (ephemeral, any member): sends `build_wants_embed` for active wants — the on-demand seller-facing list, usable from any channel. Renders the identical content as the board (same builder).
- `add_want` flow: resolve card (best-effort) → insert → `update_wants_board` → ephemeral "Added to the wants board" (the confirm notes when the name couldn't be linked: "(no Scryfall match — shown as plain text)").

- [ ] **Steps 1-5 (TDD)**: add/list/remove semantics; owner-vs-admin remove; per-user cap; expiry boundary (29 days keeps, 31 expires — injected now); board create/edit/skip via mocked channel; ALPHABETICAL ordering (insert Z then A → board lists A first; case-insensitive); browse returns the same content as the board; scryfall_uri resolved → linked line, unresolved → plain bold text; resolution failure never blocks the add; escaping of hostile card names/notes. Commit: `feat: /want card-wants board`

---

## Milestone 7 — New-product alerts

### Task 10: Watched-category product alerts

**Files:**
- Modify: `src/db/models.py` (+ `product_alerts`; guild_config `new_stock_channel_id`, `new_stock_cat_slugs` String default "pre-orders") + DDL
- Create: `src/services/product_service.py`
- Modify: `src/scraping/fetcher.py` (generalize: `fetch_category_products(base_url, cat_slug, client=None)` — extract from fetch_events; fetch_events delegates with slug "events"; existing fetcher tests must stay green)
- Modify: `src/services/scheduler.py` (after events step in run_daily_refresh: `await asyncio.to_thread(check_new_products, session, guild_id)` then post alerts)
- Modify: `src/discord_ui/commands_admin.py` (`/config new_stock_channel`, `/config new_stock_categories <comma slugs>`)
- Test: `tests/test_product_alerts.py`

**Interfaces:**
- Produces: `check_new_products(session, guild_id, base_url, tz) -> list[dict]` (products to alert, with first-run seeding: when `product_alerts` has zero rows for the guild, record ALL current products and return []); `async def post_product_alerts(bot, session, guild_id, products) -> int` — one message per product in the new-stock channel: `[{title}]({url}) · {price or '—'}` + product image as a small thumbnail embed; record_alert per product AFTER successful send (failed send → not recorded → retried next refresh).

- [ ] **Steps 1-5 (TDD)**: mock-transport fetch of a watched category; first-run seeding returns []; second run with one new product → one alert; send failure → unrecorded → retried; channel unset → skip; slug list config parsing. Existing fetcher tests green. Commit: `feat: new-product stock alerts`

---

## Milestone 8 — Hardening + deploy

### Task 11: Permissions, README, full pass, deploy

**Files:**
- Modify: `README.md` (new commands, features, Manage Roles permission, role-hierarchy note, recompute invite integer: 93184 + 268435456 = 268528640 — verify with `discord.Permissions` in the venv during the task and use the computed value)
- Modify: `tests/test_admin_commands.py` command-surface walk (roles/want groups present; public groups not Manage-Guild-gated)
- Modify: `.env.example` if any new env (expected: none)

- [ ] **Step 1**: surface test updates (failing)
- [ ] **Step 2-4**: README + test updates; full suite green, zero warnings; `git log` sanity
- [ ] **Step 5: Commit** — `docs: v2 features, permissions`
- [ ] **Step 6: Deploy** — controller step after council: `fly deploy --ha=false` (machine + volume already exist)

---

## Definition of Done (v2)

- Roles panel live; members self-toggle; first-seen matching events ping the right role exactly once
- 09:00 refresh also posts "Today at Hightide" when events exist today
- Every week-board event card has a working Going button with a live count; admins can list names
- `/card` and `/deck` render real card/deck data with images
- `/want` board tracks card wants with 30-day expiry
- New products in watched categories post once each to the stock channel
- Suite green (zero warnings), councils clean, deployed on Fly
