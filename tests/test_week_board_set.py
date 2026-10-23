import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from src.db.models import BoardPost, Event, GuildConfig
from src.db.repositories import get_board_post
from src.services.board_service import delete_board, upsert_board

TZ = ZoneInfo("Australia/Perth")
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


def _seed(session, names=("a", "b", "c"), **cfg_kw):
    base = dict(guild_id=GUILD, timezone="Australia/Perth", this_week_channel_id=111,
                week_start_mode="today_start", include_tba_section=True)
    base.update(cfg_kw)
    session.add(GuildConfig(**base))
    for i, name in enumerate(names):
        session.add(_event(name, days=1 + i))
    session.commit()


def _card_msg(channel, post, index):
    return channel.messages[json.loads(post.message_ids)[index]]


async def test_fresh_create_sends_header_plus_one_message_per_event(session):
    _seed(session)
    channel = FakeChannel()
    assert await upsert_board(_bot(channel), session, GUILD, "this_week") == "created"

    assert len(channel.sends) == 4
    assert channel.sends[0]["embed"].title == "This Week at Hightide"
    assert [s["embed"].title for s in channel.sends[1:]] == ["a", "b", "c"]
    for s in channel.sends:
        assert s["allowed_mentions"].everyone is False
        assert s["allowed_mentions"].users is False
        assert s["allowed_mentions"].roles is False

    post = get_board_post(session, GUILD, "this_week")
    sent_ids = list(channel.messages)
    assert post.message_id == sent_ids[0]
    assert json.loads(post.message_ids) == sent_ids[1:]
    stored = json.loads(post.render_hash)
    assert stored["keys"] == ["header", "wp:a", "wp:b", "wp:c"]
    assert set(stored["hashes"]) == set(stored["keys"])


async def test_identical_second_run_skipped_fetches_header_only(session):
    _seed(session)
    channel = FakeChannel()
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")
    header_id = get_board_post(session, GUILD, "this_week").message_id
    channel.sends.clear()

    assert await upsert_board(bot, session, GUILD, "this_week") == "skipped"
    assert channel.fetches == [header_id]
    assert channel.sends == []
    assert all(m.edits == [] for m in channel.messages.values())


async def test_price_change_edits_only_that_card(session):
    _seed(session)
    channel = FakeChannel()
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")
    session.query(Event).filter_by(external_id="wp:b").one().price_text = "$99"
    session.commit()

    assert await upsert_board(bot, session, GUILD, "this_week") == "edited"
    post = get_board_post(session, GUILD, "this_week")
    edited = [m.id for m in channel.messages.values() if m.edits]
    assert edited == [json.loads(post.message_ids)[1]]
    assert "$99" in channel.messages[edited[0]].embed.description
    assert channel.sends[4:] == []
    assert channel.deleted == []
    assert channel.messages[edited[0]].edits[0]["allowed_mentions"].roles is False


async def test_removed_event_message_deleted(session):
    _seed(session)
    channel = FakeChannel()
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")
    before = get_board_post(session, GUILD, "this_week")
    ids_before = json.loads(before.message_ids)
    session.query(Event).filter_by(external_id="wp:b").one().is_active = False
    session.commit()

    assert await upsert_board(bot, session, GUILD, "this_week") == "edited"
    assert channel.deleted == [ids_before[1]]
    post = get_board_post(session, GUILD, "this_week")
    assert json.loads(post.message_ids) == [ids_before[0], ids_before[2]]
    assert json.loads(post.render_hash)["keys"] == ["header", "wp:a", "wp:c"]
    assert [m.embed.title for m in channel.messages.values()][1:] == ["a", "c"]


