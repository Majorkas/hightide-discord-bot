from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, PropertyMock
from zoneinfo import ZoneInfo

import discord
import pytest

from src.db.models import Event, GuildConfig
from src.db.repositories import get_board_post
from src.services.board_service import is_new_period, upsert_board

TZ = ZoneInfo("Australia/Perth")
GUILD = 999


def _channel(channel_id=111):
    channel = AsyncMock()
    type(channel).id = PropertyMock(return_value=channel_id)
    return channel


def _message(message_id, author_id=None):
    msg = AsyncMock()
    type(msg).id = PropertyMock(return_value=message_id)
    if author_id is not None:
        author = MagicMock()
        type(author).id = PropertyMock(return_value=author_id)
        type(msg).author = PropertyMock(return_value=author)
    return msg


def _http_exc(cls, status):
    response = MagicMock()
    response.status = status
    return cls(response, "x")


def _bot(channel):
    bot = AsyncMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)
    bot.user = MagicMock()
    type(bot.user).id = PropertyMock(return_value=9999)
    return bot


def _seed(session, **cfg_kw):
    base = dict(
        guild_id=GUILD,
        timezone="Australia/Perth",
        this_week_channel_id=111,
        this_month_channel_id=222,
        daily_refresh_time="09:00",
        week_start_mode="today_start",
        include_tba_section=True,
        delete_previous_month_message=True,
    )
    base.update(cfg_kw)
    session.add(GuildConfig(**base))
    session.add(
        Event(
            guild_id=GUILD,
            external_id="wp:e1",
            title="e1",
            url="https://x",
            starts_at=datetime.now(TZ) + timedelta(hours=1),
            time_known=True,
            is_active=True,
        )
    )
    session.commit()


# ============================================================================
# Tests for is_new_period function
# ============================================================================


def test_is_new_period_detects_month_change():
    """Month change from 2026-09 to 2026-10 should return True."""
    assert is_new_period("this_month", "2026-09", "2026-10") is True


def test_is_new_period_no_change():
    """Same month 2026-10 to 2026-10 should return False."""
    assert is_new_period("this_month", "2026-10", "2026-10") is False


def test_is_new_period_none_stored_key():
    """None stored_key (first time) should return True."""
    assert is_new_period("this_month", None, "2026-10") is True


def test_is_new_period_week_change():
    """Week change should return True (general equality check)."""
    assert is_new_period("this_week", "2026-W42", "2026-W43") is True


def test_is_new_period_week_no_change():
    """Same week should return False."""
    assert is_new_period("this_week", "2026-W43", "2026-W43") is False


# ============================================================================
# Tests for rollover in upsert_board
# ============================================================================


async def test_month_rollover_recreates_and_deletes_old_message(session):
    """
    Month period change should:
    - Return "recreated"
    - Send new message with allowed_mentions=discord.AllowedMentions.none()
    - Save BoardPost before attempting delete
    - Delete old message if delete_previous_month_message=True
    """
    _seed(session)
    month_channel = _channel(222)
    bot = _bot(month_channel)

    # First call: create initial board post for this_month
    month_channel.send = AsyncMock(return_value=_message(100))
    month_channel.fetch_message = AsyncMock(return_value=_message(100, author_id=9999))

    result1 = await upsert_board(bot, session, GUILD, "this_month")
    assert result1 == "created"

    post1 = get_board_post(session, GUILD, "this_month")
    assert post1.message_id == 100

    # Simulate month change: update stored period_key to old month
    post1.period_key = "2026-09"
    session.commit()

    # Reset mocks for second call
    month_channel.send.reset_mock()
    month_channel.fetch_message.reset_mock()

    # Create new message and old message mocks for rollover
    old_msg = _message(100, author_id=9999)
    new_msg = _message(200)
    month_channel.fetch_message = AsyncMock(return_value=old_msg)
    month_channel.send = AsyncMock(return_value=new_msg)

    from unittest.mock import patch

    with patch("src.services.board_service.period_key", return_value="2026-10"):
        result2 = await upsert_board(bot, session, GUILD, "this_month")

    assert result2 == "recreated"

    # Verify send was called with allowed_mentions
    assert month_channel.send.call_count == 1
    call_kwargs = month_channel.send.call_args.kwargs
    assert isinstance(call_kwargs.get("allowed_mentions"), discord.AllowedMentions)
    assert call_kwargs.get("allowed_mentions").everyone is False
    assert call_kwargs.get("allowed_mentions").users is False
    assert call_kwargs.get("allowed_mentions").roles is False

    # Verify old message delete was awaited
    old_msg.delete.assert_awaited_once()

    # Verify new board post points to new message
    post2 = get_board_post(session, GUILD, "this_month")
    assert post2.message_id == 200
    assert post2.period_key == "2026-10"


