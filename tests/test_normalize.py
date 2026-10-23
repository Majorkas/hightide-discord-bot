from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.scraping.normalize import (
    clean_title, extract_price, extract_time, fingerprint, is_junk_title,
    parse_event_datetime,
)

TZ = ZoneInfo("Australia/Perth")
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=TZ)


def test_clean_title_entities():
    assert clean_title("05.11.26 &#8211; Weekly Duels &#8211; Yu-Gi-Oh!") == \
        "05.11.26 – Weekly Duels – Yu-Gi-Oh!"


def test_parse_dot_prefix_two_digit_year():
    dt, known = parse_event_datetime("23.10.26 – Gundam TCG – Weekly", TZ)
    assert dt is not None and dt.year == 2026 and dt.month == 10 and dt.day == 23
    assert known is False  # date only, no time


def test_parse_dot_prefix_four_digit_year():
    dt, _ = parse_event_datetime("27.10.2026 – 7PH Tuesday – MTG", TZ)
    assert dt is not None and dt.year == 2026 and dt.month == 10 and dt.day == 27


def test_parse_natural_language_suffix():
    dt, _ = parse_event_datetime(
        "Friday Night Magic – Reality Fracture draft – Friday October 23rd 2026", TZ)
    assert dt is not None and dt.month == 10 and dt.day == 23 and dt.year == 2026


def test_yearless_natural_date_with_weekday():
    dt, known = parse_event_datetime("Warhammer – Saturday November 7th", TZ, now=NOW)
    assert (dt.year, dt.month, dt.day) == (2026, 11, 7)
    assert dt.tzinfo is not None and known is False


def test_yearless_natural_date_without_weekday():
    dt, _ = parse_event_datetime("Something – November 7th", TZ, now=NOW)
    assert (dt.year, dt.month, dt.day) == (2026, 11, 7)


def test_yearless_date_within_30_days_past_does_not_roll():
    dt, _ = parse_event_datetime("Event – October 1st", TZ, now=NOW)
    assert (dt.year, dt.month, dt.day) == (2026, 10, 1)


def test_yearless_date_exactly_30_days_past_does_not_roll():
    dt, _ = parse_event_datetime("Event – September 23rd", TZ, now=NOW)
    assert (dt.year, dt.month, dt.day) == (2026, 9, 23)


def test_yearless_date_over_30_days_past_rolls_forward():
    dt, _ = parse_event_datetime("Event – September 22nd", TZ, now=NOW)
    assert (dt.year, dt.month, dt.day) == (2027, 9, 22)


def test_yearless_january_title_in_december_rolls_to_next_year():
    now = datetime(2026, 12, 20, 9, 0, tzinfo=TZ)
    dt, _ = parse_event_datetime("New Year Open – January 10th", TZ, now=now)
    assert (dt.year, dt.month, dt.day) == (2027, 1, 10)


def test_year_present_never_rolls_forward():
    dt, _ = parse_event_datetime("Old Event – January 10th 2025", TZ, now=NOW)
    assert (dt.year, dt.month, dt.day) == (2025, 1, 10)


def test_dot_prefix_wins_over_yearless_natural_date():
    dt, _ = parse_event_datetime("05.11.26 – Duels – Saturday November 7th", TZ, now=NOW)
    assert (dt.year, dt.month, dt.day) == (2026, 11, 5)


def test_yearless_invalid_day_returns_none():
    dt, known = parse_event_datetime("Event – February 30th", TZ, now=NOW)
    assert dt is None and known is False


@pytest.mark.parametrize("title", [
    "Test event 30.09", "Product placeholder", "Painters Guild Entry",
    "PAINTERS GUILD night", "A TEST", "Latest news", "Product PlaceHolder",
])
def test_is_junk_title_true(title):
    assert is_junk_title(title) is True


@pytest.mark.parametrize("title", [
    "Welcome to Night City – Cyberpunk TCG Prelease", "Weekly Duels",
    "Painters night", "Guild Wars", "",
])
def test_is_junk_title_false(title):
    assert is_junk_title(title) is False


def test_parse_no_date_returns_none():
    dt, known = parse_event_datetime("Some Undated Thing", TZ)
    assert dt is None and known is False


