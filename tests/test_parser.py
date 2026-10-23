from datetime import datetime

import pytest

from src.scraping.hightide_parser import (
    MAX_PRICE_CHARS, MAX_TITLE_CHARS, MAX_URL_CHARS, _extract_summary,
    parse_product, parse_product_or_none,
)

JUNK_SLUGS = ["test-event-30-09", "product-placeholder", "painters-guild-entry"]


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


def test_time_overlay_range_uses_start_time(tz):
    ev = parse_product(_product(
        "12.01.27 – Test Event",
        "<p>Test event</p><p>→ Time: 6:30 to 10:00 PM</p>"), tz)
    assert ev.starts_at is not None
    assert (ev.starts_at.year, ev.starts_at.month, ev.starts_at.day) == (2027, 1, 12)
    assert (ev.starts_at.hour, ev.starts_at.minute) == (18, 30)
    assert ev.time_known is True


@pytest.mark.parametrize("time_text,expected", [
    ("09:00am to 06:30pm", (9, 0)),
    ("09:00am to 06:00pm", (9, 0)),
    ("08:00am to 06:30pm", (8, 0)),
    ("10:00am to 5:30PM", (10, 0)),
    ("08:30am to 07:00PM", (8, 30)),
    ("6:30 to 10:00 PM", (18, 30)),
    ("9:30PM to 6:00PM", (21, 30)),
    ("9:00 AM - 5:00 PM", (9, 0)),
    ("7pm – 11pm", (19, 0)),
    ("From 6:30 to 10:00 PM", (18, 30)),
    ("11:00 to 2:00 PM", (11, 0)),
])
def test_range_time_extracts_start(tz, time_text, expected):
    ev = parse_product(_product(
        "12.01.27 – Test Event", f"<p>Intro</p><p>→ Time: {time_text}</p>"), tz)
    assert ev.time_known is True
    assert (ev.starts_at.hour, ev.starts_at.minute) == expected


@pytest.mark.parametrize("time_text", ["10 - 6", "10 to 6", "10 to late", "soon to later"])
def test_bare_number_range_rejected(tz, time_text):
    ev = parse_product(_product(
        "12.01.27 – Test Event", f"<p>Intro</p><p>→ Time: {time_text}</p>"), tz)
    assert ev.time_known is False
    assert (ev.starts_at.hour, ev.starts_at.minute) == (0, 0)


@pytest.mark.parametrize("prose,expected", [
    ("We'll kick off at 2pm and finish at 9pm.", (14, 0)),
    ("Kicks off at 6:30 PM sharp.", (18, 30)),
    ("Kicking off at 11am.", (11, 0)),
    ("Play starts at 7pm.", (19, 0)),
    ("Event starting at 10:15am.", (10, 15)),
    ("Doors open from 5:30pm to 10pm.", (17, 30)),
    ("FROM 8PM until late", (20, 0)),
])
def test_prose_start_time_fallback(tz, prose, expected):
    ev = parse_product(_product("12.01.27 – Test Event", f"<p>{prose}</p>"), tz)
    assert ev.time_known is True
    assert (ev.starts_at.hour, ev.starts_at.minute) == expected


@pytest.mark.parametrize("prose", [
    "We finish at 9pm.",
    "Ends at 10:00pm.",
    "Finish at 9/10pm.",
    "Starts at noon.",
    "Come from 10 to 6.",
    "Kick off at 9/10pm.",
    "Nothing about times here.",
])
def test_prose_without_start_time_stays_tbc(tz, prose):
    ev = parse_product(_product("12.01.27 – Test Event", f"<p>{prose}</p>"), tz)
    assert ev.time_known is False
    assert (ev.starts_at.hour, ev.starts_at.minute) == (0, 0)


def test_time_line_takes_precedence_over_prose(tz):
    ev = parse_product(_product(
        "12.01.27 – Test Event",
        "<p>We kick off at 2pm.</p><p>→ Time: 6:30pm</p>"), tz)
    assert (ev.starts_at.hour, ev.starts_at.minute) == (18, 30)


