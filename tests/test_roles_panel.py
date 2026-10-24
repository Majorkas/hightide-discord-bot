from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy.exc import IntegrityError

from src.bot import create_bot, register_persistent_views
from src.db.models import Base, GuildConfig, InterestRole
from src.db.repositories import (
    add_interest_role, add_keyword, get_keywords_by_role, get_or_create_config,
    list_interest_roles,
)
from src.db.session import get_engine, get_session_factory
from src.discord_ui import commands_roles
from src.discord_ui.commands_roles import RolesCog
from src.discord_ui.role_views import RolesPanelView, build_roles_panel_embed
from src.services import role_service

GUILD = 12345
CHANNEL = 555
MESSAGE = 999


def test_roles_commands_registered():
    bot = create_bot()
    names = {c.name for c in bot.tree.get_commands()}
    assert "roles" in names


def test_panel_embed_lists_roles():
    roles = [InterestRole(id=1, guild_id=1, role_id=11, label="Yu-Gi-Oh", emoji="🎴"),
             InterestRole(id=2, guild_id=1, role_id=22, label="40k", emoji=None)]
    embed = build_roles_panel_embed(roles)
    assert "Yu-Gi-Oh" in embed.description
    assert "40k" in embed.description
    assert "<@&11>" in embed.description


def test_panel_view_buttons_have_persistent_custom_ids():
    roles = [InterestRole(id=7, guild_id=1, role_id=11, label="Yu-Gi-Oh")]
    view = RolesPanelView(roles)
    assert view.timeout is None
    customs = [c.custom_id for c in view.children]
    assert customs == ["role_toggle:7"]


def test_panel_embed_empty():
    assert "No roles configured" in build_roles_panel_embed([]).description


def test_roles_group_surface():
    bot = create_bot()
    group = {c.name: c for c in bot.tree.get_commands()}["roles"]
    assert {c.name for c in group.commands} == {"setup", "add", "remove", "keyword", "list"}
    assert group.default_permissions == discord.Permissions(manage_guild=True)
    assert group.guild_only is True


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    monkeypatch.setattr(commands_roles, "_session_factory", factory)
    monkeypatch.setattr(role_service, "_session_factory", factory)
    yield factory
    engine.dispose()
    factory.kw["bind"].dispose()


@pytest.fixture
def cog():
    return RolesCog(MagicMock())


def _panel_message():
    return SimpleNamespace(id=MESSAGE, edit=AsyncMock())


@pytest.fixture
def channel():
    ch = MagicMock()
    ch.id = CHANNEL
    ch.mention = "<#555>"
    ch.send = AsyncMock(return_value=SimpleNamespace(id=MESSAGE))
    ch.fetch_message = AsyncMock(return_value=_panel_message())
    return ch


@pytest.fixture
def interaction(channel):
    i = MagicMock()
    i.guild_id = GUILD
    i.guild.get_channel = MagicMock(return_value=channel)
    i.response.send_message = AsyncMock()
    return i


def _reply(interaction) -> str:
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    return args[0]


def _cfg(db):
    with db() as s:
        return s.get(GuildConfig, GUILD)


def _set_panel(db, channel_id=CHANNEL, message_id=MESSAGE):
    with db() as s:
        cfg = get_or_create_config(s, GUILD)
        cfg.roles_channel_id = channel_id
        cfg.roles_message_id = message_id
        s.commit()


def _role(role_id=11):
    r = MagicMock()
    r.id = role_id
    return r


def _http_error(cls):
    return cls(MagicMock(status=404 if cls is discord.NotFound else 403), "x")


# ---- setup ----

async def test_setup_posts_panel_and_stores_ids(db, cog, interaction, channel):
    with db() as s:
        add_interest_role(s, GUILD, 11, "MTG")
    await cog.setup.callback(cog, interaction, channel)
    channel.send.assert_awaited_once()
    kwargs = channel.send.call_args.kwargs
    assert "MTG" in kwargs["embed"].description
    assert isinstance(kwargs["view"], RolesPanelView)
    cfg = _cfg(db)
    assert (cfg.roles_channel_id, cfg.roles_message_id) == (CHANNEL, MESSAGE)
    assert "<#555>" in _reply(interaction)


async def test_setup_existing_panel_edits_in_place(db, cog, interaction, channel):
    _set_panel(db)
    msg = _panel_message()
    channel.fetch_message.return_value = msg
    await cog.setup.callback(cog, interaction, channel)
    channel.fetch_message.assert_awaited_once_with(MESSAGE)
    msg.edit.assert_awaited_once()
    channel.send.assert_not_awaited()
    assert _cfg(db).roles_message_id == MESSAGE
    _reply(interaction)


