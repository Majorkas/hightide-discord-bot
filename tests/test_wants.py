"""Tests for /want commands and the persistent wants board."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
from zoneinfo import ZoneInfo

import discord
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
    WantLimitError, add_want_from_printing, expire_wants, list_wants, remove_want,
    update_wants_board,
)

GUILD = 4242
CHANNEL = 555
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
TZ = ZoneInfo("Australia/Perth")
BOLT_URI = "https://scryfall.com/card/msc/806/lightning-bolt?utm_source=api"


def _card(name="Lightning Bolt", uri=BOLT_URI, set_code="msc", cn="806",
          image="https://img.scryfall.com/bolt.jpg", usd="0.50"):
    """A Scryfall printing dict."""
    return {"name": name, "scryfall_uri": uri, "set": set_code, "collector_number": cn,
            "image_uris": {"normal": image} if image else None,
            "prices": {"usd": usd}}


async def _add(session, name="Lightning Bolt", user=1, note=None, now=NOW, **card_kw):
    return add_want_from_printing(session, GUILD, user, _card(name, **card_kw), note, now=now)


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


def _want(name, uri=None, user=1, note=None, created=NOW, id_=None, set_code=None, cn=None):
    return CardWant(id=id_, guild_id=GUILD, user_id=user, card_name=name, scryfall_uri=uri,
                    note=note, created_at=created, active=True,
                    set_code=set_code, collector_number=cn)


# ---------------------------------------------------------------- service: add from printing

async def test_add_stores_row_with_printing_fields(session):
    w = await _add(session, note=" bring a foil ")
    assert w.id is not None and w.active is True
    assert (w.guild_id, w.user_id) == (GUILD, 1)
    assert w.card_name == "Lightning Bolt"
    assert w.note == "bring a foil"
    assert w.scryfall_uri == BOLT_URI
    assert w.set_code == "msc" and w.collector_number == "806"
    assert w.image_url == "https://img.scryfall.com/bolt.jpg"
    assert w.created_at == NOW


async def test_add_uses_scryfall_name_not_user_text(session):
    # The stored name is the card's canonical name; there is no free-text input.
    w = add_want_from_printing(session, GUILD, 1, _card("Osgood, Operation Double",
                                                        set_code="who", cn="191"), None, now=NOW)
    assert w.card_name == "Osgood, Operation Double"
    assert w.set_code == "who" and w.collector_number == "191"


async def test_add_dfc_image_falls_back_to_front_face(session):
    card = _card(image=None)
    card["card_faces"] = [{"image_uris": {"normal": "https://img/front.jpg"}}]
    w = add_want_from_printing(session, GUILD, 1, card, None, now=NOW)
    assert w.image_url == "https://img/front.jpg"


async def test_add_no_image_when_absent(session):
    card = _card(image=None)
    w = add_want_from_printing(session, GUILD, 1, card, None, now=NOW)
    assert w.image_url is None


@pytest.mark.parametrize("card", [
    _card(name=""),           # no name
    _card(uri="not a url"),   # no safe link
    _card(uri=""),            # empty link
])
async def test_add_rejects_printing_without_name_or_link(session, card):
    with pytest.raises(ValueError):
        add_want_from_printing(session, GUILD, 1, card, None, now=NOW)
    assert list_wants(session, GUILD, now=NOW) == []


async def test_add_blank_note_becomes_none(session):
    assert (await _add(session, note="   ")).note is None


@pytest.mark.parametrize("note", ["see https://x", "ftp://x"])
async def test_urls_rejected_in_note(session, note):
    with pytest.raises(ValueError, match="Links aren't allowed"):
        await _add(session, note=note)
    assert list_wants(session, GUILD, now=NOW) == []


async def test_note_length_limit(session):
    await _add(session, note="n" * 120)  # boundary accepted
    with pytest.raises(ValueError):
        await _add(session, note="n" * 121)


# ---------------------------------------------------------------- per-user cap

async def test_per_user_cap_of_ten_active(session):
    for n in range(10):
        await _add(session, f"Card {n}", set_code="x", cn=str(n))
    with pytest.raises(WantLimitError):
        await _add(session, "Card 10")
    assert len(list_wants(session, GUILD, 1, now=NOW)) == 10


async def test_cap_is_per_user_and_per_guild(session):
    for n in range(10):
        await _add(session, f"Card {n}", set_code="x", cn=str(n))
    await _add(session, "Other", user=2)
    add_want_from_printing(session, GUILD + 1, 1, _card("Elsewhere"), None, now=NOW)
    assert want_service._active_count(session, GUILD, 1) == 10
    assert want_service._active_count(session, GUILD, 2) == 1
    assert want_service._active_count(session, GUILD + 1, 1) == 1
    with pytest.raises(WantLimitError):
        await _add(session, "Eleventh")


async def test_cap_counts_only_active(session):
    for n in range(10):
        await _add(session, f"Card {n}", set_code="x", cn=str(n))
    later = NOW + timedelta(days=31)
    await _add(session, "Fresh", now=later)
    assert [w.card_name for w in list_wants(session, GUILD, 1, now=later)] == ["Fresh"]


async def test_cap_still_bites_at_the_limit(session):
    for n in range(9):
        await _add(session, f"Card {n}", set_code="x", cn=str(n))
    await _add(session, "Tenth")  # reaches the cap
    with pytest.raises(WantLimitError):
        await _add(session, "Eleventh")
    assert want_service._active_count(session, GUILD, 1) == 10


# ---------------------------------------------------------------- remove

async def test_owner_can_remove(session):
    w = await _add(session, user=1)
    assert remove_want(session, GUILD, w.id, 1, False) is True
    assert list_wants(session, GUILD, now=NOW) == []


async def test_non_owner_cannot_remove(session):
    w = await _add(session, user=1)
    assert remove_want(session, GUILD, w.id, 2, False) is False
    assert len(list_wants(session, GUILD, now=NOW)) == 1


async def test_admin_can_remove_any(session):
    w = await _add(session, user=1)
    assert remove_want(session, GUILD, w.id, 99, True) is True
    assert list_wants(session, GUILD, now=NOW) == []


async def test_remove_unknown_or_other_guild_or_inactive(session):
    w = await _add(session, user=1)
    assert remove_want(session, GUILD, 9999, 1, True) is False
    assert remove_want(session, GUILD + 1, w.id, 1, True) is False
    expire_wants(session, GUILD, NOW + timedelta(days=31))
    assert remove_want(session, GUILD, w.id, 1, False) is False


async def test_want_ids_not_reused_after_remove(session):
    w1 = await _add(session)
    remove_want(session, GUILD, w1.id, 1, False)
    w2 = await _add(session, "Other")
    assert w2.id > w1.id


def test_card_wants_ddl_uses_autoincrement():
    ddl = str(CreateTable(CardWant.__table__).compile(dialect=sqlite.dialect()))
    assert "AUTOINCREMENT" in ddl


# ---------------------------------------------------------------- list / ordering

async def test_list_filters_by_user_and_guild(session):
    await _add(session, "A", user=1)
    await _add(session, "B", user=2)
    add_want_from_printing(session, GUILD + 1, 1, _card("C"), None, now=NOW)
    assert [w.card_name for w in list_wants(session, GUILD, now=NOW)] == ["A", "B"]
    assert [w.card_name for w in list_wants(session, GUILD, 2, now=NOW)] == ["B"]


async def test_alphabetical_order_z_then_a(session):
    await _add(session, "Zealous Conscripts")
    await _add(session, "Ancestral Recall")
    names = [w.card_name for w in list_wants(session, GUILD, now=NOW)]
    assert names == ["Ancestral Recall", "Zealous Conscripts"]


async def test_alphabetical_order_case_insensitive(session):
    for n in ["banana", "Apple", "cherry", "Banana split", "APRICOT"]:
        await _add(session, n)
    names = [w.card_name for w in list_wants(session, GUILD, now=NOW)]
    assert names == ["Apple", "APRICOT", "banana", "Banana split", "cherry"]


# ---------------------------------------------------------------- expiry

async def test_expiry_boundary_29_kept_31_expired(session):
    await _add(session, "Old", now=NOW - timedelta(days=31))
    await _add(session, "Edge", now=NOW - timedelta(days=30))
    await _add(session, "Recent", now=NOW - timedelta(days=29))
    assert expire_wants(session, GUILD, NOW) == 1
    names = [w.card_name for w in list_wants(session, GUILD, now=NOW)]
    assert names == ["Edge", "Recent"]
    row = session.execute(select(CardWant).where(CardWant.card_name == "Old")).scalar_one()
    assert row.active is False


async def test_expire_is_idempotent_and_guild_scoped(session):
    await _add(session, "Old", now=NOW - timedelta(days=40))
    add_want_from_printing(session, GUILD + 1, 1, _card("Other guild"), None,
                           now=NOW - timedelta(days=40))
    assert expire_wants(session, GUILD, NOW) == 1
    assert expire_wants(session, GUILD, NOW) == 0
    other = session.execute(
        select(CardWant).where(CardWant.guild_id == GUILD + 1)).scalar_one()
    assert other.active is True


async def test_list_wants_expires_at_list_time(session):
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


def test_render_includes_printing_when_present():
    embed = build_wants_embed([_want("Lightning Bolt", BOLT_URI, set_code="msc", cn="806")], TZ)
    assert "[**Lightning Bolt**]" in embed.description
    assert "— MSC #806" in embed.description


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
    hostile = "@everyone <@123> [x](http://evil) **b** `c` ‮"
    embed = build_wants_embed(
        [_want(hostile, note="@here <@&5> [y](http://evil2)\nsecond line", user=9)], TZ)
    d = embed.description
    assert "@everyone" not in d.replace("@​everyone", "")
    assert "<@123>" not in d and "<@&5>" not in d
    assert "[x](http://evil)" not in d and "[y](http://evil2)" not in d
    assert "‮" not in d
    assert "\n" not in d
    assert "<@9>" in d


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


def test_accent_folded_order_in_renderer():
    wants = [_want("Zebra", id_=1), _want("Éclair", id_=2), _want("Ætherize", id_=3),
             _want("Apple", id_=4), _want("Eagle", id_=5)]
    order = [ln.split("**")[1] for ln in build_wants_embed(wants, TZ).description.split("\n")]
    assert order == ["Ætherize", "Apple", "Eagle", "Éclair", "Zebra"]


async def test_accent_folded_order_in_list_wants(session):
    for n in ["Zebra", "Éclair", "Ætherize", "Apple", "Eagle", "à la mode"]:
        await _add(session, n)
    names = [w.card_name for w in list_wants(session, GUILD, now=NOW)]
    assert names == ["à la mode", "Ætherize", "Apple", "Eagle", "Éclair", "Zebra"]


async def test_accent_equal_names_tiebreak_by_id(session):
    first = await _add(session, "Éclair")
    second = await _add(session, "Eclair", user=2)
    assert [w.id for w in list_wants(session, GUILD, now=NOW)] == [first.id, second.id]
    wants = [_want("Eclair", id_=9), _want("Éclair", id_=3)]
    assert [ln.split("**")[1] for ln in build_wants_embed(wants, TZ).description.split("\n")] \
        == ["Éclair", "Eclair"]


# ---------------------------------------------------------------- board upsert

async def test_board_no_channel(session):
    session.add(GuildConfig(guild_id=GUILD, timezone="UTC"))
    session.commit()
    channel = _make_channel()
    assert await update_wants_board(_bot(channel), session, GUILD, now=NOW) == "no_channel"
    channel.send.assert_not_awaited()


async def test_board_create_then_skip_then_edit(session):
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
    assert "Ancestral Recall" in msg.edit.call_args.kwargs["embed"].description
    channel.send.assert_awaited_once()
    assert await update_wants_board(bot, session, GUILD, now=NOW) == "skipped"


async def test_board_never_pings(session):
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


async def test_board_recreates_when_message_deleted(session):
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


async def test_board_recreates_when_edit_hits_not_found(session):
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


async def test_board_expired_wants_drop_off(session):
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


async def test_board_save_failure_removes_new_message(session):
    _config_channel(session)
    channel = _make_channel()
    msg = _make_msg(900)
    channel.send.return_value = msg
    bot = _bot(channel)
    with patch("src.services.want_service.save_board_post", side_effect=RuntimeError("db")):
        with pytest.raises(RuntimeError):
            await update_wants_board(bot, session, GUILD, now=NOW)
    msg.delete.assert_awaited_once()


async def test_board_and_browse_render_identically(session):
    await _add(session, "Lightning Bolt")
    await _add(session, "apple", user=2, note="mint", set_code="x", cn="1")
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
    i.followup.send.assert_awaited_once()
    args, kwargs = i.followup.send.call_args
    return (args[0] if args else kwargs.get("content", "")), kwargs


def _configure(db, channel_id=CHANNEL):
    with db() as s:
        _config_channel(s, channel_id)


def _patch_printings(monkeypatch, printings):
    m = AsyncMock(return_value=printings)
    monkeypatch.setattr(commands_roles, "scryfall_printings", m)
    return m


async def test_add_defers_ephemeral_then_shows_picker(db, cog, monkeypatch):
    _configure(db)
    _patch_printings(monkeypatch, [_card()])
    order = []
    i = _interaction()
    i.response.defer.side_effect = lambda **k: order.append(("defer", k))
    await cog.want_add.callback(cog, i, "Lightning Bolt", None)
    assert order[0] == ("defer", {"ephemeral": True})
    i.followup.send.assert_awaited_once()
    kw = i.followup.send.call_args.kwargs
    assert kw["ephemeral"] is True and kw["view"] is not None and kw["embed"] is not None


async def test_add_no_match_replies_ephemeral(db, cog, monkeypatch):
    _configure(db)
    _patch_printings(monkeypatch, [])
    i = _interaction()
    await cog.want_add.callback(cog, i, "Not A Real Card", None)
    text_, kw = i.followup.send.call_args
    assert "couldn't find" in text_[0]
    assert kw.get("ephemeral") is True or i.followup.send.call_args.kwargs["ephemeral"] is True


async def test_add_timeout_replies_ephemeral(db, cog, monkeypatch):
    _configure(db)
    async def slow(*a, **k):
        await asyncio.sleep(5)
    monkeypatch.setattr(commands_roles, "scryfall_printings", slow)
    monkeypatch.setattr(commands_roles, "_LOOKUP_TIMEOUT", 0.01)
    i = _interaction()
    await cog.want_add.callback(cog, i, "Black Lotus", None)
    text_ = i.followup.send.call_args.args[0]
    assert "too long" in text_


async def test_picker_confirm_adds_want_and_updates_board(db, cog, monkeypatch):
    _configure(db)
    card = _card("Osgood, Operation Double", set_code="who", cn="191",
                 uri="https://scryfall.com/card/who/191/osgood")
    _patch_printings(monkeypatch, [card])
    i = _interaction(user_id=7)
    await cog.want_add.callback(cog, i, "Osgood, Operation Double", "foil")
    view = i.followup.send.call_args.kwargs["view"]

    # simulate selecting the printing then confirming
    confirm_interaction = _interaction(user_id=7)
    confirm_interaction.response.defer = AsyncMock()
    confirm_interaction.edit_original_response = AsyncMock()
    await view._do_confirm(confirm_interaction)

    with db() as s:
        w = s.execute(select(CardWant)).scalar_one()
        assert w.card_name == "Osgood, Operation Double"
        assert w.set_code == "who" and w.collector_number == "191"
    # board was updated with the new want
    cog.channel.send.assert_awaited_once()
    d = cog.channel.send.call_args.kwargs["embed"].description
    assert "Osgood, Operation Double" in d and "WHO #191" in d


async def test_picker_rejects_other_users(db, cog, monkeypatch):
    _configure(db)
    _patch_printings(monkeypatch, [_card()])
    i = _interaction(user_id=1)
    await cog.want_add.callback(cog, i, "Lightning Bolt", None)
    view = i.followup.send.call_args.kwargs["view"]
    stranger = _interaction(user_id=2)
    assert await view.interaction_check(stranger) is False
    stranger.response.send_message.assert_awaited_once()


async def test_remove_owner_and_non_owner_and_admin(db, cog, monkeypatch):
    _configure(db)
    with db() as s:
        s.add(_want("A", id_=1, user=1))
        s.add(_want("B", id_=2, user=1))
        s.commit()

    stranger = _interaction(user_id=2)
    await cog.want_remove.callback(cog, stranger, 1)
    assert "not found" in _sent(stranger)[0].lower() or "your" in _sent(stranger)[0].lower()
    with db() as s:
        assert len(s.execute(select(CardWant)).scalars().all()) == 2

    owner = _interaction(user_id=1)
    await cog.want_remove.callback(cog, owner, 1)
    assert "Removed" in _sent(owner)[0]

    admin = _interaction(user_id=3, manage_guild=True)
    await cog.want_remove.callback(cog, admin, 2)
    assert "Removed" in _sent(admin)[0]
    with db() as s:
        assert s.execute(select(CardWant)).first() is None
    assert cog.msg.edit.call_args.kwargs["embed"].description == "No active wants."


async def test_remove_replies_ephemeral(db, cog):
    _configure(db)
    i = _interaction()
    await cog.want_remove.callback(cog, i, 12345)
    assert _sent(i)[1]["ephemeral"] is True


async def test_remove_no_id_opens_picker_with_own_wants(db, cog):
    _configure(db)
    with db() as s:
        s.add(_want("Lightning Bolt", BOLT_URI, user=1, set_code="msc", cn="806"))
        s.add(_want("Not Yours", user=2))
        s.commit()
    i = _interaction(user_id=1)
    await cog.want_remove.callback(cog, i, None)
    i.response.send_message.assert_awaited_once()
    kw = i.response.send_message.call_args.kwargs
    assert kw["ephemeral"] is True
    view = kw["view"]
    select = view.children[0]
    # only the user's own want is listed, labelled with name + printing
    assert len(select.options) == 1
    assert select.options[0].label == "Lightning Bolt — MSC #806"
    i.response.defer.assert_not_awaited()  # picker path doesn't defer


async def test_remove_no_id_with_no_wants_says_so(db, cog):
    _configure(db)
    i = _interaction(user_id=1)
    await cog.want_remove.callback(cog, i, None)
    args, kw = i.response.send_message.call_args
    assert "no active wants" in args[0].lower() and kw["ephemeral"] is True


async def test_remove_picker_selecting_removes_and_updates_board(db, cog):
    _configure(db)
    with db() as s:
        s.add(_want("Lightning Bolt", BOLT_URI, user=1, set_code="msc", cn="806"))
        s.commit()
        wid = s.execute(select(CardWant)).scalar_one().id
    i = _interaction(user_id=1)
    await cog.want_remove.callback(cog, i, None)
    view = i.response.send_message.call_args.kwargs["view"]

    sel_i = _interaction(user_id=1)
    sel_i.data = {"values": [str(wid)]}
    sel_i.response.defer = AsyncMock()
    sel_i.edit_original_response = AsyncMock()
    await view.children[0].callback(sel_i)

    with db() as s:
        assert s.execute(select(CardWant)).first() is None
    assert "Removed" in sel_i.followup.send.call_args.args[0]
    assert "Lightning Bolt" in sel_i.followup.send.call_args.args[0]
    cog.channel.send.assert_awaited_once()  # board updated


async def test_remove_picker_rejects_other_users(db, cog):
    _configure(db)
    with db() as s:
        s.add(_want("Lightning Bolt", user=1))
        s.commit()
    i = _interaction(user_id=1)
    await cog.want_remove.callback(cog, i, None)
    view = i.response.send_message.call_args.kwargs["view"]
    stranger = _interaction(user_id=2)
    assert await view.interaction_check(stranger) is False
    stranger.response.send_message.assert_awaited_once()


async def test_list_shows_only_own_wants(db, cog):
    _configure(db)
    with db() as s:
        s.add(_want("Mine", user=1, note="note"))
        s.add(_want("Theirs", user=2))
        s.commit()
    i = _interaction(user_id=1)
    await cog.want_list.callback(cog, i)
    i.response.send_message.assert_awaited_once()
    args, kw = i.response.send_message.call_args
    assert kw["ephemeral"] is True
    assert "Mine" in args[0] and "Theirs" not in args[0]


async def test_list_empty(db, cog):
    i = _interaction()
    await cog.want_list.callback(cog, i)
    args, kw = i.response.send_message.call_args
    assert kw["ephemeral"] is True and "no active wants" in args[0].lower()


async def test_browse_matches_board_and_is_ephemeral(db, cog):
    _configure(db)
    with db() as s:
        s.add(_want("Lightning Bolt", BOLT_URI, user=1))
        s.add(_want("apple", user=2, note="mint"))
        s.commit()
    # build the board once so we can compare
    channel = _make_channel()
    channel.send.return_value = _make_msg(900)
    await update_wants_board(_bot(channel), _session_for(db), GUILD, now=NOW)
    board = channel.send.call_args.kwargs["embed"]

    i = _interaction(user_id=3)
    await cog.want_browse.callback(cog, i)
    kw = i.response.send_message.call_args.kwargs
    assert kw["ephemeral"] is True
    assert kw["embed"].description == board.description
    assert kw["embed"].title == board.title
    assert kw["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


def _session_for(db):
    return db()


async def test_browse_without_config_uses_default_timezone(db, cog):
    i = _interaction()
    await cog.want_browse.callback(cog, i)
    assert i.response.send_message.call_args.kwargs["embed"].description == "No active wants."


def test_want_group_shape():
    g = RolesCog.want_group
    assert g.name == "want"
    assert g.guild_only is True
    assert g.default_permissions is None
    assert {c.name for c in g.commands} == {"add", "browse", "remove", "list"}


async def test_want_add_has_per_user_cooldown():
    import uuid
    from discord import app_commands
    uid = uuid.uuid4().int >> 64
    cmd = RolesCog.want_add

    def at(offset):
        i = _interaction(uid)
        i.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset)
        return i
    assert await cmd._check_can_run(at(0)) is True
    with pytest.raises(app_commands.CommandOnCooldown):
        await cmd._check_can_run(at(1))
    assert await cmd._check_can_run(at(6)) is True


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


def test_card_wants_printing_columns_added_to_legacy_db(tmp_path):
    from src.db.session import ensure_card_wants_printing_columns
    engine = get_engine(f"sqlite:///{tmp_path / 'legacy2.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE card_wants (id INTEGER PRIMARY KEY, guild_id BIGINT, user_id BIGINT, "
            "card_name VARCHAR, scryfall_uri VARCHAR, note VARCHAR, created_at DATETIME, "
            "active BOOLEAN)"))
    ensure_card_wants_printing_columns(engine)
    ensure_card_wants_printing_columns(engine)
    with engine.connect() as conn:
        row = conn.execute(text("PRAGMA table_info(card_wants)")).fetchall()
    cols = {r[1] for r in row}
    assert {"set_code", "collector_number", "image_url"} <= cols
    engine.dispose()


# ---------------------------------------------------------------- board lock / channel move

async def test_board_lock_concurrent_updates_send_once(session):
    _config_channel(session)
    channel = _make_channel()
    msg = _make_msg(900)

    async def slow_send(**kwargs):
        await asyncio.sleep(0.02)
        return msg
    channel.send.side_effect = slow_send
    channel.fetch_message.return_value = msg
    bot = _bot(channel)
    await _add(session, "Lightning Bolt")
    results = await asyncio.gather(update_wants_board(bot, session, GUILD, now=NOW),
                                   update_wants_board(bot, session, GUILD, now=NOW))
    assert sorted(results) == ["created", "skipped"]
    assert channel.send.await_count == 1


OLD_CHANNEL = 111
BOT_ID = 77


def _moved_setup(session, *, get_channel_old=True):
    from src.db.repositories import save_board_post
    _config_channel(session)
    save_board_post(session, GUILD, "wants", "wants", OLD_CHANNEL, 700, "oldhash")
    old_ch, new_ch = _make_channel(OLD_CHANNEL), _make_channel(CHANNEL)
    old_msg, new_msg = _make_msg(700), _make_msg(900)
    old_msg.author = MagicMock(id=BOT_ID)
    old_ch.fetch_message.return_value = old_msg
    new_ch.send.return_value = new_msg
    bot = MagicMock()
    bot.user = MagicMock(id=BOT_ID)
    chans = {OLD_CHANNEL: old_ch, CHANNEL: new_ch}
    bot.get_channel.side_effect = lambda cid: (
        None if cid == OLD_CHANNEL and not get_channel_old else chans.get(cid))
    bot.fetch_channel = AsyncMock(side_effect=lambda cid: chans[cid])
    return bot, old_ch, new_ch, old_msg


async def test_channel_move_posts_new_and_deletes_old(session):
    bot, old_ch, new_ch, old_msg = _moved_setup(session)
    assert await update_wants_board(bot, session, GUILD, now=NOW) == "created"
    new_ch.send.assert_awaited_once()
    old_ch.fetch_message.assert_awaited_once_with(700)
    old_msg.delete.assert_awaited_once()
    post = get_board_post(session, GUILD, "wants")
    assert (post.channel_id, post.message_id) == (CHANNEL, 900)
    bot.fetch_channel.assert_not_awaited()


async def test_channel_move_old_channel_fetch_fallback(session):
    bot, old_ch, new_ch, old_msg = _moved_setup(session, get_channel_old=False)
    assert await update_wants_board(bot, session, GUILD, now=NOW) == "created"
    bot.fetch_channel.assert_awaited_once_with(OLD_CHANNEL)
    old_msg.delete.assert_awaited_once()
    assert get_board_post(session, GUILD, "wants").channel_id == CHANNEL


async def test_channel_move_authorship_guard_keeps_foreign_message(session):
    bot, old_ch, new_ch, old_msg = _moved_setup(session)
    old_msg.author = MagicMock(id=BOT_ID + 1)
    assert await update_wants_board(bot, session, GUILD, now=NOW) == "created"
    old_msg.delete.assert_not_awaited()


@pytest.mark.parametrize("where", ["fetch_message", "delete", "fetch_channel"])
@pytest.mark.parametrize("exc", [
    _not_found(), discord.Forbidden(MagicMock(status=403), "no"),
    discord.HTTPException(MagicMock(status=500), "boom"),
])
async def test_channel_move_cleanup_failures_are_tolerated(session, where, exc):
    bot, old_ch, new_ch, old_msg = _moved_setup(session, get_channel_old=(where != "fetch_channel"))
    if where == "fetch_message":
        old_ch.fetch_message.side_effect = exc
    elif where == "delete":
        old_msg.delete.side_effect = exc
    else:
        bot.fetch_channel = AsyncMock(
            side_effect=lambda cid: (_ for _ in ()).throw(exc) if cid == OLD_CHANNEL else new_ch)
    assert await update_wants_board(bot, session, GUILD, now=NOW) == "created"
    post = get_board_post(session, GUILD, "wants")
    assert (post.channel_id, post.message_id) == (CHANNEL, 900)


async def test_channel_move_save_failure_leaves_old_message(session):
    bot, old_ch, new_ch, old_msg = _moved_setup(session)
    with patch("src.services.want_service.save_board_post", side_effect=RuntimeError("db")):
        with pytest.raises(RuntimeError):
            await update_wants_board(bot, session, GUILD, now=NOW)
    old_msg.delete.assert_not_awaited()
    new_ch.send.return_value.delete.assert_awaited_once()


async def test_same_channel_never_deletes_anything(session):
    _config_channel(session)
    channel = _make_channel()
    msg = _make_msg(900)
    channel.send.return_value = msg
    channel.fetch_message.return_value = msg
    bot = _bot(channel)
    await update_wants_board(bot, session, GUILD, now=NOW)
    await _add(session)
    await update_wants_board(bot, session, GUILD, now=NOW)
    msg.delete.assert_not_awaited()


# ---------------------------------------------------------------- Perth date rendering

def test_board_renders_perth_date_not_utc():
    created = datetime(2026, 10, 23, 17, 0, tzinfo=timezone.utc)  # Perth: Oct 24 01:00
    d = build_wants_embed([_want("A", created=created, user=7)], TZ).description
    assert d.endswith("Oct 24") and "Oct 23" not in d


async def test_board_post_renders_perth_date(session):
    _config_channel(session)
    session.add(_want("A", created=datetime(2026, 10, 23, 17, 0, tzinfo=timezone.utc)))
    session.commit()
    channel = _make_channel()
    channel.send.return_value = _make_msg(900)
    await update_wants_board(_bot(channel), session, GUILD, now=NOW)
    d = channel.send.call_args.kwargs["embed"].description
    assert d.endswith("Oct 24") and "Oct 23" not in d


async def test_list_truncates_on_line_boundary(db, cog):
    import re
    _configure(db)
    with db() as s:
        for n in range(30):
            s.add(_want(f"{n:02d}" + "*" * 90, note="_" * 20, user=1))
        s.commit()
    i = _interaction(user_id=1)
    await cog.want_list.callback(cog, i)
    text_ = i.response.send_message.call_args.args[0]
    assert len(text_) <= commands_roles._MAX_MESSAGE_CHARS
    lines = text_.split("\n")
    assert re.fullmatch(r"… \+\d+ more", lines[-1])
    kept = lines[:-1]
    assert 0 < len(kept) < 30 and len(kept) + int(lines[-1].split("+")[1].split()[0]) == 30
    line_re = re.compile(r"#\d+ \*\*(?:\\.|[^*\\])+\*\* · (?:\\.|[^*\\])+ · [A-Z][a-z]{2} \d{2}")
    assert all(line_re.fullmatch(ln) for ln in kept)
    assert not text_.endswith("\\")
