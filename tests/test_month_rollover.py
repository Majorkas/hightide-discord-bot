from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
from zoneinfo import ZoneInfo

import discord
import pytest

from src.db.models import BoardPost, Event, GuildConfig
from src.db.repositories import get_board_post
from src.services.board_service import is_new_period, upsert_board
from src.services.board_service import period_key as current_period_key

TZ = ZoneInfo("Australia/Perth")
GUILD = 999
STALE_KEY = "2000-01"


def _channel(channel_id=111, guild_id=GUILD):
    channel = AsyncMock()
    type(channel).id = PropertyMock(return_value=channel_id)
    channel.guild = MagicMock()
    channel.guild.id = guild_id
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


def _two_channel_bot(stored, configured):
    """Bot resolving 111 -> stored and 222 -> configured."""
    by_id = {111: stored, 222: configured}
    bot = AsyncMock()
    bot.user = MagicMock()
    type(bot.user).id = PropertyMock(return_value=9999)
    bot.get_channel = MagicMock(side_effect=lambda cid: by_id.get(cid))
    bot.fetch_channel = AsyncMock(side_effect=lambda cid: by_id[cid])
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


def _seed_post(session, *, channel_id=222, message_id=100, key=STALE_KEY,
               render_hash="old_hash", board_type="this_month"):
    session.add(
        BoardPost(
            guild_id=GUILD,
            board_type=board_type,
            channel_id=channel_id,
            message_id=message_id,
            period_key=key,
            render_hash=render_hash,
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

    with patch(
        "src.services.board_service.period_key", return_value="2026-W43"
    ):
        result2 = await upsert_board(bot, session, GUILD, "this_week")

    # Should NOT recreate, should use edit path
    assert result2 == "edited"
    assert week_channel.send.await_count == 1  # only the initial creation
    msg.edit.assert_awaited_once()
    session.expire_all()
    post2 = get_board_post(session, GUILD, "this_week")
    assert post2.period_key == "2026-W43"
    assert post2.message_id == 300


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
    _seed(session)
    # Admin reconfigured channels mid-month: the stored post lives on channel 111.
    _seed_post(session, channel_id=111, message_id=100, key="2026-09")

    stored_channel = _channel(111)
    configured_channel = _channel(222)
    bot = _two_channel_bot(stored_channel, configured_channel)

    old_msg = _message(100, author_id=9999)
    new_msg = _message(200)
    stored_channel.fetch_message = AsyncMock(return_value=old_msg)
    configured_channel.send = AsyncMock(return_value=new_msg)

    with patch("src.services.board_service.period_key", return_value="2026-10"):
        result = await upsert_board(bot, session, GUILD, "this_month")

    assert result == "recreated"

    stored_channel.fetch_message.assert_awaited_once_with(100)
    old_msg.delete.assert_awaited_once()
    configured_channel.send.assert_awaited_once()

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
# Rollover failure paths
# ============================================================================


async def test_month_rollover_same_channel_reuses_prefetched_message(session):
    _seed(session)
    _seed_post(session, channel_id=222, message_id=100)
    month_channel = _channel(222)
    old_msg = _message(100, author_id=9999)
    month_channel.fetch_message = AsyncMock(return_value=old_msg)
    month_channel.send = AsyncMock(return_value=_message(200))

    result = await upsert_board(_bot(month_channel), session, GUILD, "this_month")

    assert result == "recreated"
    month_channel.fetch_message.assert_awaited_once_with(100)
    old_msg.delete.assert_awaited_once()


@pytest.mark.parametrize("exc_cls,status", [
    (discord.NotFound, 404),
    (discord.Forbidden, 403),
    (discord.HTTPException, 500),
])
async def test_month_rollover_old_delete_failure_still_recreated(session, exc_cls, status):
    _seed(session)
    _seed_post(session, channel_id=222, message_id=100)
    month_channel = _channel(222)
    old_msg = _message(100, author_id=9999)
    old_msg.delete = AsyncMock(side_effect=_http_exc(exc_cls, status))
    month_channel.fetch_message = AsyncMock(return_value=old_msg)
    month_channel.send = AsyncMock(return_value=_message(200))

    result = await upsert_board(_bot(month_channel), session, GUILD, "this_month")

    assert result == "recreated"
    old_msg.delete.assert_awaited_once()
    post = get_board_post(session, GUILD, "this_month")
    assert post.message_id == 200
    assert post.period_key == current_period_key("this_month", TZ)


async def test_month_rollover_old_channel_fetch_message_http_error_still_recreated(session):
    _seed(session)
    _seed_post(session, channel_id=111, message_id=100)
    stored_channel = _channel(111)
    configured_channel = _channel(222)
    stored_channel.fetch_message = AsyncMock(side_effect=_http_exc(discord.HTTPException, 500))
    configured_channel.send = AsyncMock(return_value=_message(200))
    bot = _two_channel_bot(stored_channel, configured_channel)

    result = await upsert_board(bot, session, GUILD, "this_month")

    assert result == "recreated"
    stored_channel.fetch_message.assert_awaited_once_with(100)
    post = get_board_post(session, GUILD, "this_month")
    assert (post.channel_id, post.message_id) == (222, 200)


async def test_month_rollover_stored_channel_unresolvable_still_recreated(session):
    _seed(session)
    _seed_post(session, channel_id=111, message_id=100)
    configured_channel = _channel(222)
    configured_channel.send = AsyncMock(return_value=_message(200))
    bot = _bot(configured_channel)
    bot.get_channel = MagicMock(side_effect=lambda cid: configured_channel if cid == 222 else None)

    async def fetch_channel(cid):
        if cid == 222:
            return configured_channel
        raise _http_exc(discord.NotFound, 404)

    bot.fetch_channel = AsyncMock(side_effect=fetch_channel)

    result = await upsert_board(bot, session, GUILD, "this_month")

    assert result == "recreated"
    post = get_board_post(session, GUILD, "this_month")
    assert (post.channel_id, post.message_id) == (222, 200)


async def test_month_rollover_send_failure_propagates_and_keeps_old_post(session):
    _seed(session)
    _seed_post(session, channel_id=222, message_id=100)
    month_channel = _channel(222)
    old_msg = _message(100, author_id=9999)
    month_channel.fetch_message = AsyncMock(return_value=old_msg)
    month_channel.send = AsyncMock(side_effect=RuntimeError("send failed"))

    with pytest.raises(RuntimeError, match="send failed"):
        await upsert_board(_bot(month_channel), session, GUILD, "this_month")

    session.expire_all()
    post = get_board_post(session, GUILD, "this_month")
    assert (post.period_key, post.message_id) == (STALE_KEY, 100)
    old_msg.delete.assert_not_awaited()


async def test_month_rollover_save_failure_deletes_new_message_and_propagates(session):
    _seed(session)
    _seed_post(session, channel_id=222, message_id=100)
    month_channel = _channel(222)
    old_msg = _message(100, author_id=9999)
    new_msg = _message(200)
    month_channel.fetch_message = AsyncMock(return_value=old_msg)
    month_channel.send = AsyncMock(return_value=new_msg)

    with patch("src.services.board_service.save_board_post", side_effect=RuntimeError("db down")):
        with pytest.raises(RuntimeError, match="db down"):
            await upsert_board(_bot(month_channel), session, GUILD, "this_month")

    new_msg.delete.assert_awaited_once()
    old_msg.delete.assert_not_awaited()


async def test_month_rollover_save_failure_cleanup_failure_still_raises_original(session):
    _seed(session)
    _seed_post(session, channel_id=222, message_id=100)
    month_channel = _channel(222)
    new_msg = _message(200)
    new_msg.delete = AsyncMock(side_effect=_http_exc(discord.Forbidden, 403))
    month_channel.fetch_message = AsyncMock(return_value=_message(100, author_id=9999))
    month_channel.send = AsyncMock(return_value=new_msg)

    with patch("src.services.board_service.save_board_post", side_effect=RuntimeError("db down")):
        with pytest.raises(RuntimeError, match="db down"):
            await upsert_board(_bot(month_channel), session, GUILD, "this_month")

    new_msg.delete.assert_awaited_once()


async def test_month_rollover_skips_delete_when_old_channel_in_other_guild(session):
    _seed(session)
    _seed_post(session, channel_id=111, message_id=100)
    stored_channel = _channel(111, guild_id=12345)
    configured_channel = _channel(222)
    old_msg = _message(100, author_id=9999)
    stored_channel.fetch_message = AsyncMock(return_value=old_msg)
    configured_channel.send = AsyncMock(return_value=_message(200))
    bot = _two_channel_bot(stored_channel, configured_channel)

    result = await upsert_board(bot, session, GUILD, "this_month")

    assert result == "recreated"
    stored_channel.fetch_message.assert_not_awaited()
    old_msg.delete.assert_not_awaited()
    assert get_board_post(session, GUILD, "this_month").message_id == 200


# ============================================================================
# Rollover behavior paths
# ============================================================================


async def test_month_rollover_with_identical_hash_still_recreates(session):
    _seed(session)
    month_channel = _channel(222)
    bot = _bot(month_channel)
    month_channel.send = AsyncMock(return_value=_message(100))

    assert await upsert_board(bot, session, GUILD, "this_month") == "created"

    post = get_board_post(session, GUILD, "this_month")
    stored_hash = post.render_hash
    post.period_key = STALE_KEY  # only the period changes; content is identical
    session.commit()

    old_msg = _message(100, author_id=9999)
    month_channel.fetch_message = AsyncMock(return_value=old_msg)
    month_channel.send = AsyncMock(return_value=_message(200))

    result = await upsert_board(bot, session, GUILD, "this_month")

    assert result == "recreated"
    month_channel.send.assert_awaited_once()
    old_msg.delete.assert_awaited_once()
    post = get_board_post(session, GUILD, "this_month")
    assert post.render_hash == stored_hash
    assert post.message_id == 200
    assert post.period_key == current_period_key("this_month", TZ)


async def test_upsert_after_completed_rollover_is_skipped(session):
    _seed(session)
    _seed_post(session, channel_id=222, message_id=100)
    month_channel = _channel(222)
    bot = _bot(month_channel)
    month_channel.fetch_message = AsyncMock(return_value=_message(100, author_id=9999))
    month_channel.send = AsyncMock(return_value=_message(200))

    assert await upsert_board(bot, session, GUILD, "this_month") == "recreated"

    new_msg = _message(200, author_id=9999)
    month_channel.fetch_message = AsyncMock(return_value=new_msg)
    month_channel.send.reset_mock()

    assert await upsert_board(bot, session, GUILD, "this_month") == "skipped"
    month_channel.send.assert_not_awaited()
    new_msg.edit.assert_not_awaited()
    new_msg.delete.assert_not_awaited()


async def test_no_channel_preempts_rollover(session):
    _seed(session, this_month_channel_id=None)
    _seed_post(session, channel_id=222, message_id=100)
    month_channel = _channel(222)
    old_msg = _message(100, author_id=9999)
    month_channel.fetch_message = AsyncMock(return_value=old_msg)

    result = await upsert_board(_bot(month_channel), session, GUILD, "this_month")

    assert result == "no_channel"
    month_channel.send.assert_not_awaited()
    old_msg.delete.assert_not_awaited()
    assert get_board_post(session, GUILD, "this_month").message_id == 100


async def test_this_month_same_period_skipped_then_edited_on_change(session):
    _seed(session)
    month_channel = _channel(222)
    bot = _bot(month_channel)
    month_channel.send = AsyncMock(return_value=_message(100))
    assert await upsert_board(bot, session, GUILD, "this_month") == "created"

    msg = _message(100, author_id=9999)
    month_channel.fetch_message = AsyncMock(return_value=msg)

    assert await upsert_board(bot, session, GUILD, "this_month") == "skipped"
    msg.edit.assert_not_awaited()

    old_hash = get_board_post(session, GUILD, "this_month").render_hash
    session.query(Event).filter_by(external_id="wp:e1").one().title = "changed"
    session.commit()

    assert await upsert_board(bot, session, GUILD, "this_month") == "edited"
    msg.edit.assert_awaited_once()
    assert month_channel.send.await_count == 1
    session.expire_all()
    post = get_board_post(session, GUILD, "this_month")
    assert post.render_hash != old_hash
    assert post.message_id == 100
