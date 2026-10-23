from src.db.models import Base
from src.db.repositories import (
    get_active_events,
    get_or_create_config,
    mark_absent_events_stale,
    upsert_event,
)
from src.db.session import get_engine, get_session_factory


def _session():
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = get_session_factory("sqlite:///:memory:")
    factory.configure(bind=engine)
    return factory()


def test_config_defaults():
    s = _session()
    cfg = get_or_create_config(s, 7)
    assert cfg.timezone == "Australia/Perth"
    assert cfg.daily_refresh_time == "09:00"
    assert cfg.include_tba_section is True


def test_upsert_creates_then_updates():
    s = _session()
    ev, created = upsert_event(s, 7, {"external_id": "wp:1", "title": "A", "url": "u"})
    assert created is True
    ev2, created2 = upsert_event(s, 7, {"external_id": "wp:1", "title": "A2", "url": "u"})
    assert created2 is False
    assert ev2.title == "A2"
    assert len(get_active_events(s, 7)) == 1


def test_stale_marking_after_two_misses():
    s = _session()
    upsert_event(s, 7, {"external_id": "wp:1", "title": "A", "url": "u"})
    assert mark_absent_events_stale(s, 7, set()) == 0  # 1 miss
    assert mark_absent_events_stale(s, 7, set()) == 1  # 2 misses -> deactivated
    assert get_active_events(s, 7) == []
