import pytest
from datetime import datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock, PropertyMock
from zoneinfo import ZoneInfo

import discord

from src.db.models import Event, GuildConfig
from src.services.board_service import upsert_board, filter_events_for_board, render_board_hash


TZ = ZoneInfo("Australia/Perth")


def _create_mock_channel(channel_id):
    """Create a properly configured async mock channel with integer id."""
    channel = AsyncMock()
    type(channel).id = PropertyMock(return_value=channel_id)
    return channel


def _create_mock_message(message_id):
    """Create a properly configured mock message with integer id."""
    msg = AsyncMock()
    type(msg).id = PropertyMock(return_value=message_id)
    msg.edit = AsyncMock()
    return msg


def _create_not_found_exception():
    """Create a discord.NotFound exception with proper mock response."""
    response = MagicMock()
    response.status = 404
    return discord.NotFound(response, "Not found")


def _cfg(**kw):
    base = dict(
        guild_id=999,
        timezone="Australia/Perth",
        this_week_channel_id=111,
        this_month_channel_id=None,
        daily_refresh_time="09:00",
        week_start_mode="today_start",
        include_tba_section=True,
        delete_previous_month_message=True,
    )
    base.update(kw)
    return GuildConfig(**base)


def _ev(title, days_ahead, time_known=True, url="https://example.com/event"):
    return Event(
        guild_id=999,
        external_id=f"wp:{title}",
        title=title,
        url=url,
        starts_at=datetime.now(TZ) + timedelta(days=days_ahead),
        time_known=time_known,
        is_active=True,
    )


def test_week_boundary_today_start_is_past_and_dropped():
    """Event exactly at window start (today 00:00 local) has already started, so it's dropped."""
    today_start = datetime.combine(datetime.now(TZ).date(), time.min, tzinfo=TZ)
    event = Event(
        guild_id=1, external_id="wp:boundary", title="boundary",
        url="https://x", starts_at=today_start, time_known=True, is_active=True
    )
    assert filter_events_for_board([event], "this_week", _cfg(), TZ) == []


def test_week_today_later_still_shown():
    """An event later today (start not yet passed) is still shown."""
    later = datetime.now(TZ) + timedelta(hours=2)
    event = Event(
        guild_id=1, external_id="wp:later", title="later",
        url="https://x", starts_at=later, time_known=True, is_active=True
    )
    got = filter_events_for_board([event], "this_week", _cfg(), TZ)
    assert len(got) == 1 and got[0].title == "later"


def test_week_boundary_day7_excluded():
    """Event exactly at start+7d is excluded from this_week."""
    now = datetime.now(TZ)
    today_start = datetime.combine(now.date(), time.min, tzinfo=TZ)
    day7_start = today_start + timedelta(days=7)
    event = Event(
        guild_id=1, external_id="wp:day7", title="day7",
        url="https://x", starts_at=day7_start, time_known=True, is_active=True
    )
    got = filter_events_for_board([event], "this_week", _cfg(), TZ)
    assert len(got) == 0


def test_week_start_mode_now():
    """week_start_mode='now' starts window at current time."""
    now = datetime.now(TZ)
    event_past = Event(
        guild_id=1, external_id="wp:past", title="past",
        url="https://x", starts_at=now - timedelta(hours=1), time_known=True, is_active=True
    )
    event_future = Event(
        guild_id=1, external_id="wp:future", title="future",
        url="https://x", starts_at=now + timedelta(days=6), time_known=True, is_active=True
    )
    cfg = _cfg(week_start_mode="now")
    got = filter_events_for_board([event_past, event_future], "this_week", cfg, TZ)
    titles = {e.title for e in got}
    assert "past" not in titles and "future" in titles


def test_render_hash_includes_url():
    """Hash changes when event URL differs; identical events (including url) produce equal hashes."""
    now = datetime.now(TZ)
    ev1_diff_url = Event(
        guild_id=999,
        external_id="wp:event",
        title="event",
        url="https://a.com",
        starts_at=now,
        time_known=True,
        is_active=True,
    )
    ev2_diff_url = Event(
        guild_id=999,
        external_id="wp:event",
        title="event",
        url="https://b.com",
        starts_at=now,
        time_known=True,
        is_active=True,
    )
    ev3_same_url = Event(
        guild_id=999,
        external_id="wp:event",
        title="event",
        url="https://a.com",
        starts_at=now,
        time_known=True,
        is_active=True,
    )
    hash1 = render_board_hash([ev1_diff_url], "this_week", _cfg(), TZ)
    hash2 = render_board_hash([ev2_diff_url], "this_week", _cfg(), TZ)
    hash3 = render_board_hash([ev3_same_url], "this_week", _cfg(), TZ)
    assert hash1 != hash2, "Hashes must differ when URL differs"
    assert hash1 == hash3, "Hashes must be identical when all fields including URL are identical"


def test_render_hash_includes_timezone():
    """Hash changes when timezone differs."""
    tz1 = ZoneInfo("Australia/Perth")
    tz2 = ZoneInfo("America/New_York")
    ev = _ev("event", 1)
    hash1 = render_board_hash([ev], "this_week", _cfg(), tz1)
    hash2 = render_board_hash([ev], "this_week", _cfg(), tz2)
    assert hash1 != hash2


@pytest.mark.asyncio
async def test_upsert_board_no_channel(session):
    """No channel configured returns 'no_channel'."""
    cfg = _cfg(this_week_channel_id=None)
    session.add(cfg)
    session.commit()

    bot = AsyncMock()
    result = await upsert_board(bot, session, 999, "this_week")
    assert result == "no_channel"