def test_parse_dot_prefix_invalid_date_feb_31():
    dt, known = parse_event_datetime("31.02.26 – Fake Event", TZ)
    assert dt is None and known is False


def test_parse_dot_prefix_invalid_date_month_99():
    dt, known = parse_event_datetime("99.99.26 – Fake", TZ)
    assert dt is None and known is False


def test_extract_time():
    html = '<p>→ Location: Hightide</p><p>→ Time: 6:30pm</p><p>→ Ticket: $10</p>'
    assert extract_time(html) == "6:30pm"


def test_extract_price():
    html = '<p>→ Time: 12 PM</p><p>→ Ticket: $15</p>'
    assert extract_price(html) == "$15"


def test_extract_price_free():
    assert extract_price('<p>→ Ticket: Free</p>') == "Free"


def test_fingerprint_stable():
    a = fingerprint("t", "2026-10-23", "$15", "sum")
    b = fingerprint("t", "2026-10-23", "$15", "sum")
    assert a == b and len(a) == 40


_BASE_FP_INPUTS = ("t", "2026-10-23T18:00", "$15", "sum")


@pytest.mark.parametrize("idx,new_value", [
    (0, "t2"), (1, "2026-10-24T18:00"), (2, "$16"), (3, "sum2"),
])
def test_fingerprint_changes_with_each_input(idx, new_value):
    varied = list(_BASE_FP_INPUTS)
    varied[idx] = new_value
    assert fingerprint(*varied) != fingerprint(*_BASE_FP_INPUTS)


def test_fingerprint_none_equals_empty_string():
    assert fingerprint("t", None, None, None) == fingerprint("t", "", "", "")


def test_fingerprint_pipe_join_collision_is_known():
    # Known, accepted property: parts are "|"-joined, so ("a","b") and ("a|b",)
    # hash identically. Real inputs are (title, iso, price, summary); a collision
    # would need a literal "|" in one field AND matching neighbours.
    assert fingerprint("a", "b") == fingerprint("a|b")


@pytest.mark.parametrize("title,ymd", [
    ("Event – October 23rd 2026", (2026, 10, 23)),
    ("Event – october 23rd 2026", (2026, 10, 23)),
    ("Event – October 1st 2026", (2026, 10, 1)),
    ("Event – October 2nd 2026", (2026, 10, 2)),
    ("Event – Friday, October 23rd, 2026", (2026, 10, 23)),
    ("5.1.26 – Event", (2026, 1, 5)),
    # Dot prefix wins over a natural-language date when both are present.
    ("05.11.26 – Event – Friday November 6th 2026", (2026, 11, 5)),
])
def test_parse_date_forms(title, ymd):
    dt, _ = parse_event_datetime(title, TZ)
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == ymd
    assert dt.tzinfo is TZ


def test_parse_natural_date_without_year_uses_current_year():
    dt, _ = parse_event_datetime("Event – Saturday November 7th", TZ, now=NOW)
    assert dt is not None and dt.tzinfo is TZ
    assert (dt.year, dt.month, dt.day) == (2026, 11, 7)


@pytest.mark.parametrize("html,expected", [
    ("<p>→ Time: 12 PM</p>", "12 PM"),
    ("<p>→ Time: 12:00am</p>", "12:00am"),
    ("<p>→ time: 6pm</p>", "6pm"),
    ("<p>→ TIME: 6pm</p>", "6pm"),
    ("→ Time: 6pm", "6pm"),  # terminated by end-of-string
    ("→ Time: 6pm<br>", "6pm"),  # terminated by "<"
    ("<p>→ Time: soonish</p>", "soonish"),  # malformed: extraction is raw, parser validates
    ("<p>no time here</p>", None),
])
def test_extract_time_table(html, expected):
    assert extract_time(html) == expected


@pytest.mark.parametrize("html,expected", [
    ("<p>→ Ticket: $15</p>", "$15"),
    ("<p>→ ticket: $15</p>", "$15"),
    ("→ Ticket: $15", "$15"),
    ("→ Ticket: $15<br>", "$15"),
    ("<p>→ Time: 6pm</p>", None),
    ("", None),
])
def test_extract_price_table(html, expected):
    assert extract_price(html) == expected
