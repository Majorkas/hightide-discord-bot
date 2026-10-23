from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from src.bot import create_bot
from src.db.models import Base, Event, GuildConfig
from src.db.session import get_engine, get_session_factory
from src.discord_ui import commands_user
from src.discord_ui.commands_user import UserCog

GUILD = 12345


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    monkeypatch.setattr(commands_user, "_session_factory", factory)
    yield factory
    engine.dispose()
    factory.kw["bind"].dispose()


@pytest.fixture
def cog():
    return UserCog(MagicMock())


@pytest.fixture
def interaction():
    i = MagicMock()
    i.guild_id = GUILD
    i.response.send_message = AsyncMock()
    return i


def test_user_commands_registered():
    bot = create_bot()
    names = {c.name for c in bot.tree.get_commands()}
    assert "events" in names


async def test_events_upcoming_sends_ephemeral_embed(db, cog, interaction):
    tz = ZoneInfo("Australia/Perth")
    with db() as s:
        s.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth"))
        s.add(Event(
            guild_id=GUILD, external_id="wp:test", title="Test Event",
            url="https://example.com",
            starts_at=datetime.now(tz) + timedelta(days=2),
            time_known=True, is_active=True,
        ))
        s.commit()
    await cog.events_upcoming.callback(cog, interaction)
    interaction.response.send_message.assert_awaited_once()
    _, kwargs = interaction.response.send_message.call_args
    assert kwargs["ephemeral"] is True
    assert isinstance(kwargs["embed"], discord.Embed)
    embed = kwargs["embed"]
    assert embed.title == "Upcoming Events"
    assert embed.description is None
    assert any("Test Event" in f.value for f in embed.fields)


async def test_events_upcoming_failure_is_ephemeral(db, cog, interaction, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(commands_user, "get_active_events", boom)
    await cog.events_upcoming.callback(cog, interaction)
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs["ephemeral"] is True
    assert args[0] == "Could not load events right now — try again later."
    assert "RuntimeError" not in args[0]
    assert "db down" not in args[0]


def _seed(db, events, **cfg_kw):
    with db() as s:
        s.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth", **cfg_kw))
        for e in events:
            s.add(e)
        s.commit()


def _ev(title, starts_at, guild=GUILD):
    return Event(guild_id=guild, external_id=f"wp:{title}", title=title,
                 url="https://example.com", starts_at=starts_at,
                 time_known=True, is_active=True)


async def _embed_text(cog, interaction):
    await cog.events_upcoming.callback(cog, interaction)
    _, kwargs = interaction.response.send_message.call_args
    embed = kwargs["embed"]
    return " ".join(f"{f.name} {f.value}" for f in embed.fields)


async def test_events_upcoming_is_unwindowed_from_today(db, cog, interaction):
    tz = ZoneInfo("Australia/Perth")
    now = datetime.now(tz)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    _seed(db, [
        _ev("YesterdayEvt", now - timedelta(days=1)),
        _ev("TodayEvt", today_start),
        _ev("ThreeWeeksEvt", now + timedelta(weeks=3)),
    ])
    text = await _embed_text(cog, interaction)
    assert "TodayEvt" in text
    assert "ThreeWeeksEvt" in text
    assert "YesterdayEvt" not in text


async def test_events_upcoming_tba_only_when_enabled(db, cog, interaction):
    _seed(db, [_ev("UndatedEvt", None)], include_tba_section=True)
    assert "UndatedEvt" in await _embed_text(cog, interaction)


async def test_events_upcoming_tba_hidden_when_disabled(db, cog, interaction):
    _seed(db, [_ev("UndatedEvt", None)], include_tba_section=False)
    assert "UndatedEvt" not in await _embed_text(cog, interaction)


async def test_events_upcoming_ignores_week_start_mode(db, cog, interaction):
    tz = ZoneInfo("Australia/Perth")
    _seed(db, [_ev("FarEvt", datetime.now(tz) + timedelta(days=30))],
          week_start_mode="now")
    assert "FarEvt" in await _embed_text(cog, interaction)


async def test_events_upcoming_guild_isolation(db, cog, interaction):
    tz = ZoneInfo("Australia/Perth")
    soon = datetime.now(tz) + timedelta(days=2)
    _seed(db, [_ev("Mine", soon), _ev("Theirs", soon, guild=GUILD + 1)])
    text = await _embed_text(cog, interaction)
    assert "Mine" in text and "Theirs" not in text
