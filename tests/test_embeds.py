from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.db.models import Event
from src.discord_ui.embeds import build_board_embed


TZ = ZoneInfo("Australia/Perth")


def make_event(title: str, starts_at: datetime | None, time_known: bool = True,
               price_text: str | None = None, is_active: bool = True) -> Event:
    """Create a minimal Event object for testing."""
    return Event(
        guild_id=1,
        external_id=f"ext_{title}",
        title=title,
        url=f"https://example.com/{title}",
        starts_at=starts_at,
        time_known=time_known,
        price_text=price_text,
        is_active=is_active,
    )


def test_empty_state():
    """Test 'No events found' when no events match."""
    events = []
    embed = build_board_embed(events, "Test Board", TZ, include_tba=False)
    assert embed.description == "No events found for this period."
    assert len(embed.fields) == 0


def test_grouping_by_local_day():
    """Test events are grouped by local day and time TBC for date-only events."""
    tz = ZoneInfo("Australia/Perth")
    # Use UTC times that will land in Perth timezone
    dt1 = datetime(2026, 10, 23, 6, 30, tzinfo=timezone.utc)  # 14:30 Perth
    dt2 = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)  # 18:00 Perth
    dt3 = datetime(2026, 10, 24, 2, 0, tzinfo=timezone.utc)   # 10:00 Perth
    
    events = [
        make_event("Event 1", dt1, time_known=True, price_text="$20"),
        make_event("Event TBC", datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc), 
                   time_known=False),
        make_event("Event 2", dt2, time_known=True),
        make_event("Event 3", dt3, time_known=True),
    ]
    
    embed = build_board_embed(events, "Test", tz, include_tba=False)
    
    # Should have 2 day fields (23rd and 24th)
    assert len(embed.fields) == 2
    
    # Check first day has correct format
    assert "Fri 23 Oct 2026" in [f.name for f in embed.fields]
    assert "Sat 24 Oct 2026" in [f.name for f in embed.fields]
    
    # Check first day values contain time TBC and price suffix
    day1_field = next(f for f in embed.fields if "23 Oct" in f.name)
    assert "time TBC" in day1_field.value  # date-only event
    assert "$20" in day1_field.value  # price suffix
    assert "`14:30`" in day1_field.value  # time known


def test_tba_section_last_and_conditional():
    """Test TBA section appears last and only when include_tba=True."""
    dt = datetime(2026, 10, 23, 6, 30, tzinfo=timezone.utc)
    events = [
        make_event("Dated Event", dt),
        make_event("TBA Event", None),
    ]
    
    # With include_tba=False
    embed = build_board_embed(events, "Test", TZ, include_tba=False)
    field_names = [f.name for f in embed.fields]
    assert "Date TBA" not in field_names
    assert len(embed.fields) == 1
    
    # With include_tba=True
    embed = build_board_embed(events, "Test", TZ, include_tba=True)
    field_names = [f.name for f in embed.fields]
    assert "Date TBA" in field_names
    assert field_names[-1] == "Date TBA"  # should be last
    assert len(embed.fields) == 2


def test_field_value_truncation():
    """Test per-field value is capped at 1024 chars with '… +N more' suffix."""
    dt = datetime(2026, 10, 23, 6, 30, tzinfo=timezone.utc)
    
    # Create many events for a single day to exceed 1024 chars per field
    events = []
    for i in range(50):
        title = f"Very Long Event Title Number {i} with Extra Text to Increase Length Over One Thousand Characters"
        events.append(make_event(title, dt))

    embed = build_board_embed(events, "Test", TZ, include_tba=False)

    assert len(embed.fields) >= 1
    # UNCONDITIONAL: every field value must be <= 1024 chars
    for field in embed.fields:
        assert len(field.value) <= 1024, f"Field '{field.name}' has {len(field.value)} chars, exceeds 1024"
    # Expected kept lines: greedy fill of the 1014-char budget (1024 minus 10 reserved for the marker)
    kept = 0
    used = 0
    for ev in sorted(events, key=lambda x: x.starts_at):
        line = f"`14:30` [{ev.title}]({ev.url})"
        if used + len(line) + 1 > 1024 - 10:
            break
        used += len(line) + 1
        kept += 1
    expected_dropped = len(events) - kept
    assert expected_dropped == 46
    assert len(embed.fields) == 1
    value_lines = embed.fields[0].value.split("\n")
    assert value_lines[-1] == f"… +{expected_dropped} more"
    assert len(value_lines) - 1 == kept == 4


