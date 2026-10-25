"""Tests for decision-bot DB models, DDL, and repositories."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import sqlite

from src.db.models import (
    Base, Decision, DecisionChannel, DecisionConfig, TrelloMemberMap, TrelloSync,
)
from src.db.repositories import (
    add_decision, add_decision_channel, get_confirmed_unsynced_decisions, get_decision,
    get_or_create_decision_config, get_trello_member, get_trello_sync, list_decision_channels,
    list_decisions, record_trello_sync, remove_decision_channel, remove_trello_member,
    set_decision_status, set_trello_member,
)
from src.db.session import ensure_decision_tables, get_engine

GUILD = 4242


def _add(session, **kw):
    base = dict(guild_id=GUILD, channel_id=1, message_id=2, user_id=3,
                content="raw text", summary="We decided X")
    base.update(kw)
    return add_decision(session, **base)


# --- models / DDL ---

def test_decision_autoincrement():
    ddl = str(CreateTable(Decision.__table__).compile(dialect=sqlite.dialect()))
    assert "AUTOINCREMENT" in ddl


def test_ensure_decision_tables_creates_all_and_is_idempotent(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'd.db'}")
    ensure_decision_tables(engine)
    ensure_decision_tables(engine)  # idempotent
    with engine.connect() as conn:
        names = {r[0] for r in conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert {"decisions", "trello_sync", "decision_config", "decision_channels",
            "trello_member_map"} <= names
    engine.dispose()


# --- decision lifecycle ---

def test_add_decision_defaults_pending(session):
    d = _add(session)
    assert d.id is not None and d.status == "pending"
    assert (d.guild_id, d.channel_id, d.message_id, d.user_id) == (GUILD, 1, 2, 3)
    assert d.confirmed_by is None and d.confirmed_at is None


def test_confirm_records_confirmer_and_timestamp(session):
    d = _add(session)
    out = set_decision_status(session, GUILD, d.id, "confirmed", confirmed_by=99)
    assert out.status == "confirmed" and out.confirmed_by == 99
    assert out.confirmed_at is not None


def test_reject_has_no_confirmer(session):
    d = _add(session)
    out = set_decision_status(session, GUILD, d.id, "rejected")
    assert out.status == "rejected" and out.confirmed_by is None


def test_set_status_unknown_or_other_guild(session):
    d = _add(session)
    assert set_decision_status(session, GUILD, 9999, "confirmed") is None
    assert set_decision_status(session, GUILD + 1, d.id, "confirmed") is None


def test_list_decisions_filter_and_order(session):
    a = _add(session, summary="first")
    b = _add(session, summary="second")
    set_decision_status(session, GUILD, a.id, "confirmed", confirmed_by=1)
    assert [d.summary for d in list_decisions(session, GUILD)] == ["second", "first"]
    assert [d.summary for d in list_decisions(session, GUILD, "confirmed")] == ["first"]
    assert [d.summary for d in list_decisions(session, GUILD, "pending")] == ["second"]


def test_list_decisions_guild_scoped(session):
    _add(session, summary="ours")
    _add(session, guild_id=GUILD + 1, summary="theirs")
    assert [d.summary for d in list_decisions(session, GUILD)] == ["ours"]


# --- trello sync ---

def test_confirmed_unsynced_and_record(session):
    a = _add(session, summary="a")
    b = _add(session, summary="b")
    set_decision_status(session, GUILD, a.id, "confirmed", confirmed_by=1)
    set_decision_status(session, GUILD, b.id, "confirmed", confirmed_by=1)
    assert {d.summary for d in get_confirmed_unsynced_decisions(session, GUILD)} == {"a", "b"}
    record_trello_sync(session, a.id, "card1", "https://trello.com/c/card1")
    assert [d.summary for d in get_confirmed_unsynced_decisions(session, GUILD)] == ["b"]
    assert get_trello_sync(session, a.id).trello_card_id == "card1"


def test_record_trello_sync_idempotent(session):
    d = _add(session)
    set_decision_status(session, GUILD, d.id, "confirmed", confirmed_by=1)
    record_trello_sync(session, d.id, "card1", None)
    record_trello_sync(session, d.id, "card1", None)  # no raise
    rows = session.execute(select(TrelloSync).where(TrelloSync.decision_id == d.id)).scalars().all()
    assert len(rows) == 1


def test_pending_decisions_are_not_unsynced(session):
    _add(session)
    assert get_confirmed_unsynced_decisions(session, GUILD) == []


# --- decision config ---

def test_decision_config_get_or_create(session):
    cfg = get_or_create_decision_config(session, GUILD)
    assert cfg.enabled is False and cfg.trello_list_id is None
    cfg.enabled = True
    cfg.trello_board_id = "board1"
    cfg.trello_list_id = "list1"
    session.commit()
    again = get_or_create_decision_config(session, GUILD)
    assert again.enabled is True and again.trello_list_id == "list1"


# --- monitored channels ---

def test_decision_channels_add_list_remove(session):
    add_decision_channel(session, GUILD, 100)
    add_decision_channel(session, GUILD, 200)
    add_decision_channel(session, GUILD + 1, 300)
    assert set(list_decision_channels(session, GUILD)) == {100, 200}
    assert remove_decision_channel(session, GUILD, 100) is True
    assert list_decision_channels(session, GUILD) == [200]
    assert remove_decision_channel(session, GUILD, 999) is False


def test_decision_channels_add_idempotent(session):
    add_decision_channel(session, GUILD, 100)
    add_decision_channel(session, GUILD, 100)  # no raise
    assert list_decision_channels(session, GUILD) == [100]


# --- member map ---

def test_trello_member_map_set_get_update_remove(session):
    set_trello_member(session, GUILD, 7, "trello_a")
    assert get_trello_member(session, GUILD, 7) == "trello_a"
    set_trello_member(session, GUILD, 7, "trello_b")  # update
    assert get_trello_member(session, GUILD, 7) == "trello_b"
    assert get_trello_member(session, GUILD, 8) is None
    assert get_trello_member(session, GUILD + 1, 7) is None
    assert remove_trello_member(session, GUILD, 7) is True
    assert get_trello_member(session, GUILD, 7) is None
    assert remove_trello_member(session, GUILD, 7) is False
