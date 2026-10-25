from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.db.models import Event
from src.services.board_service import _window, filter_events_for_board, period_key, render_board_hash

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
    # "today" is 3 hours from now: still upcoming, in this week; day6 in; day8 out
    today = Event(guild_id=1, external_id="wp:today", title="today", url="https://x",
                  starts_at=datetime.now(TZ) + timedelta(hours=3), time_known=True, is_active=True)
    events = [today, _ev("day6", 6), _ev("day8", 8)]
    got = filter_events_for_board(events, "this_week", _cfg(), TZ)
    titles = {e.title for e in got}
    assert "today" in titles and "day6" in titles and "day8" not in titles


def test_month_window_current_month_only():
    now = datetime.now(TZ)
    # later this month (upcoming), clamped so we never cross into next month
    in_month = Event(guild_id=1, external_id="wp:in", title="in", url="u",
                     starts_at=now + timedelta(hours=3), is_active=True)
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


def test_render_hash_sensitive_to_summary_and_image():
    base = _ev("x", 1)
    base.summary = "one"
    base.image_url = None
    same = _ev("x", 1)
    same.starts_at = base.starts_at
    same.summary = "one"
    same.image_url = None
    h = render_board_hash([base], "this_week", _cfg(), TZ)
    assert h == render_board_hash([same], "this_week", _cfg(), TZ)
    same.summary = "two"
    assert h != render_board_hash([same], "this_week", _cfg(), TZ)
    same.summary = "one"
    same.image_url = "https://img.example/a.png"
    assert h != render_board_hash([same], "this_week", _cfg(), TZ)


# --- window boundaries with a fixed clock ---

def _at(*args):
    return datetime(*args, tzinfo=TZ)


def _ev_at(title, starts_at, **kw):
    return Event(guild_id=1, external_id=f"wp:{title}", title=title, url="https://x",
                 starts_at=starts_at, time_known=True, is_active=True, **kw)


def _titles(events, board, now, cfg=None):
    return {e.title for e in filter_events_for_board(events, board, cfg or _cfg(), TZ, now=now)}


def test_month_first_day_shows_while_upcoming():
    # An event on the month's 1st, viewed from just before it, is in-window and upcoming
    now = _at(2026, 10, 1, 8, 0)
    assert _titles([_ev_at("first", _at(2026, 10, 1, 18, 30))], "this_month", now) == {"first"}


def test_month_last_minute_included():
    now = _at(2026, 10, 15, 12, 0)
    assert _titles([_ev_at("last", _at(2026, 10, 31, 23, 59))], "this_month", now) == {"last"}


def test_next_month_first_midnight_excluded():
    now = _at(2026, 10, 15, 12, 0)
    assert _titles([_ev_at("next", _at(2026, 11, 1, 0, 0))], "this_month", now) == set()


def test_december_rolls_over_to_january():
    now = _at(2026, 12, 15, 12, 0)
    events = [
        _ev_at("dec1", _at(2026, 12, 1, 0, 0)),    # past: dropped once started
        _ev_at("dec31", _at(2026, 12, 31, 23, 59)),
        _ev_at("jan1", _at(2027, 1, 1, 0, 0)),
        _ev_at("nov30", _at(2026, 11, 30, 23, 59)),
    ]
    assert _titles(events, "this_month", now) == {"dec31"}


def test_utc_event_lands_in_local_month():
    """2026-10-31T17:00Z is Nov 1 01:00 in Perth: November, not October."""
    ev = _ev_at("edge", datetime(2026, 10, 31, 17, 0, tzinfo=ZoneInfo("UTC")))
    assert _titles([ev], "this_month", _at(2026, 10, 15, 12, 0)) == set()
    # Nov 1 00:30 Perth: the 01:00 event hasn't started yet, so it's shown
    assert _titles([ev], "this_month", _at(2026, 11, 1, 0, 30)) == {"edge"}
    # Nov 1 02:00 Perth: it has started, so it's dropped even though it's the 1st
    assert _titles([ev], "this_month", _at(2026, 11, 1, 2, 0)) == set()