def test_field_count_cap():
    """Test max 25 fields total, overflow days go into 'Later' field."""
    # Create events across 30 different days with shorter titles
    # to hit field cap before hitting character limit
    events = []
    base_dt = datetime(2026, 1, 1, 6, 30, tzinfo=timezone.utc)
    for day_offset in range(30):
        dt = base_dt + timedelta(days=day_offset)
        # Shorter title to ensure we hit field cap, not char limit
        title = f"Event {day_offset}"
        events.append(make_event(title, dt))

    embed = build_board_embed(events, "Test", TZ, include_tba=False)

    # UNCONDITIONAL: max 25 fields
    assert len(embed.fields) <= 25, f"Embed has {len(embed.fields)} fields, exceeds 25"

    # With 30 days, we should have "Later" field to catch overflow
    field_names = [f.name for f in embed.fields]
    assert "Later" in field_names, "Expected 'Later' field when 30 days of events provided"


def test_embed_total_char_limit():
    """Test embed stays within 6000 total chars by truncating from the end."""
    base_dt = datetime(2026, 1, 1, 6, 30, tzinfo=timezone.utc)

    # Create 140 events to far exceed 6000 char limit
    events = []
    long_title = "X" * 120  # Very long title
    for day_offset in range(35):
        for i in range(4):
            dt = base_dt + timedelta(days=day_offset)
            events.append(make_event(f"{long_title} {day_offset}_{i}", dt))

    embed = build_board_embed(events, "Test", TZ, include_tba=False)

    # Count total characters
    total_chars = len(embed.title or "")
    total_chars += len(embed.description or "")
    for field in embed.fields:
        total_chars += len(field.name) + len(field.value)
    total_chars += len(embed.footer.text or "")

    # UNCONDITIONAL: must be at or under 6000
    assert total_chars <= 6000, f"Embed has {total_chars} chars, exceeds 6000"
    # UNCONDITIONAL: every field value <= 1024
    for field in embed.fields:
        assert len(field.value) <= 1024, f"Field '{field.name}' has {len(field.value)} chars"

    # Each day has 4 ~170-char lines; 3 fit in the 1014 budget, so exactly 1 is dropped per day.
    # Trimmed fields collapse into a trailing indicator: 140 events - 6 days * 4 = 116 removed.
    assert [f.name for f in embed.fields] == [
        "Thu 01 Jan 2026", "Fri 02 Jan 2026", "Sat 03 Jan 2026",
        "Sun 04 Jan 2026", "Mon 05 Jan 2026", "Tue 06 Jan 2026", "…",
    ]
    for field in embed.fields[:-1]:
        value_lines = field.value.split("\n")
        assert value_lines[-1] == "… +1 more"
        assert len(value_lines) - 1 == 3
    assert embed.fields[-1].value == "… +116 more events"


def test_tba_dropped_when_over_limit():
    """Test TBA field is dropped entirely when the 6000-char limit forces trimming."""
    base_dt = datetime(2026, 1, 1, 6, 30, tzinfo=timezone.utc)

    # Create enough content to trigger limits + TBA events
    events = []
    long_title = "X" * 120
    for day_offset in range(35):
        for i in range(4):
            dt = base_dt + timedelta(days=day_offset)
            events.append(make_event(f"{long_title} {day_offset}_{i}", dt))

    # Add TBA events
    tba_count = 8
    for i in range(tba_count):
        events.append(make_event(f"TBA Event {i} with long title " + "Y" * 80, None))

    embed = build_board_embed(events, "Test", TZ, include_tba=True)

    # UNCONDITIONAL: field count <= 25
    assert len(embed.fields) <= 25, f"Embed has {len(embed.fields)} fields"
    # UNCONDITIONAL: every field value <= 1024
    for field in embed.fields:
        assert len(field.value) <= 1024, f"Field '{field.name}' exceeds 1024 chars"
    # UNCONDITIONAL: total <= 6000
    total_chars = len(embed.title or "")
    total_chars += len(embed.description or "")
    for field in embed.fields:
        total_chars += len(field.name) + len(field.value)
    total_chars += len(embed.footer.text or "")
    assert total_chars <= 6000, f"Embed has {total_chars} chars"

    # The 6000-char guard removes the TBA field and every day field after the 6th, collapsing
    # them into one indicator: (140 dated + 8 TBA) - 6 days * 4 = 124 events removed.
    assert [f.name for f in embed.fields] == [
        "Thu 01 Jan 2026", "Fri 02 Jan 2026", "Sat 03 Jan 2026",
        "Sun 04 Jan 2026", "Mon 05 Jan 2026", "Tue 06 Jan 2026", "…",
    ]
    assert all(f.name != "Date TBA" for f in embed.fields)
    assert embed.fields[-1].value == "… +124 more events"


