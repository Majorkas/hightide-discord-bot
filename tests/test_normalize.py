from zoneinfo import ZoneInfo

from src.scraping.normalize import (
    clean_title, extract_price, extract_time, fingerprint, parse_event_datetime,
)

TZ = ZoneInfo("Australia/Perth")


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