async def test_month_rollover_respects_delete_previous_month_message_false(session):
    """
    When delete_previous_month_message=False, old message should NOT be deleted.
    """
    _seed(session, delete_previous_month_message=False)
    month_channel = _channel(222)
    bot = _bot(month_channel)

    # First call: create initial
    month_channel.send = AsyncMock(return_value=_message(100))
    month_channel.fetch_message = AsyncMock(return_value=_message(100, author_id=9999))

    result1 = await upsert_board(bot, session, GUILD, "this_month")
    assert result1 == "created"

    post1 = get_board_post(session, GUILD, "this_month")
    assert post1.message_id == 100

    # Simulate month change
    post1.period_key = "2026-09"
    session.commit()

    month_channel.send.reset_mock()
    month_channel.fetch_message.reset_mock()

    # Create mocks for rollover
    old_msg = _message(100, author_id=9999)
    new_msg = _message(200)
    month_channel.fetch_message = AsyncMock(return_value=old_msg)
    month_channel.send = AsyncMock(return_value=new_msg)

    from unittest.mock import patch

    with patch("src.services.board_service.period_key", return_value="2026-10"):
        result2 = await upsert_board(bot, session, GUILD, "this_month")

    assert result2 == "recreated"

    # Verify old message delete was NOT awaited
    old_msg.delete.assert_not_called()

    # Verify new board post
    post2 = get_board_post(session, GUILD, "this_month")
    assert post2.message_id == 200


async def test_month_rollover_handles_missing_old_message(session):
    """
    Old message already deleted: fetch_message raises NotFound.
    Should still recreate successfully.
    """
    _seed(session)
    month_channel = _channel(222)
    month_channel.send = AsyncMock(return_value=_message(100))
    month_channel.fetch_message = AsyncMock(return_value=_message(50, author_id=9999))

    bot = _bot(month_channel)

    result1 = await upsert_board(bot, session, GUILD, "this_month")
    assert result1 == "created"

    post1 = get_board_post(session, GUILD, "this_month")
    post1.period_key = "2026-09"
    session.commit()

    month_channel.send.reset_mock()
    month_channel.fetch_message = AsyncMock(
        side_effect=_http_exc(discord.NotFound, 404)
    )

    from unittest.mock import patch

    with patch("src.services.board_service.period_key", return_value="2026-10"):
        month_channel.send = AsyncMock(return_value=_message(200))
        result2 = await upsert_board(bot, session, GUILD, "this_month")

    assert result2 == "recreated"
    post2 = get_board_post(session, GUILD, "this_month")
    assert post2.message_id == 200


async def test_month_rollover_handles_forbidden_on_fetch(session):
    """
    fetch_message raises Forbidden (e.g., no perms on old channel).
    Should still recreate successfully and skip delete.
    """
    _seed(session)
    month_channel = _channel(222)
    month_channel.send = AsyncMock(return_value=_message(100))
    month_channel.fetch_message = AsyncMock(return_value=_message(50, author_id=9999))

    bot = _bot(month_channel)

    result1 = await upsert_board(bot, session, GUILD, "this_month")
    assert result1 == "created"

    post1 = get_board_post(session, GUILD, "this_month")
    post1.period_key = "2026-09"
    session.commit()

    month_channel.send.reset_mock()
    month_channel.fetch_message = AsyncMock(
        side_effect=_http_exc(discord.Forbidden, 403)
    )

    from unittest.mock import patch

    with patch("src.services.board_service.period_key", return_value="2026-10"):
        month_channel.send = AsyncMock(return_value=_message(200))
        result2 = await upsert_board(bot, session, GUILD, "this_month")

    assert result2 == "recreated"


