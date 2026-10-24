import pytest
from sqlalchemy import inspect, text

from src.db.models import Base, EventRolePing, RoleKeyword
from src.db.repositories import (
    add_interest_role, add_keyword, get_keywords_by_role, has_ping,
    list_interest_roles, record_ping, remove_interest_role, remove_keyword,
)
from src.db.session import (
    ensure_guild_config_v2_columns, get_engine, get_session_factory,
)


def _session():
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = get_session_factory("sqlite:///:memory:")
    factory.configure(bind=engine)
    return factory()


def test_add_list_remove_role():
    s = _session()
    r = add_interest_role(s, 1, 111, "Yu-Gi-Oh", emoji="🎴")
    assert r.id is not None
    roles = list_interest_roles(s, 1)
    assert [x.label for x in roles] == ["Yu-Gi-Oh"]
    assert roles[0].emoji == "🎴"
    assert remove_interest_role(s, 1, r.id) is True
    assert list_interest_roles(s, 1) == []
    assert remove_interest_role(s, 1, 999) is False


def test_keywords_and_ping_once():
    s = _session()
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


def test_guild_isolation():
    s = _session()
    r1 = add_interest_role(s, 1, 111, "A")
    r2 = add_interest_role(s, 2, 222, "B")
    assert [x.label for x in list_interest_roles(s, 1)] == ["A"]
    assert [x.label for x in list_interest_roles(s, 2)] == ["B"]
    assert list_interest_roles(s, 1)[0].sort_order == 0
    assert list_interest_roles(s, 2)[0].sort_order == 0


def test_cascade_delete_removes_keywords_and_pings():
    """Verify that removing a role cascades to remove its keywords and pings."""
    s = _session()
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
    assert get_keywords_by_role(s, 1) == {}
    assert has_ping(s, 999, r.id) is False


def test_remove_interest_role_guild_guard():
    """Verify that remove_interest_role enforces guild isolation."""
    s = _session()
    r1 = add_interest_role(s, 1, 111, "Guild 1 Role")
    r2 = add_interest_role(s, 2, 222, "Guild 2 Role")
    
    # Attempt to remove guild 1 role as if it belongs to guild 2
    assert remove_interest_role(s, 2, r1.id) is False
    
    # Role should still exist
    assert len(list_interest_roles(s, 1)) == 1
    assert list_interest_roles(s, 1)[0].id == r1.id


def test_add_keyword_rejects_empty_input():
    """Verify that add_keyword rejects empty or whitespace-only keywords."""
    s = _session()
    r = add_interest_role(s, 1, 111, "Test Role")
    
    # Empty string should raise ValueError
    with pytest.raises(ValueError):
        add_keyword(s, r.id, "")
    
    # Whitespace-only should raise ValueError
    with pytest.raises(ValueError):
        add_keyword(s, r.id, "   ")
    with pytest.raises(ValueError):
        add_keyword(s, r.id, "\t\n")


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