async def test_setup_existing_panel_missing_posts_new(db, cog, interaction, channel):
    _set_panel(db, message_id=1)
    channel.fetch_message.side_effect = _http_error(discord.NotFound)
    await cog.setup.callback(cog, interaction, channel)
    channel.send.assert_awaited_once()
    assert _cfg(db).roles_message_id == MESSAGE


async def test_setup_forbidden_fetch_replies_error(db, cog, interaction, channel):
    _set_panel(db)
    channel.fetch_message.side_effect = _http_error(discord.Forbidden)
    await cog.setup.callback(cog, interaction, channel)
    channel.send.assert_not_awaited()
    assert "permission" in _reply(interaction).lower()


async def test_setup_forbidden_send_replies_error(db, cog, interaction, channel):
    channel.send.side_effect = _http_error(discord.Forbidden)
    await cog.setup.callback(cog, interaction, channel)
    assert "permission" in _reply(interaction).lower()
    assert _cfg(db) is None or _cfg(db).roles_message_id is None


async def test_setup_other_channel_posts_new(db, cog, interaction, channel):
    _set_panel(db, channel_id=777)
    await cog.setup.callback(cog, interaction, channel)
    channel.fetch_message.assert_not_awaited()
    channel.send.assert_awaited_once()
    cfg = _cfg(db)
    assert (cfg.roles_channel_id, cfg.roles_message_id) == (CHANNEL, MESSAGE)


# ---- add ----

async def test_add_creates_role(db, cog, interaction):
    await cog.add.callback(cog, interaction, _role(11), "Yu-Gi-Oh", "🎴")
    with db() as s:
        rows = list_interest_roles(s, GUILD)
    assert [(r.role_id, r.label, r.emoji) for r in rows] == [(11, "Yu-Gi-Oh", "🎴")]
    assert "Yu-Gi-Oh" in _reply(interaction)


async def test_add_duplicate_role_friendly_error(db, cog, interaction):
    with db() as s:
        add_interest_role(s, GUILD, 11, "A")
    await cog.add.callback(cog, interaction, _role(11), "B", None)
    assert "already" in _reply(interaction).lower()
    with db() as s:
        assert [r.label for r in list_interest_roles(s, GUILD)] == ["A"]


async def test_add_integrity_error_rolls_back(db, cog, interaction, monkeypatch):
    def boom(*a, **k):
        raise IntegrityError("x", {}, Exception("dup"))
    monkeypatch.setattr(commands_roles, "add_interest_role", boom)
    await cog.add.callback(cog, interaction, _role(11), "B", None)
    assert "already" in _reply(interaction).lower()


async def test_add_label_too_long_rejected(db, cog, interaction):
    await cog.add.callback(cog, interaction, _role(11), "x" * 61, None)
    assert "60" in _reply(interaction)
    with db() as s:
        assert list_interest_roles(s, GUILD) == []


async def test_add_blank_label_rejected(db, cog, interaction):
    await cog.add.callback(cog, interaction, _role(11), "   ", None)
    _reply(interaction)
    with db() as s:
        assert list_interest_roles(s, GUILD) == []


@pytest.mark.parametrize("emoji", ["abc", "x" * 65])
async def test_add_invalid_emoji_rejected(db, cog, interaction, emoji):
    await cog.add.callback(cog, interaction, _role(11), "MTG", emoji)
    assert "emoji" in _reply(interaction).lower()
    with db() as s:
        assert list_interest_roles(s, GUILD) == []


async def test_add_custom_emoji_accepted(db, cog, interaction):
    await cog.add.callback(cog, interaction, _role(11), "MTG", "<:mtg:123456789012345678>")
    with db() as s:
        assert list_interest_roles(s, GUILD)[0].emoji == "<:mtg:123456789012345678>"


async def test_add_beyond_25_roles_rejected(db, cog, interaction):
    with db() as s:
        for n in range(25):
            add_interest_role(s, GUILD, 100 + n, f"R{n}")
    await cog.add.callback(cog, interaction, _role(999), "Extra", None)
    assert "25" in _reply(interaction)
    with db() as s:
        assert len(list_interest_roles(s, GUILD)) == 25


async def test_add_refreshes_existing_panel(db, cog, interaction, channel):
    _set_panel(db)
    msg = _panel_message()
    channel.fetch_message.return_value = msg
    await cog.add.callback(cog, interaction, _role(11), "MTG", None)
    msg.edit.assert_awaited_once()
    kwargs = msg.edit.call_args.kwargs
    assert "MTG" in kwargs["embed"].description
    assert [c.custom_id for c in kwargs["view"].children] == ["role_toggle:1"]
    _reply(interaction)