def test_many_days_plus_tba_with_include_true():
    """Test 30 days + 1 undated event with include_tba=True: ≤25 fields with Date TBA and Later."""
    base_dt = datetime(2026, 1, 1, 6, 30, tzinfo=timezone.utc)
    events = []

    # Create 30 distinct short-titled events on different days
    for day_offset in range(30):
        dt = base_dt + timedelta(days=day_offset)
        events.append(make_event(f"E{day_offset}", dt))

    # Add one undated event
    events.append(make_event("TBA thing", None))

    embed = build_board_embed(events, "Test", TZ, include_tba=True)

    # UNCONDITIONAL: must be ≤25 fields
    assert len(embed.fields) <= 25, f"Embed has {len(embed.fields)} fields, exceeds 25"
    # UNCONDITIONAL: Date TBA field must be present
    field_names = [f.name for f in embed.fields]
    assert "Date TBA" in field_names, "Expected 'Date TBA' field when TBA events exist and include_tba=True"
    # UNCONDITIONAL: Later field must be present (overflow from 30 days)
    assert "Later" in field_names, "Expected 'Later' field when 30 days of events overflow"

def test_later_lines_carry_day_prefix():
    base_dt = datetime(2026, 1, 1, 6, 30, tzinfo=timezone.utc)
    events = [make_event(f"E{i}", base_dt + timedelta(days=i)) for i in range(26)]
    embed = build_board_embed(events, "Test", TZ, include_tba=False)
    later = next(f for f in embed.fields if f.name == "Later")
    # Day 25 (index 24) is Sat 25 Jan 2026 in Perth.
    assert later.value.split("\n")[0].startswith("Sun 25 Jan `14:30`")
    assert "Mon 26 Jan `14:30`" in later.value


def test_deterministic_order_within_day_and_tba():
    dt = datetime(2026, 10, 23, 6, 30, tzinfo=timezone.utc)
    a = [make_event("B", dt), make_event("A", dt), make_event("Z", None), make_event("M", None)]
    e1 = build_board_embed(a, "T", TZ, include_tba=True)
    e2 = build_board_embed(list(reversed(a)), "T", TZ, include_tba=True)
    assert [(f.name, f.value) for f in e1.fields] == [(f.name, f.value) for f in e2.fields]
    day = e1.fields[0].value.split("\n")
    assert "[A]" in day[0] and "[B]" in day[1]
    tba = e1.fields[-1].value.split("\n")
    assert tba[0].startswith("[M]") and tba[1].startswith("[Z]")


def test_truncate_lines_marker_only_when_first_line_too_long():
    from src.discord_ui.embeds import _truncate_lines
    text, dropped = _truncate_lines(["x" * 50, "y"], 20)
    assert text == "… +2 more" and dropped == 2


def test_truncate_lines_keeps_contiguous_prefix():
    from src.discord_ui.embeds import _truncate_lines
    # A short line after a too-long one must not be kept (no gaps).
    text, dropped = _truncate_lines(["a" * 10, "b" * 40, "c"], 30)
    assert text == "a" * 10 + "\n… +2 more" and dropped == 2


def test_truncate_lines_no_truncation_when_fits():
    from src.discord_ui.embeds import _truncate_lines
    assert _truncate_lines(["a", "b"], 10) == ("a\nb", 0)


def _total_chars(embed) -> int:
    return (len(embed.title or "") + len(embed.description or "")
            + sum(len(f.name) + len(f.value) for f in embed.fields)
            + len(embed.footer.text or ""))


def test_guard_collapses_removed_content_into_indicator():
    base_dt = datetime(2026, 1, 1, 6, 30, tzinfo=timezone.utc)
    events = []
    for day in range(20):
        for i in range(3):
            events.append(make_event(f"{'W' * 150} {day}_{i}", base_dt + timedelta(days=day)))
    embed = build_board_embed(events, "Test", TZ, include_tba=False)

    assert len(embed.fields) <= 25
    assert all(len(f.value) <= 1024 for f in embed.fields)
    assert _total_chars(embed) <= 6000
    indicator = embed.fields[-1]
    assert indicator.name == "…"
    shown = len(embed.fields) - 1
    assert indicator.value == f"… +{60 - shown * 3} more events"
    assert "+" in indicator.value and "more events" in indicator.value


