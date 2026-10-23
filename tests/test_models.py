from datetime import datetime, timezone

from src.db.models import Base, Event, GuildConfig
from src.db.session import get_engine, get_session_factory


def test_models_create_and_insert():
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = get_session_factory("sqlite:///:memory:")
    # bind factory's engine for the in-memory case
    factory.configure(bind=engine)
    session = factory()
    gc = GuildConfig(guild_id=1)
    session.add(gc)
    ev = Event(guild_id=1, external_id="wp:1", title="Test", url="https://x")
    session.add(ev)
    session.commit()
    assert session.query(GuildConfig).count() == 1
    assert session.query(Event).first().external_id == "wp:1"


def test_tz_datetime_round_trip():
    """Test that timezone-aware datetimes round-trip through SQLite correctly."""
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = get_session_factory("sqlite:///:memory:")
    factory.configure(bind=engine)
    
    # Insert an Event with timezone-aware starts_at
    session = factory()
    test_time = datetime(2026, 10, 23, 14, 30, 0, tzinfo=timezone.utc)
    event = Event(
        guild_id=1,
        external_id="wp:tz-test",
        title="TZ Test",
        url="https://x",
        starts_at=test_time,
    )
    session.add(event)
    session.commit()
    event_id = event.id
    
    # Force a fresh read by expiring all objects
    session.expire_all()
    
    # Retrieve and verify tzinfo is preserved
    retrieved_event = session.query(Event).filter_by(id=event_id).first()
    assert retrieved_event is not None
    assert retrieved_event.starts_at is not None
    assert retrieved_event.starts_at.tzinfo is not None
    assert retrieved_event.starts_at.tzinfo == timezone.utc
    assert retrieved_event.starts_at == test_time

