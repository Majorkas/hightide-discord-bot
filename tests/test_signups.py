"""Tests for signup table and service."""
from src.db.models import Base, Event, EventSignup
from src.db.repositories import get_event_by_id
from src.db.session import get_engine, get_session_factory
from src.services.signup_service import toggle_signup, count_signups, list_signups


def _get_session():
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = get_session_factory("sqlite:///:memory:")
    factory.configure(bind=engine)
    return factory()


def test_toggle_signup_add():
    """Toggle add → (True, 1)."""
    s = _get_session()
    # Create a test event
    s.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Test Event",
        url="http://test.com",
        is_active=True
    ))
    s.commit()
    
    # Toggle add
    added, count = toggle_signup(s, guild_id=1, event_id=1, user_id=100)
    assert added is True
    assert count == 1


def test_toggle_signup_remove():
    """Toggle again → (False, 0)."""
    s = _get_session()
    # Create a test event
    s.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Test Event",
        url="http://test.com",
        is_active=True
    ))
    s.commit()
    
    # First toggle adds
    toggle_signup(s, guild_id=1, event_id=1, user_id=100)
    
    # Second toggle removes
    added, count = toggle_signup(s, guild_id=1, event_id=1, user_id=100)
    assert added is False
    assert count == 0


def test_duplicate_inserts_impossible():
    """Second add path is the toggle (no duplicate inserts possible)."""
    s = _get_session()
    s.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Test Event",
        url="http://test.com",
        is_active=True
    ))
    s.commit()
    
    # First toggle adds
    added1, count1 = toggle_signup(s, guild_id=1, event_id=1, user_id=100)
    assert added1 is True
    assert count1 == 1
    
    # Try to add again (via toggle) - should toggle off
    added2, count2 = toggle_signup(s, guild_id=1, event_id=1, user_id=100)
    assert added2 is False
    assert count2 == 0
    
    # Toggle back on
    added3, count3 = toggle_signup(s, guild_id=1, event_id=1, user_id=100)
    assert added3 is True
    assert count3 == 1


def test_counts_per_event_isolated():
    """Counts per event isolated."""
    s = _get_session()
    s.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Event 1",
        url="http://test1.com",
        is_active=True
    ))
    s.add(Event(
        guild_id=1,
        external_id="ext_2",
        title="Event 2",
        url="http://test2.com",
        is_active=True
    ))
    s.commit()
    
    # Add signups to different events
    toggle_signup(s, guild_id=1, event_id=1, user_id=100)
    toggle_signup(s, guild_id=1, event_id=1, user_id=101)
    toggle_signup(s, guild_id=1, event_id=2, user_id=100)
    
    # Check counts are isolated per event
    assert count_signups(s, event_id=1) == 2
    assert count_signups(s, event_id=2) == 1


def test_list_returns_signups():
    """List returns signups."""
    s = _get_session()
    s.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Test Event",
        url="http://test.com",
        is_active=True
    ))
    s.commit()
    
    # Add several signups
    toggle_signup(s, guild_id=1, event_id=1, user_id=100)
    toggle_signup(s, guild_id=1, event_id=1, user_id=101)
    toggle_signup(s, guild_id=1, event_id=1, user_id=102)
    
    signups = list_signups(s, guild_id=1, event_id=1)
    assert len(signups) == 3
    user_ids = sorted([su.user_id for su in signups])
    assert user_ids == [100, 101, 102]


def test_get_event_by_id_unknown():
    """Unknown event id → None."""
    s = _get_session()
    event = get_event_by_id(s, guild_id=1, event_id=999)
    assert event is None


def test_get_event_by_id_found():
    """Get event by id returns event when found."""
    s = _get_session()
    s.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Test Event",
        url="http://test.com",
        is_active=True
    ))
    s.commit()
    
    event = get_event_by_id(s, guild_id=1, event_id=1)
    assert event is not None
    assert event.title == "Test Event"


def test_guild_isolation():
    """Same event id in two guilds → independent signup sets."""
    s = _get_session()
    s.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Event 1",
        url="http://test1.com",
        is_active=True
    ))
    s.add(Event(
        guild_id=2,
        external_id="ext_1",
        title="Event 1",
        url="http://test1.com",
        is_active=True
    ))
    s.commit()
    
    # Add signups to same event id but different guilds
    toggle_signup(s, guild_id=1, event_id=1, user_id=100)
    toggle_signup(s, guild_id=2, event_id=2, user_id=100)
    
    # Check isolation
    signups_guild1 = list_signups(s, guild_id=1, event_id=1)
    signups_guild2 = list_signups(s, guild_id=2, event_id=2)
    
    assert len(signups_guild1) == 1
    assert len(signups_guild2) == 1
    assert signups_guild1[0].guild_id == 1
    assert signups_guild2[0].guild_id == 2


def test_double_toggle_rapid():
    """Toggle twice in a row → net zero rows."""
    s = _get_session()
    s.add(Event(
        guild_id=1,
        external_id="ext_1",
        title="Test Event",
        url="http://test.com",
        is_active=True
    ))
    s.commit()
    
    # Toggle twice
    toggle_signup(s, guild_id=1, event_id=1, user_id=100)
    toggle_signup(s, guild_id=1, event_id=1, user_id=100)
    
    # Check no rows remain
    signups = list_signups(s, guild_id=1, event_id=1)
    assert len(signups) == 0
    assert count_signups(s, event_id=1) == 0