async def test_add_without_panel_does_not_fetch(db, cog, interaction, channel):
    await cog.add.callback(cog, interaction, _role(11), "MTG", None)
    channel.fetch_message.assert_not_awaited()


async def test_panel_deleted_clears_ids_and_notes(db, cog, interaction, channel):
    _set_panel(db)
    channel.fetch_message.side_effect = _http_error(discord.NotFound)
    await cog.add.callback(cog, interaction, _role(11), "MTG", None)
    cfg = _cfg(db)
    assert cfg.roles_message_id is None and cfg.roles_channel_id is None
    assert "/roles setup" in _reply(interaction)
    with db() as s:
        assert len(list_interest_roles(s, GUILD)) == 1


async def test_panel_channel_gone_clears_ids(db, cog, interaction):
    _set_panel(db)
    interaction.guild.get_channel = MagicMock(return_value=None)
    await cog.add.callback(cog, interaction, _role(11), "MTG", None)
    assert _cfg(db).roles_message_id is None
    assert "/roles setup" in _reply(interaction)


async def test_panel_forbidden_keeps_ids_and_errors(db, cog, interaction, channel):
    _set_panel(db)
    channel.fetch_message.side_effect = _http_error(discord.Forbidden)
    await cog.add.callback(cog, interaction, _role(11), "MTG", None)
    assert _cfg(db).roles_message_id == MESSAGE
    assert "permission" in _reply(interaction).lower()


async def test_add_escapes_mentions_in_reply(db, cog, interaction):
    await cog.add.callback(cog, interaction, _role(11), "@everyone", None)
    assert "@everyone" not in _reply(interaction).replace("@\u200beveryone", "")


# ---- remove ----

async def test_remove_role_and_refresh(db, cog, interaction, channel):
    with db() as s:
        r = add_interest_role(s, GUILD, 11, "MTG")
        rid = r.id
    _set_panel(db)
    msg = _panel_message()
    channel.fetch_message.return_value = msg
    await cog.remove.callback(cog, interaction, rid)
    with db() as s:
        assert list_interest_roles(s, GUILD) == []
    msg.edit.assert_awaited_once()
    _reply(interaction)


async def test_remove_unknown_id(db, cog, interaction):
    await cog.remove.callback(cog, interaction, 999)
    assert "no role" in _reply(interaction).lower()


async def test_remove_other_guild_role_refused(db, cog, interaction):
    with db() as s:
        r = add_interest_role(s, 999, 11, "Other")
        rid = r.id
    await cog.remove.callback(cog, interaction, rid)
    assert "no role" in _reply(interaction).lower()
    with db() as s:
        assert len(list_interest_roles(s, 999)) == 1


# ---- keyword ----

async def test_keyword_add_and_remove(db, cog, interaction):
    with db() as s:
        rid = add_interest_role(s, GUILD, 11, "MTG").id
    await cog.keyword.callback(cog, interaction, rid, "add", "Magic")
    with db() as s:
        assert get_keywords_by_role(s, GUILD)[rid] == ["magic"]
    _reply(interaction)

    interaction.response.send_message.reset_mock()
    await cog.keyword.callback(cog, interaction, rid, "remove", "magic")
    with db() as s:
        assert get_keywords_by_role(s, GUILD)[rid] == []
    _reply(interaction)


async def test_keyword_duplicate_friendly_error(db, cog, interaction):
    with db() as s:
        rid = add_interest_role(s, GUILD, 11, "MTG").id
        add_keyword(s, rid, "magic")
    await cog.keyword.callback(cog, interaction, rid, "add", "magic")
    assert "already" in _reply(interaction).lower()


async def test_keyword_empty_rejected(db, cog, interaction):
    with db() as s:
        rid = add_interest_role(s, GUILD, 11, "MTG").id
    await cog.keyword.callback(cog, interaction, rid, "add", "   ")
    assert "empty" in _reply(interaction).lower()


async def test_keyword_too_long_rejected(db, cog, interaction):
    with db() as s:
        rid = add_interest_role(s, GUILD, 11, "MTG").id
    await cog.keyword.callback(cog, interaction, rid, "add", "k" * 101)
    assert "100" in _reply(interaction)


async def test_keyword_remove_missing(db, cog, interaction):
    with db() as s:
        rid = add_interest_role(s, GUILD, 11, "MTG").id
    await cog.keyword.callback(cog, interaction, rid, "remove", "nope")
    assert "no keyword" in _reply(interaction).lower()


async def test_keyword_unknown_role(db, cog, interaction):
    await cog.keyword.callback(cog, interaction, 999, "add", "x")
    assert "no role" in _reply(interaction).lower()


