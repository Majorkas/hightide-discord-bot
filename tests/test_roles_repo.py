import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateTable

from src.db.models import EventRolePing, InterestRole, RoleKeyword
from src.db import repositories
from src.db.repositories import (
    add_interest_role, add_keyword, get_keywords_by_role, has_ping,
    list_interest_roles, record_ping, remove_interest_role, remove_keyword,
)
from src.db.session import (
    ensure_guild_config_v2_columns, get_engine, get_session_factory,
)


def test_add_list_remove_role(session):
    s = session
    r = add_interest_role(s, 1, 111, "Yu-Gi-Oh", emoji="🎴")
    assert r.id is not None
    roles = list_interest_roles(s, 1)
    assert [x.label for x in roles] == ["Yu-Gi-Oh"]
    assert roles[0].emoji == "🎴"
    assert remove_interest_role(s, 1, r.id) is True
    assert list_interest_roles(s, 1) == []
    assert remove_interest_role(s, 1, 999) is False


def test_keywords_and_ping_once(session):
    s = session
    r = add_interest_role(s, 1, 111, "MTG")
    add_keyword(s, r.id, "mtg")
    add_keyword(s, r.id, "magic")
    kw = get_keywords_by_role(s, 1)
    assert kw[r.id] == ["mtg", "magic"]
    assert remove_keyword(s, r.id, "magic") is True
    assert get_keywords_by_role(s, 1)[r.id] == ["mtg"]
    assert has_ping(s, 42, r.id) is False
    record_ping(s, 1, 42, r.id)
    assert has_ping(s, 42, r.id) is True


def test_guild_isolation(session):
    s = session
    add_interest_role(s, 1, 111, "A")
    add_interest_role(s, 2, 222, "B")
    assert [x.label for x in list_interest_roles(s, 1)] == ["A"]
    assert [x.label for x in list_interest_roles(s, 2)] == ["B"]
    assert list_interest_roles(s, 1)[0].sort_order == 0
    assert list_interest_roles(s, 2)[0].sort_order == 0


def test_cascade_delete_removes_keywords_and_pings(session):
    """Verify that removing a role cascades to remove its keywords and pings."""
    s = session
    r = add_interest_role(s, 1, 111, "Test Role")
    add_keyword(s, r.id, "keyword1")
    add_keyword(s, r.id, "keyword2")
    # Create an event and ping it with this role
    record_ping(s, 1, 999, r.id)

    # Verify setup
    assert get_keywords_by_role(s, 1)[r.id] == ["keyword1", "keyword2"]
    assert has_ping(s, 999, r.id) is True

    # Remove the role and verify cascade
    assert remove_interest_role(s, 1, r.id) is True

    # Direct assertions: keywords should be deleted
    orphan_kw = s.execute(select(RoleKeyword).where(RoleKeyword.interest_role_id == r.id)).scalars().all()
    assert len(orphan_kw) == 0

    # Direct assertions: pings should be deleted
    orphan_pings = s.execute(select(EventRolePing).where(EventRolePing.interest_role_id == r.id)).scalars().all()
    assert len(orphan_pings) == 0


def test_remove_interest_role_guild_guard(session):
    """Verify that remove_interest_role enforces guild isolation."""
    s = session
    r1 = add_interest_role(s, 1, 111, "Guild 1 Role")
    add_interest_role(s, 2, 222, "Guild 2 Role")

    # Attempt to remove guild 1 role as if it belongs to guild 2
    assert remove_interest_role(s, 2, r1.id) is False

    # Role should still exist
    assert len(list_interest_roles(s, 1)) == 1
    assert list_interest_roles(s, 1)[0].id == r1.id


def test_add_keyword_rejects_empty_input(session):
    """Verify that add_keyword rejects empty or whitespace-only keywords."""
    s = session
    r = add_interest_role(s, 1, 111, "Test Role")

    # Empty string should raise ValueError
    with pytest.raises(ValueError):
        add_keyword(s, r.id, "")

    # Whitespace-only should raise ValueError
    with pytest.raises(ValueError):
        add_keyword(s, r.id, "   ")
    with pytest.raises(ValueError):
        add_keyword(s, r.id, "\t\n")


def test_interest_role_sort_order_increments(session):
    """Verify that sort_order increments correctly for multiple roles in same guild."""
    s = session
    r1 = add_interest_role(s, 1, 111, "First")
    r2 = add_interest_role(s, 1, 222, "Second")
    r3 = add_interest_role(s, 1, 333, "Third")

    roles = list_interest_roles(s, 1)
    assert len(roles) == 3
    assert roles[0].sort_order == 0
    assert roles[1].sort_order == 1
    assert roles[2].sort_order == 2
    assert [r.label for r in roles] == ["First", "Second", "Third"]


_LEGACY_GUILD_CONFIG_DDL = (
    "CREATE TABLE guild_config (guild_id BIGINT PRIMARY KEY, timezone VARCHAR, "
    "this_week_channel_id BIGINT, this_month_channel_id BIGINT, daily_refresh_time VARCHAR, "
    "week_start_mode VARCHAR, include_tba_section BOOLEAN, delete_previous_month_message BOOLEAN, "
    "weekly_image_url VARCHAR, weekly_seeded BOOLEAN)"
)


