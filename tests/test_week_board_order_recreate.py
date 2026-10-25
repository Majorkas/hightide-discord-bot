"""
Tests for order-aware recreate logic and delete tolerance in week board sets.
"""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.db.models import BoardPost, Event, GuildConfig
from src.db.repositories import get_board_post
from src.services.board_service import delete_board, upsert_board

TZ = __import__('zoneinfo').ZoneInfo("Australia/Perth")
GUILD = 999
BOT_ID = 42


def _http_exc(cls, status):
    response = MagicMock()
    response.status = status
    return cls(response, "x")


class FakeMessage:
    def __init__(self, channel, message_id, author_id, embed):
        self.channel = channel
        self.id = message_id
        self.author = SimpleNamespace(id=author_id)
        self.embed = embed
        self.edits = []

    async def edit(self, **kwargs):
        if self.id not in self.channel.messages:
            raise _http_exc(discord.NotFound, 404)
        self.embed = kwargs["embed"]
        self.edits.append(kwargs)

    async def delete(self):
        self.channel.messages.pop(self.id, None)
        self.channel.deleted.append(self.id)


class FakeChannel:
    def __init__(self, channel_id=111):
        self.id = channel_id
        self.messages: dict[int, FakeMessage] = {}
        self.sends: list[dict] = []
        self.fetches: list[int] = []
        self.deleted: list[int] = []
        self._next = 1000

    async def send(self, **kwargs):
        self._next += 1
        msg = FakeMessage(self, self._next, BOT_ID, kwargs["embed"])
        self.messages[msg.id] = msg
        self.sends.append(kwargs)
        return msg

    async def fetch_message(self, message_id):
        self.fetches.append(message_id)
        if message_id not in self.messages:
            raise _http_exc(discord.NotFound, 404)
        return self.messages[message_id]


def _bot(channel):
    bot = MagicMock()
    bot.user.id = BOT_ID
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)
    return bot


def _event(name, days=1, **kw):
    base = dict(guild_id=GUILD, external_id=f"wp:{name}", title=name, url=f"https://x/{name}",
                starts_at=datetime.now(TZ) + timedelta(days=days), time_known=True,
                is_active=True)
    base.update(kw)
    return Event(**base)


async def test_overflow_promotion_recreates_not_appends(session):
    """Overflow-promotion scenario: 9 dated events + TBA.
    After first event drops, card promoted from overflow should land
    in correct order, not at bottom. Should trigger full recreate.
    
    Initial: [header, e0-e7 visible, overflow, tba]
    After e0 drops: [header, e1-e7 visible, e8 (promoted from overflow), tba]
    
    e8's position changed within the sequence, so this triggers recreate."""
    session.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth", 
                           this_week_channel_id=111, week_start_mode="today_start",
                           include_tba_section=True))
    for i in range(9):
        session.add(_event(f"e{i}", days=0.3 + 0.6 * i))
    session.add(_event("tba", starts_at=None))
    session.commit()

    channel = FakeChannel()
    bot = _bot(channel)
    
    # Create initial board with 9 cards + header + overflow + tba
    result1 = await upsert_board(bot, session, GUILD, "this_week")
    assert result1 == "created"
    
    post_before = get_board_post(session, GUILD, "this_week")
    keys_before = json.loads(post_before.render_hash)["keys"]
    ids_before = [post_before.message_id] + json.loads(post_before.message_ids)
    
    channel.sends.clear()
    channel.deleted.clear()
    
    # Drop the first event
    session.query(Event).filter_by(external_id="wp:e0").one().is_active = False
    session.commit()
    
    # Re-upsert should recreate (not just append)
    result2 = await upsert_board(bot, session, GUILD, "this_week")
    
    post_after = get_board_post(session, GUILD, "this_week")
    keys_after = json.loads(post_after.render_hash)["keys"]
    
    # e0 should no longer be in keys
    assert "wp:e0" not in keys_after
    # tba should still be there
    assert "tba" in keys_after
    
    # With recreate logic (overflow promoted means position change),
    # all old non-header messages should be deleted and fresh set sent
    # Most of the original messages should be deleted
    assert len(channel.deleted) >= len(ids_before) - 2  # all but header and maybe one
    
    # Final state: all messages gone except new ones
    assert all(mid not in channel.messages for mid in ids_before if mid != post_before.message_id)


async def test_monday_rollover_with_kept_tba_recreates(session):
    """Monday rollover: keys change (e.g. period rollover) but TBA is kept.
    Should trigger recreate, not just send new cards."""
    session.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth",
                           this_week_channel_id=111, week_start_mode="today_start",
                           include_tba_section=True))
    session.add(_event("a", days=1))
    session.add(_event("b", days=2))
    session.add(_event("tba", starts_at=None))
    session.commit()

    channel = FakeChannel()
    bot = _bot(channel)
    
    # Initial board
    await upsert_board(bot, session, GUILD, "this_week")
    initial_ids = list(channel.messages.keys())
    initial_count = len(initial_ids)
    
    channel.sends.clear()
    channel.deleted.clear()
    
    # Simulate Monday rollover: event "a" now belongs to previous period (mark inactive),
    # add new event "c"
    session.query(Event).filter_by(external_id="wp:a").one().is_active = False
    session.add(_event("c", days=3))
    session.commit()
    
    # Re-upsert with key change
    result = await upsert_board(bot, session, GUILD, "this_week")
    
    post = get_board_post(session, GUILD, "this_week")
    keys = json.loads(post.render_hash)["keys"]
    
    # tba should still be there (kept)
    assert "tba" in keys
    assert "wp:a" not in keys
    assert "wp:c" in keys
    
    # With recreate logic, we should delete old messages and send fresh
    assert len(channel.deleted) > 0


