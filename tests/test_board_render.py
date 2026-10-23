from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.db.models import Event
from src.services.board_service import filter_events_for_board, render_board_hash

TZ = ZoneInfo("Australia/Perth")


def _cfg(**kw):
    base = dict(week_start_mode="today_start", include_tba_section=True)
    base.update(kw)
    return SimpleNamespace(**base)


def _ev(title, days_ahead, time_known=True):
    return Event(
        guild_id=1, external_id=f"wp:{title}", title=title, url="https://x",
        starts_at=datetime.now(TZ) + timedelta(days=days_ahead),
        time_known=time_known, is_active=True,
    )


def test_week_window_includes_today_and_6_days():
    events = [_ev("today", 0), _ev("day6", 6), _ev("day8", 8)]
    got = filter_events_for_board(events, "this_week", _cfg(), TZ)
    titles = {e.title for e in got}
    assert "today" in titles and "day6" in titles and "day8" not in titles


def test_month_window_current_month_only():
    now = datetime.now(TZ)
    in_month = _ev("in", 0)
    # force an event into next month
    next_month = Event(
        guild_id=1, external_id="wp:out", title="out", url="u",
        starts_at=(now.replace(day=28) + timedelta(days=10)), is_active=True,
    )
    got = filter_events_for_board([in_month, next_month], "this_month", _cfg(), TZ)
    titles = {e.title for e in got}
    assert "in" in titles and "out" not in titles


def test_tba_included_only_when_enabled():
    tba = Event(guild_id=1, external_id="wp:tba", title="tba", url="u",
                starts_at=None, is_active=True)
    assert filter_events_for_board([tba], "this_week", _cfg(), TZ) == [tba]
    assert filter_events_for_board(
        [tba], "this_week", _cfg(include_tba_section=False), TZ) == []


def test_render_hash_changes_with_content():
    a = [_ev("x", 1)]
    b = [_ev("y", 1)]
    assert render_board_hash(a, "this_week", _cfg(), TZ) != \
        render_board_hash(b, "this_week", _cfg(), TZ)
    assert render_board_hash(a, "this_week", _cfg(), TZ) == \
        render_board_hash(a, "this_week", _cfg(), TZ)
