from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest
from sqlalchemy import select

from src.bot import create_bot
from src.db.models import Base, Event, EventSource, GuildConfig
from src.db.session import get_engine, get_session_factory
from src.discord_ui import commands_admin
from src.discord_ui.commands_admin import AdminCog

GUILD = 12345
BOT_ID = 777


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
    assert set(groups) == {"config", "source", "board", "events"}
    assert {c.name for c in groups["config"].commands} == {
        "this_week_channel", "this_month_channel", "timezone",
        "daily_refresh_time", "include_tba",
    }
    assert {c.name for c in groups["source"].commands} == {"list", "add", "remove"}
    assert {c.name for c in groups["board"].commands} == {"preview", "refresh", "recreate"}
    for name in ("config", "source", "board"):
        assert groups[name].default_permissions == discord.Permissions(manage_guild=True)
    assert groups["events"].default_permissions is None
    assert {c.name for c in groups["events"].commands} == {"upcoming"}
    for g in groups.values():
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


async def test_board_preview(db, cog, interaction):
    tz = ZoneInfo("Australia/Perth")
    with db() as s:
        # Create config and active event
        cfg = GuildConfig(guild_id=GUILD, timezone="Australia/Perth")
        s.add(cfg)
        event = Event(
            guild_id=GUILD,
            external_id="wp:test",
            title="Test Event",
            url="https://example.com",
            starts_at=datetime.now(tz) + timedelta(days=1),
            time_known=True,
            is_active=True,
        )
        s.add(event)
        s.commit()
    await cog.board_preview.callback(cog, interaction, "this_week")
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "embed" in kwargs
    assert isinstance(kwargs["embed"], discord.Embed)


async def test_board_refresh_success(db, cog, interaction, monkeypatch):
    """Test /board refresh success path sends a followup with refresh stats."""
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    mock_refresh = AsyncMock(return_value={"events": {"added": 1}, "this_week": "created", "this_month": "skipped"})
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    await cog.board_refresh.callback(cog, interaction)

    # Verify defer was called first
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    mock_refresh.assert_awaited_once_with(cog.bot, GUILD)

    # Verify followup was sent with success message
    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert "Refresh complete" in args[0]
    assert kwargs.get("ephemeral") is True


async def test_board_refresh_error(db, cog, interaction, monkeypatch):
    """Test /board refresh error path sends failure followup without propagating."""
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    mock_refresh = AsyncMock(side_effect=RuntimeError("Test error"))
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    # Should not raise
    await cog.board_refresh.callback(cog, interaction)

    # Verify defer was called
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)

    # Verify error followup was sent
    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert "Refresh failed" in args[0]
    assert "RuntimeError" in args[0]
    assert "Test error" not in args[0]
    assert kwargs.get("ephemeral") is True


async def test_board_recreate_with_existing_post(db, cog, interaction, monkeypatch):
    """Test /board recreate deletes old message and DB row, then upserts."""
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # Seed DB with a board post
    from src.db.models import BoardPost
    with db() as s:
        post = BoardPost(
            guild_id=GUILD, board_type="this_week",
            period_key="2026-W43", channel_id=111, message_id=555, render_hash="old"
        )
        s.add(post)
        s.commit()

    # Mock Discord message fetch and delete
    mock_channel = MagicMock()
    mock_message = AsyncMock()
    mock_message.author.id = BOT_ID
    mock_message.delete = AsyncMock()
    mock_channel.fetch_message = AsyncMock(return_value=mock_message)
    cog.bot.get_channel = MagicMock(return_value=mock_channel)
    cog.bot.user.id = BOT_ID

    # Mock upsert_board
    mock_upsert = AsyncMock(return_value="created")
    monkeypatch.setattr("src.services.board_service.upsert_board", mock_upsert)

    await cog.board_recreate.callback(cog, interaction, "this_week")

    # Verify defer was called
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)

    # Verify old message was deleted
    mock_channel.fetch_message.assert_awaited_once_with(555)
    mock_message.delete.assert_awaited_once()

    # Verify DB row was deleted
    with db() as s:
        from src.db.repositories import get_board_post
        post = get_board_post(s, GUILD, "this_week")
        assert post is None

    # Verify upsert was called
    mock_upsert.assert_awaited_once()

    # Verify followup was sent
    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert "this_week" in args[0]
    assert "created" in args[0]
    assert kwargs.get("ephemeral") is True


async def test_board_recreate_message_not_found(db, cog, interaction, monkeypatch):
    """Test /board recreate handles discord.NotFound when deleting old message."""
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # Seed DB with a board post
    from src.db.models import BoardPost
    with db() as s:
        post = BoardPost(
            guild_id=GUILD, board_type="this_week",
            period_key="2026-W43", channel_id=111, message_id=555, render_hash="old"
        )
        s.add(post)
        s.commit()

    # Mock Discord message fetch to raise NotFound
    mock_channel = MagicMock()
    mock_response = MagicMock()
    mock_response.status = 404
    mock_response.reason = 'Not Found'
    mock_channel.fetch_message = AsyncMock(side_effect=discord.NotFound(mock_response, "message not found"))
    cog.bot.get_channel = MagicMock(return_value=mock_channel)

    # Mock upsert_board
    mock_upsert = AsyncMock(return_value="created")
    monkeypatch.setattr("src.services.board_service.upsert_board", mock_upsert)

    # Should not raise even though message is missing
    await cog.board_recreate.callback(cog, interaction, "this_week")

    # DB row should still be deleted
    with db() as s:
        from src.db.repositories import get_board_post
        post = get_board_post(s, GUILD, "this_week")
        assert post is None

    # Upsert should still be called
    mock_upsert.assert_awaited_once()

    # Followup should be sent
    interaction.followup.send.assert_awaited_once()


