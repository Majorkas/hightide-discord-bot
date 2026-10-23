import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, PropertyMock
from zoneinfo import ZoneInfo

import discord
import pytest

from src.db.models import Event, GuildConfig
from src.db.repositories import get_board_post
from src.services.board_service import (
    _get_board_lock, delete_board, period_key, render_board_hash, upsert_board,
)

TZ = ZoneInfo("Australia/Perth")
GUILD = 999


def _channel(channel_id=111):
    channel = AsyncMock()
    type(channel).id = PropertyMock(return_value=channel_id)
    return channel


def _message(message_id):
    msg = AsyncMock()
    type(msg).id = PropertyMock(return_value=message_id)
    return msg


def _http_exc(cls, status):
    response = MagicMock()
    response.status = status
    return cls(response, "x")


def _bot(channel):
    bot = AsyncMock()
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)
    return bot


def _seed(session, **cfg_kw):
    base = dict(guild_id=GUILD, timezone="Australia/Perth", this_week_channel_id=111,
                this_month_channel_id=222, daily_refresh_time="09:00",
                week_start_mode="today_start", include_tba_section=True)
    base.update(cfg_kw)
    session.add(GuildConfig(**base))
    session.add(Event(guild_id=GUILD, external_id="wp:e1", title="e1", url="https://x",
                      starts_at=datetime.now(TZ) + timedelta(hours=1),
                      time_known=True, is_active=True))
    session.commit()


async def test_edit_branch_notfound_race_recreates(session):
    _seed(session)
    channel = _channel()
    channel.send = AsyncMock(side_effect=[_message(555), _message(666)])
    bot = _bot(channel)
    assert await upsert_board(bot, session, GUILD, "this_week") == "created"

    # Content changes (hash differs) and the message disappears.
    session.add(Event(guild_id=GUILD, external_id="wp:e2", title="e2", url="https://x",
                      starts_at=datetime.now(TZ) + timedelta(hours=2),
                      time_known=True, is_active=True))
    session.commit()
    channel.fetch_message = AsyncMock(side_effect=_http_exc(discord.NotFound, 404))

    assert await upsert_board(bot, session, GUILD, "this_week") == "created"
    assert channel.send.await_count == 2
    assert channel.fetch_message.await_count == 1
    assert get_board_post(session, GUILD, "this_week").message_id == 666


async def test_edit_raising_notfound_recreates(session):
    _seed(session)
    channel = _channel()
    first, edited = _message(555), _message(555)
    channel.send = AsyncMock(side_effect=[first, _message(777)])
    channel.fetch_message = AsyncMock(return_value=edited)
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")

    session.add(Event(guild_id=GUILD, external_id="wp:e2", title="e2", url="https://x",
                      starts_at=datetime.now(TZ) + timedelta(hours=2),
                      time_known=True, is_active=True))
    session.commit()
    edited.edit = AsyncMock(side_effect=_http_exc(discord.NotFound, 404))

    assert await upsert_board(bot, session, GUILD, "this_week") == "created"
    assert get_board_post(session, GUILD, "this_week").message_id == 777


async def test_forbidden_on_fetch_propagates_without_recreate(session):
    _seed(session)
    channel = _channel()
    channel.send = AsyncMock(side_effect=[_message(555), _message(666)])
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")

    # Identical content (skip path) but fetch is forbidden.
    channel.fetch_message = AsyncMock(side_effect=_http_exc(discord.Forbidden, 403))
    with pytest.raises(discord.Forbidden):
        await upsert_board(bot, session, GUILD, "this_week")
    assert channel.send.await_count == 1
    assert get_board_post(session, GUILD, "this_week").message_id == 555


async def test_forbidden_on_fetch_in_edit_path_propagates(session):
    _seed(session)
    channel = _channel()
    channel.send = AsyncMock(side_effect=[_message(555), _message(666)])
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")

    session.add(Event(guild_id=GUILD, external_id="wp:e2", title="e2", url="https://x",
                      starts_at=datetime.now(TZ) + timedelta(hours=2),
                      time_known=True, is_active=True))
    session.commit()
    channel.fetch_message = AsyncMock(side_effect=_http_exc(discord.Forbidden, 403))

    with pytest.raises(discord.Forbidden):
        await upsert_board(bot, session, GUILD, "this_week")
    assert channel.send.await_count == 1


async def test_forbidden_on_edit_propagates_without_recreate(session):
    _seed(session)
    channel = _channel()
    first, fetched = _message(555), _message(555)
    channel.send = AsyncMock(side_effect=[first, _message(777)])
    channel.fetch_message = AsyncMock(return_value=fetched)
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")

    session.add(Event(guild_id=GUILD, external_id="wp:e2", title="e2", url="https://x",
                      starts_at=datetime.now(TZ) + timedelta(hours=2),
                      time_known=True, is_active=True))
    session.commit()
    fetched.edit = AsyncMock(side_effect=_http_exc(discord.Forbidden, 403))

    with pytest.raises(discord.Forbidden):
        await upsert_board(bot, session, GUILD, "this_week")
    assert channel.send.await_count == 1


async def test_new_post_save_failure_deletes_sent_message(session, monkeypatch):
    _seed(session)
    channel = _channel()
    sent = _message(555)
    channel.send = AsyncMock(return_value=sent)
    bot = _bot(channel)
    monkeypatch.setattr("src.services.board_service.save_board_post",
                        MagicMock(side_effect=RuntimeError("db down")))

    with pytest.raises(RuntimeError, match="db down"):
        await upsert_board(bot, session, GUILD, "this_week")
    sent.delete.assert_awaited_once()


