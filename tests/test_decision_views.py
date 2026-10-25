"""Tests for the decision Confirm/Dismiss buttons and the action handler."""
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from src.db.models import Decision
from src.discord_ui.decision_views import (
    DecisionAction, DecisionView, build_candidate_embed,
)
from src.services import decision_service
from src.services.decision_service import (
    create_pending_decision, handle_decision_action, message_link,
)

GUILD = 4242


def _interaction(user_id=99, guild_id=GUILD, with_embed=True):
    i = AsyncMock(spec=discord.Interaction)
    i.guild_id = guild_id
    i.user = MagicMock(id=user_id)
    i.response = AsyncMock()
    i.followup = AsyncMock()
    i.edit_original_response = AsyncMock()
    embed = discord.Embed(title="Decision detected — confirm?", description="d")
    i.message = MagicMock()
    i.message.embeds = [embed] if with_embed else []
    i.guild = MagicMock()
    i.guild.get_member = MagicMock(return_value=MagicMock(display_name="Sam"))
    i.guild.get_channel = MagicMock(return_value=MagicMock(name="staff"))
    return i


def _db_with_pending(db, summary="Run Friday drafts"):
    with db() as s:
        d = create_pending_decision(s, GUILD, 1, 2, 3, "raw excerpt", summary)
        return d.id


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.db.models import Base
    from src.db.session import get_engine, get_session_factory
    url = f"sqlite:///{tmp_path / 'dv.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    monkeypatch.setattr(decision_service, "_session_factory", factory)
    yield factory
    engine.dispose()


# --- view / button shape ---

def test_candidate_embed_escapes_and_mentions_decider():
    e = build_candidate_embed("We decided **drafts** @here", 7, "raw <@5> text")
    assert e.title == "Decision detected — confirm?"
    assert "<@7>" in e.description
    assert "@here" not in e.description.replace("@​here", "")
    assert "**drafts**" not in e.description  # markdown escaped
    assert "<@5>" not in e.description


def test_decision_view_has_confirm_and_dismiss():
    v = DecisionView(5)
    ids = [c.custom_id for c in v.children]
    assert ids == ["decision:confirm:5", "decision:dismiss:5"]


def test_decision_action_custom_id_roundtrip():
    a = DecisionAction("confirm", 12)
    assert a.item.custom_id == "decision:confirm:12"
    d = DecisionAction("dismiss", 12)
    assert d.item.custom_id == "decision:dismiss:12"


async def test_decision_action_dispatch_reconstructs_and_calls():
    i = _interaction()
    match = {"action": "confirm", "decision_id": "9"}
    with patch("src.services.decision_service.handle_decision_action",
               new=AsyncMock()) as m:
        btn = await DecisionAction.from_custom_id(i, MagicMock(), match)
        await btn.callback(i)
    m.assert_awaited_once_with(i, "confirm", 9)


# --- message link ---

def test_message_link_format():
    assert message_link(1, 2, 3) == "https://discord.com/channels/1/2/3"


# --- confirm path ---

async def test_confirm_marks_confirmed_and_edits_card(db, monkeypatch):
    did = _db_with_pending(db)
    monkeypatch.setattr(decision_service, "sync_decision_to_trello",
                        AsyncMock(return_value=True))
    i = _interaction()
    await handle_decision_action(i, "confirm", did)
    with db() as s:
        d = s.execute(__import__("sqlalchemy").select(Decision)).scalar_one()
        assert d.status == "confirmed" and d.confirmed_by == 99
    # card edited to confirmed, buttons removed
    i.edit_original_response.assert_awaited_once()
    kw = i.edit_original_response.call_args.kwargs
    assert kw["view"] is None and kw["embed"].color.value == 0x1F8B4C
    assert "synced to Trello" in i.followup.send.call_args.args[0]


async def test_confirm_without_trello_notes_failure(db, monkeypatch):
    did = _db_with_pending(db)
    monkeypatch.setattr(decision_service, "sync_decision_to_trello",
                        AsyncMock(return_value=False))
    i = _interaction()
    await handle_decision_action(i, "confirm", did)
    msg = i.followup.send.call_args.args[0]
    assert "confirmed" in msg and "sync failed" in msg


async def test_confirm_unknown_or_resolved_replies_gone(db, monkeypatch):
    i = _interaction()
    await handle_decision_action(i, "confirm", 9999)
    assert "no longer pending" in i.followup.send.call_args.args[0]


# --- dismiss path ---

async def test_dismiss_marks_rejected_and_edits_card(db):
    did = _db_with_pending(db)
    i = _interaction()
    await handle_decision_action(i, "dismiss", did)
    with db() as s:
        d = s.execute(__import__("sqlalchemy").select(Decision)).scalar_one()
        assert d.status == "rejected"
    kw = i.edit_original_response.call_args.kwargs
    assert kw["view"] is None and kw["embed"].color.value == 0x95A5A6
    assert "Dismissed" in i.followup.send.call_args.args[0]


async def test_dismiss_unknown_replies_gone(db):
    i = _interaction()
    await handle_decision_action(i, "dismiss", 9999)
    assert "no longer pending" in i.followup.send.call_args.args[0]