def test_guard_indicator_never_exceeds_field_cap():
    # 25 fields each ~250 chars: total > 6000 only via many fields; indicator must still fit the cap.
    base_dt = datetime(2026, 1, 1, 6, 30, tzinfo=timezone.utc)
    events = [make_event(f"{'Q' * 200}{d}", base_dt + timedelta(days=d)) for d in range(40)]
    embed = build_board_embed(events, "Test", TZ, include_tba=True)
    assert len(embed.fields) <= 25
    assert _total_chars(embed) <= 6000


HOSTILE_TITLE = "x](https://evil.example) @everyone <@123>"


def test_hostile_title_cannot_take_over_link():
    dt = datetime(2026, 10, 23, 6, 30, tzinfo=timezone.utc)
    ev = make_event(HOSTILE_TITLE, dt, price_text="$5](https://evil.example) @here")
    ev.url = "https://example.com/ok"
    embed = build_board_embed([ev], "T", TZ, include_tba=False)
    value = embed.fields[0].value
    assert "](https://evil.example)" not in value
    assert "<@123>" not in value
    assert "@everyone" not in value
    assert "@here" not in value
    assert value.count("](") == 1  # only the legitimate link


def test_hostile_url_breakout_is_neutralised():
    dt = datetime(2026, 10, 23, 6, 30, tzinfo=timezone.utc)
    ev = make_event("Safe", dt)
    ev.url = "https://example.com/a) [click](https://evil.example"
    embed = build_board_embed([ev], "T", TZ, include_tba=False)
    value = embed.fields[0].value
    assert "[click](https://evil.example)" not in value
    assert value.endswith("(https://example.com/a%29%20[click]%28https://evil.example)")
    assert value.count("](") == 1


@pytest.mark.parametrize("url", [None, "", "javascript:alert(1)", "ftp://x.example/a", "not a url"])
def test_invalid_url_renders_plain_text(url):
    dt = datetime(2026, 10, 23, 6, 30, tzinfo=timezone.utc)
    ev = make_event("Plain Title", dt)
    ev.url = url
    value = build_board_embed([ev], "T", TZ, include_tba=False).fields[0].value
    assert "](" not in value
    assert "Plain Title" in value


def test_invalid_url_plain_text_in_tba_and_later():
    ev = make_event("Undated", None)
    ev.url = None
    embed = build_board_embed([ev], "T", TZ, include_tba=True)
    assert embed.fields[-1].value == "Undated"


def test_newlines_collapsed_in_title():
    dt = datetime(2026, 10, 23, 6, 30, tzinfo=timezone.utc)
    ev = make_event("Line1\n# Heading\nLine3", dt)
    value = build_board_embed([ev], "T", TZ, include_tba=False).fields[0].value
    assert "\n" not in value