async def test_keyword_other_guild_role_refused(db, cog, interaction):
    with db() as s:
        rid = add_interest_role(s, 999, 11, "Other").id
    await cog.keyword.callback(cog, interaction, rid, "add", "x")
    assert "no role" in _reply(interaction).lower()
    with db() as s:
        assert get_keywords_by_role(s, 999)[rid] == []


# ---- list ----

async def test_list_empty(db, cog, interaction):
    await cog.list_roles.callback(cog, interaction)
    assert "no roles" in _reply(interaction).lower()


async def test_list_shows_rows(db, cog, interaction):
    with db() as s:
        rid = add_interest_role(s, GUILD, 11, "MTG").id
        add_keyword(s, rid, "magic")
    await cog.list_roles.callback(cog, interaction)
    text = _reply(interaction)
    assert "MTG" in text and "magic" in text and "11" in text


# ---- toggle ----

def _toggle_interaction(role, member_roles, guild_id=GUILD):
    i = MagicMock()
    i.guild_id = guild_id
    i.guild.get_role = MagicMock(return_value=role)
    i.user.roles = member_roles
    i.user.add_roles = AsyncMock()
    i.user.remove_roles = AsyncMock()
    i.response.send_message = AsyncMock()
    return i


async def test_toggle_adds_role(db):
    with db() as s:
        rid = add_interest_role(s, GUILD, 11, "MTG").id
    role = object()
    i = _toggle_interaction(role, [])
    await role_service.handle_role_toggle(i, rid)
    i.user.add_roles.assert_awaited_once()
    assert i.user.add_roles.call_args.args[0] is role
    assert i.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_toggle_removes_role(db):
    with db() as s:
        rid = add_interest_role(s, GUILD, 11, "MTG").id
    role = object()
    i = _toggle_interaction(role, [role])
    await role_service.handle_role_toggle(i, rid)
    i.user.remove_roles.assert_awaited_once()
    i.user.add_roles.assert_not_awaited()


async def test_toggle_unknown_row(db):
    i = _toggle_interaction(object(), [])
    await role_service.handle_role_toggle(i, 404)
    assert "isn't available" in i.response.send_message.call_args.args[0]


async def test_toggle_wrong_guild(db):
    with db() as s:
        rid = add_interest_role(s, 999, 11, "MTG").id
    i = _toggle_interaction(object(), [])
    await role_service.handle_role_toggle(i, rid)
    i.user.add_roles.assert_not_awaited()
    assert "isn't available" in i.response.send_message.call_args.args[0]


async def test_toggle_disabled_row(db):
    with db() as s:
        row = add_interest_role(s, GUILD, 11, "MTG")
        row.enabled = False
        s.commit()
        rid = row.id
    i = _toggle_interaction(object(), [])
    await role_service.handle_role_toggle(i, rid)
    i.user.add_roles.assert_not_awaited()


async def test_toggle_role_deleted_in_discord(db):
    with db() as s:
        rid = add_interest_role(s, GUILD, 11, "MTG").id
    i = _toggle_interaction(None, [])
    await role_service.handle_role_toggle(i, rid)
    assert "no longer exists" in i.response.send_message.call_args.args[0]


async def test_toggle_forbidden(db):
    with db() as s:
        rid = add_interest_role(s, GUILD, 11, "MTG").id
    i = _toggle_interaction(object(), [])
    i.user.add_roles.side_effect = _http_error(discord.Forbidden)
    await role_service.handle_role_toggle(i, rid)
    assert "can't manage" in i.response.send_message.call_args.args[0]


async def test_button_callback_routes_to_toggle(monkeypatch):
    seen = []

    async def fake(interaction, row_id):
        seen.append((interaction, row_id))
    monkeypatch.setattr(role_service, "handle_role_toggle", fake)
    view = RolesPanelView([InterestRole(id=7, guild_id=1, role_id=11, label="X")])
    marker = object()
    await view.children[0].callback(marker)
    assert seen == [(marker, 7)]


# ---- persistent view registration ----

def test_register_persistent_views(db):
    with db() as s:
        add_interest_role(s, GUILD, 11, "MTG")
        cfg = get_or_create_config(s, GUILD)
        cfg.roles_message_id = MESSAGE
        get_or_create_config(s, 2)  # no panel -> skipped
        add_interest_role(s, 2, 22, "Skipped")
        s.commit()
    bot = MagicMock()
    register_persistent_views(bot, db)
    bot.add_view.assert_called_once()
    view = bot.add_view.call_args.args[0]
    assert [c.custom_id for c in view.children] == ["role_toggle:1"]
    assert bot.add_view.call_args.kwargs.get("message_id") == MESSAGE


def test_register_persistent_views_none(db):
    bot = MagicMock()
    register_persistent_views(bot, db)
    bot.add_view.assert_not_called()
