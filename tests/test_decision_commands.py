"""Tests for the /decision admin command group."""
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import select

from src.db.models import Base, Decision, DecisionChannel, GuildConfig, TrelloMemberMap
from src.db.session import get_engine, get_session_factory
from src.discord_ui import decision_commands
from src.discord_ui.decision_commands import DecisionCog
from src.services import decision_service

GUILD = 4242


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'dc.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    monkeypatch.setattr(decision_commands, "_session_factory", factory)
    yield factory
    engine.dispose()
    factory.kw["bind"].dispose()


@pytest.fixture
def cog():
    return DecisionCog(MagicMock())


def _interaction(user_id=1, guild_id=GUILD, manage_guild=True):
    i = AsyncMock(spec=discord.Interaction)
    i.guild_id = guild_id
    i.user = MagicMock(id=user_id)
    i.user.guild_permissions = discord.Permissions(manage_guild=manage_guild)
    i.user.display_name = "Admin"
    i.response = AsyncMock()
    i.followup = AsyncMock()
    i.id = 555
    i.channel = MagicMock(id=9, name="staff")
    i.guild = MagicMock()
    i.guild.get_member = MagicMock(return_value=MagicMock(display_name="Sam"))
    i.guild.get_channel = MagicMock(return_value=MagicMock(name="staff"))
    return i


def _channel(cid=9):
    c = MagicMock()
    c.id = cid
    c.mention = f"<#{cid}>"
    c.name = "staff"
    return c


# --- monitor / unmonitor ---

async def test_monitor_adds_channel(db, cog):
    i = _interaction()
    await cog.monitor.callback(cog, i, _channel(9))
    with db() as s:
        ids = [r[0] for r in s.execute(select(DecisionChannel.channel_id))]
    assert ids == [9]
    assert "Monitoring" in i.response.send_message.call_args.args[0]


async def test_monitor_notes_disabled_state(db, cog):
    i = _interaction()
    await cog.monitor.callback(cog, i, _channel(9))
    assert "disabled" in i.response.send_message.call_args.args[0]


async def test_unmonitor_removes(db, cog):
    with db() as s:
        s.add(DecisionChannel(guild_id=GUILD, channel_id=9))
        s.commit()
    i = _interaction()
    await cog.unmonitor.callback(cog, i, _channel(9))
    with db() as s:
        assert s.execute(select(DecisionChannel)).scalars().all() == []
    assert "Stopped" in i.response.send_message.call_args.args[0]


async def test_unmonitor_not_monitored(db, cog):
    i = _interaction()
    await cog.unmonitor.callback(cog, i, _channel(9))
    assert "wasn't being monitored" in i.response.send_message.call_args.args[0]


# --- config ---

async def test_config_on_off(db, cog):
    i = _interaction()
    await cog.decision_config.callback(cog, i, "on")
    with db() as s:
        from src.db.repositories import get_or_create_decision_config
        assert get_or_create_decision_config(s, GUILD).enabled is True
    i2 = _interaction()
    await cog.decision_config.callback(cog, i2, "off")
    with db() as s:
        from src.db.repositories import get_or_create_decision_config
        assert get_or_create_decision_config(s, GUILD).enabled is False


async def test_config_board_requires_both_ids(db, cog):
    i = _interaction()
    await cog.decision_config.callback(cog, i, "board", board_id="b1", list_id=None)
    assert "both" in i.response.send_message.call_args.args[0]


# --- meetings_channel + meetings ---

async def test_meetings_channel_sets_config(db, cog):
    i = _interaction()
    await cog.meetings_channel.callback(cog, i, _channel(777))
    with db() as s:
        from src.db.repositories import get_or_create_decision_config
        assert get_or_create_decision_config(s, GUILD).meetings_channel_id == 777
    assert "777" in i.response.send_message.call_args.args[0] or "transcribed" in i.response.send_message.call_args.args[0]


async def test_meetings_empty(db, cog):
    i = _interaction()
    await cog.meetings.callback(cog, i)
    assert "No meetings" in i.response.send_message.call_args.args[0]