def test_bidi_controls_stripped_from_title():
    dt = datetime(2026, 10, 23, 6, 30, tzinfo=timezone.utc)
    ev = make_event("Evil\u202eTitle\u2066x\u2069", dt)
    ev.url = "https://example.com/ok"
    value = build_board_embed([ev], "T", TZ, include_tba=False).fields[0].value
    assert "EvilTitlex" in value
    assert not any(c in value for c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def test_hostile_title_escaped_in_tba():
    ev = make_event(HOSTILE_TITLE, None)
    ev.url = "https://example.com/ok"
    value = build_board_embed([ev], "T", TZ, include_tba=True).fields[-1].value
    assert "](https://evil.example)" not in value
    assert "<@123>" not in value and "@everyone" not in value

def test_many_days_plus_tba_with_include_false():
    """Test 30 days + 1 undated event with include_tba=False: ≤25 fields (no Date TBA)."""
    base_dt = datetime(2026, 1, 1, 6, 30, tzinfo=timezone.utc)
    events = []

    # Create 30 distinct short-titled events on different days
    for day_offset in range(30):
        dt = base_dt + timedelta(days=day_offset)
        events.append(make_event(f"E{day_offset}", dt))

    # Add one undated event (but won't be included)
    events.append(make_event("TBA thing", None))

    embed = build_board_embed(events, "Test", TZ, include_tba=False)

    # UNCONDITIONAL: must be ≤25 fields
    assert len(embed.fields) <= 25, f"Embed has {len(embed.fields)} fields, exceeds 25"
    # UNCONDITIONAL: Date TBA field must NOT be present
    field_names = [f.name for f in embed.fields]
    assert "Date TBA" not in field_names, "Date TBA should not appear when include_tba=False"
    # UNCONDITIONAL: Later field must be present (overflow from 30 days)
    assert "Later" in field_names, "Expected 'Later' field when 30 days of events overflow"


# --- rich per-event cards (this_week board) ---

from src.discord_ui.embeds import build_event_card, build_week_board_payload  # noqa: E402


def _rich_event(title, starts_at, *, summary=None, image_url=None, url="https://example.com/e",
                time_known=True, price_text=None):
    ev = make_event(title, starts_at, time_known=time_known, price_text=price_text)
    ev.url = url
    ev.summary = summary
    ev.image_url = image_url
    return ev


def _dated(n):
    base = datetime(2026, 10, 23, 6, 30, tzinfo=timezone.utc)
    return [_rich_event(f"Ev{i}", base + timedelta(hours=i)) for i in range(n)]


def test_rich_card_fields_present():
    dt = datetime(2026, 10, 23, 10, 30, tzinfo=timezone.utc)  # Fri 18:30 Perth
    ev = _rich_event("Card Night", dt, summary="Bring a deck.", price_text="$10",
                     image_url="https://img.example.com/a.png", url="https://example.com/card")
    embeds = build_week_board_payload([ev], "This Week", TZ, include_tba=False)
    assert len(embeds) == 2
    card = embeds[1]
    assert card.title == "Card Night"
    assert card.url == "https://example.com/card"
    assert "Fri 23 Oct · 18:30 · $10" in card.description
    assert "Bring a deck." in card.description
    assert card.image.url == "https://img.example.com/a.png"


def test_rich_card_without_optional_fields():
    dt = datetime(2026, 10, 23, 10, 30, tzinfo=timezone.utc)
    ev = _rich_event("Plain", dt)
    card = build_week_board_payload([ev], "T", TZ, include_tba=False)[1]
    assert card.description == "Fri 23 Oct · 18:30"
    assert card.image.url is None


@pytest.mark.parametrize("img", ["javascript:alert(1)", "ftp://x/a.png", "nope", ""])
def test_rich_card_invalid_image_skipped(img):
    dt = datetime(2026, 10, 23, 10, 30, tzinfo=timezone.utc)
    card = build_week_board_payload([_rich_event("X", dt, image_url=img)], "T", TZ, False)[1]
    assert card.image.url is None


def test_rich_card_time_tbc():
    dt = datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc)
    ev = _rich_event("NoTime", dt, time_known=False, price_text="$5")
    card = build_week_board_payload([ev], "T", TZ, include_tba=False)[1]
    assert "Fri 23 Oct · time TBC · $5" in card.description


def test_rich_card_escapes_summary_and_price():
    dt = datetime(2026, 10, 23, 10, 30, tzinfo=timezone.utc)
    ev = _rich_event("T", dt, summary="[x](https://evil.example) @everyone <@123>\n# H",
                     price_text="$5](https://evil.example) @here")
    card = build_week_board_payload([ev], "T", TZ, include_tba=False)[1]
    assert "](https://evil.example)" not in card.description
    assert "@everyone" not in card.description and "@here" not in card.description
    assert "<@123>" not in card.description
    assert card.description.count("\n") == 1


def test_rich_eight_dated_events_caps_cards_with_overflow():
    embeds = build_week_board_payload(_dated(8), "T", TZ, include_tba=False)
    assert len(embeds) == 9
    assert [e.title for e in embeds[1:8]] == [f"Ev{i}" for i in range(7)]
    assert embeds[8].title == "Also this week"
    assert "Ev7" in embeds[8].description
    assert "Fri 23 Oct" in embeds[8].description


def test_rich_seven_events_no_overflow_embed():
    embeds = build_week_board_payload(_dated(7), "T", TZ, include_tba=False)
    assert len(embeds) == 8
    assert all(e.title != "Also this week" for e in embeds)


def test_rich_tba_embed_by_flag():
    dt = datetime(2026, 10, 23, 10, 30, tzinfo=timezone.utc)
    events = [_rich_event("Dated", dt), _rich_event("Undated", None, url="https://example.com/u")]
    with_tba = build_week_board_payload(events, "T", TZ, include_tba=True)
    assert with_tba[-1].title == "Date TBA"
    assert "[Undated](https://example.com/u)" in with_tba[-1].description
    without = build_week_board_payload(events, "T", TZ, include_tba=False)
    assert all(e.title != "Date TBA" for e in without)
    assert len(without) == 2