def test_past_events_dropped_in_all_week_modes():
    """A started event leaves the board regardless of week_start_mode."""
    now = _at(2026, 10, 23, 15, 0)
    ev = [_ev_at("morning", _at(2026, 10, 23, 10, 0))]
    assert _titles(ev, "this_week", now, _cfg(week_start_mode="today_start")) == set()
    assert _titles(ev, "this_week", now, _cfg(week_start_mode="now")) == set()
    assert _titles(ev, "this_week", now, _cfg(week_start_mode="monday")) == set()


# --- monday mode: current calendar week, past days dropped ---

MON = _cfg(week_start_mode="monday")


def test_monday_mode_wednesday_shows_wed_to_sunday():
    now = _at(2026, 10, 28, 15, 0)  # Wednesday
    events = [
        _ev_at("tue", _at(2026, 10, 27, 20, 0)),          # past: dropped
        _ev_at("wed_morning", _at(2026, 10, 28, 9, 0)),   # started: dropped
        _ev_at("sun_late", _at(2026, 11, 1, 23, 59)),
        _ev_at("next_mon_midnight", _at(2026, 11, 2, 0, 0)),
    ]
    assert _titles(events, "this_week", now, MON) == {"sun_late"}


def test_monday_mode_monday_shows_full_week():
    now = _at(2026, 10, 26, 10, 0)  # Monday
    events = [
        _ev_at("sun_before", _at(2026, 10, 25, 23, 59)),
        _ev_at("mon_start", _at(2026, 10, 26, 0, 0)),   # started this morning: dropped
        _ev_at("mon_later", _at(2026, 10, 26, 18, 30)),  # later today: shown
        _ev_at("sun_end", _at(2026, 11, 1, 23, 59)),
        _ev_at("next_mon", _at(2026, 11, 2, 0, 0)),
    ]
    assert _titles(events, "this_week", now, MON) == {"mon_later", "sun_end"}


def test_monday_mode_sunday_shows_sunday_only():
    now = _at(2026, 11, 1, 12, 0)  # Sunday
    events = [
        _ev_at("sat", _at(2026, 10, 31, 20, 0)),
        _ev_at("sun", _at(2026, 11, 1, 18, 0)),
        _ev_at("mon", _at(2026, 11, 2, 0, 0)),
    ]
    assert _titles(events, "this_week", now, MON) == {"sun"}


def test_monday_mode_window_bounds_span_month_end():
    now = _at(2026, 10, 28, 15, 0)
    start, end = _window("this_week", MON, TZ, now=now)
    assert start == _at(2026, 10, 28, 0, 0)
    assert end == _at(2026, 11, 2, 0, 0)


def test_monday_mode_period_key_matches_iso_week():
    now = _at(2026, 10, 28, 15, 0)
    start, end = _window("this_week", MON, TZ, now=now)
    key = period_key("this_week", TZ, now=now)
    iso = now.isocalendar()
    assert key == f"{iso.year}-W{iso.week:02d}"
    assert period_key("this_week", TZ, now=end - timedelta(seconds=1)) == key
    assert period_key("this_week", TZ, now=end) != key


def test_period_key_with_fixed_now():
    assert period_key("this_month", TZ, now=_at(2026, 12, 31, 23, 0)) == "2026-12"
    assert period_key("this_week", TZ, now=_at(2026, 10, 23, 12, 0)) == "2026-W43"


# --- hash sensitivity and ordering ---

def _hash_ev(**kw):
    base = dict(guild_id=1, external_id="wp:h", title="h", url="https://x",
                starts_at=_at(2026, 10, 23, 10, 0), time_known=True,
                price_text="$10", is_active=True)
    base.update(kw)
    return Event(**base)


def _h(*events):
    return render_board_hash(list(events), "this_week", _cfg(), TZ)


def test_hash_sensitive_to_price_text():
    assert _h(_hash_ev(price_text="$10")) != _h(_hash_ev(price_text="$12"))


def test_hash_sensitive_to_time_known():
    assert _h(_hash_ev(time_known=True)) != _h(_hash_ev(time_known=False))


def test_hash_sensitive_to_starts_at():
    assert _h(_hash_ev(starts_at=_at(2026, 10, 23, 10, 0))) != \
        _h(_hash_ev(starts_at=_at(2026, 10, 23, 11, 0)))


def test_hash_total_order_independent_of_input_order():
    a = _hash_ev(external_id="wp:a")
    b = _hash_ev(external_id="wp:b")
    assert _h(a, b) == _h(b, a)