def test_prose_ignored_when_title_has_no_date(tz):
    ev = parse_product(_product("Mystery Night", "<p>Kick off at 2pm.</p>"), tz)
    assert ev.starts_at is None and ev.time_known is False


BOARD_TO_DEATH_HTML = (
    "<p>BOARD TO DEATH</p>\n<p>Four gaming tribes. One spooky day of gaming.</p>\n"
    "<p>Join MDD, Tabletop Titans, Bastardcon, and Hightide at 3 Josephson St "
    "Fremantle this Halloween for a cursed day of board games, seances, and "
    "maniacal laughter.</p>\n"
    "<p>You&#39;re welcome to dress up as a vampire, zombie, accountant, or "
    "anything else.</p>\n"
    "<p>We&#39;ll kick off at 2pm and finish at 9/10pm, but feel free to come and "
    "chill in Hightide earlier if you really can&#39;t get a grip on your "
    "cardboard addiction.</p>\n<p>We hope to see you soon.</p>\n<p></p>\n"
)


def test_board_to_death_live_shape_kicks_off_at_two_pm(tz):
    ev = parse_product(_product(
        "Board to Death &#8211; October 31st 2026", BOARD_TO_DEATH_HTML), tz)
    assert (ev.starts_at.year, ev.starts_at.month, ev.starts_at.day) == (2026, 10, 31)
    assert (ev.starts_at.hour, ev.starts_at.minute) == (14, 0)
    assert ev.time_known is True


def test_all_fixture_products_parse_without_error(fixture_products, tz):
    parsed = [parse_product_or_none(p, tz) for p in fixture_products]
    assert sum(ev is None for ev in parsed) == 3
    for product, ev in zip(fixture_products, parsed):
        if ev is not None and ev.starts_at is not None:
            assert ev.starts_at.tzinfo is not None, (
                f"Product {product['id']} has non-aware datetime")


@pytest.mark.parametrize("slug", JUNK_SLUGS)
def test_junk_fixture_products_are_filtered(by_slug, tz, slug):
    assert parse_product_or_none(by_slug(slug), tz) is None


@pytest.mark.parametrize("slug", [
    "05-11-26-weekly-duels", "friday-night-magic-countersculpt",
    "welcome-to-night-city-cyperpunk-tcg-prelease",
])
def test_real_fixture_products_not_filtered(by_slug, tz, slug):
    p = by_slug(slug)
    ev = parse_product_or_none(p, tz)
    assert ev is not None and ev.external_id == f"wp:{p['id']}"


def test_parse_product_or_none_matches_parse_product_for_real(by_slug, tz):
    p = by_slug("05-11-26-weekly-duels")
    assert parse_product_or_none(p, tz) == parse_product(p, tz)


def test_yearless_warhammer_fixture_parses_date(by_slug, tz, monkeypatch):
    monkeypatch.setattr(
        "src.scraping.normalize._now", lambda tz: datetime(2026, 10, 23, 12, tzinfo=tz))
    ev = parse_product(by_slug("warhammer-40000-the-damnos-incident"), tz)
    assert ev.starts_at is not None
    assert (ev.starts_at.year, ev.starts_at.month, ev.starts_at.day) == (2026, 11, 7)
    # Fixture has "→ Time: 08:00am to 06:30pm" → range start.
    assert ev.time_known is True
    assert (ev.starts_at.hour, ev.starts_at.minute) == (8, 0)


def test_undated_title_falls_back_to_tba(tz):
    ev = parse_product(_product("Mystery Night", "<p>Hello</p>"), tz)
    assert ev.starts_at is None and ev.time_known is False


def test_remaining_undated_real_fixtures_fall_back_to_tba(fixture_products, tz):
    undated = [
        p["slug"] for p in fixture_products
        if (ev := parse_product_or_none(p, tz)) is not None and ev.starts_at is None
    ]
    assert undated == []


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
