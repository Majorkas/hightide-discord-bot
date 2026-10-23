import pytest

from src.scraping.hightide_parser import (
    MAX_PRICE_CHARS, MAX_TITLE_CHARS, MAX_URL_CHARS, _extract_summary,
    parse_product,
)

UNDATED_SLUGS = [
    "test-event-30-09",
    "painters-guild-entry",
    "product-placeholder",
    # Yearless natural-language date: not parseable, falls back to TBA.
    "warhammer-40000-the-damnos-incident-saturday-november-7th",
]


@pytest.fixture
def by_slug(fixture_products):
    def _by_slug(slug_part: str):
        return next(p for p in fixture_products if slug_part in p["slug"])
    return _by_slug


def _product(title: str, content: str, pid: int = 9000, link: str = "http://example.com"):
    return {
        "id": pid, "slug": f"s-{pid}", "title": {"rendered": title},
        "content": {"rendered": content}, "link": link,
    }


def test_external_id_format(by_slug, tz):
    p = by_slug("05-11-26-weekly-duels")
    ev = parse_product(p, tz)
    assert ev.external_id == f"wp:{p['id']}"


def test_dot_prefix_date_and_time_known(by_slug, tz):
    ev = parse_product(by_slug("05-11-26-weekly-duels"), tz)
    assert ev.starts_at is not None
    assert (ev.starts_at.year, ev.starts_at.month, ev.starts_at.day) == (2026, 11, 5)
    assert ev.time_known is True  # fixture content has → Time: 6:30pm
    assert ev.price_text == "$10"


def test_natural_language_title_date(by_slug, tz):
    ev = parse_product(by_slug("friday-night-magic-countersculpt"), tz)
    assert ev.starts_at is not None
    assert (ev.starts_at.month, ev.starts_at.day, ev.starts_at.year) == (10, 23, 2026)


def test_image_from_embed(by_slug, tz):
    ev = parse_product(by_slug("05-11-26-weekly-duels"), tz)
    assert ev.image_url == (
        "https://hightidetabletop.com.au/wp-content/uploads/2026/10/original-38.png"
    )


def test_title_entities_cleaned(by_slug, tz):
    ev = parse_product(by_slug("05-11-26-weekly-duels"), tz)
    assert "&#8211;" not in ev.title


def test_summary_excludes_arrow_lines_and_title_repeat(by_slug, tz):
    """Summary must exclude arrow metadata lines and title-duplicate paragraphs."""
    ev = parse_product(by_slug("05-11-26-weekly-duels"), tz)
    assert ev.summary is not None
    assert "→" not in ev.summary
    assert not ev.summary.startswith("Weekly Duels")


def test_time_overlay_from_prefix(tz):
    """Time overlay should handle 'From X' prefix."""
    ev = parse_product(_product(
        "12.01.27 – Test Event",
        "<p>Test event</p><p>→ Time: From 6:30 PM</p>"), tz)
    assert ev.starts_at is not None
    assert ev.starts_at.hour == 18
    assert ev.starts_at.minute == 30
    assert ev.time_known is True


def test_time_overlay_range_leaves_time_unknown(tz):
    """Time overlay should NOT extract start time from ranges."""
    ev = parse_product(_product(
        "12.01.27 – Test Event",
        "<p>Test event</p><p>→ Time: 6:30 to 10:00 PM</p>"), tz)
    assert ev.starts_at is not None
    assert (ev.starts_at.year, ev.starts_at.month, ev.starts_at.day) == (2027, 1, 12)
    assert ev.starts_at.hour == 0
    assert ev.starts_at.minute == 0
    assert ev.time_known is False


def test_all_fixture_products_parse_without_error(fixture_products, tz):
    for product in fixture_products:
        ev = parse_product(product, tz)
        if ev.starts_at is not None:
            assert ev.starts_at.tzinfo is not None, (
                f"Product {product['id']} has non-aware datetime")


@pytest.mark.parametrize("slug", UNDATED_SLUGS)
def test_undated_fixtures_fall_back_to_tba(by_slug, tz, slug):
    ev = parse_product(by_slug(slug), tz)
    assert ev.starts_at is None
    assert ev.time_known is False


@pytest.mark.parametrize("time_text", ["10 - 6", "11", "soonish", "From 7"])
def test_bare_number_or_malformed_time_rejected(tz, time_text):
    ev = parse_product(_product(
        "12.01.27 – Test Event", f"<p>Intro</p><p>→ Time: {time_text}</p>"), tz)
    assert ev.starts_at is not None
    assert ev.time_known is False
    assert (ev.starts_at.hour, ev.starts_at.minute) == (0, 0)


def test_time_with_am_pm_and_no_colon_accepted(tz):
    ev = parse_product(_product(
        "12.01.27 – Test Event", "<p>Intro</p><p>→ Time: 7pm</p>"), tz)
    assert ev.time_known is True and ev.starts_at.hour == 19


def test_summary_skips_empty_first_paragraph():
    html = "<p>&nbsp;</p><p>  </p><p>Real description here.</p>"
    assert _extract_summary(html, "12.01.27 – Test Event") == "Real description here."


def test_summary_none_when_nothing_qualifies():
    assert _extract_summary("<p>&nbsp;</p><p>→ Time: 6pm</p>", "T") is None


@pytest.mark.parametrize("title", [
    "7PH Tuesday – MTG – October 20th 2026",
    "20.10.26 – 7PH Tuesday – MTG – October 20th 2026",
    "20.10.26 – 7PH Tuesday – MTG",
])
def test_summary_skips_title_echo_without_date(title):
    html = "<p>7PH Tuesday – MTG</p><p>Bring a deck.</p>"
    assert _extract_summary(html, title) == "Bring a deck."


def test_summary_skips_title_echo_with_natural_date():
    html = "<p>7PH Tuesday – MTG – October 20th 2026</p><p>Bring a deck.</p>"
    assert _extract_summary(html, "20.10.26 – 7PH Tuesday – MTG") == "Bring a deck."


def test_ingest_length_caps(tz):
    ev = parse_product(_product(
        "12.01.27 – " + "T" * 500,
        "<p>x</p><p>→ Ticket: " + "$" * 300 + "</p>",
        link="https://example.com/" + "u" * 800), tz)
    assert len(ev.title) == MAX_TITLE_CHARS
    assert len(ev.price_text) == MAX_PRICE_CHARS
    assert len(ev.url) == MAX_URL_CHARS
    assert ev.starts_at is not None  # date parsed from the full title