async def test_new_post_save_failure_cleanup_failure_still_reraises_original(session, monkeypatch):
    _seed(session)
    channel = _channel()
    sent = _message(555)
    sent.delete = AsyncMock(side_effect=_http_exc(discord.Forbidden, 403))
    channel.send = AsyncMock(return_value=sent)
    bot = _bot(channel)
    monkeypatch.setattr("src.services.board_service.save_board_post",
                        MagicMock(side_effect=RuntimeError("db down")))

    with pytest.raises(RuntimeError, match="db down"):
        await upsert_board(bot, session, GUILD, "this_week")
    sent.delete.assert_awaited_once()


async def test_delete_board_not_found(session):
    _seed(session)
    bot = _bot(_channel())
    assert await delete_board(bot, session, GUILD, "this_week") == "not_found"


async def test_delete_board_deletes_message_and_row_under_lock(session):
    _seed(session)
    channel = _channel()
    channel.send = AsyncMock(return_value=_message(555))
    bot = _bot(channel)
    bot.user.id = 42
    await upsert_board(bot, session, GUILD, "this_week")

    old = _message(555)
    old.author.id = 42
    lock_states = []

    async def fetch_message(message_id):
        lock_states.append(_get_board_lock(GUILD, "this_week").locked())
        return old

    channel.fetch_message = AsyncMock(side_effect=fetch_message)

    assert await delete_board(bot, session, GUILD, "this_week") == "deleted"
    assert lock_states == [True]
    old.delete.assert_awaited_once()
    assert get_board_post(session, GUILD, "this_week") is None
    assert not _get_board_lock(GUILD, "this_week").locked()


async def test_delete_board_serialised_with_upsert(session):
    _seed(session)
    channel = _channel()
    channel.send = AsyncMock(return_value=_message(555))
    bot = _bot(channel)
    bot.user.id = 42
    await upsert_board(bot, session, GUILD, "this_week")

    async with _get_board_lock(GUILD, "this_week"):
        task = asyncio.ensure_future(delete_board(bot, session, GUILD, "this_week"))
        await asyncio.sleep(0.02)
        assert not task.done()
    assert await task == "deleted"


async def test_get_channel_none_falls_back_to_fetch_channel(session):
    _seed(session)
    channel = _channel()
    channel.send = AsyncMock(return_value=_message(555))
    bot = _bot(channel)
    bot.get_channel = MagicMock(return_value=None)

    assert await upsert_board(bot, session, GUILD, "this_week") == "created"
    bot.fetch_channel.assert_awaited_once_with(111)
    channel.send.assert_awaited_once()


async def test_this_month_board_created(session):
    _seed(session)
    channel = _channel(222)
    channel.send = AsyncMock(return_value=_message(444))
    bot = _bot(channel)

    assert await upsert_board(bot, session, GUILD, "this_month") == "created"
    bot.get_channel.assert_called_once_with(222)
    assert get_board_post(session, GUILD, "this_week") is None
    assert get_board_post(session, GUILD, "this_month").message_id == 444


async def test_board_post_persisted_after_create(session):
    _seed(session)
    channel = _channel()
    channel.send = AsyncMock(return_value=_message(555))
    bot = _bot(channel)

    await upsert_board(bot, session, GUILD, "this_week")

    post = get_board_post(session, GUILD, "this_week")
    cfg = session.get(GuildConfig, GUILD)
    events = [e for e in session.query(Event).all()]
    assert post.channel_id == 111
    assert post.message_id == 555
    assert post.period_key == period_key("this_week", TZ)
    assert post.render_hash == render_board_hash(events, "this_week", cfg, TZ)


async def test_concurrent_upserts_serialised_by_lock(session):
    _seed(session)
    channel = _channel()
    msg = _message(555)

    async def slow_send(*args, **kwargs):
        await asyncio.sleep(0.05)
        return msg

    channel.send = AsyncMock(side_effect=slow_send)
    channel.fetch_message = AsyncMock(return_value=msg)
    bot = _bot(channel)

    results = await asyncio.gather(
        upsert_board(bot, session, GUILD, "this_week"),
        upsert_board(bot, session, GUILD, "this_week"),
    )

    assert sorted(results) == ["created", "skipped"]
    assert channel.send.await_count == 1


async def test_this_week_send_uses_embeds_list(session):
    _seed(session)
    channel = _channel()
    channel.send = AsyncMock(return_value=_message(555))
    assert await upsert_board(_bot(channel), session, GUILD, "this_week") == "created"
    kwargs = channel.send.call_args.kwargs
    assert "embed" not in kwargs
    assert isinstance(kwargs["embeds"], list) and kwargs["embeds"][0].title == "e1"
    assert kwargs["allowed_mentions"].everyone is False


async def test_this_week_edit_uses_embeds_list(session):
    _seed(session)
    channel = _channel()
    channel.send = AsyncMock(return_value=_message(555))
    await upsert_board(_bot(channel), session, GUILD, "this_week")
    existing = _message(555)
    channel.fetch_message = AsyncMock(return_value=existing)
    ev = session.query(Event).one()
    ev.summary = "changed summary"
    session.commit()
    assert await upsert_board(_bot(channel), session, GUILD, "this_week") == "edited"
    kwargs = existing.edit.call_args.kwargs
    assert "embed" not in kwargs
    assert "changed summary" in kwargs["embeds"][0].description
    assert kwargs["allowed_mentions"].roles is False


async def test_this_month_still_single_embed(session):
    _seed(session)
    channel = _channel(222)
    channel.send = AsyncMock(return_value=_message(777))
    await upsert_board(_bot(channel), session, GUILD, "this_month")
    kwargs = channel.send.call_args.kwargs
    assert isinstance(kwargs["embed"], discord.Embed)
    assert "embeds" not in kwargs