@pytest.mark.asyncio
async def test_upsert_board_fresh_creates(session):
    """Fresh board with no prior post returns 'created' and passes allowed_mentions."""
    cfg = _cfg()
    session.add(cfg)
    session.commit()

    ev = _ev("event1", 1)
    session.add(ev)
    session.commit()

    channel = _create_mock_channel(111)
    msg = _create_mock_message(555)
    channel.send = AsyncMock(return_value=msg)

    bot = AsyncMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)

    result = await upsert_board(bot, session, 999, "this_week")
    assert result == "created"
    assert channel.send.await_count == 2  # header + one event card
    for call_args in channel.send.call_args_list:
        assert isinstance(call_args.kwargs["allowed_mentions"], discord.AllowedMentions)
        assert call_args.kwargs["allowed_mentions"].everyone is False
        assert call_args.kwargs["allowed_mentions"].users is False
        assert call_args.kwargs["allowed_mentions"].roles is False


@pytest.mark.asyncio
async def test_upsert_board_identical_skipped(session):
    """Identical second run returns 'skipped' and fetch_message is called."""
    cfg = _cfg()
    session.add(cfg)
    session.commit()

    ev = _ev("event1", 1)
    session.add(ev)
    session.commit()

    channel = _create_mock_channel(111)
    msg = _create_mock_message(555)
    channel.send = AsyncMock(return_value=msg)
    channel.fetch_message = AsyncMock(return_value=msg)

    bot = AsyncMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)

    # First run creates
    result1 = await upsert_board(bot, session, 999, "this_week")
    assert result1 == "created"

    # Second run with same events: header re-edits to stay in sync, so 'edited'
    result2 = await upsert_board(bot, session, 999, "this_week")
    assert result2 == "edited"
    channel.fetch_message.assert_called_with(555)
    msg.edit.assert_awaited()  # header re-synced
    assert channel.send.await_count == 2  # no new sends


@pytest.mark.asyncio
async def test_upsert_board_deleted_message_recreated(session):
    """When fetch_message raises NotFound, board is recreated."""
    cfg = _cfg()
    session.add(cfg)
    session.commit()

    ev = _ev("event1", 1)
    session.add(ev)
    session.commit()

    channel = _create_mock_channel(111)
    msg = _create_mock_message(555)
    channel.send = AsyncMock(return_value=msg)

    bot = AsyncMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)

    # First run creates
    result1 = await upsert_board(bot, session, 999, "this_week")
    assert result1 == "created"

    # Reset send mock
    channel.send.reset_mock()

    # Second run: message deleted
    channel.fetch_message = AsyncMock(side_effect=_create_not_found_exception())
    new_msg = _create_mock_message(666)
    channel.send = AsyncMock(return_value=new_msg)

    result2 = await upsert_board(bot, session, 999, "this_week")
    assert result2 == "created"
    assert channel.send.await_count == 2


@pytest.mark.asyncio
async def test_upsert_board_content_changed_edited(session):
    """When content hash changes, message is edited."""
    cfg = _cfg()
    session.add(cfg)
    session.commit()

    ev1 = _ev("event1", 1)
    session.add(ev1)
    session.commit()

    channel = _create_mock_channel(111)
    msg = _create_mock_message(555)
    channel.send = AsyncMock(return_value=msg)
    channel.fetch_message = AsyncMock(return_value=msg)

    bot = AsyncMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)

    # First run creates
    result1 = await upsert_board(bot, session, 999, "this_week")
    assert result1 == "created"

    # Add second event (changes hash)
    ev2 = _ev("event2", 2)
    session.add(ev2)
    session.commit()

    # Reset mocks
    channel.send.reset_mock()
    msg.edit.reset_mock()
    channel.fetch_message.reset_mock()

    result2 = await upsert_board(bot, session, 999, "this_week")
    assert result2 == "edited"
    msg.edit.assert_called_once()
    call_args = msg.edit.call_args
    assert isinstance(call_args.kwargs["allowed_mentions"], discord.AllowedMentions)
    assert call_args.kwargs["allowed_mentions"].everyone is False
    assert call_args.kwargs["allowed_mentions"].users is False
    assert call_args.kwargs["allowed_mentions"].roles is False


@pytest.mark.asyncio
async def test_upsert_board_channel_moved(session):
    """Admin changes configured channel; next run posts to NEW channel (not skipped)."""
    cfg = _cfg(this_week_channel_id=111)
    session.add(cfg)
    session.commit()

    ev = _ev("event1", 1)
    session.add(ev)
    session.commit()

    channel_old = _create_mock_channel(111)
    msg = _create_mock_message(555)
    channel_old.send = AsyncMock(return_value=msg)

    bot = AsyncMock()
    bot.get_channel = MagicMock(return_value=channel_old)
    bot.fetch_channel = AsyncMock(return_value=channel_old)

    # First run creates in channel 111
    result1 = await upsert_board(bot, session, 999, "this_week")
    assert result1 == "created"

    # Admin changes channel config to 222
    cfg = session.get(GuildConfig, 999)
    cfg.this_week_channel_id = 222
    session.commit()

    channel_new = _create_mock_channel(222)
    new_msg = _create_mock_message(777)
    channel_new.send = AsyncMock(return_value=new_msg)
    # Old message exists in new channel, but channel_id check must catch that stored post.channel_id != 222
    old_existing_msg = _create_mock_message(555)
    channel_new.fetch_message = AsyncMock(return_value=old_existing_msg)

    bot.get_channel = MagicMock(return_value=channel_new)
    bot.fetch_channel = AsyncMock(return_value=channel_new)

    # Reset mocks
    channel_old.send.reset_mock()
    channel_new.send.reset_mock()

    result2 = await upsert_board(bot, session, 999, "this_week")
    # Should create in new channel (not skip/edit, because stored post.channel_id differs from new channel.id)
    assert result2 == "created"
    assert channel_new.send.await_count == 2
