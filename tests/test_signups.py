"""Tests for signup table and service."""
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
    """Same event_id in two guilds → list_signups respects guild isolation."""
    from src.db.models import EventSignup
    
    # Create signup rows with SAME event_id but different guild_ids.
    # This genuinely tests that list_signups(guild1, event_id=X) never returns guild2's rows.
    session.add(EventSignup(guild_id=1, event_id=100, user_id=200))
    session.add(EventSignup(guild_id=2, event_id=100, user_id=201))
    session.commit()

    # Check isolation: guild1 and guild2 have separate signup lists for same event_id
    signups_guild1 = list_signups(session, guild_id=1, event_id=100)
    signups_guild2 = list_signups(session, guild_id=2, event_id=100)

    assert len(signups_guild1) == 1
    assert len(signups_guild2) == 1
    assert signups_guild1[0].guild_id == 1
    assert signups_guild1[0].user_id == 200
    assert signups_guild2[0].guild_id == 2
    assert signups_guild2[0].user_id == 201


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


def test_concurrent_toggle_race(tmp_path):
    """Concurrent toggle-add of same (event, user) on file-backed DB → exactly one row, no exception."""
    from unittest import mock
    from src.db.models import Base, EventSignup
    from src.db.session import get_engine, get_session_factory
    from sqlalchemy import select

    # Use a file-backed SQLite DB
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    
    engine = get_engine(db_url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(db_url)
    factory.configure(bind=engine)

    # Create the event
    setup_session = factory()
    setup_session.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Test Event",
        url="http://test.com",
        is_active=True
    ))
    setup_session.commit()
    setup_session.close()

    # Create two separate sessions
    session_a = factory()
    session_b = factory()

    try:
        # Mock session_a.execute to simulate the race: on the first call (the check in toggle_signup),
        # session_b inserts+commits, but we return a class that makes scalar_one_or_none return None
        # (simulating that A's check didn't see B's row yet due to isolation).
        # This causes toggle_signup to try to insert, hitting IntegrityError.
        real_execute = session_a.execute
        call_count = [0]

        def interleaved_execute(stmt, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # On the first execute (the check inside toggle_signup),
                # inject the concurrent insert from session_b
                session_b.add(EventSignup(guild_id=1, event_id=1, user_id=100))
                session_b.commit()
                # Return None to simulate race: A didn't see B's row yet
                class FakeResult:
                    def scalar_one_or_none(self):
                        return None
                return FakeResult()
            # For other calls (like count_signups), use the real execute
            return real_execute(stmt, *args, **kwargs)

        with mock.patch.object(session_a, "execute", side_effect=interleaved_execute):
            # Session A calls toggle_signup. Inside toggle_signup:
            # 1. It calls execute (our mock intercepts, B inserts, we return None result)
            # 2. Check returns None, so it tries to insert
            # 3. Insert fails with IntegrityError (B's row exists)
            # 4. Catches IntegrityError, returns added=True with recomputed count
            added, count = toggle_signup(session_a, guild_id=1, event_id=1, user_id=100)

        # Assert the race was handled correctly
        assert added is True, f"Expected added=True (race caught), got {added}"
        assert count == 1, f"Expected count=1, got {count}"

        # Verify exactly one row exists
        final_check = session_a.execute(select(EventSignup).where(
            EventSignup.event_id == 1,
            EventSignup.user_id == 100
        )).scalar_one_or_none()
        assert final_check is not None
    finally:
        session_a.close()
        session_b.close()
        engine.dispose()