async def test_new_event_mid_week_sends_one_message_others_untouched(session):
    _seed(session)
    channel = FakeChannel()
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")
    before_ids = json.loads(get_board_post(session, GUILD, "this_week").message_ids)
    session.add(_event("ab", days=1, starts_at=datetime.now(TZ) + timedelta(days=1, hours=1)))
    session.commit()
    channel.sends.clear()

    assert await upsert_board(bot, session, GUILD, "this_week") == "edited"
    assert [s["embed"].title for s in channel.sends] == ["ab"]
    post = get_board_post(session, GUILD, "this_week")
    assert json.loads(post.render_hash)["keys"] == ["header", "wp:a", "wp:ab", "wp:b", "wp:c"]
    ids = json.loads(post.message_ids)
    assert ids[0] == before_ids[0] and ids[2:] == before_ids[1:]
    for mid in before_ids:
        assert channel.messages[mid].edits == []
    assert channel.deleted == []


async def test_header_not_found_recreates_whole_set(session):
    _seed(session)
    channel = FakeChannel()
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")
    old = get_board_post(session, GUILD, "this_week")
    old_ids = [old.message_id] + json.loads(old.message_ids)
    channel.messages.pop(old.message_id)
    channel.sends.clear()

    assert await upsert_board(bot, session, GUILD, "this_week") == "created"
    assert len(channel.sends) == 4
    assert set(old_ids[1:]) <= set(channel.deleted)
    assert not set(old_ids) & set(channel.messages)
    post = get_board_post(session, GUILD, "this_week")
    assert post.message_id not in old_ids


async def test_header_edit_not_found_race_recreates_set(session):
    _seed(session)
    channel = FakeChannel()
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")
    header_id = get_board_post(session, GUILD, "this_week").message_id
    session.add(_event("d", days=4))
    session.commit()
    real_fetch = channel.fetch_message

    async def fetch_then_vanish(message_id):
        msg = await real_fetch(message_id)
        if message_id == header_id:
            channel.messages.pop(header_id)
        return msg

    channel.fetch_message = fetch_then_vanish
    channel.sends.clear()

    assert await upsert_board(bot, session, GUILD, "this_week") == "created"
    assert len(channel.sends) == 5
    assert get_board_post(session, GUILD, "this_week").message_id != header_id


async def test_card_message_deleted_externally_is_resent_on_edit(session):
    _seed(session)
    channel = FakeChannel()
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")
    post = get_board_post(session, GUILD, "this_week")
    gone = json.loads(post.message_ids)[0]
    channel.messages.pop(gone)
    session.query(Event).filter_by(external_id="wp:a").one().price_text = "$1"
    session.commit()
    channel.sends.clear()

    assert await upsert_board(bot, session, GUILD, "this_week") == "edited"
    assert [s["embed"].title for s in channel.sends] == ["a"]
    assert gone not in json.loads(get_board_post(session, GUILD, "this_week").message_ids)


async def test_stale_delete_skips_foreign_author(session):
    _seed(session)
    channel = FakeChannel()
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")
    post = get_board_post(session, GUILD, "this_week")
    victim = json.loads(post.message_ids)[1]
    channel.messages[victim].author = SimpleNamespace(id=7)
    session.query(Event).filter_by(external_id="wp:b").one().is_active = False
    session.commit()

    await upsert_board(bot, session, GUILD, "this_week")
    assert channel.deleted == []


async def test_stale_delete_tolerates_forbidden(session):
    _seed(session)
    channel = FakeChannel()
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")
    victim = json.loads(get_board_post(session, GUILD, "this_week").message_ids)[1]
    channel.messages[victim].delete = AsyncMock(side_effect=_http_exc(discord.Forbidden, 403))
    session.query(Event).filter_by(external_id="wp:b").one().is_active = False
    session.commit()

    assert await upsert_board(bot, session, GUILD, "this_week") == "edited"


async def test_legacy_single_message_post_is_upgraded_to_set(session):
    _seed(session, names=("a",))
    channel = FakeChannel()
    legacy = await channel.send(embed=discord.Embed(title="old"))
    session.add(BoardPost(guild_id=GUILD, board_type="this_week", period_key="2000-W01",
                          channel_id=111, message_id=legacy.id, render_hash="flatsha"))
    session.commit()
    channel.sends.clear()

    assert await upsert_board(_bot(channel), session, GUILD, "this_week") == "edited"
    assert legacy.embed.title == "This Week at Hightide"
    assert [s["embed"].title for s in channel.sends] == ["a"]
    post = get_board_post(session, GUILD, "this_week")
    assert post.message_id == legacy.id
    assert json.loads(post.render_hash)["keys"] == ["header", "wp:a"]


