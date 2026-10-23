# Discord Events Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a single-guild Discord bot that pulls Hightide Tabletop events from the WordPress REST API and maintains persistent, edit-in-place `#this-week` and `#this-month` event boards.

**Architecture:** Layered — WP REST fetcher → parser/normalizer → SQLAlchemy (SQLite) repositories → board/scheduler services → discord.py slash-command UI. Boards are single persistent bot messages edited in place, keyed by `board_posts` rows and `render_hash` change detection.

**Tech Stack:** Python 3.11+, discord.py 2.x, httpx, beautifulsoup4, SQLAlchemy 2.x, python-dateutil, tenacity, pytest.

**Spec:** [docs/superpowers/specs/2026-10-23-discord-events-bot-design.md](../specs/2026-10-23-discord-events-bot-design.md)

## Global Constraints

- Python 3.11+; all datetimes timezone-aware (UTC stored, rendered in configured tz)
- Default timezone `Australia/Perth`; daily refresh `09:00`; `week_start_mode` default `today_start`
- Data source is the WP REST API: `GET /wp-json/wp/v2/product?product_cat={id}&per_page=100&_embed=1&page=N`; category id resolved via `GET /wp-json/wp/v2/product_cat?slug=events`
- `external_id` format: `wp:{post_id}`; unique per guild
- `fingerprint` = `sha1(title | starts_at_iso | price_text | summary)`
- Events missing across 2 consecutive refreshes → `is_active = false`
- Admin commands require Manage Guild; user commands are ephemeral
- Boards edit in place; recreate if stored `message_id` was deleted
- Interest roles are **out of scope** (v2)
- Tests run with `python3 -m pytest` from repo root

## File Structure

| File | Responsibility |
|---|---|
| `src/config.py` | Env vars, constants |
| `src/bot.py` | Entry point, intents, cog loading, command sync |
| `src/db/models.py` | SQLAlchemy declarative models |
| `src/db/session.py` | Engine + session factory |
| `src/db/repositories.py` | Queries for events, board_posts, guild_config, event_sources |
| `src/scraping/fetcher.py` | WP REST client (httpx + tenacity) |
| `src/scraping/hightide_parser.py` | JSON → `NormalizedEvent` |
| `src/scraping/normalize.py` | Date/time/price parsing, fingerprint, title cleanup |
| `src/services/event_service.py` | Upsert/dedupe/stale detection |
| `src/services/board_service.py` | Board payload building, hashing, create/edit |
| `src/services/scheduler.py` | `discord.ext.tasks` daily loop + month rollover |
| `src/discord_ui/embeds.py` | Board + preview embeds |
| `src/discord_ui/commands_admin.py` | `/config`, `/source`, `/board` |
| `src/discord_ui/commands_user.py` | `/events upcoming` |

---

## Milestone 1 — Foundation

### Task 1: Project scaffold + config

**Files:**
- Create: `pyproject.toml`
- Create: `src/__init__.py`, `src/db/__init__.py`, `src/scraping/__init__.py`, `src/services/__init__.py`, `src/discord_ui/__init__.py`
- Create: `src/config.py`
- Create: `tests/__init__.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `config.DISCORD_TOKEN: str`, `config.DATABASE_URL: str` (default `sqlite:///./hightide.db`), `config.DEFAULT_TIMEZONE: str` (default `Australia/Perth`), `config.GUILD_ID: int | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
import importlib
import os


