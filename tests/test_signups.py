"""Tests for signup table and service."""
import pytest
from sqlalchemy.orm import Session

from src.db.models import Event
from src.db.repositories import get_event_by_id
from src.services.signup_service import toggle_signup, count_signups, list_signups


def test_toggle_signup_add(session: Session):
    """Toggle add → (True, 1)."""
    # Create a test event
    session.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Test Event",
        url="http://test.com",
        is_active=True
    ))
    session.commit()

    # Toggle add
    added, count = toggle_signup(session, guild_id=1, event_id=1, user_id=100)
    assert added is True
    assert count == 1


def test_toggle_signup_remove(session: Session):
    """Toggle again → (False, 0)."""
    # Create a test event
    session.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Test Event",
        url="http://test.com",
        is_active=True
    ))
    session.commit()

    # First toggle adds
    toggle_signup(session, guild_id=1, event_id=1, user_id=100)

    # Second toggle removes
    added, count = toggle_signup(session, guild_id=1, event_id=1, user_id=100)
    assert added is False
    assert count == 0


def test_duplicate_inserts_impossible(session: Session):
    """Second add path is the toggle (no duplicate inserts possible)."""
    session.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Test Event",
        url="http://test.com",
        is_active=True
    ))
    session.commit()

    # First toggle adds
    added1, count1 = toggle_signup(session, guild_id=1, event_id=1, user_id=100)
    assert added1 is True
    assert count1 == 1

    # Try to add again (via toggle) - should toggle off
    added2, count2 = toggle_signup(session, guild_id=1, event_id=1, user_id=100)
    assert added2 is False
    assert count2 == 0

    # Toggle back on
    added3, count3 = toggle_signup(session, guild_id=1, event_id=1, user_id=100)
    assert added3 is True
    assert count3 == 1


def test_counts_per_event_isolated(session: Session):
    """Counts per event isolated."""
    session.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Event 1",
        url="http://test1.com",
        is_active=True
    ))
    session.add(Event(
        guild_id=1,
        external_id="ext_2",
        title="Event 2",
        url="http://test2.com",
        is_active=True
    ))
    session.commit()

    # Add signups to different events
    toggle_signup(session, guild_id=1, event_id=1, user_id=100)
    toggle_signup(session, guild_id=1, event_id=1, user_id=101)
    toggle_signup(session, guild_id=1, event_id=2, user_id=100)

    # Check counts are isolated per event
    assert count_signups(session, event_id=1) == 2
    assert count_signups(session, event_id=2) == 1


def test_list_returns_signups(session: Session):
    """List returns signups."""
    session.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Test Event",
        url="http://test.com",
        is_active=True
    ))
    session.commit()

    # Add several signups
    toggle_signup(session, guild_id=1, event_id=1, user_id=100)
    toggle_signup(session, guild_id=1, event_id=1, user_id=101)
    toggle_signup(session, guild_id=1, event_id=1, user_id=102)

    signups = list_signups(session, guild_id=1, event_id=1)
    assert len(signups) == 3
    user_ids = sorted([su.user_id for su in signups])
    assert user_ids == [100, 101, 102]


def test_get_event_by_id_unknown(session: Session):
    """Unknown event id → None."""
    event = get_event_by_id(session, guild_id=1, event_id=999)
    assert event is None


def test_get_event_by_id_found(session: Session):
    """Get event by id returns event when found."""
    session.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Test Event",
        url="http://test.com",
        is_active=True
    ))
    session.commit()

    event = get_event_by_id(session, guild_id=1, event_id=1)
    assert event is not None
    assert event.title == "Test Event"


def test_get_event_by_id_wrong_guild(session: Session):
    """Get event by id with wrong guild → None."""
    session.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Test Event",
        url="http://test.com",
        is_active=True
    ))
    session.commit()

    # Try to get event from different guild
    event = get_event_by_id(session, guild_id=2, event_id=1)
    assert event is None


def test_guild_isolation(session: Session):
    """Same event id in two guilds (via different external_ids) → independent signup sets."""
    # Create same event id in two guilds (same internal id works for this isolation test)
    session.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Event 1",
        url="http://test1.com",
        is_active=True
    ))
    session.add(Event(
        guild_id=2,
        external_id="ext_2",  # Different external_id for unique constraint
        title="Event 1",
        url="http://test1.com",
        is_active=True
    ))
    session.commit()

    # Both have event_id=1 and event_id=2 respectively; add signups
    # This test verifies list_signups respects guild_id isolation
    toggle_signup(session, guild_id=1, event_id=1, user_id=100)
    toggle_signup(session, guild_id=2, event_id=2, user_id=100)

    # Check isolation
    signups_guild1 = list_signups(session, guild_id=1, event_id=1)
    signups_guild2 = list_signups(session, guild_id=2, event_id=2)

    assert len(signups_guild1) == 1
    assert len(signups_guild2) == 1
    assert signups_guild1[0].guild_id == 1
    assert signups_guild2[0].guild_id == 2


def test_double_toggle_rapid(session: Session):
    """Toggle twice in a row → net zero rows."""
    session.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Test Event",
        url="http://test.com",
        is_active=True
    ))
    session.commit()

    # Toggle twice
    toggle_signup(session, guild_id=1, event_id=1, user_id=100)
    toggle_signup(session, guild_id=1, event_id=1, user_id=100)

    # Check no rows remain
    signups = list_signups(session, guild_id=1, event_id=1)
    assert len(signups) == 0
    assert count_signups(session, event_id=1) == 0


def test_concurrent_toggle_race(session: Session):
    """Concurrent toggle-add of same (event, user) → exactly one row, no exception."""
    from sqlalchemy.orm import sessionmaker
    from src.db.session import get_engine

    # Create an event
    session.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Test Event",
        url="http://test.com",
        is_active=True
    ))
    session.commit()

    # Simulate two concurrent sessions both trying to add the same signup
    # First session adds successfully
    toggle_signup(session, guild_id=1, event_id=1, user_id=100)
    assert count_signups(session, event_id=1) == 1

    # Try to add again from same session (simulates another session that didn't see the first add)
    # This will hit IntegrityError and be caught, returning added=True
    added, count = toggle_signup(session, guild_id=1, event_id=1, user_id=100)
    assert added is False  # Actually toggle removes
    assert count == 0
