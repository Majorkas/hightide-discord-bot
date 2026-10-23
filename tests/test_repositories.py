from datetime import datetime, timezone

from src.db.models import BoardPost
from src.db.repositories import (
    get_active_events,
    get_board_post,
    get_or_create_config,
    mark_absent_events_stale,
    save_board_post,
    upsert_event,
)


def _ev(external_id="wp:1", **extra):
    return {"external_id": external_id, "title": "A", "url": "u", **extra}


def test_config_defaults(session):
    cfg = get_or_create_config(session, 7)
    assert cfg.timezone == "Australia/Perth"
    assert cfg.daily_refresh_time == "09:00"
    assert cfg.include_tba_section is True
    assert cfg.week_start_mode == "monday"


def test_get_or_create_config_idempotent(session):
    cfg = get_or_create_config(session, 7)
    cfg.timezone = "UTC"
    cfg.daily_refresh_time = "10:30"
    session.commit()
    cfg2 = get_or_create_config(session, 7)
    assert cfg2 is cfg
    assert cfg2.timezone == "UTC"
    assert cfg2.daily_refresh_time == "10:30"


def test_upsert_creates_then_updates(session):
    ev, created, _ = upsert_event(session, 7, _ev())
    assert created is True
    ev2, created2, _ = upsert_event(session, 7, {**_ev(), "title": "A2"})
    assert created2 is False
    assert ev2.title == "A2"
    assert len(get_active_events(session, 7)) == 1


def test_upsert_keeps_first_seen_at(session):
    ev, _, _ = upsert_event(session, 7, _ev())
    first = ev.first_seen_at
    ev2, _, _ = upsert_event(session, 7, {**_ev(), "title": "A2"})
    assert ev2.first_seen_at == first
    assert ev2.last_seen_at >= first


def test_upsert_partial_data_leaves_other_columns(session):
    upsert_event(session, 7, _ev(price_text="$10", summary="S", fingerprint="f1"))
    ev, created, _ = upsert_event(session, 7, {"external_id": "wp:1", "title": "New"})
    assert created is False
    assert ev.title == "New"
    assert ev.url == "u"
    assert ev.price_text == "$10"
    assert ev.summary == "S"
    assert ev.fingerprint == "f1"


def test_upsert_stores_fingerprint(session):
    ev, _, _ = upsert_event(session, 7, _ev(fingerprint="abc123"))
    assert ev.fingerprint == "abc123"


def test_stale_marking_after_two_misses(session):
    upsert_event(session, 7, _ev())
    assert mark_absent_events_stale(session, 7, set()) == 0  # 1 miss
    assert mark_absent_events_stale(session, 7, set()) == 1  # 2 misses -> deactivated
    assert get_active_events(session, 7) == []


def test_stale_seen_event_not_incremented(session):
    ev, _, _ = upsert_event(session, 7, _ev())
    assert mark_absent_events_stale(session, 7, {"wp:1"}) == 0
    assert ev.missed_refreshes == 0
    assert ev.is_active is True


def test_stale_miss_seen_miss_resets_count(session):
    ev, _, _ = upsert_event(session, 7, _ev())
    mark_absent_events_stale(session, 7, set())
    assert ev.missed_refreshes == 1
    upsert_event(session, 7, _ev())  # seen again
    assert ev.missed_refreshes == 0
    mark_absent_events_stale(session, 7, set())
    assert ev.missed_refreshes == 1
    assert ev.is_active is True


def test_reappearance_after_deactivation_reactivates(session):
    upsert_event(session, 7, _ev())
    mark_absent_events_stale(session, 7, set())
    mark_absent_events_stale(session, 7, set())
    assert get_active_events(session, 7) == []
    ev, created, _ = upsert_event(session, 7, _ev())
    assert created is False
    assert ev.is_active is True
    assert ev.missed_refreshes == 0


def test_inactive_events_not_recounted(session):
    ev, _, _ = upsert_event(session, 7, _ev())
    mark_absent_events_stale(session, 7, set())
    assert mark_absent_events_stale(session, 7, set()) == 1
    assert ev.missed_refreshes == 2
    assert mark_absent_events_stale(session, 7, set()) == 0
    assert ev.missed_refreshes == 2


def test_stale_guild_isolation(session):
    other, _, _ = upsert_event(session, 8, _ev())
    upsert_event(session, 7, _ev())
    mark_absent_events_stale(session, 7, set())
    mark_absent_events_stale(session, 7, set())
    assert other.is_active is True
    assert other.missed_refreshes == 0
    assert len(get_active_events(session, 8)) == 1


def test_get_board_post_absent_returns_none(session):
    assert get_board_post(session, 7, "this_week") is None


def test_save_board_post_create_then_update(session):
    p1 = save_board_post(session, 7, "this_week", "2026-W43", 1, 100, "h1")
    p2 = save_board_post(session, 7, "this_week", "2026-W43", 1, 200, "h2")
    assert p1.id == p2.id
    assert session.query(BoardPost).count() == 1
    got = get_board_post(session, 7, "this_week")
    assert got.message_id == 200
    assert got.render_hash == "h2"
    assert got.updated_at.tzinfo is not None
    assert got.updated_at <= datetime.now(timezone.utc)


def test_week_and_month_posts_coexist(session):
    save_board_post(session, 7, "this_week", "2026-W43", 1, 100, "w")
    save_board_post(session, 7, "this_month", "2026-10", 2, 200, "m")
    assert session.query(BoardPost).count() == 2
    assert get_board_post(session, 7, "this_week").message_id == 100
    assert get_board_post(session, 7, "this_month").message_id == 200


def test_upsert_event_changed_flag(session):
    _, created, changed = upsert_event(session, 7, _ev(fingerprint="f1"))
    assert created and changed
    _, created, changed = upsert_event(session, 7, _ev(fingerprint="f1"))
    assert not created and not changed
    _, created, changed = upsert_event(session, 7, _ev(fingerprint="f2"))
    assert not created and changed