async def test_this_week_period_change_does_not_trigger_rollover(session):
    """
    this_week period change should use normal edit path, not rollover logic.
    """
    _seed(session)
    week_channel = _channel(111)
    msg = _message(300)
    msg.edit = AsyncMock()
    week_channel.fetch_message = AsyncMock(return_value=msg)
    week_channel.send = AsyncMock(return_value=_message(400))

    bot = _bot(week_channel)

    # Create initial
    result1 = await upsert_board(bot, session, GUILD, "this_week")
    assert result1 == "created"

    post1 = get_board_post(session, GUILD, "this_week")
    assert post1.message_id == 400

    # Simulate week change (but not month change)
    post1.period_key = "2026-W42"
    session.commit()

    week_channel.fetch_message.reset_mock()
    msg.edit.reset_mock()

    from unittest.mock import patch

    with patch(
        "src.services.board_service.period_key", return_value="2026-W43"
    ):
        result2 = await upsert_board(bot, session, GUILD, "this_week")

    # Should NOT recreate, should use edit path
    assert result2 == "edited"
    msg.edit.assert_called_once()


async def test_month_rollover_skips_delete_for_foreign_author(session):
    """
    Old message sent by different author: should NOT be deleted (authorship guard).
    """
    _seed(session)
    month_channel = _channel(222)
    bot = _bot(month_channel)

    # First call
    month_channel.send = AsyncMock(return_value=_message(100))
    month_channel.fetch_message = AsyncMock(return_value=_message(100, author_id=9999))

    result1 = await upsert_board(bot, session, GUILD, "this_month")
    assert result1 == "created"

    post1 = get_board_post(session, GUILD, "this_month")
    post1.period_key = "2026-09"
    session.commit()

    month_channel.send.reset_mock()
    month_channel.fetch_message.reset_mock()

    # Create old message with different author
    old_msg = _message(100, author_id=8888)  # Different author, not the bot
    new_msg = _message(200)
    month_channel.fetch_message = AsyncMock(return_value=old_msg)
    month_channel.send = AsyncMock(return_value=new_msg)

    from unittest.mock import patch

    with patch("src.services.board_service.period_key", return_value="2026-10"):
        result2 = await upsert_board(bot, session, GUILD, "this_month")

    assert result2 == "recreated"

    # Old message delete should NOT have been awaited (authorship guard)
    old_msg.delete.assert_not_called()

    post2 = get_board_post(session, GUILD, "this_month")
    assert post2.message_id == 200


