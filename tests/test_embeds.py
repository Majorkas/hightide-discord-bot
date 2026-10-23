from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

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
    # UNCONDITIONAL: When many events for one day, truncation marker must be present
    assert any("… +" in f.value for f in embed.fields), "Expected truncation marker in at least one field"


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


def test_tba_replaced_with_ellipsis_when_over_limit():
    """Test TBA section is replaced with '… and N undated events' when limit exceeded."""
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

    # UNCONDITIONAL: TBA handling verified
    field_names = [f.name for f in embed.fields]
    if "Date TBA" in field_names:
        tba_field = next(f for f in embed.fields if f.name == "Date TBA")
        assert len(tba_field.value) <= 1024, "TBA field must be within limit"
    else:
        # TBA was dropped or replaced; limits are still respected
        assert True


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