def test_rich_tba_hostile_title_escaped():
    ev = _rich_event(HOSTILE_TITLE, None)
    embeds = build_week_board_payload([ev], "T", TZ, include_tba=True)
    assert len(embeds) == 2 and embeds[1].title == "Date TBA"
    assert "](https://evil.example)" not in embeds[1].description
    assert "@everyone" not in embeds[1].description


def test_rich_never_exceeds_ten_embeds():
    events = _dated(40) + [_rich_event(f"U{i}", None) for i in range(40)]
    embeds = build_week_board_payload(events, "T", TZ, include_tba=True)
    assert len(embeds) == 10  # header + 7 cards + overflow + TBA
    assert [e.title for e in embeds[-2:]] == ["Also this week", "Date TBA"]


def test_rich_empty_state_single_embed():
    embeds = build_week_board_payload([], "T", TZ, include_tba=True)
    assert len(embeds) == 1
    assert embeds[0].title == "T"
    assert embeds[0].description == "No events found for this period."
    assert embeds[0].footer.text.startswith("Last updated ")


def test_rich_tba_only_not_empty_state():
    embeds = build_week_board_payload([_rich_event("U", None)], "T", TZ, include_tba=True)
    assert len(embeds) == 2 and embeds[1].title == "Date TBA"
    assert embeds[0].description != "No events found for this period."


def test_rich_footer_on_first_embed_only():
    events = _dated(8) + [_rich_event("U", None)]
    embeds = build_week_board_payload(events, "T", TZ, include_tba=True)
    assert embeds[0].footer.text.endswith(" AWST")
    assert embeds[0].footer.text.startswith("Last updated ")
    assert all(e.footer.text is None for e in embeds[1:])


def test_week_header_skim_list_covers_all_dated_events():
    embeds = build_week_board_payload(_dated(8), "This Week at Hightide", TZ, include_tba=False)
    header = embeds[0]
    assert header.title == "This Week at Hightide"
    lines = header.description.split("\n")
    assert len(lines) == 8  # 7 cards + the overflow event
    assert lines[0] == "Fri 23 — [Ev0](https://example.com/e)"
    assert lines[7].endswith("[Ev7](https://example.com/e)")


def test_week_header_skim_list_escapes_hostile_title():
    dt = datetime(2026, 10, 23, 10, 30, tzinfo=timezone.utc)
    header = build_week_board_payload([_rich_event(HOSTILE_TITLE, dt)], "T", TZ, False)[0]
    assert "](https://evil.example)" not in header.description
    assert "@everyone" not in header.description


def test_week_payload_order_header_cards_overflow_tba():
    events = _dated(9) + [_rich_event("U", None)]
    embeds = build_week_board_payload(events, "T", TZ, include_tba=True)
    assert [e.title for e in embeds] == ["T"] + [f"Ev{i}" for i in range(7)] + [
        "Also this week", "Date TBA"]


def test_week_cards_carry_no_footer_or_author():
    embeds = build_week_board_payload(_dated(2), "T", TZ, include_tba=False)
    assert embeds[1].footer.text is None and embeds[1].author.name is None
    assert embeds[0].image.url is None


def test_build_event_card_matches_payload_card():
    dt = datetime(2026, 10, 23, 10, 30, tzinfo=timezone.utc)
    ev = _rich_event("Card", dt, summary="S", image_url="https://img.example.com/a.png")
    assert build_event_card(ev, TZ).to_dict() == build_week_board_payload([ev], "T", TZ, False)[1].to_dict()


def test_week_message_each_embed_within_discord_limits():
    base = datetime(2026, 10, 23, 6, 30, tzinfo=timezone.utc)
    events = [
        _rich_event("T" * 200 + str(i), base + timedelta(hours=i), summary="[" * 280,
                    price_text="(" * 80, url=f"https://example.com/{i}")
        for i in range(40)
    ] + [_rich_event("U" * 200 + str(i), None) for i in range(60)]
    embeds = build_week_board_payload(events, "T", TZ, include_tba=True)
    assert len(embeds) <= 10
    assert all(_total_chars(e) <= 6000 for e in embeds)
    assert all(len(e.description or "") <= 4096 for e in embeds)
    assert all(len(e.title or "") <= 256 for e in embeds)