async def test_month_rollover_delete_attempts_on_stored_channel(session):
    """
    Rollover delete logic fetches and deletes the old message from the stored channel (111),
    while sending the new message to the configured channel (222).
    This test discriminates: if upsert_board used the configured channel for delete,
    stored_channel.fetch_message would never be awaited and assertions would fail.
    """
    from src.db.models import BoardPost, GuildConfig, Event
    from unittest.mock import patch
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    
    # Create config with this_month_channel_id = 222 and delete_previous_month_message = True
    cfg = GuildConfig(
        guild_id=GUILD,
        timezone="Australia/Perth",
        this_week_channel_id=111,
        this_month_channel_id=222,
        daily_refresh_time="09:00",
        week_start_mode="today_start",
        include_tba_section=True,
        delete_previous_month_message=True,  # EXPLICITLY True
    )
    session.add(cfg)
    session.add(
        Event(
            guild_id=GUILD,
            external_id="wp:e1",
            title="e1",
            url="https://x",
            starts_at=datetime.now(ZoneInfo("Australia/Perth")) + timedelta(hours=1),
            time_known=True,
            is_active=True,
        )
    )
    session.commit()
    
    # Manually create a BoardPost with channel_id=111 (stored channel) to simulate
    # admin having reconfigured channels mid-month
    post = BoardPost(
        guild_id=GUILD,
        board_type="this_month",
        channel_id=111,  # Stored on old channel
        message_id=100,
        period_key="2026-09",  # Last month (old)
        render_hash="old_hash",
    )
    session.add(post)
    session.commit()
    
    # Verify post was created with correct channel_id
    retrieved_post = get_board_post(session, GUILD, "this_month")
    assert retrieved_post.channel_id == 111, f"Expected channel_id=111, got {retrieved_post.channel_id}"
    
    # Create two distinct channel mocks
    stored_channel = _channel(111)
    configured_channel = _channel(222)
    
    # Create bot with get_channel/fetch_channel returning the right channel per ID
    bot = AsyncMock()
    bot.user = MagicMock()
    type(bot.user).id = PropertyMock(return_value=9999)
    
    def get_channel_impl(cid):
        return stored_channel if cid == 111 else (configured_channel if cid == 222 else None)
    
    async def fetch_channel_impl(cid):
        return stored_channel if cid == 111 else configured_channel
    
    bot.get_channel = MagicMock(side_effect=get_channel_impl)
    bot.fetch_channel = AsyncMock(side_effect=fetch_channel_impl)
    
    # Setup mocks for rollover: new message goes to configured channel
    old_msg = _message(100, author_id=9999)
    new_msg = _message(200)
    
    stored_channel.fetch_message = AsyncMock(return_value=old_msg)
    configured_channel.send = AsyncMock(return_value=new_msg)
    
    # Run upsert_board which should detect rollover and delete from stored channel
    with patch("src.services.board_service.period_key", return_value="2026-10"):
        # Also patch get_or_create_config to ensure it returns our seeded config
        def get_or_create_config_impl(sess, gid):
            c = sess.get(GuildConfig, gid)
            if c is None:
                c = GuildConfig(guild_id=gid, delete_previous_month_message=True)
                sess.add(c)
                sess.commit()
            # Verify delete_previous_month_message is True
            if not c.delete_previous_month_message:
                raise AssertionError(f"Config has delete_previous_month_message={c.delete_previous_month_message}")
            return c
        
        with patch("src.services.board_service.get_or_create_config", side_effect=get_or_create_config_impl):
            result = await upsert_board(bot, session, GUILD, "this_month")
    
    assert result == "recreated"
    
    # CRITICAL ASSERTIONS: verify delete happened on STORED channel (111)
    stored_channel.fetch_message.assert_awaited_once_with(100)
    old_msg.delete.assert_awaited_once()
    
    # Verify send happened on CONFIGURED channel (222)
    configured_channel.send.assert_awaited_once()
    
    # Verify new post has channel_id=222 and message_id=200
    post_after = get_board_post(session, GUILD, "this_month")
    assert post_after.message_id == 200
    assert post_after.channel_id == 222
    assert post_after.period_key == "2026-10"


async def test_initial_fetch_http_exception_propagates_no_duplicate_post(session):
    """
    REGRESSION TEST: HTTPException from initial fetch should propagate.
    Before fix: caught by broad exception, msg=None, posts duplicate.
    After fix: propagates, upsert_board raises, no message sent.
    """
    _seed(session)
    month_channel = _channel(222)
    bot = _bot(month_channel)

    # First call: create initial board post
    month_channel.send = AsyncMock(return_value=_message(100))
    month_channel.fetch_message = AsyncMock(return_value=_message(100, author_id=9999))

    result1 = await upsert_board(bot, session, GUILD, "this_month")
    assert result1 == "created"

    post1 = get_board_post(session, GUILD, "this_month")
    assert post1.message_id == 100

    # Reset for second call
    month_channel.send.reset_mock()
    month_channel.fetch_message.reset_mock()

    # Second call: fetch_message raises HTTPException (5xx or rate limit)
    month_channel.fetch_message = AsyncMock(
        side_effect=_http_exc(discord.HTTPException, 500)
    )

    # Verify HTTPException propagates (upsert_board raises)
    with pytest.raises(discord.HTTPException):
        await upsert_board(bot, session, GUILD, "this_month")

    # Verify NO new message was sent (this prevents the duplicate post regression)
    month_channel.send.assert_not_called()


# ============================================================================
# Tests for scheduler retry logic
# ============================================================================
# Scheduler tests moved to tests/test_scheduler.py for better organization

