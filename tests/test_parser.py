import json
from pathlib import Path
from zoneinfo import ZoneInfo

from src.scraping.hightide_parser import parse_product

TZ = ZoneInfo("Australia/Perth")
FIXTURE = json.loads(
    Path("tests/fixtures/wp_products_events.json").read_text())


def _by_slug(slug_part: str):
    return next(p for p in FIXTURE if slug_part in p["slug"])


def test_external_id_format():
    p = _by_slug("05-11-26-weekly-duels")
    ev = parse_product(p, TZ)
    assert ev.external_id == f"wp:{p['id']}"


def test_dot_prefix_date_and_time_known():
    ev = parse_product(_by_slug("05-11-26-weekly-duels"), TZ)
    assert ev.starts_at is not None
    assert (ev.starts_at.year, ev.starts_at.month, ev.starts_at.day) == (2026, 11, 5)
    assert ev.time_known is True  # fixture content has → Time: 6:30pm
    assert ev.price_text == "$10"


def test_natural_language_title_date():
    ev = parse_product(_by_slug("friday-night-magic-countersculpt"), TZ)
    assert ev.starts_at is not None
    assert (ev.starts_at.month, ev.starts_at.day, ev.starts_at.year) == (10, 23, 2026)


def test_image_from_embed():
    ev = parse_product(_by_slug("05-11-26-weekly-duels"), TZ)
    assert ev.image_url is None or ev.image_url.startswith("https://")


def test_title_entities_cleaned():
    ev = parse_product(_by_slug("05-11-26-weekly-duels"), TZ)
    assert "&#8211;" not in ev.title


def test_summary_excludes_arrow_lines_and_title_repeat():
    """Summary must exclude arrow metadata lines and title-duplicate paragraphs."""
    ev = parse_product(_by_slug("05-11-26-weekly-duels"), TZ)
    assert ev.summary is not None
    assert "→" not in ev.summary
    # Title is "05.11.26 &#8211; Weekly Duels &#8211; Yu-Gi-Oh!"
    # First paragraph in content is "Weekly Duels &#8211; Yu-Gi-Oh!"
    # Should skip this and use the second paragraph (the actual description)
    assert not ev.summary.startswith("Weekly Duels")


def test_time_overlay_from_prefix():
    """Time overlay should handle 'From X' prefix."""
    product = {
        "id": 9999,
        "slug": "test-from-time",
        "title": {"rendered": "12.01.27 – Test Event"},
        "content": {"rendered": "<p>Test event</p><p>→ Time: From 6:30 PM</p>"},
        "link": "http://example.com",
    }
    ev = parse_product(product, TZ)
    assert ev.starts_at is not None
    assert ev.starts_at.hour == 18
    assert ev.starts_at.minute == 30
    assert ev.time_known is True


def test_time_overlay_range_leaves_time_unknown():
    """Time overlay should NOT extract start time from ranges."""
    product = {
        "id": 9998,
        "slug": "test-range-time",
        "title": {"rendered": "12.01.27 – Test Event"},
        "content": {"rendered": "<p>Test event</p><p>→ Time: 6:30 to 10:00 PM</p>"},
        "link": "http://example.com",
    }
    ev = parse_product(product, TZ)
    assert ev.starts_at is not None
    # starts_at should have date from title but NOT time from range
    assert (ev.starts_at.year, ev.starts_at.month, ev.starts_at.day) == (2027, 1, 12)
    # Time should be default (00:00) since range cannot be parsed
    assert ev.starts_at.hour == 0
    assert ev.starts_at.minute == 0
    assert ev.time_known is False


def test_all_fixture_products_parse_without_error():
    """All 31 fixture products should parse successfully without raising."""
    for product in FIXTURE:
        ev = parse_product(product, TZ)
        # Verify every starts_at is either None or timezone-aware
        if ev.starts_at is not None:
            assert ev.starts_at.tzinfo is not None, f"Product {product['id']} has non-aware datetime"