def test_config_defaults(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DEFAULT_TIMEZONE", raising=False)
    monkeypatch.delenv("GUILD_ID", raising=False)
    import src.config as config
    importlib.reload(config)
    assert config.DISCORD_TOKEN == "test-token"
    assert config.DATABASE_URL == "sqlite:///./hightide.db"
    assert config.DEFAULT_TIMEZONE == "Australia/Perth"
    assert config.GUILD_ID is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.config'`

- [ ] **Step 3: Write minimal implementation**

```toml
# pyproject.toml
[project]
name = "hightide-discord-bot"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "discord.py>=2.3",
    "httpx>=0.27",
    "beautifulsoup4>=4.12",
    "SQLAlchemy>=2.0",
    "python-dateutil>=2.9",
    "tenacity>=8.2",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23"]

[tool.pytest.ini_options]
pythonpath = ["."]
asyncio_mode = "auto"
testpaths = ["tests"]
```

```python
# src/config.py
import os

DISCORD_TOKEN: str = os.environ.get("DISCORD_TOKEN", "")
DATABASE_URL: str = os.environ.get("DATABASE_URL", "sqlite:///./hightide.db")
DEFAULT_TIMEZONE: str = os.environ.get("DEFAULT_TIMEZONE", "Australia/Perth")
_guild = os.environ.get("GUILD_ID")
GUILD_ID: int | None = int(_guild) if _guild else None
```

- [ ] **Step 4: Install deps + run test**

Run: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/python -m pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: project scaffold + config"
```

---

### Task 2: Database models + session

**Files:**
- Create: `src/db/models.py`
- Create: `src/db/session.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: `GuildConfig`, `EventSource`, `Event`, `BoardPost` models; `get_engine(url)`, `get_session_factory(url)`; `Base`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models.py
from src.db.models import Base, Event, GuildConfig
from src.db.session import get_engine, get_session_factory


def test_models_create_and_insert():
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = get_session_factory("sqlite:///:memory:")
    # bind factory's engine for the in-memory case
    factory.configure(bind=engine)
    session = factory()
    gc = GuildConfig(guild_id=1)
    session.add(gc)
    ev = Event(guild_id=1, external_id="wp:1", title="Test", url="https://x")
    session.add(ev)
    session.commit()
    assert session.query(GuildConfig).count() == 1
    assert session.query(Event).first().external_id == "wp:1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.db.models'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/db/models.py
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GuildConfig(Base):
    __tablename__ = "guild_config"

    guild_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timezone: Mapped[str] = mapped_column(String, default="Australia/Perth")
    this_week_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    this_month_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_refresh_time: Mapped[str] = mapped_column(String, default="09:00")
    week_start_mode: Mapped[str] = mapped_column(String, default="today_start")
    include_tba_section: Mapped[bool] = mapped_column(Boolean, default=True)
    delete_previous_month_message: Mapped[bool] = mapped_column(Boolean, default=True)


class EventSource(Base):
    __tablename__ = "event_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("guild_id", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(Integer)
    external_id: Mapped[str] = mapped_column(String)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(String)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    time_known: Mapped[bool] = mapped_column(Boolean, default=False)
    price_text: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    missed_refreshes: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class BoardPost(Base):
    __tablename__ = "board_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(Integer)
    board_type: Mapped[str] = mapped_column(String)  # this_week | this_month
    period_key: Mapped[str] = mapped_column(String)  # e.g. 2026-W43 | 2026-10
    channel_id: Mapped[int] = mapped_column(Integer)
    message_id: Mapped[int] = mapped_column(Integer)
    render_hash: Mapped[str] = mapped_column(String, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
```

```python
# src/db/session.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def get_engine(url: str):
    return create_engine(url, future=True)


def get_session_factory(url: str) -> sessionmaker:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: db models + session factory"
```

---

### Task 3: Repositories

**Files:**
- Create: `src/db/repositories.py`
- Test: `tests/test_repositories.py`

**Interfaces:**
- Consumes: models + session factory from Task 2
- Produces: `get_or_create_config(session, guild_id) -> GuildConfig`; `upsert_event(session, guild_id, data: dict) -> tuple[Event, bool]` (bool = created); `get_active_events(session, guild_id) -> list[Event]`; `mark_absent_events_stale(session, guild_id, seen_external_ids: set[str]) -> int` (returns count newly deactivated); `get_board_post(session, guild_id, board_type) -> BoardPost | None`; `save_board_post(session, guild_id, board_type, period_key, channel_id, message_id, render_hash) -> BoardPost`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_repositories.py
from src.db.models import Base
from src.db.repositories import (
    get_active_events,
    get_or_create_config,
    mark_absent_events_stale,
    upsert_event,
)
from src.db.session import get_engine, get_session_factory


def _session():
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = get_session_factory("sqlite:///:memory:")
    factory.configure(bind=engine)
    return factory()


def test_config_defaults():
    s = _session()
    cfg = get_or_create_config(s, 7)
    assert cfg.timezone == "Australia/Perth"
    assert cfg.daily_refresh_time == "09:00"
    assert cfg.include_tba_section is True


def test_upsert_creates_then_updates():
    s = _session()
    ev, created = upsert_event(s, 7, {"external_id": "wp:1", "title": "A", "url": "u"})
    assert created is True
    ev2, created2 = upsert_event(s, 7, {"external_id": "wp:1", "title": "A2", "url": "u"})
    assert created2 is False
    assert ev2.title == "A2"
    assert len(get_active_events(s, 7)) == 1


def test_stale_marking_after_two_misses():
    s = _session()
    upsert_event(s, 7, {"external_id": "wp:1", "title": "A", "url": "u"})
    assert mark_absent_events_stale(s, 7, set()) == 0  # 1 miss
    assert mark_absent_events_stale(s, 7, set()) == 1  # 2 misses -> deactivated
    assert get_active_events(s, 7) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_repositories.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/db/repositories.py
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import BoardPost, Event, EventSource, GuildConfig


def get_or_create_config(session: Session, guild_id: int) -> GuildConfig:
    cfg = session.get(GuildConfig, guild_id)
    if cfg is None:
        cfg = GuildConfig(guild_id=guild_id)
        session.add(cfg)
        session.commit()
    return cfg


def upsert_event(session: Session, guild_id: int, data: dict) -> tuple[Event, bool]:
    stmt = select(Event).where(
        Event.guild_id == guild_id, Event.external_id == data["external_id"]
    )
    ev = session.execute(stmt).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    created = ev is None
    if created:
        ev = Event(guild_id=guild_id, external_id=data["external_id"])
        session.add(ev)
    for key in (
        "source_url", "title", "url", "starts_at", "time_known",
        "price_text", "summary", "image_url", "fingerprint",
    ):
        if key in data:
            setattr(ev, key, data[key])
    ev.last_seen_at = now
    ev.missed_refreshes = 0
    ev.is_active = True
    session.commit()
    return ev, created


def get_active_events(session: Session, guild_id: int) -> list[Event]:
    stmt = select(Event).where(Event.guild_id == guild_id, Event.is_active.is_(True))
    return list(session.execute(stmt).scalars())


def mark_absent_events_stale(session: Session, guild_id: int, seen_external_ids: set[str]) -> int:
    actives = get_active_events(session, guild_id)
    deactivated = 0
    for ev in actives:
        if ev.external_id not in seen_external_ids:
            ev.missed_refreshes += 1
            if ev.missed_refreshes >= 2:
                ev.is_active = False
                deactivated += 1
    session.commit()
    return deactivated


def get_board_post(session: Session, guild_id: int, board_type: str) -> BoardPost | None:
    stmt = select(BoardPost).where(
        BoardPost.guild_id == guild_id, BoardPost.board_type == board_type
    )
    return session.execute(stmt).scalar_one_or_none()


def save_board_post(session, guild_id, board_type, period_key, channel_id,
                    message_id, render_hash) -> BoardPost:
    post = get_board_post(session, guild_id, board_type)
    if post is None:
        post = BoardPost(guild_id=guild_id, board_type=board_type)
        session.add(post)
    post.period_key = period_key
    post.channel_id = channel_id
    post.message_id = message_id
    post.render_hash = render_hash
    post.updated_at = datetime.now(timezone.utc)
    session.commit()
    return post
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_repositories.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: repositories for config/events/boards"
```

---

### Task 4: Bot entry point + `/config` and `/source` commands

**Files:**
- Create: `src/bot.py`
- Create: `src/discord_ui/commands_admin.py`
- Test: `tests/test_admin_commands.py`

**Interfaces:**
- Consumes: `config.*`, repositories from Task 3
- Produces: `create_bot() -> commands.Bot`; `AdminCog` with `/config` and `/source` groups

- [ ] **Step 1: Write the failing test**

```python
# tests/test_admin_commands.py
from src.bot import create_bot


def test_bot_has_admin_commands():
    bot = create_bot()
    names = {c.name for c in bot.tree.get_commands()}
    assert "config" in names
    assert "source" in names
    assert "board" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_admin_commands.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.bot'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/bot.py
import logging

import discord
from discord.ext import commands

from src import config
from src.db.models import Base
from src.db.session import get_engine

log = logging.getLogger("hightide")


def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def setup_hook() -> None:
        engine = get_engine(config.DATABASE_URL)
        Base.metadata.create_all(engine)
        from src.discord_ui.commands_admin import AdminCog
        from src.discord_ui.commands_user import UserCog
        await bot.add_cog(AdminCog(bot))
        await bot.add_cog(UserCog(bot))
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
        log.info("commands synced")

    return bot


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = create_bot()
    bot.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()
```

```python
# src/discord_ui/commands_admin.py
import discord
from discord import app_commands
from discord.ext import commands

from src import config
from src.db.repositories import get_or_create_config
from src.db.session import get_session_factory


def _session():
    return get_session_factory(config.DATABASE_URL)()


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    config_group = app_commands.Group(
        name="config", description="Bot configuration",
        default_permissions=discord.Permissions(manage_guild=True),
    )
    source_group = app_commands.Group(
        name="source", description="Event sources",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @config_group.command(name="this_week_channel")
    async def set_week_channel(self, interaction: discord.Interaction,
                               channel: discord.TextChannel) -> None:
        session = _session()
        cfg = get_or_create_config(session, interaction.guild_id)
        cfg.this_week_channel_id = channel.id
        session.commit()
        await interaction.response.send_message(
            f"this-week channel set to {channel.mention}", ephemeral=True)

    @config_group.command(name="this_month_channel")
    async def set_month_channel(self, interaction: discord.Interaction,
                                channel: discord.TextChannel) -> None:
        session = _session()
        cfg = get_or_create_config(session, interaction.guild_id)
        cfg.this_month_channel_id = channel.id
        session.commit()
        await interaction.response.send_message(
            f"this-month channel set to {channel.mention}", ephemeral=True)

    @config_group.command(name="timezone")
    async def set_timezone(self, interaction: discord.Interaction, tz: str) -> None:
        session = _session()
        cfg = get_or_create_config(session, interaction.guild_id)
        cfg.timezone = tz
        session.commit()
        await interaction.response.send_message(f"timezone set to {tz}", ephemeral=True)

    @config_group.command(name="daily_refresh_time")
    async def set_refresh_time(self, interaction: discord.Interaction, hhmm: str) -> None:
        session = _session()
        cfg = get_or_create_config(session, interaction.guild_id)
        cfg.daily_refresh_time = hhmm
        session.commit()
        await interaction.response.send_message(
            f"daily refresh set to {hhmm}", ephemeral=True)

    @config_group.command(name="include_tba")
    async def set_include_tba(self, interaction: discord.Interaction, value: bool) -> None:
        session = _session()
        cfg = get_or_create_config(session, interaction.guild_id)
        cfg.include_tba_section = value
        session.commit()
        await interaction.response.send_message(
            f"include_tba_section = {value}", ephemeral=True)

    @source_group.command(name="list")
    async def source_list(self, interaction: discord.Interaction) -> None:
        from sqlalchemy import select
        from src.db.models import EventSource
        session = _session()
        rows = session.execute(select(EventSource).where(
            EventSource.guild_id == interaction.guild_id)).scalars().all()
        if not rows:
            await interaction.response.send_message("No sources configured.", ephemeral=True)
            return
        lines = [f"`{r.id}` {'✅' if r.enabled else '⛔'} {r.url}" for r in rows]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @source_group.command(name="add")
    async def source_add(self, interaction: discord.Interaction, url: str) -> None:
        from src.db.models import EventSource
        session = _session()
        session.add(EventSource(guild_id=interaction.guild_id, url=url, enabled=True))
        session.commit()
        await interaction.response.send_message(f"Added source: {url}", ephemeral=True)

    @source_group.command(name="remove")
    async def source_remove(self, interaction: discord.Interaction, source_id: int) -> None:
        from src.db.models import EventSource
        session = _session()
        row = session.get(EventSource, source_id)
        if row and row.guild_id == interaction.guild_id:
            session.delete(row)
            session.commit()
            await interaction.response.send_message(f"Removed source {source_id}",
                                                    ephemeral=True)
        else:
            await interaction.response.send_message("Source not found.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_admin_commands.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: bot entry point + /config and /source commands"
```

---

## Milestone 2 — Scraper

### Task 5: Normalization helpers

**Files:**
- Create: `src/scraping/normalize.py`
- Test: `tests/test_normalize.py`

**Interfaces:**
- Produces: `clean_title(html: str) -> str`; `parse_event_datetime(title: str, tz) -> tuple[datetime | None, bool]`; `extract_time(content_html: str) -> str | None`; `extract_price(content_html: str) -> str | None`; `fingerprint(*parts: str | None) -> str`; `strip_html(html: str) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_normalize.py
from zoneinfo import ZoneInfo

from src.scraping.normalize import (
    clean_title, extract_price, extract_time, fingerprint, parse_event_datetime,
)

TZ = ZoneInfo("Australia/Perth")


def test_clean_title_entities():
    assert clean_title("05.11.26 &#8211; Weekly Duels &#8211; Yu-Gi-Oh!") == \
        "05.11.26 – Weekly Duels – Yu-Gi-Oh!"


def test_parse_dot_prefix_two_digit_year():
    dt, known = parse_event_datetime("23.10.26 – Gundam TCG – Weekly", TZ)
    assert dt is not None and dt.year == 2026 and dt.month == 10 and dt.day == 23
    assert known is False  # date only, no time


def test_parse_dot_prefix_four_digit_year():
    dt, _ = parse_event_datetime("27.10.2026 – 7PH Tuesday – MTG", TZ)
    assert dt is not None and dt.year == 2026 and dt.month == 10 and dt.day == 27


def test_parse_natural_language_suffix():
    dt, _ = parse_event_datetime(
        "Friday Night Magic – Reality Fracture draft – Friday October 23rd 2026", TZ)
    assert dt is not None and dt.month == 10 and dt.day == 23 and dt.year == 2026


def test_parse_no_date_returns_none():
    dt, known = parse_event_datetime("Some Undated Thing", TZ)
    assert dt is None and known is False


def test_extract_time():
    html = '<p>→ Location: Hightide</p><p>→ Time: 6:30pm</p><p>→ Ticket: $10</p>'
    assert extract_time(html) == "6:30pm"


def test_extract_price():
    html = '<p>→ Time: 12 PM</p><p>→ Ticket: $15</p>'
    assert extract_price(html) == "$15"


def test_extract_price_free():
    assert extract_price('<p>→ Ticket: Free</p>') == "Free"


def test_fingerprint_stable():
    a = fingerprint("t", "2026-10-23", "$15", "sum")
    b = fingerprint("t", "2026-10-23", "$15", "sum")
    assert a == b and len(a) == 40
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_normalize.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/scraping/normalize.py
import hashlib
import html as html_lib
import re
from datetime import datetime

from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

_DOT_PREFIX = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})\b")
_NATURAL_DATE = re.compile(
    r"((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"→\s*Time:\s*(.+?)\s*(?:<|$)", re.IGNORECASE)
_TICKET_RE = re.compile(r"→\s*Ticket:\s*(.+?)\s*(?:<|$)", re.IGNORECASE)


def clean_title(raw: str) -> str:
    return html_lib.unescape(raw).strip()


def strip_html(raw: str) -> str:
    return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)


def parse_event_datetime(title: str, tz) -> tuple[datetime | None, bool]:
    m = _DOT_PREFIX.search(title)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        return datetime(year, month, day, tzinfo=tz), False
    m = _NATURAL_DATE.search(title)
    if m:
        try:
            dt = dateutil_parser.parse(m.group(0))
            return dt.replace(tzinfo=tz), False
        except (ValueError, OverflowError):
            return None, False
    return None, False


def extract_time(content_html: str) -> str | None:
    m = _TIME_RE.search(content_html)
    return m.group(1).strip() if m else None


def extract_price(content_html: str) -> str | None:
    m = _TICKET_RE.search(content_html)
    return m.group(1).strip() if m else None


def fingerprint(*parts: str | None) -> str:
    joined = "|".join(p or "" for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_normalize.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: normalization helpers (dates, price, fingerprint)"
```

---

### Task 6: WP REST fetcher + parser

**Files:**
- Create: `src/scraping/fetcher.py`
- Create: `src/scraping/hightide_parser.py`
- Test: `tests/test_parser.py`
- Fixture: `tests/fixtures/wp_products_events.json` (already captured)

**Interfaces:**
- Consumes: normalize helpers from Task 5
- Produces: `fetch_events(base_url: str) -> list[dict]` (raw product JSON across pages); `parse_product(product: dict, tz) -> NormalizedEvent`; `NormalizedEvent` dataclass with fields `external_id, title, url, starts_at, time_known, price_text, summary, image_url, fingerprint`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_parser.py
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from src.scraping.hightide_parser import parse_product

TZ = ZoneInfo("Australia/Perth")
FIXTURE = json.loads(
    Path("tests/fixtures/wp_products_events.json").read_text())


def _by_slug(slug_part: str):
    return next(p for p in FIXTURE if slug_part in p["slug"])


def test_external_id_format():
    p = _by_slug("05-11-26-weekly-duels")
    ev = parse_product(p, TZ)
    assert ev.external_id == f"wp:{p['id']}"


def test_dot_prefix_date_and_time_known():
    ev = parse_product(_by_slug("05-11-26-weekly-duels"), TZ)
    assert ev.starts_at is not None
    assert (ev.starts_at.year, ev.starts_at.month, ev.starts_at.day) == (2026, 11, 5)
    assert ev.time_known is True  # fixture content has → Time: 6:30pm
    assert ev.price_text == "$10"


def test_natural_language_title_date():
    ev = parse_product(_by_slug("friday-night-magic-countersculpt"), TZ)
    assert ev.starts_at is not None
    assert (ev.starts_at.month, ev.starts_at.day, ev.starts_at.year) == (10, 23, 2026)


def test_image_from_embed():
    ev = parse_product(_by_slug("05-11-26-weekly-duels"), TZ)
    assert ev.image_url is None or ev.image_url.startswith("https://")


def test_title_entities_cleaned():
    ev = parse_product(_by_slug("05-11-26-weekly-duels"), TZ)
    assert "&#8211;" not in ev.title
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_parser.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/scraping/fetcher.py
import logging

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger("hightide.fetcher")

UA = {"User-Agent": "hightide-events-bot/0.1 (+discord community bot)"}


class FetchError(Exception):
    pass


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def _get(client: httpx.Client, url: str, **params) -> httpx.Response:
    resp = client.get(url, params=params, headers=UA, timeout=15.0)
    resp.raise_for_status()
    return resp


def resolve_events_category_id(client: httpx.Client, base_url: str) -> int:
    resp = _get(client, f"{base_url}/wp-json/wp/v2/product_cat", slug="events")
    data = resp.json()
    if not data:
        raise FetchError("events category not found")
    return int(data[0]["id"])


def fetch_events(base_url: str) -> list[dict]:
    """Fetch all Events-category products across all pages."""
    with httpx.Client() as client:
        cat_id = resolve_events_category_id(client, base_url)
        products: list[dict] = []
        page = 1
        while True:
            resp = _get(
                client,
                f"{base_url}/wp-json/wp/v2/product",
                product_cat=cat_id, per_page=100, page=page, _embed="1",
            )
            products.extend(resp.json())
            total_pages = int(resp.headers.get("X-WP-TotalPages", "1"))
            if page >= total_pages:
                break
            page += 1
        log.info("fetched %d products from events category", len(products))
        return products
```

```python
# src/scraping/hightide_parser.py
from dataclasses import dataclass
from datetime import datetime

from dateutil import parser as dateutil_parser

from src.scraping.normalize import (
    clean_title, extract_price, extract_time, fingerprint, parse_event_datetime,
    strip_html,
)


@dataclass
class NormalizedEvent:
    external_id: str
    title: str
    url: str
    starts_at: datetime | None
    time_known: bool
    price_text: str | None
    summary: str | None
    image_url: str | None
    fingerprint: str


def _image_url(product: dict) -> str | None:
    embedded = product.get("_embedded", {})
    media = embedded.get("wp:featuredmedia") or []
    if media and isinstance(media, list):
        return media[0].get("source_url")
    return None


def parse_product(product: dict, tz) -> NormalizedEvent:
    title = clean_title(product["title"]["rendered"])
    content = product.get("content", {}).get("rendered", "")
    starts_at, _ = parse_event_datetime(title, tz)

    time_known = False
    time_str = extract_time(content)
    if starts_at is not None and time_str:
        try:
            t = dateutil_parser.parse(time_str).time()
            starts_at = starts_at.replace(hour=t.hour, minute=t.minute, second=0)
            time_known = True
        except (ValueError, OverflowError):
            pass

    price_text = extract_price(content)
    summary_text = strip_html(content) or None
    if summary_text and len(summary_text) > 280:
        summary_text = summary_text[:277] + "..."
    starts_iso = starts_at.isoformat() if starts_at else None
    fp = fingerprint(title, starts_iso, price_text, summary_text)

    return NormalizedEvent(
        external_id=f"wp:{product['id']}",
        title=title,
        url=product["link"],
        starts_at=starts_at,
        time_known=time_known,
        price_text=price_text,
        summary=summary_text,
        image_url=_image_url(product),
        fingerprint=fp,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_parser.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: WP REST fetcher + product parser"
```

---

### Task 7: Event service (upsert/dedupe/stale) + `/board preview`

**Files:**
- Create: `src/services/event_service.py`
- Create: `src/discord_ui/embeds.py`
- Modify: `src/discord_ui/commands_admin.py` (add `/board preview`)
- Test: `tests/test_event_dedupe.py`

**Interfaces:**
- Consumes: fetcher/parser (Task 6), repositories (Task 3)
- Produces: `refresh_events(session, guild_id, base_url, tz) -> dict` (stats: fetched, created, updated, deactivated); `build_board_embed(events, title, tz, include_tba) -> discord.Embed`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_event_dedupe.py
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from src.db.models import Base
from src.db.repositories import get_active_events
from src.db.session import get_engine, get_session_factory
from src.scraping.hightide_parser import parse_product
from src.services.event_service import upsert_products

TZ = ZoneInfo("Australia/Perth")
FIXTURE = json.loads(Path("tests/fixtures/wp_products_events.json").read_text())


def _session():
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = get_session_factory("sqlite:///:memory:")
    factory.configure(bind=engine)
    return factory()


def test_upsert_all_fixture_events():
    s = _session()
    stats = upsert_products(s, guild_id=1, products=FIXTURE, tz=TZ)
    assert stats["created"] == len(FIXTURE)
    assert len(get_active_events(s, 1)) == len(FIXTURE)


def test_upsert_is_idempotent():
    s = _session()
    upsert_products(s, 1, FIXTURE, TZ)
    stats = upsert_products(s, 1, FIXTURE, TZ)
    assert stats["created"] == 0
    assert stats["updated"] == len(FIXTURE)
    assert len(get_active_events(s, 1)) == len(FIXTURE)


def test_stale_deactivation():
    s = _session()
    upsert_products(s, 1, FIXTURE, TZ)
    upsert_products(s, 1, FIXTURE[:5], TZ)  # miss 1
    stats = upsert_products(s, 1, FIXTURE[:5], TZ)  # miss 2
    assert stats["deactivated"] == len(FIXTURE) - 5
    assert len(get_active_events(s, 1)) == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_event_dedupe.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/services/event_service.py
import logging

from sqlalchemy.orm import Session

from src.db.repositories import (
    get_active_events, mark_absent_events_stale, upsert_event,
)
from src.scraping.fetcher import fetch_events
from src.scraping.hightide_parser import parse_product

log = logging.getLogger("hightide.events")


def upsert_products(session: Session, guild_id: int, products: list[dict], tz) -> dict:
    created = updated = 0
    seen: set[str] = set()
    for product in products:
        ev = parse_product(product, tz)
        seen.add(ev.external_id)
        _, was_created = upsert_event(session, guild_id, {
            "external_id": ev.external_id,
            "title": ev.title,
            "url": ev.url,
            "starts_at": ev.starts_at,
            "time_known": ev.time_known,
            "price_text": ev.price_text,
            "summary": ev.summary,
            "image_url": ev.image_url,
            "fingerprint": ev.fingerprint,
        })
        if was_created:
            created += 1
        else:
            updated += 1
    deactivated = mark_absent_events_stale(session, guild_id, seen)
    stats = {
        "fetched": len(products), "created": created,
        "updated": updated, "deactivated": deactivated,
    }
    log.info("upsert complete: %s", stats)
    return stats


def refresh_events(session: Session, guild_id: int, base_url: str, tz) -> dict:
    products = fetch_events(base_url)
    return upsert_products(session, guild_id, products, tz)
```

```python
# src/discord_ui/embeds.py
from collections import defaultdict
from datetime import datetime

import discord

from src.db.models import Event


def build_board_embed(events: list[Event], title: str, tz,
                      include_tba: bool) -> discord.Embed:
    embed = discord.Embed(title=title, color=0x1F8B4C)
    dated = [e for e in events if e.starts_at is not None]
    tba = [e for e in events if e.starts_at is None]

    by_day: dict[datetime.date, list[Event]] = defaultdict(list)
    for e in dated:
        local = e.starts_at.astimezone(tz)
        by_day[local.date()].append(e)

    if not dated and not (include_tba and tba):
        embed.description = "No events found for this period."

    for day in sorted(by_day):
        lines = []
        for e in sorted(by_day[day], key=lambda x: x.starts_at):
            local = e.starts_at.astimezone(tz)
            time_part = local.strftime("%H:%M") if e.time_known else "time TBC"
            price_part = f" · {e.price_text}" if e.price_text else ""
            lines.append(f"`{time_part}` [{e.title}]({e.url}){price_part}")
        embed.add_field(name=day.strftime("%a %d %b %Y"),
                        value="\n".join(lines), inline=False)

    if include_tba and tba:
        lines = [f"[{e.title}]({e.url})" for e in tba]
        embed.add_field(name="Date TBA", value="\n".join(lines), inline=False)

    embed.set_footer(text=f"Last updated {datetime.now(tz).strftime('%H:%M')} AWST")
    return embed
```

Add to `AdminCog` in `src/discord_ui/commands_admin.py`:

```python
    board_group = app_commands.Group(
        name="board", description="Event boards",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @board_group.command(name="preview")
    @app_commands.choices(board_type=[
        app_commands.Choice(name="this_week", value="this_week"),
        app_commands.Choice(name="this_month", value="this_month"),
    ])
    async def board_preview(self, interaction: discord.Interaction,
                            board_type: str) -> None:
        from zoneinfo import ZoneInfo
        from src.db.repositories import get_active_events, get_or_create_config
        from src.discord_ui.embeds import build_board_embed
        from src.services.board_service import filter_events_for_board

        session = _session()
        cfg = get_or_create_config(session, interaction.guild_id)
        tz = ZoneInfo(cfg.timezone)
        events = filter_events_for_board(
            get_active_events(session, interaction.guild_id), board_type, cfg, tz)
        embed = build_board_embed(events, board_type.replace("_", " ").title(),
                                  tz, cfg.include_tba_section)
        await interaction.response.send_message(embed=embed, ephemeral=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_event_dedupe.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: event service + board preview embed"
```

---

## Milestone 3 — Board system

### Task 8: Board service (windowing, hashing, create/edit)

**Files:**
- Create: `src/services/board_service.py`
- Test: `tests/test_board_render.py`

**Interfaces:**
- Consumes: repositories, embeds
- Produces: `filter_events_for_board(events, board_type, cfg, tz) -> list[Event]`; `render_board_hash(events, board_type, cfg, tz) -> str`; `upsert_board(bot, session, guild_id, board_type) -> str` (returns action: `created|edited|skipped|no_channel`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_board_render.py
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.db.models import Event
from src.services.board_service import filter_events_for_board, render_board_hash

TZ = ZoneInfo("Australia/Perth")


def _cfg(**kw):
    base = dict(week_start_mode="today_start", include_tba_section=True)
    base.update(kw)
    return SimpleNamespace(**base)


def _ev(title, days_ahead, time_known=True):
    return Event(
        guild_id=1, external_id=f"wp:{title}", title=title, url="https://x",
        starts_at=datetime.now(TZ) + timedelta(days=days_ahead),
        time_known=time_known, is_active=True,
    )


def test_week_window_includes_today_and_6_days():
    events = [_ev("today", 0), _ev("day6", 6), _ev("day8", 8)]
    got = filter_events_for_board(events, "this_week", _cfg(), TZ)
    titles = {e.title for e in got}
    assert "today" in titles and "day6" in titles and "day8" not in titles


def test_month_window_current_month_only():
    now = datetime.now(TZ)
    in_month = _ev("in", 0)
    # force an event into next month
    next_month = Event(
        guild_id=1, external_id="wp:out", title="out", url="u",
        starts_at=(now.replace(day=28) + timedelta(days=10)), is_active=True,
    )
    got = filter_events_for_board([in_month, next_month], "this_month", _cfg(), TZ)
    titles = {e.title for e in got}
    assert "in" in titles and "out" not in titles


def test_tba_included_only_when_enabled():
    tba = Event(guild_id=1, external_id="wp:tba", title="tba", url="u",
                starts_at=None, is_active=True)
    assert filter_events_for_board([tba], "this_week", _cfg(), TZ) == [tba]
    assert filter_events_for_board(
        [tba], "this_week", _cfg(include_tba_section=False), TZ) == []


def test_render_hash_changes_with_content():
    a = [_ev("x", 1)]
    b = [_ev("y", 1)]
    assert render_board_hash(a, "this_week", _cfg(), TZ) != \
        render_board_hash(b, "this_week", _cfg(), TZ)
    assert render_board_hash(a, "this_week", _cfg(), TZ) == \
        render_board_hash(a, "this_week", _cfg(), TZ)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_board_render.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# src/services/board_service.py
import hashlib
import logging
from datetime import datetime, time, timedelta

import discord
from sqlalchemy.orm import Session

from src.db.models import Event
from src.db.repositories import (
    get_active_events, get_board_post, get_or_create_config, save_board_post,
)
from src.discord_ui.embeds import build_board_embed

log = logging.getLogger("hightide.board")


def _window(board_type: str, cfg, tz) -> tuple[datetime, datetime]:
    now = datetime.now(tz)
    if board_type == "this_week":
        if cfg.week_start_mode == "today_start":
            start = datetime.combine(now.date(), time.min, tzinfo=tz)
        else:
            start = now
        return start, start + timedelta(days=7)
    # this_month
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def period_key(board_type: str, tz) -> str:
    now = datetime.now(tz)
    if board_type == "this_month":
        return now.strftime("%Y-%m")
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def filter_events_for_board(events: list[Event], board_type: str, cfg, tz) -> list[Event]:
    start, end = _window(board_type, cfg, tz)
    out = []
    for e in events:
        if e.starts_at is None:
            if cfg.include_tba_section:
                out.append(e)
            continue
        local = e.starts_at.astimezone(tz)
        if start <= local < end:
            out.append(e)
    return out


def render_board_hash(events: list[Event], board_type: str, cfg, tz) -> str:
    parts = [board_type]
    for e in sorted(events, key=lambda x: (x.starts_at or datetime.max.replace(tzinfo=tz), x.title)):
        parts.append(f"{e.external_id}|{e.title}|{e.starts_at}|{e.price_text}|{e.time_known}")
    return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()


async def upsert_board(bot, session: Session, guild_id: int, board_type: str) -> str:
    cfg = get_or_create_config(session, guild_id)
    tz_channel = cfg.this_week_channel_id if board_type == "this_week" else cfg.this_month_channel_id
    if not tz_channel:
        return "no_channel"
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(cfg.timezone)
    events = filter_events_for_board(get_active_events(session, guild_id), board_type, cfg, tz)
    new_hash = render_board_hash(events, board_type, cfg, tz)
    key = period_key(board_type, tz)
    title = "This Week at Hightide" if board_type == "this_week" else "This Month at Hightide"
    embed = build_board_embed(events, title, tz, cfg.include_tba_section)

    channel = bot.get_channel(tz_channel) or await bot.fetch_channel(tz_channel)
    post = get_board_post(session, guild_id, board_type)

    if post and post.render_hash == new_hash and post.period_key == key:
        return "skipped"

    # try to edit existing
    if post:
        try:
            msg = await channel.fetch_message(post.message_id)
            await msg.edit(embed=embed)
            save_board_post(session, guild_id, board_type, key,
                            channel.id, msg.id, new_hash)
            return "edited"
        except discord.NotFound:
            log.info("board message missing for %s, recreating", board_type)

    msg = await channel.send(embed=embed)
    save_board_post(session, guild_id, board_type, key, channel.id, msg.id, new_hash)
    return "created"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_board_render.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: board service (windowing, hashing, create/edit)"
```

---

### Task 9: `/board refresh`, `/board recreate` + scheduler loop

**Files:**
- Create: `src/services/scheduler.py`
- Modify: `src/discord_ui/commands_admin.py` (add refresh/recreate)
- Modify: `src/bot.py` (start scheduler)
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: board_service, event_service
- Produces: `RefreshScheduler` (discord.ext.tasks loop, 60s tick); `run_daily_refresh(bot, guild_id) -> dict`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler.py
from src.services.scheduler import should_run_now


def test_should_run_now_at_configured_time():
    assert should_run_now("09:00", 9, 0) is True
    assert should_run_now("09:00", 9, 1) is False
    assert should_run_now("9:05", 9, 5) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_scheduler.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

```python
# src/services/scheduler.py
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from discord.ext import tasks

from src import config
from src.db.repositories import get_or_create_config
from src.db.session import get_session_factory
from src.services.board_service import upsert_board
from src.services.event_service import refresh_events

log = logging.getLogger("hightide.scheduler")


def should_run_now(hhmm: str, hour: int, minute: int) -> bool:
    try:
        h, m = (int(p) for p in hhmm.split(":"))
    except ValueError:
        return False
    return (hour, minute) == (h, m)


async def run_daily_refresh(bot, guild_id: int) -> dict:
    session = get_session_factory(config.DATABASE_URL)()
    cfg = get_or_create_config(session, guild_id)
    tz = ZoneInfo(cfg.timezone)
    stats = refresh_events(session, guild_id, "https://hightidetabletop.com.au", tz)
    week = await upsert_board(bot, session, guild_id, "this_week")
    month = await upsert_board(bot, session, guild_id, "this_month")
    result = {"events": stats, "this_week": week, "this_month": month}
    log.info("daily refresh: %s", result)
    return result


class RefreshScheduler:
    def __init__(self, bot):
        self.bot = bot
        self._last_run_key: str | None = None

    def start(self):
        self.tick.start()

    @tasks.loop(seconds=60)
    async def tick(self):
        await self.bot.wait_until_ready()
        session = get_session_factory(config.DATABASE_URL)()
        try:
            cfg = get_or_create_config(session, config.GUILD_ID or 0)
            tz = ZoneInfo(cfg.timezone)
            now = datetime.now(tz)
            run_key = now.strftime("%Y-%m-%d")
            if (should_run_now(cfg.daily_refresh_time, now.hour, now.minute)
                    and self._last_run_key != run_key):
                self._last_run_key = run_key
                await run_daily_refresh(self.bot, cfg.guild_id)
        except Exception:
            log.exception("scheduler tick failed")
        finally:
            session.close()
```

Wire into `src/bot.py` `setup_hook`:

```python
        from src.services.scheduler import RefreshScheduler
        RefreshScheduler(bot).start()
```

Add to `AdminCog` board_group in `src/discord_ui/commands_admin.py`:

```python
    @board_group.command(name="refresh")
    async def board_refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        from src.services.scheduler import run_daily_refresh
        result = await run_daily_refresh(self.bot, interaction.guild_id)
        await interaction.followup.send(f"Refresh complete: `{result}`", ephemeral=True)

    @board_group.command(name="recreate")
    @app_commands.choices(board_type=[
        app_commands.Choice(name="this_week", value="this_week"),
        app_commands.Choice(name="this_month", value="this_month"),
    ])
    async def board_recreate(self, interaction: discord.Interaction,
                             board_type: str) -> None:
        from src.db.repositories import get_board_post
        session = _session()
        post = get_board_post(session, interaction.guild_id, board_type)
        if post:
            session.delete(post)
            session.commit()
        from src.services.board_service import upsert_board
        action = await upsert_board(self.bot, session, interaction.guild_id, board_type)
        await interaction.response.send_message(
            f"{board_type} board action: {action}", ephemeral=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: /board refresh & recreate + daily scheduler"
```

---

## Milestone 4 — Month rollover

### Task 10: Month rollover + previous-month deletion

**Files:**
- Modify: `src/services/board_service.py` (rollover logic in `upsert_board`)
- Test: `tests/test_month_rollover.py`

**Interfaces:**
- Consumes: board_service from Task 8
- Produces: `upsert_board` additionally returns `recreated` on month rollover; deletes prior monthly message when `delete_previous_month_message` is true

- [ ] **Step 1: Write the failing test**

```python
# tests/test_month_rollover.py
from types import SimpleNamespace

from src.services.board_service import is_new_period


def test_new_period_detects_month_change():
    cfg = SimpleNamespace()
    assert is_new_period("this_month", "2026-09", "2026-10") is True
    assert is_new_period("this_month", "2026-10", "2026-10") is False
    assert is_new_period("this_month", None, "2026-10") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_month_rollover.py -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write minimal implementation**

Add to `src/services/board_service.py`:

```python
def is_new_period(board_type: str, stored_key: str | None, current_key: str) -> bool:
    return stored_key != current_key
```

In `upsert_board`, before editing, add rollover handling:

```python
    if board_type == "this_month" and post and is_new_period(
            board_type, post.period_key, key):
        # month rollover: create fresh message, delete previous
        msg = await channel.send(embed=embed)
        if cfg.delete_previous_month_message:
            try:
                old = await channel.fetch_message(post.message_id)
                await old.delete()
            except discord.NotFound:
                pass
        save_board_post(session, guild_id, board_type, key,
                        channel.id, msg.id, new_hash)
        return "recreated"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_month_rollover.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: month rollover with previous-month deletion"
```

---

## Milestone 5 — Hardening

### Task 11: `/events upcoming` user command + logging polish + full test pass

**Files:**
- Create: `src/discord_ui/commands_user.py`
- Test: `tests/test_user_commands.py`

**Interfaces:**
- Produces: `UserCog` with `/events upcoming` (ephemeral)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_commands.py
from src.bot import create_bot


def test_user_commands_registered():
    bot = create_bot()
    names = {c.name for c in bot.tree.get_commands()}
    assert "events" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_user_commands.py -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

```python
# src/discord_ui/commands_user.py
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from src import config
from src.db.repositories import get_active_events, get_or_create_config
from src.db.session import get_session_factory
from src.discord_ui.embeds import build_board_embed
from src.services.board_service import filter_events_for_board


class UserCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="events", description="Upcoming events")
    async def events_upcoming(self, interaction: discord.Interaction) -> None:
        session = get_session_factory(config.DATABASE_URL)()
        cfg = get_or_create_config(session, interaction.guild_id)
        tz = ZoneInfo(cfg.timezone)
        events = filter_events_for_board(
            get_active_events(session, interaction.guild_id),
            "this_week", cfg, tz)
        embed = build_board_embed(events, "Upcoming Events", tz,
                                  cfg.include_tba_section)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(UserCog(bot))
```

- [ ] **Step 4: Run full test suite**

Run: `.venv/bin/python -m pytest -v`
Expected: ALL PASS (Task 1–11 tests)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: /events upcoming + hardening"
```

---

### Task 12: Deploy polish — README + env example + ROADMAP

**Files:**
- Create: `README.md`
- Create: `.env.example`
- Create: `ROADMAP.md`
- Create: `.gitignore`

- [ ] **Step 1: Write files**

`.gitignore`:

```gitignore
.venv/
__pycache__/
*.pyc
*.db
.env
```

`.env.example`:

```dotenv
DISCORD_TOKEN=your-bot-token
DATABASE_URL=sqlite:///./hightide.db
DEFAULT_TIMEZONE=Australia/Perth
GUILD_ID=
```

`README.md`: setup steps (venv, `pip install -e '.[dev]'`, set env, `python -m src.bot`), invite permissions, command overview, deploy note for Railway/Render worker.

`ROADMAP.md`: milestones 1–5 with completion checkboxes.

- [ ] **Step 2: Final full test run**

Run: `.venv/bin/python -m pytest -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "docs: README, env example, roadmap"
```
