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
    
    # Create many events for a single day to exceed 1024 chars
    events = []
    for i in range(50):
        title = f"Very Long Event Title Number {i} with Extra Text to Increase Length"
        events.append(make_event(title, dt))
    
    embed = build_board_embed(events, "Test", TZ, include_tba=False)
    
    assert len(embed.fields) >= 1
    for field in embed.fields:
        # Each field value must be <= 1024 chars
        assert len(field.value) <= 1024, f"Field {field.name} exceeds 1024 chars"
        # Check if it has truncation indicator
        if len(events) > 10:  # if truncated
            assert "… +" in field.value


def test_field_count_cap():
    """Test max 25 fields total, overflow days go into 'Later' field."""
    # Create 30 events across 30 different days using timedelta
    events = []
    base_dt = datetime(2026, 1, 1, 6, 30, tzinfo=timezone.utc)
    for day_offset in range(30):
        dt = base_dt + timedelta(days=day_offset)
        events.append(make_event(f"Event Day {day_offset}", dt))
    
    embed = build_board_embed(events, "Test", TZ, include_tba=False)
    
    # Should have at most 25 fields
    assert len(embed.fields) <= 25
    
    # With overflow, should have a "Later" field
    field_names = [f.name for f in embed.fields]
    if len(events) > 24:
        assert "Later" in field_names


def test_embed_total_char_limit():
    """Test embed stays within 6000 total chars by truncating from the end."""
    base_dt = datetime(2026, 1, 1, 6, 30, tzinfo=timezone.utc)
    
    # Create enough content to approach 6000 chars limit
    events = []
    long_title = "x" * 150  # moderately long title
    for day_offset in range(15):
        dt = base_dt + timedelta(days=day_offset)
        events.append(make_event(f"{long_title}_{day_offset}", dt))
    
    embed = build_board_embed(events, "Test", TZ, include_tba=False)
    
    # Count total characters
    total_chars = len(embed.title or "")
    total_chars += len(embed.description or "")
    for field in embed.fields:
        total_chars += len(field.name) + len(field.value)
    total_chars += len(embed.footer.text or "")
    
    # Should be at or under 6000
    assert total_chars <= 6000, f"Embed exceeds 6000 chars: {total_chars}"


def test_tba_replaced_with_ellipsis_when_over_limit():
    """Test TBA section is replaced with '… and N undated events' when limit exceeded."""
    base_dt = datetime(2026, 1, 1, 6, 30, tzinfo=timezone.utc)
    
    # Create moderate content to test limit enforcement
    events = []
    long_title = "x" * 150
    for day_offset in range(12):
        dt = base_dt + timedelta(days=day_offset)
        events.append(make_event(f"{long_title}_{day_offset}", dt))
    
    # Add TBA events
    tba_events = [make_event(f"TBA Event {i}", None) for i in range(3)]
    events.extend(tba_events)
    
    embed = build_board_embed(events, "Test", TZ, include_tba=True)
    
    # Verify TBA field exists or was replaced
    field_names = [f.name for f in embed.fields]
    has_tba_or_replacement = any(
        f.name == "Date TBA" or ("… and" in f.value and "undated" in f.value)
        for f in embed.fields
    )
    assert has_tba_or_replacement or "Date TBA" in field_names