async def test_pure_suffix_append_no_recreate(session):
    """Adding a new event at the END of the week should NOT trigger recreate.
    Only one new message should be sent (the new event card)."""
    session.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth",
                           this_week_channel_id=111, week_start_mode="today_start",
                           include_tba_section=False))
    session.add(_event("a", days=1))
    session.add(_event("b", days=2))
    session.commit()

    channel = FakeChannel()
    bot = _bot(channel)
    
    # Initial board: header + 2 cards
    await upsert_board(bot, session, GUILD, "this_week")
    initial_send_count = len(channel.sends)
    assert initial_send_count == 3  # header, a, b
    
    initial_ids = json.loads(get_board_post(session, GUILD, "this_week").message_ids)
    
    channel.sends.clear()
    channel.deleted.clear()
    
    # Add new event at the END: day 3
    session.add(_event("c", days=3))
    session.commit()
    
    result = await upsert_board(bot, session, GUILD, "this_week")
    
    # Should only send one new message (the new event)
    new_sends = [s for s in channel.sends]
    assert len(new_sends) == 1
    assert new_sends[0]["embed"].title == "c"
    
    # No deletes for pure suffix append
    assert len(channel.deleted) == 0
    
    # Post should have all cards in order
    post = get_board_post(session, GUILD, "this_week")
    keys = json.loads(post.render_hash)["keys"]
    assert keys == ["header", "wp:a", "wp:b", "wp:c"]


async def test_same_keys_content_change_edits_in_place(session):
    """When keys don't change but content does, should edit in place, no recreate."""
    session.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth",
                           this_week_channel_id=111, week_start_mode="today_start",
                           include_tba_section=False))
    session.add(_event("a", days=1))
    session.add(_event("b", days=2))
    session.commit()

    channel = FakeChannel()
    bot = _bot(channel)
    
    await upsert_board(bot, session, GUILD, "this_week")
    initial_ids = json.loads(get_board_post(session, GUILD, "this_week").message_ids)
    
    channel.sends.clear()
    channel.deleted.clear()
    
    # Change price of event b
    session.query(Event).filter_by(external_id="wp:b").one().price_text = "$99"
    session.commit()
    
    result = await upsert_board(bot, session, GUILD, "this_week")
    
    # Should not recreate: no deletes, only edit
    assert len(channel.deleted) == 0
    assert len(channel.sends) == 0  # No new sends, only edits
    
    # The message for "b" should have been edited
    b_msg_id = initial_ids[1]  # b is the second card
    assert channel.messages[b_msg_id].edits


async def test_unchanged_board_skipped(session):
    """Unchanged board should be skipped."""
    session.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth",
                           this_week_channel_id=111, week_start_mode="today_start",
                           include_tba_section=False))
    session.add(_event("a", days=1))
    session.commit()

    channel = FakeChannel()
    bot = _bot(channel)
    
    result1 = await upsert_board(bot, session, GUILD, "this_week")
    assert result1 == "created"
    
    channel.sends.clear()
    channel.deleted.clear()
    
    result2 = await upsert_board(bot, session, GUILD, "this_week")
    assert result2 == "edited"  # header re-syncs each run
    assert len(channel.sends) == 0
    assert len(channel.deleted) == 0


async def test_delete_board_tolerates_non_http_exception_on_one_message(session):
    """delete_board should tolerate any Exception on per-message delete,
    not just HTTPException. One card's delete raises aiohttp.ClientError,
    others should still be deleted."""
    session.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth",
                           this_week_channel_id=111, week_start_mode="today_start",
                           include_tba_section=False))
    session.add(_event("a", days=1))
    session.add(_event("b", days=2))
    session.commit()

    channel = FakeChannel()
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")
    
    post = get_board_post(session, GUILD, "this_week")
    all_ids = [post.message_id] + json.loads(post.message_ids)
    
    # Make one message's delete raise a generic Exception
    victim = all_ids[1]  # Second card
    original_delete = channel.messages[victim].delete
    
    async def flaky_delete():
        raise ConnectionError("Network error")
    
    channel.messages[victim].delete = flaky_delete
    channel.deleted.clear()
    
    # delete_board should continue despite the error
    result = await delete_board(bot, session, GUILD, "this_week")
    
    assert result == "deleted"
    # All other messages should be deleted
    remaining_ids = [mid for mid in all_ids if mid in channel.messages]
    assert len(remaining_ids) == 1  # Only the victim remains (delete failed)
    assert remaining_ids[0] == victim
    # Row should still be removed from DB
    assert get_board_post(session, GUILD, "this_week") is None
