"""Tests for /want commands and the persistent wants board."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
from zoneinfo import ZoneInfo

import discord
import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import sqlite

from src.db.models import Base, CardWant, GuildConfig
from src.db.repositories import get_board_post
from src.db.session import get_engine, get_session_factory
from src.discord_ui import commands_roles
from src.discord_ui.commands_roles import RolesCog
from src.discord_ui.embeds import build_wants_embed
from src.services import want_service
from src.services.want_service import (
    WantLimitError, add_want, expire_wants, list_wants, remove_want, update_wants_board,
)

GUILD = 4242
CHANNEL = 555
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
TZ = ZoneInfo("Australia/Perth")
BOLT_URI = "https://scryfall.com/card/msc/806/lightning-bolt?utm_source=api"


@pytest.fixture
def scry():
    with patch("src.services.want_service.scryfall_card_exact", new_callable=AsyncMock) as m:
        m.return_value = None
        yield m


def _hit(uri=BOLT_URI):
    return {"name": "Lightning Bolt", "scryfall_uri": uri}


async def _add(session, name="Lightning Bolt", user=1, note=None, now=NOW):
    return await add_want(session, GUILD, user, name, note, now=now)


def _make_channel(channel_id=CHANNEL):
    channel = AsyncMock()
    type(channel).id = PropertyMock(return_value=channel_id)
    return channel


def _make_msg(message_id):
    msg = AsyncMock()
    type(msg).id = PropertyMock(return_value=message_id)
    msg.edit = AsyncMock()
    return msg


def _not_found():
    resp = MagicMock()
    resp.status = 404
    return discord.NotFound(resp, "gone")


def _bot(channel):
    bot = MagicMock()
    bot.get_channel.return_value = channel
    bot.fetch_channel = AsyncMock(return_value=channel)
    return bot


def _config_channel(session, channel_id=CHANNEL, tz="Australia/Perth"):
    session.add(GuildConfig(guild_id=GUILD, timezone=tz, wants_channel_id=channel_id))
    session.commit()


def _want(name, uri=None, user=1, note=None, created=NOW, id_=None):
    return CardWant(id=id_, guild_id=GUILD, user_id=user, card_name=name, scryfall_uri=uri,
                    note=note, created_at=created, active=True)


# ---------------------------------------------------------------- service: add / resolve

async def test_add_stores_row_with_resolved_uri(session, scry):
    scry.return_value = _hit()
    w = await _add(session, "  Lightning Bolt  ", note=" bring a foil ")
    assert w.id is not None and w.active is True
    assert (w.guild_id, w.user_id) == (GUILD, 1)
    assert w.card_name == "Lightning Bolt"  # stripped
    assert w.note == "bring a foil"
    assert w.scryfall_uri == BOLT_URI
    assert w.created_at == NOW


async def test_add_resolves_exact_without_retries(session, scry):
    await _add(session, "Lightning Bolt")
    scry.assert_awaited_once()
    assert scry.call_args.args == ("Lightning Bolt",)
    assert scry.call_args.kwargs["retries"] is False
    assert scry.call_args.kwargs["timeout"] < want_service.DEFAULT_RESOLVE_TIMEOUT


async def test_unresolved_card_has_null_uri(session, scry):
    scry.return_value = None
    assert (await _add(session, "Not A Card")).scryfall_uri is None


@pytest.mark.parametrize("exc", [
    httpx.TransportError("down"),
    httpx.HTTPStatusError("403", request=MagicMock(), response=MagicMock()),
    RuntimeError("boom"),
])
async def test_resolution_failure_never_blocks_want(session, scry, exc):
    scry.side_effect = exc
    w = await _add(session, "Black Lotus")
    assert w.scryfall_uri is None
    assert len(list_wants(session, GUILD, now=NOW)) == 1


async def test_resolution_timeout_never_blocks_want(session, scry):
    async def slow(*a, **k):
        await asyncio.sleep(5)
    scry.side_effect = slow
    w = await add_want(session, GUILD, 1, "Black Lotus", None, now=NOW, resolve_timeout=0.01)
    assert w.scryfall_uri is None


async def test_unsafe_scryfall_uri_not_stored(session, scry):
    scry.return_value = {"scryfall_uri": "javascript:alert(1)"}
    assert (await _add(session)).scryfall_uri is None
    scry.return_value = {"scryfall_uri": None}
    assert (await _add(session, "B")).scryfall_uri is None
    scry.return_value = {}
    assert (await _add(session, "C")).scryfall_uri is None


@pytest.mark.parametrize("name", ["", "   ", "\n\t"])
async def test_empty_name_rejected_before_resolving(session, scry, name):
    with pytest.raises(ValueError):
        await _add(session, name)
    scry.assert_not_awaited()
    assert list_wants(session, GUILD, now=NOW) == []


async def test_name_and_note_length_limits(session, scry):
    await _add(session, "x" * 200, note="n" * 120)  # boundaries accepted
    with pytest.raises(ValueError):
        await _add(session, "x" * 201)
    with pytest.raises(ValueError):
        await _add(session, "ok", note="n" * 121)


async def test_blank_note_becomes_none(session, scry):
    assert (await _add(session, note="   ")).note is None


# ---------------------------------------------------------------- per-user cap

async def test_per_user_cap_of_ten_active(session, scry):
    for n in range(10):
        await _add(session, f"Card {n}")
    scry.reset_mock()
    with pytest.raises(WantLimitError):
        await _add(session, "Card 10")
    scry.assert_not_awaited()  # cap checked before the network call
    assert len(list_wants(session, GUILD, 1, now=NOW)) == 10


async def test_cap_is_per_user_and_per_guild(session, scry):
    for n in range(10):
        await _add(session, f"Card {n}")
    await _add(session, "Other", user=2)
    await add_want(session, GUILD + 1, 1, "Elsewhere", None, now=NOW)


async def test_cap_counts_only_active(session, scry):
    for n in range(10):
        await _add(session, f"Card {n}")
    # Expired wants free up slots.
    later = NOW + timedelta(days=31)
    await _add(session, "Fresh", now=later)
    assert [w.card_name for w in list_wants(session, GUILD, 1, now=later)] == ["Fresh"]


async def test_cap_rechecked_after_resolution(session, scry):
    for n in range(9):
        await _add(session, f"Card {n}")

    async def racing(*a, **k):
        # A concurrent add lands while this one awaits Scryfall.
        session.add(_want("Racer"))
        session.commit()
        return None
    scry.side_effect = racing
    with pytest.raises(WantLimitError):
        await _add(session, "Card 9")
    assert len(list_wants(session, GUILD, 1, now=NOW)) == 10


# ---------------------------------------------------------------- remove

async def test_owner_can_remove(session, scry):
    w = await _add(session, user=1)
    assert remove_want(session, GUILD, w.id, 1, False) is True
    assert list_wants(session, GUILD, now=NOW) == []


async def test_non_owner_cannot_remove(session, scry):
    w = await _add(session, user=1)
    assert remove_want(session, GUILD, w.id, 2, False) is False
    assert len(list_wants(session, GUILD, now=NOW)) == 1


async def test_admin_can_remove_any(session, scry):
    w = await _add(session, user=1)
    assert remove_want(session, GUILD, w.id, 99, True) is True
    assert list_wants(session, GUILD, now=NOW) == []


async def test_remove_unknown_or_other_guild_or_inactive(session, scry):
    w = await _add(session, user=1)
    assert remove_want(session, GUILD, 9999, 1, True) is False
    assert remove_want(session, GUILD + 1, w.id, 1, True) is False
    expire_wants(session, GUILD, NOW + timedelta(days=31))
    assert remove_want(session, GUILD, w.id, 1, False) is False


async def test_want_ids_not_reused_after_remove(session, scry):
    w1 = await _add(session)
    remove_want(session, GUILD, w1.id, 1, False)
    w2 = await _add(session, "Other")
    assert w2.id > w1.id


def test_card_wants_ddl_uses_autoincrement():
    ddl = str(CreateTable(CardWant.__table__).compile(dialect=sqlite.dialect()))
    assert "AUTOINCREMENT" in ddl


# ---------------------------------------------------------------- list / ordering

async def test_list_filters_by_user_and_guild(session, scry):
    await _add(session, "A", user=1)
    await _add(session, "B", user=2)
    await add_want(session, GUILD + 1, 1, "C", None, now=NOW)
    assert [w.card_name for w in list_wants(session, GUILD, now=NOW)] == ["A", "B"]
    assert [w.card_name for w in list_wants(session, GUILD, 2, now=NOW)] == ["B"]


async def test_alphabetical_order_z_then_a(session, scry):
    await _add(session, "Zealous Conscripts")
    await _add(session, "Ancestral Recall")
    names = [w.card_name for w in list_wants(session, GUILD, now=NOW)]
    assert names == ["Ancestral Recall", "Zealous Conscripts"]


async def test_alphabetical_order_case_insensitive(session, scry):
    for n in ["banana", "Apple", "cherry", "Banana split", "APRICOT"]:
        await _add(session, n)
    names = [w.card_name for w in list_wants(session, GUILD, now=NOW)]
    assert names == ["Apple", "APRICOT", "banana", "Banana split", "cherry"]


# ---------------------------------------------------------------- expiry

async def test_expiry_boundary_29_kept_31_expired(session, scry):
    await _add(session, "Old", now=NOW - timedelta(days=31))
    await _add(session, "Edge", now=NOW - timedelta(days=30))
    await _add(session, "Recent", now=NOW - timedelta(days=29))
    assert expire_wants(session, GUILD, NOW) == 1
    names = [w.card_name for w in list_wants(session, GUILD, now=NOW)]
    assert names == ["Edge", "Recent"]  # exactly 30 days is not yet "past"
    row = session.execute(select(CardWant).where(CardWant.card_name == "Old")).scalar_one()
    assert row.active is False


async def test_expire_is_idempotent_and_guild_scoped(session, scry):
    await _add(session, "Old", now=NOW - timedelta(days=40))
    await add_want(session, GUILD + 1, 1, "Other guild", None, now=NOW - timedelta(days=40))
    assert expire_wants(session, GUILD, NOW) == 1
    assert expire_wants(session, GUILD, NOW) == 0
    other = session.execute(
        select(CardWant).where(CardWant.guild_id == GUILD + 1)).scalar_one()
    assert other.active is True


async def test_list_wants_expires_at_list_time(session, scry):
    await _add(session, "Old", now=NOW - timedelta(days=31))
    assert list_wants(session, GUILD, now=NOW) == []
    row = session.execute(select(CardWant)).scalar_one()
    assert row.active is False


# ---------------------------------------------------------------- rendering

def test_resolved_is_linked_unresolved_is_plain():
    embed = build_wants_embed([_want("Lightning Bolt", BOLT_URI), _want("Mystery")], TZ)
    lines = embed.description.split("\n")
    assert lines[0].startswith(f"[**Lightning Bolt**]({BOLT_URI}) — <@1>")
    assert lines[1].startswith("**Mystery** — <@1>")
    assert "](" not in lines[1]
    assert embed.title == "Card Wants"


def test_render_orders_alphabetically_case_insensitive():
    embed = build_wants_embed([_want("zebra"), _want("Apple"), _want("banana")], TZ)
    order = [ln.split("**")[1] for ln in embed.description.split("\n")]
    assert order == ["Apple", "banana", "zebra"]


def test_line_format_with_note_and_date():
    created = datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)  # Perth: Oct 04, 04:00
    with_note = build_wants_embed([_want("A", note="foil only", user=7, created=created)], TZ)
    assert with_note.description == "**A** — <@7> · foil only · Oct 04"
    no_note = build_wants_embed([_want("A", user=7, created=created)], TZ)
    assert no_note.description == "**A** — <@7> · Oct 04"


def test_empty_board_text():
    assert build_wants_embed([], TZ).description == "No active wants."


def test_board_caps_at_25_lines():
    embed = build_wants_embed([_want(f"Card {n:02d}") for n in range(30)], TZ)
    lines = embed.description.split("\n")
    assert len(lines) == 26
    assert lines[-1] == "… +5 more"
    assert "Card 24" in lines[24] and "Card 25" not in embed.description


def test_board_respects_embed_description_limit():
    wants = [_want("<" * 100 + str(n), note="@" * 120, uri=BOLT_URI + "?" + "a" * 200)
             for n in range(25)]
    embed = build_wants_embed(wants, TZ)
    assert len(embed.description) <= 4096
    assert embed.description.rsplit("\n", 1)[-1].startswith("… +")


def test_hostile_card_and_note_are_escaped():
    hostile = "@everyone <@123> [x](http://evil) **b** `c` \u202e"
    embed = build_wants_embed(
        [_want(hostile, note="@here <@&5> [y](http://evil2)\nsecond line", user=9)], TZ)
    d = embed.description
    assert "@everyone" not in d.replace("@\u200beveryone", "")
    assert "<@123>" not in d and "<@&5>" not in d
    assert "[x](http://evil)" not in d and "[y](http://evil2)" not in d
    assert "\u202e" not in d
    assert "\n" not in d  # note newline collapsed: still exactly one line
    assert "<@9>" in d  # the author mention is ours and intentional


def test_hostile_card_name_in_link_text_cannot_break_out():
    embed = build_wants_embed([_want("x](http://evil)[y", BOLT_URI)], TZ)
    assert embed.description.startswith("[**x\\]\\(http://evil\\)\\[y**](")


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "ftp://x/y", "not a url", ""])
def test_unsafe_uri_renders_plain(bad):
    embed = build_wants_embed([_want("Card", bad)], TZ)
    assert embed.description.startswith("**Card** — ")


def test_uri_with_paren_is_percent_encoded():
    embed = build_wants_embed([_want("Card", "https://x.test/a)b c")], TZ)
    assert "](https://x.test/a%29b%20c)" in embed.description


# ---------------------------------------------------------------- board upsert

async def test_board_no_channel(session, scry):
    session.add(GuildConfig(guild_id=GUILD, timezone="UTC"))
    session.commit()
    channel = _make_channel()
    assert await update_wants_board(_bot(channel), session, GUILD, now=NOW) == "no_channel"
    channel.send.assert_not_awaited()


async def test_board_create_then_skip_then_edit(session, scry):
    _config_channel(session)
    channel = _make_channel()
    msg = _make_msg(900)
    channel.send.return_value = msg
    channel.fetch_message.return_value = msg
    bot = _bot(channel)

    await _add(session, "Lightning Bolt")
    assert await update_wants_board(bot, session, GUILD, now=NOW) == "created"
    channel.send.assert_awaited_once()
    kwargs = channel.send.call_args.kwargs
    assert kwargs["embed"].title == "Card Wants"
    assert "Lightning Bolt" in kwargs["embed"].description
    post = get_board_post(session, GUILD, "wants")
    assert (post.channel_id, post.message_id) == (CHANNEL, 900)

    assert await update_wants_board(bot, session, GUILD, now=NOW) == "skipped"
    channel.send.assert_awaited_once()
    msg.edit.assert_not_awaited()

    await _add(session, "Ancestral Recall", user=2)
    assert await update_wants_board(bot, session, GUILD, now=NOW) == "edited"
    msg.edit.assert_awaited_once()
    assert msg.edit.call_args.kwargs["embed"].description.startswith("**Ancestral Recall**")
    channel.send.assert_awaited_once()
    assert await update_wants_board(bot, session, GUILD, now=NOW) == "skipped"


async def test_board_never_pings(session, scry):
    _config_channel(session)
    channel = _make_channel()
    msg = _make_msg(900)
    channel.send.return_value = msg
    channel.fetch_message.return_value = msg
    bot = _bot(channel)
    await _add(session)
    await update_wants_board(bot, session, GUILD, now=NOW)
    am = channel.send.call_args.kwargs["allowed_mentions"]
    assert am.to_dict() == discord.AllowedMentions.none().to_dict()
    await _add(session, "Other", user=2)
    await update_wants_board(bot, session, GUILD, now=NOW)
    am = msg.edit.call_args.kwargs["allowed_mentions"]
    assert am.to_dict() == discord.AllowedMentions.none().to_dict()


async def test_board_recreates_when_message_deleted(session, scry):
    _config_channel(session)
    channel = _make_channel()
    old, new = _make_msg(900), _make_msg(901)
    channel.send.side_effect = [old, new]
    channel.fetch_message.side_effect = _not_found()
    bot = _bot(channel)
    await _add(session)
    assert await update_wants_board(bot, session, GUILD, now=NOW) == "created"
    assert await update_wants_board(bot, session, GUILD, now=NOW) == "created"
    assert channel.send.await_count == 2
    assert get_board_post(session, GUILD, "wants").message_id == 901


async def test_board_recreates_when_edit_hits_not_found(session, scry):
    _config_channel(session)
    channel = _make_channel()
    old, new = _make_msg(900), _make_msg(901)
    old.edit.side_effect = _not_found()
    channel.send.side_effect = [old, new]
    channel.fetch_message.return_value = old
    bot = _bot(channel)
    await _add(session)
    await update_wants_board(bot, session, GUILD, now=NOW)
    await _add(session, "Other")
    assert await update_wants_board(bot, session, GUILD, now=NOW) == "created"
    assert get_board_post(session, GUILD, "wants").message_id == 901


async def test_board_expired_wants_drop_off(session, scry):
    _config_channel(session)
    channel = _make_channel()
    msg = _make_msg(900)
    channel.send.return_value = msg
    channel.fetch_message.return_value = msg
    bot = _bot(channel)
    await _add(session, "Old")
    await update_wants_board(bot, session, GUILD, now=NOW)
    later = NOW + timedelta(days=31)
    assert await update_wants_board(bot, session, GUILD, now=later) == "edited"
    assert msg.edit.call_args.kwargs["embed"].description == "No active wants."
    row = session.execute(select(CardWant)).scalar_one()
    assert row.active is False


async def test_board_save_failure_removes_new_message(session, scry):
    _config_channel(session)
    channel = _make_channel()
    msg = _make_msg(900)
    channel.send.return_value = msg
    bot = _bot(channel)
    with patch("src.services.want_service.save_board_post", side_effect=RuntimeError("db")):
        with pytest.raises(RuntimeError):
            await update_wants_board(bot, session, GUILD, now=NOW)
    msg.delete.assert_awaited_once()


async def test_board_and_browse_render_identically(session, scry):
    scry.return_value = _hit()
    await _add(session, "Lightning Bolt")
    scry.return_value = None
    await _add(session, "apple", user=2, note="mint")
    _config_channel(session)
    channel = _make_channel()
    channel.send.return_value = _make_msg(900)
    await update_wants_board(_bot(channel), session, GUILD, now=NOW)
    board = channel.send.call_args.kwargs["embed"]
    browse = build_wants_embed(list_wants(session, GUILD, now=NOW), TZ)
    assert board.description == browse.description
    assert board.title == browse.title


# ---------------------------------------------------------------- handlers

@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'wants.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    monkeypatch.setattr(commands_roles, "_session_factory", factory)
    yield factory
    engine.dispose()
    factory.kw["bind"].dispose()


@pytest.fixture
def cog():
    bot = MagicMock()
    channel = _make_channel()
    msg = _make_msg(900)
    channel.send.return_value = msg
    channel.fetch_message.return_value = msg
    bot.get_channel.return_value = channel
    bot.fetch_channel = AsyncMock(return_value=channel)
    c = RolesCog(bot)
    c.channel, c.msg = channel, msg
    return c


def _interaction(user_id=1, manage_guild=False, guild_id=GUILD):
    i = AsyncMock(spec=discord.Interaction)
    i.response = AsyncMock()
    i.followup = AsyncMock()
    i.guild_id = guild_id
    i.user = MagicMock(id=user_id)
    i.user.guild_permissions = discord.Permissions(manage_guild=manage_guild)
    return i


def _sent(i):
    """(text, kwargs) of the single followup reply."""
    i.followup.send.assert_awaited_once()
    args, kwargs = i.followup.send.call_args
    return (args[0] if args else kwargs.get("content", "")), kwargs


def _configure(db, channel_id=CHANNEL):
    with db() as s:
        _config_channel(s, channel_id)


async def test_add_defers_ephemeral_before_resolving(db, cog, scry):
    _configure(db)
    order = []
    i = _interaction()
    i.response.defer.side_effect = lambda **k: order.append(("defer", k))
    scry.side_effect = lambda *a, **k: order.append("resolve")
    await cog.want_add.callback(cog, i, "Lightning Bolt", None)
    assert order[0] == ("defer", {"ephemeral": True})
    assert "resolve" in order
    i.response.send_message.assert_not_awaited()


async def test_add_resolves_with_lookup_timeout(db, cog, scry):
    _configure(db)
    await cog.want_add.callback(cog, _interaction(), "Lightning Bolt", None)
    assert scry.call_args.kwargs["retries"] is False
    assert scry.call_args.kwargs["timeout"] < commands_roles._LOOKUP_TIMEOUT


async def test_add_handler_enforces_wait_for_timeout(db, cog, scry, monkeypatch):
    monkeypatch.setattr(commands_roles, "_LOOKUP_TIMEOUT", 0.01)

    async def slow(*a, **k):
        await asyncio.sleep(5)
    scry.side_effect = slow
    _configure(db)
    i = _interaction()
    await cog.want_add.callback(cog, i, "Black Lotus", None)
    text_, _ = _sent(i)
    assert "Added to the wants board" in text_
    assert "no Scryfall match" in text_
    with db() as s:
        assert s.execute(select(CardWant)).scalar_one().scryfall_uri is None


async def test_add_confirm_linked_vs_unlinked(db, cog, scry):
    _configure(db)
    scry.return_value = _hit()
    i = _interaction()
    await cog.want_add.callback(cog, i, "Lightning Bolt", "foil")
    text_, kw = _sent(i)
    assert "Added to the wants board" in text_
    assert "no Scryfall match" not in text_
    assert kw["ephemeral"] is True

    scry.return_value = None
    i2 = _interaction()
    await cog.want_add.callback(cog, i2, "Mystery", None)
    text_, _ = _sent(i2)
    assert "(no Scryfall match — shown as plain text)" in text_


async def test_add_updates_board_message(db, cog, scry):
    _configure(db)
    scry.return_value = _hit()
    await cog.want_add.callback(cog, _interaction(user_id=7), "Lightning Bolt", "foil")
    cog.channel.send.assert_awaited_once()
    d = cog.channel.send.call_args.kwargs["embed"].description
    assert f"[**Lightning Bolt**]({BOLT_URI}) — <@7> · foil · " in d


async def test_add_strips_and_rejects_empty_name(db, cog, scry):
    _configure(db)
    i = _interaction()
    await cog.want_add.callback(cog, i, "   ", None)
    text_, kw = _sent(i)
    assert kw["ephemeral"] is True and "card name" in text_.lower()
    scry.assert_not_awaited()
    i2 = _interaction()
    await cog.want_add.callback(cog, i2, "  Lightning Bolt \n", None)
    assert scry.call_args.args == ("Lightning Bolt",)


async def test_add_rejects_overlong_inputs(db, cog, scry):
    _configure(db)
    i = _interaction()
    await cog.want_add.callback(cog, i, "x" * 201, None)
    assert "200" in _sent(i)[0]
    i2 = _interaction()
    await cog.want_add.callback(cog, i2, "ok", "n" * 121)
    assert "120" in _sent(i2)[0]
    with db() as s:
        assert s.execute(select(CardWant)).first() is None


async def test_add_cap_error_is_ephemeral(db, cog, scry):
    _configure(db)
    for n in range(10):
        await cog.want_add.callback(cog, _interaction(), f"Card {n}", None)
    i = _interaction()
    await cog.want_add.callback(cog, i, "Card 10", None)
    text_, kw = _sent(i)
    assert kw["ephemeral"] is True and "10" in text_
    with db() as s:
        assert len(s.execute(select(CardWant)).scalars().all()) == 10


async def test_add_without_wants_channel_still_saves(db, cog, scry):
    i = _interaction()
    await cog.want_add.callback(cog, i, "Lightning Bolt", None)
    text_, _ = _sent(i)
    assert "wants_channel" in text_
    cog.channel.send.assert_not_awaited()
    with db() as s:
        assert len(s.execute(select(CardWant)).scalars().all()) == 1


async def test_add_survives_board_update_failure(db, cog, scry):
    _configure(db)
    cog.channel.send.side_effect = discord.Forbidden(MagicMock(status=403), "nope")
    i = _interaction()
    await cog.want_add.callback(cog, i, "Lightning Bolt", None)
    text_, _ = _sent(i)
    assert "saved" in text_.lower() and "board" in text_.lower()
    with db() as s:
        assert len(s.execute(select(CardWant)).scalars().all()) == 1


async def test_add_escapes_card_name_in_confirm(db, cog, scry):
    _configure(db)
    i = _interaction()
    await cog.want_add.callback(cog, i, "@everyone [x](http://evil)", None)
    text_, _ = _sent(i)
    assert "@everyone" not in text_.replace("@\u200beveryone", "")
    assert "[x](http://evil)" not in text_


async def test_remove_owner_and_non_owner_and_admin(db, cog, scry):
    _configure(db)
    await cog.want_add.callback(cog, _interaction(user_id=1), "A", None)
    await cog.want_add.callback(cog, _interaction(user_id=1), "B", None)
    with db() as s:
        ids = {w.card_name: w.id for w in s.execute(select(CardWant)).scalars()}

    stranger = _interaction(user_id=2)
    await cog.want_remove.callback(cog, stranger, ids["A"])
    assert "not found" in _sent(stranger)[0].lower() or "your" in _sent(stranger)[0].lower()
    with db() as s:
        assert len(s.execute(select(CardWant)).scalars().all()) == 2

    owner = _interaction(user_id=1)
    await cog.want_remove.callback(cog, owner, ids["A"])
    assert "Removed" in _sent(owner)[0]

    admin = _interaction(user_id=3, manage_guild=True)
    await cog.want_remove.callback(cog, admin, ids["B"])
    assert "Removed" in _sent(admin)[0]
    with db() as s:
        assert s.execute(select(CardWant)).first() is None
    assert cog.msg.edit.call_args.kwargs["embed"].description == "No active wants."


async def test_remove_replies_ephemeral(db, cog, scry):
    _configure(db)
    i = _interaction()
    await cog.want_remove.callback(cog, i, 12345)
    assert _sent(i)[1]["ephemeral"] is True


async def test_list_shows_only_own_wants(db, cog, scry):
    _configure(db)
    await cog.want_add.callback(cog, _interaction(user_id=1), "Mine", "note")
    await cog.want_add.callback(cog, _interaction(user_id=2), "Theirs", None)
    i = _interaction(user_id=1)
    await cog.want_list.callback(cog, i)
    i.response.send_message.assert_awaited_once()
    args, kw = i.response.send_message.call_args
    assert kw["ephemeral"] is True
    assert "Mine" in args[0] and "Theirs" not in args[0]


async def test_list_empty(db, cog, scry):
    i = _interaction()
    await cog.want_list.callback(cog, i)
    args, kw = i.response.send_message.call_args
    assert kw["ephemeral"] is True and "no active wants" in args[0].lower()


async def test_list_escapes_hostile_content(db, cog, scry):
    await cog.want_add.callback(cog, _interaction(), "@everyone <@5> [x](http://e)", "@here")
    i = _interaction()
    await cog.want_list.callback(cog, i)
    args, _ = i.response.send_message.call_args
    assert "<@5>" not in args[0] and "[x](http://e)" not in args[0]
    assert "@everyone" not in args[0].replace("@\u200beveryone", "")


async def test_browse_matches_board_and_is_ephemeral(db, cog, scry):
    _configure(db)
    scry.return_value = _hit()
    await cog.want_add.callback(cog, _interaction(user_id=1), "Lightning Bolt", None)
    scry.return_value = None
    await cog.want_add.callback(cog, _interaction(user_id=2), "apple", "mint")
    board = cog.msg.edit.call_args.kwargs["embed"]  # second add edited the board in place

    i = _interaction(user_id=3)
    await cog.want_browse.callback(cog, i)
    i.response.send_message.assert_awaited_once()
    kw = i.response.send_message.call_args.kwargs
    assert kw["ephemeral"] is True
    assert kw["embed"].description == board.description
    assert kw["embed"].title == board.title
    assert kw["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


async def test_browse_without_config_uses_default_timezone(db, cog, scry):
    i = _interaction()
    await cog.want_browse.callback(cog, i)
    assert i.response.send_message.call_args.kwargs["embed"].description == "No active wants."


def test_want_group_shape():
    g = RolesCog.want_group
    assert g.name == "want"
    assert g.guild_only is True
    assert g.default_permissions is None  # public: not manage-guild
    assert {c.name for c in g.commands} == {"add", "browse", "remove", "list"}


async def test_want_add_has_per_user_cooldown():
    from datetime import datetime as dt
    import uuid
    uid = uuid.uuid4().int >> 64
    cmd = RolesCog.want_add

    def at(offset):
        i = _interaction(uid)
        i.created_at = dt(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset)
        return i
    from discord import app_commands
    assert await cmd._check_can_run(at(0)) is True
    with pytest.raises(app_commands.CommandOnCooldown):
        await cmd._check_can_run(at(1))
    assert await cmd._check_can_run(at(6)) is True


async def test_admin_wants_channel_setter(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from src.discord_ui import commands_admin
    from src.discord_ui.commands_admin import AdminCog
    url = f"sqlite:///{tmp_path / 'adm.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    monkeypatch.setattr(commands_admin, "_session_factory", factory)
    cog = AdminCog(MagicMock())
    i = MagicMock()
    i.guild_id = GUILD
    i.response.send_message = AsyncMock()
    await cog.set_wants_channel.callback(cog, i, SimpleNamespace(id=321, mention="<#321>"))
    with factory() as s:
        assert s.get(GuildConfig, GUILD).wants_channel_id == 321
    args, kw = i.response.send_message.call_args
    assert "<#321>" in args[0] and kw["ephemeral"] is True
    engine.dispose()
    factory.kw["bind"].dispose()


def test_wants_channel_column_added_to_legacy_db(tmp_path):
    from src.db.session import ensure_guild_config_v2_columns
    engine = get_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE guild_config (guild_id BIGINT PRIMARY KEY, timezone VARCHAR)"))
    ensure_guild_config_v2_columns(engine)
    ensure_guild_config_v2_columns(engine)
    with engine.connect() as conn:
        conn.execute(text("SELECT wants_channel_id FROM guild_config"))
    engine.dispose()