async def test_meetings_lists_past(db, cog):
    with db() as s:
        from src.db.repositories import add_meeting
        add_meeting(s, GUILD, 777, "staff.mp3", "Discussed the new deckboxes", "excerpt")
        s.commit()
    i = _interaction()
    await cog.meetings.callback(cog, i)
    text = i.response.send_message.call_args.args[0]
    assert "staff.mp3" in text and "deckboxes" in text
    assert i.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_config_board_sets_target(db, cog):
    i = _interaction()
    await cog.decision_config.callback(cog, i, "board", board_id="b1", list_id="l1")
    with db() as s:
        from src.db.repositories import get_or_create_decision_config
        cfg = get_or_create_decision_config(s, GUILD)
        assert cfg.trello_board_id == "b1" and cfg.trello_list_id == "l1"


async def test_config_invalid_setting(db, cog):
    i = _interaction()
    await cog.decision_config.callback(cog, i, "bogus")
    assert "on, off, or board" in i.response.send_message.call_args.args[0]


# --- map / unmap ---

async def test_map_and_unmap_member(db, cog):
    user = MagicMock(id=7, mention="<@7>")
    i = _interaction()
    await cog.map_member.callback(cog, i, user, "trello_abc")
    with db() as s:
        row = s.execute(select(TrelloMemberMap)).scalar_one()
        assert row.trello_member_id == "trello_abc"
    i2 = _interaction()
    await cog.unmap_member.callback(cog, i2, user)
    with db() as s:
        assert s.execute(select(TrelloMemberMap)).scalars().all() == []
    assert "Removed" in i2.response.send_message.call_args.args[0]


async def test_unmap_member_not_mapped(db, cog):
    user = MagicMock(id=7, mention="<@7>")
    i = _interaction()
    await cog.unmap_member.callback(cog, i, user)
    assert "no Trello mapping" in i.response.send_message.call_args.args[0]


# --- list ---

async def test_list_empty(db, cog):
    i = _interaction()
    await cog.list_cmd.callback(cog, i, None)
    assert "No decisions" in i.response.send_message.call_args.args[0]


async def test_list_shows_status_and_summary(db, cog):
    with db() as s:
        decision_service.create_pending_decision(s, GUILD, 1, 2, 3, "raw", "Run drafts")
        s.commit()
    i = _interaction()
    await cog.list_cmd.callback(cog, i, None)
    text = i.response.send_message.call_args.args[0]
    assert "Run drafts" in text and "pending" in text
    assert i.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_list_invalid_status(db, cog):
    i = _interaction()
    await cog.list_cmd.callback(cog, i, "bogus")
    assert "pending, confirmed, or rejected" in i.response.send_message.call_args.args[0]


# --- create ---

async def test_create_logs_and_syncs(db, cog, monkeypatch):
    monkeypatch.setattr(decision_service, "sync_decision_to_trello",
                        AsyncMock(return_value=True))
    i = _interaction()
    await cog.create.callback(cog, i, "Run Friday drafts at 7pm", None)
    with db() as s:
        d = s.execute(select(Decision)).scalar_one()
        assert d.summary == "Run Friday drafts at 7pm" and d.status == "confirmed"
    msg = i.followup.send.call_args.args[0]
    assert "Logged decision" in msg and "synced to Trello" in msg


async def test_create_notes_sync_failure(db, cog, monkeypatch):
    monkeypatch.setattr(decision_service, "sync_decision_to_trello",
                        AsyncMock(return_value=False))
    i = _interaction()
    await cog.create.callback(cog, i, "X", None)
    assert "sync failed" in i.followup.send.call_args.args[0]


# --- sync ---

async def test_sync_nothing_pending(db, cog):
    i = _interaction()
    await cog.sync.callback(cog, i)
    assert "Nothing to sync" in i.followup.send.call_args.args[0]


async def test_sync_retries_unsynced(db, cog, monkeypatch):
    with db() as s:
        d = decision_service.create_pending_decision(s, GUILD, 1, 2, 3, "raw", "X")
        decision_service.confirm_decision(s, GUILD, d.id, confirmed_by=1)
        s.commit()
    monkeypatch.setattr(decision_service, "sync_decision_to_trello",
                        AsyncMock(return_value=True))
    i = _interaction()
    await cog.sync.callback(cog, i)
    assert "Synced 1/1" in i.followup.send.call_args.args[0]