def _legacy_guild_config_db(url):
    """Create a legacy guild_config table without the v2 columns."""
    engine = get_engine(url)
    with engine.begin() as conn:
        conn.execute(text(_LEGACY_GUILD_CONFIG_DDL))
        conn.execute(text(
            "INSERT INTO guild_config (guild_id, timezone, this_week_channel_id, "
            "this_month_channel_id, daily_refresh_time, week_start_mode, "
            "include_tba_section, delete_previous_month_message, weekly_image_url, "
            "weekly_seeded) VALUES (1, 'UTC', NULL, NULL, '09:00', 'monday', 1, 1, NULL, 0)"
        ))
    return engine


def test_ensure_guild_config_v2_columns_adds_daily_post_columns_with_default(tmp_path):
    engine = _legacy_guild_config_db(f"sqlite:///{tmp_path / 'legacy_dp.db'}")
    try:
        ensure_guild_config_v2_columns(engine)
        ensure_guild_config_v2_columns(engine)
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT daily_post_time, last_daily_post_date FROM guild_config")).one()
        assert tuple(row) == ("09:00", None)
    finally:
        engine.dispose()


def test_ensure_guild_config_v2_columns_idempotent_on_legacy_table(tmp_path):
    """Verify DDL helper adds v2 columns and is idempotent."""
    engine = _legacy_guild_config_db(f"sqlite:///{tmp_path / 'legacy_gc.db'}")
    try:
        # First call adds columns
        ensure_guild_config_v2_columns(engine)
        cols = {c["name"] for c in inspect(engine).get_columns("guild_config")}
        assert "roles_channel_id" in cols
        assert "roles_message_id" in cols
        assert "announce_channel_id" in cols

        # Second call is idempotent (no error)
        ensure_guild_config_v2_columns(engine)
        cols = {c["name"] for c in inspect(engine).get_columns("guild_config")}
        assert "roles_channel_id" in cols
        assert "roles_message_id" in cols
        assert "announce_channel_id" in cols
    finally:
        engine.dispose()


def test_interest_role_ids_never_reused_after_delete(session):
    r1 = add_interest_role(session, 1, 111, "First")
    first_id = r1.id
    assert remove_interest_role(session, 1, first_id) is True
    r2 = add_interest_role(session, 1, 222, "Second")
    assert r2.id > first_id


def test_interest_roles_ddl_uses_autoincrement():
    ddl = str(CreateTable(InterestRole.__table__).compile(dialect=sqlite.dialect()))
    assert "AUTOINCREMENT" in ddl


def test_record_ping_twice_one_row(session):
    r = add_interest_role(session, 1, 111, "MTG")
    record_ping(session, 1, 42, r.id)
    record_ping(session, 1, 42, r.id)
    rows = session.execute(select(EventRolePing)).scalars().all()
    assert len(rows) == 1


def test_record_ping_concurrent_duplicate_does_not_raise(session, monkeypatch):
    r = add_interest_role(session, 1, 111, "MTG")
    record_ping(session, 1, 42, r.id)
    # Simulate the race: has_ping saw nothing, but the row landed before our commit.
    monkeypatch.setattr(repositories, "has_ping", lambda *a, **k: False)
    record_ping(session, 1, 42, r.id)
    assert len(session.execute(select(EventRolePing)).scalars().all()) == 1
    record_ping(session, 1, 43, r.id)  # session still usable after rollback
    assert len(session.execute(select(EventRolePing)).scalars().all()) == 2


def test_v2_columns_partial_migration_succeeds(tmp_path):
    engine = _legacy_guild_config_db(f"sqlite:///{tmp_path / 'partial.db'}")
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN roles_channel_id BIGINT"))
        ensure_guild_config_v2_columns(engine)
        cols = {c["name"] for c in inspect(engine).get_columns("guild_config")}
        assert {"roles_channel_id", "roles_message_id", "announce_channel_id"} <= cols
    finally:
        engine.dispose()


def test_v2_columns_preexisting_row_survives_with_nulls_then_populates(tmp_path):
    engine = _legacy_guild_config_db(f"sqlite:///{tmp_path / 'row.db'}")
    try:
        ensure_guild_config_v2_columns(engine)
        with engine.begin() as conn:
            row = conn.execute(text(
                "SELECT timezone, roles_channel_id, roles_message_id, announce_channel_id "
                "FROM guild_config WHERE guild_id = 1")).one()
            assert tuple(row) == ("UTC", None, None, None)
            conn.execute(text("UPDATE guild_config SET roles_channel_id = 10, "
                              "roles_message_id = 20, announce_channel_id = 30 "
                              "WHERE guild_id = 1"))
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT roles_channel_id, roles_message_id, announce_channel_id "
                "FROM guild_config WHERE guild_id = 1")).one()
        assert tuple(row) == (10, 20, 30)
    finally:
        engine.dispose()


def test_v2_columns_non_duplicate_operational_error_reraises(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'empty.db'}")  # no guild_config table
    try:
        with pytest.raises(OperationalError):
            ensure_guild_config_v2_columns(engine)
    finally:
        engine.dispose()
