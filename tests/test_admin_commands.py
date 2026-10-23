from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import select

from src.bot import create_bot
from src.db.models import Base, EventSource, GuildConfig
from src.db.session import get_engine, get_session_factory
from src.discord_ui import commands_admin
from src.discord_ui.commands_admin import AdminCog

GUILD = 12345


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    monkeypatch.setattr(commands_admin, "_session_factory", factory)
    yield factory
    engine.dispose()
    factory.kw["bind"].dispose()


@pytest.fixture
def cog():
    return AdminCog(MagicMock())


@pytest.fixture
def interaction():
    i = MagicMock()
    i.guild_id = GUILD
    i.response.send_message = AsyncMock()
    return i


def _reply(interaction) -> str:
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    return args[0]


def _config(db):
    with db() as s:
        return s.get(GuildConfig, GUILD)


def _sources(db):
    with db() as s:
        return s.execute(select(EventSource)).scalars().all()


def test_command_surface():
    bot = create_bot()
    groups = {c.name: c for c in bot.tree.get_commands()}
    assert set(groups) == {"config", "source", "board"}
    assert {c.name for c in groups["config"].commands} == {
        "this_week_channel", "this_month_channel", "timezone",
        "daily_refresh_time", "include_tba",
    }
    assert {c.name for c in groups["source"].commands} == {"list", "add", "remove"}
    assert {c.name for c in groups["board"].commands} == {"preview"}
    for g in groups.values():
        assert g.default_permissions == discord.Permissions(manage_guild=True)
        assert g.guild_only is True


async def test_set_week_channel(db, cog, interaction):
    channel = SimpleNamespace(id=999, mention="<#999>")
    await cog.set_week_channel.callback(cog, interaction, channel)
    assert _config(db).this_week_channel_id == 999
    assert "<#999>" in _reply(interaction)


async def test_set_month_channel(db, cog, interaction):
    channel = SimpleNamespace(id=888, mention="<#888>")
    await cog.set_month_channel.callback(cog, interaction, channel)
    assert _config(db).this_month_channel_id == 888
    assert "<#888>" in _reply(interaction)


async def test_set_timezone(db, cog, interaction):
    await cog.set_timezone.callback(cog, interaction, "Europe/London")
    assert _config(db).timezone == "Europe/London"
    assert "Europe/London" in _reply(interaction)


@pytest.mark.parametrize("tz", ["Mars/Olympus", "", "../etc/passwd"])
async def test_set_timezone_invalid(db, cog, interaction, tz):
    await cog.set_timezone.callback(cog, interaction, tz)
    assert _config(db) is None
    assert "timezone" in _reply(interaction).lower()


async def test_set_refresh_time(db, cog, interaction):
    await cog.set_refresh_time.callback(cog, interaction, "07:45")
    assert _config(db).daily_refresh_time == "07:45"
    assert "07:45" in _reply(interaction)


@pytest.mark.parametrize("value", ["9:00", "24:00", "12:60", "0900", "09:00:00", "ab:cd", ""])
async def test_set_refresh_time_invalid(db, cog, interaction, value):
    await cog.set_refresh_time.callback(cog, interaction, value)
    assert _config(db) is None
    assert "HH:MM" in _reply(interaction)


async def test_set_include_tba(db, cog, interaction):
    await cog.set_include_tba.callback(cog, interaction, False)
    assert _config(db).include_tba_section is False
    assert "False" in _reply(interaction)


async def test_source_add(db, cog, interaction):
    await cog.source_add.callback(cog, interaction, "https://example.com/events")
    rows = _sources(db)
    assert len(rows) == 1
    assert rows[0].url == "https://example.com/events"
    assert rows[0].guild_id == GUILD
    assert rows[0].enabled is True
    assert "https://example.com/events" in _reply(interaction)


@pytest.mark.parametrize("url", [
    "ftp://example.com", "example.com", "https://", "javascript:alert(1)", "",
    "https://example.com/" + "a" * 500,
])
async def test_source_add_invalid(db, cog, interaction, url):
    await cog.source_add.callback(cog, interaction, url)
    assert _sources(db) == []
    assert "Invalid URL" in _reply(interaction)


async def test_source_list_empty(db, cog, interaction):
    await cog.source_list.callback(cog, interaction)
    assert "No sources" in _reply(interaction)


async def test_source_list_populated(db, cog, interaction):
    with db() as s:
        s.add(EventSource(guild_id=GUILD, url="https://a.example", enabled=True))
        s.add(EventSource(guild_id=GUILD, url="https://b.example", enabled=False))
        s.add(EventSource(guild_id=999, url="https://other.example", enabled=True))
        s.commit()
    await cog.source_list.callback(cog, interaction)
    text = _reply(interaction)
    assert "https://a.example" in text
    assert "https://b.example" in text
    assert "other.example" not in text


async def test_source_remove(db, cog, interaction):
    with db() as s:
        row = EventSource(guild_id=GUILD, url="https://a.example", enabled=True)
        s.add(row)
        s.commit()
        sid = row.id
    await cog.source_remove.callback(cog, interaction, sid)
    assert _sources(db) == []
    assert "Removed" in _reply(interaction)


async def test_source_remove_wrong_guild(db, cog, interaction):
    with db() as s:
        row = EventSource(guild_id=999, url="https://a.example", enabled=True)
        s.add(row)
        s.commit()
        sid = row.id
    await cog.source_remove.callback(cog, interaction, sid)
    assert len(_sources(db)) == 1
    assert "not found" in _reply(interaction)


async def test_source_remove_missing_id(db, cog, interaction):
    await cog.source_remove.callback(cog, interaction, 4242)
    assert "not found" in _reply(interaction)