async def test_empty_week_is_header_only(session):
    _seed(session, names=())
    channel = FakeChannel()
    assert await upsert_board(_bot(channel), session, GUILD, "this_week") == "created"
    assert len(channel.sends) == 1
    assert channel.sends[0]["embed"].description == "No events found for this period."
    assert get_board_post(session, GUILD, "this_week").message_ids == "[]"


async def test_overflow_and_tba_messages_keyed(session):
    _seed(session, names=())
    for i in range(8):
        session.add(_event(f"e{i}", starts_at=datetime.now(TZ) + timedelta(minutes=i + 1)))
    session.add(_event("tba", starts_at=None))
    session.commit()
    channel = FakeChannel()
    await upsert_board(_bot(channel), session, GUILD, "this_week")
    keys = json.loads(get_board_post(session, GUILD, "this_week").render_hash)["keys"]
    assert keys[0] == "header" and keys[-2:] == ["overflow", "tba"]
    assert len(keys) == 10


async def test_save_failure_deletes_all_newly_sent_messages(session, monkeypatch):
    _seed(session)
    channel = FakeChannel()
    monkeypatch.setattr("src.services.board_service.save_board_post",
                        MagicMock(side_effect=RuntimeError("db down")))
    with pytest.raises(RuntimeError, match="db down"):
        await upsert_board(_bot(channel), session, GUILD, "this_week")
    assert channel.messages == {}
    assert len(channel.deleted) == 4


async def test_send_failure_midway_cleans_up_sent_messages(session):
    _seed(session)
    channel = FakeChannel()
    real_send = channel.send
    calls = []

    async def flaky(**kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise _http_exc(discord.Forbidden, 403)
        return await real_send(**kwargs)

    channel.send = flaky
    with pytest.raises(discord.Forbidden):
        await upsert_board(_bot(channel), session, GUILD, "this_week")
    assert channel.messages == {}
    assert get_board_post(session, GUILD, "this_week") is None


async def test_concurrent_week_upserts_serialised(session):
    _seed(session)
    channel = FakeChannel()
    real_send = channel.send

    async def slow_send(**kwargs):
        await asyncio.sleep(0.01)
        return await real_send(**kwargs)

    channel.send = slow_send
    bot = _bot(channel)
    results = await asyncio.gather(upsert_board(bot, session, GUILD, "this_week"),
                                   upsert_board(bot, session, GUILD, "this_week"))
    assert sorted(results) == ["created", "skipped"]
    assert len(channel.messages) == 4


async def test_delete_board_deletes_header_and_all_cards(session):
    _seed(session)
    channel = FakeChannel()
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")

    assert await delete_board(bot, session, GUILD, "this_week") == "deleted"
    assert channel.messages == {}
    assert len(channel.deleted) == 4
    assert get_board_post(session, GUILD, "this_week") is None


async def test_delete_board_authorship_guard_per_message(session):
    _seed(session)
    channel = FakeChannel()
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")
    post = get_board_post(session, GUILD, "this_week")
    foreign = json.loads(post.message_ids)[0]
    channel.messages[foreign].author = SimpleNamespace(id=7)

    await delete_board(bot, session, GUILD, "this_week")
    assert list(channel.messages) == [foreign]
    assert get_board_post(session, GUILD, "this_week") is None


async def test_delete_board_continues_past_missing_message(session):
    _seed(session)
    channel = FakeChannel()
    bot = _bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")
    post = get_board_post(session, GUILD, "this_week")
    channel.messages.pop(json.loads(post.message_ids)[0])

    await delete_board(bot, session, GUILD, "this_week")
    assert channel.messages == {}
    assert get_board_post(session, GUILD, "this_week") is None