async def test_board_recreate_no_existing_post(db, cog, interaction, monkeypatch):
    """Test /board recreate skips deletion when no prior post exists."""
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # No prior post in DB

    # Mock upsert_board
    mock_upsert = AsyncMock(return_value="created")
    monkeypatch.setattr("src.services.board_service.upsert_board", mock_upsert)

    await cog.board_recreate.callback(cog, interaction, "this_month")

    # Upsert should be called
    mock_upsert.assert_awaited_once()

    # Followup should be sent
    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert "this_month" in args[0]
    assert kwargs.get("ephemeral") is True


# --- /board recreate hardening ---

def _seed_post(db):
    from src.db.models import BoardPost
    with db() as s:
        s.add(BoardPost(guild_id=GUILD, board_type="this_week", period_key="2026-W43",
                        channel_id=111, message_id=555, render_hash="old"))
        s.commit()


def _post_exists(db) -> bool:
    from src.db.repositories import get_board_post
    with db() as s:
        return get_board_post(s, GUILD, "this_week") is not None


def _recreate_setup(db, cog, interaction, monkeypatch, upsert=None, author_id=BOT_ID):
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    _seed_post(db)
    message = AsyncMock()
    message.author.id = author_id
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    cog.bot.get_channel = MagicMock(return_value=channel)
    cog.bot.fetch_channel = AsyncMock(return_value=channel)
    cog.bot.user.id = BOT_ID
    upsert = upsert or AsyncMock(return_value="created")
    monkeypatch.setattr("src.services.board_service.upsert_board", upsert)
    return message, channel, upsert


async def test_board_recreate_upsert_failure_sends_ephemeral_followup(db, cog, interaction, monkeypatch):
    upsert = AsyncMock(side_effect=RuntimeError("kaput"))
    _recreate_setup(db, cog, interaction, monkeypatch, upsert=upsert)

    await cog.board_recreate.callback(cog, interaction, "this_week")

    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert "failed" in args[0].lower()
    assert "RuntimeError" in args[0]
    assert "kaput" not in args[0]
    assert kwargs.get("ephemeral") is True


async def test_board_recreate_forbidden_on_delete_still_completes(db, cog, interaction, monkeypatch):
    message, _, upsert = _recreate_setup(db, cog, interaction, monkeypatch)
    response = MagicMock()
    response.status = 403
    message.delete = AsyncMock(side_effect=discord.Forbidden(response, "nope"))

    await cog.board_recreate.callback(cog, interaction, "this_week")

    assert not _post_exists(db)
    upsert.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert "created" in args[0]
    assert kwargs.get("ephemeral") is True


async def test_board_recreate_get_channel_none_falls_back_to_fetch_channel(db, cog, interaction, monkeypatch):
    message, channel, upsert = _recreate_setup(db, cog, interaction, monkeypatch)
    cog.bot.get_channel = MagicMock(return_value=None)

    await cog.board_recreate.callback(cog, interaction, "this_week")

    cog.bot.fetch_channel.assert_awaited_once_with(111)
    channel.fetch_message.assert_awaited_once_with(555)
    message.delete.assert_awaited_once()
    upsert.assert_awaited_once()


async def test_board_recreate_skips_delete_for_non_bot_message(db, cog, interaction, monkeypatch):
    message, _, upsert = _recreate_setup(db, cog, interaction, monkeypatch, author_id=BOT_ID + 1)

    await cog.board_recreate.callback(cog, interaction, "this_week")

    message.delete.assert_not_awaited()
    assert not _post_exists(db)
    upsert.assert_awaited_once()


async def test_board_recreate_upserts_after_row_deleted(db, cog, interaction, monkeypatch):
    seen = {}

    async def upsert(bot, session, guild_id, board_type):
        from src.db.repositories import get_board_post
        seen["row_present"] = get_board_post(session, guild_id, board_type) is not None
        return "created"

    _recreate_setup(db, cog, interaction, monkeypatch, upsert=AsyncMock(side_effect=upsert))

    await cog.board_recreate.callback(cog, interaction, "this_week")

    assert seen == {"row_present": False}


async def test_board_preview_failure_sends_ephemeral_error(db, cog, interaction, monkeypatch):
    monkeypatch.setattr("src.services.board_service.filter_events_for_board",
                        MagicMock(side_effect=RuntimeError("kaput")))

    await cog.board_preview.callback(cog, interaction, "this_week")

    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "failed" in args[0].lower()
    assert "RuntimeError" in args[0]
    assert "kaput" not in args[0]
    assert kwargs.get("ephemeral") is True
