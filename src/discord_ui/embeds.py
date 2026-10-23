from collections import defaultdict
from datetime import date, datetime

import discord

from src.db.models import Event

# Discord embed limits
MAX_EMBED_FIELDS = 25
MAX_FIELD_VALUE_CHARS = 1024
MAX_EMBED_CHARS = 6000


def _truncate_lines(lines: list[str], max_chars: int) -> tuple[list[str], int]:
    """Truncate lines to fit within max_chars, return (truncated_lines, dropped_count)."""
    result = []
    current_len = 0
    dropped = 0
    for line in lines:
        if current_len + len(line) + 1 <= max_chars - 10:  # reserve 10 chars for "… +N more"
            result.append(line)
            current_len += len(line) + 1
        else:
            dropped += 1
    return result, dropped


def _count_embed_chars(embed: discord.Embed) -> int:
    """Count total characters in embed (title, description, fields, footer)."""
    count = len(embed.title or "")
    count += len(embed.description or "")
    for field in embed.fields:
        count += len(field.name) + len(field.value)
    count += len(embed.footer.text or "")
    return count


def build_board_embed(events: list[Event], title: str, tz,
                      include_tba: bool) -> discord.Embed:
    embed = discord.Embed(title=title, color=0x1F8B4C)
    dated = [e for e in events if e.starts_at is not None]
    tba = [e for e in events if e.starts_at is None]

    by_day: dict[date, list[Event]] = defaultdict(list)
    for e in dated:
        local = e.starts_at.astimezone(tz)
        by_day[local.date()].append(e)

    if not dated and not (include_tba and tba):
        embed.description = "No events found for this period."

    days = sorted(by_day)
    later_lines = []

    # Always cap day-fields at 24, reserving the 25th field for "Later" or TBA
    max_day_fields = MAX_EMBED_FIELDS - 1

    for i, day in enumerate(days):
        # Cap at max_day_fields to reserve space for "Later" and/or TBA
        if len(embed.fields) >= max_day_fields:
            for e in sorted(by_day[day], key=lambda x: x.starts_at):
                local = e.starts_at.astimezone(tz)
                time_part = local.strftime("%H:%M") if e.time_known else "time TBC"
                price_part = f" · {e.price_text}" if e.price_text else ""
                later_lines.append(f"`{time_part}` [{e.title}]({e.url}){price_part}")
            continue

        lines = []
        for e in sorted(by_day[day], key=lambda x: x.starts_at):
            local = e.starts_at.astimezone(tz)
            time_part = local.strftime("%H:%M") if e.time_known else "time TBC"
            price_part = f" · {e.price_text}" if e.price_text else ""
            lines.append(f"`{time_part}` [{e.title}]({e.url}){price_part}")

        # Truncate per-field to 1024 chars
        truncated, dropped = _truncate_lines(lines, MAX_FIELD_VALUE_CHARS)
        value_text = "\n".join(truncated)
        if dropped > 0:
            value_text += f"\n… +{dropped} more"
        embed.add_field(name=day.strftime("%a %d %b %Y"), value=value_text, inline=False)

    # Handle "Later" field if there are overflow days
    if later_lines:
        truncated, dropped = _truncate_lines(later_lines, MAX_FIELD_VALUE_CHARS)
        value_text = "\n".join(truncated)
        if dropped > 0:
            value_text += f"\n… +{dropped} more"
        embed.add_field(name="Later", value=value_text, inline=False)

    # Add TBA section (capped at 1024 chars)
    tba_field_added = False
    if include_tba and tba:
        tba_lines = [f"[{e.title}]({e.url})" for e in tba]
        tba_value = "\n".join(tba_lines)
        # Truncate TBA value to 1024 chars with indicator
        if len(tba_value) > MAX_FIELD_VALUE_CHARS:
            truncated, dropped = _truncate_lines(tba_lines, MAX_FIELD_VALUE_CHARS)
            tba_value = "\n".join(truncated)
            if dropped > 0:
                tba_value += f"\n… +{dropped} more"
        embed.add_field(name="Date TBA", value=tba_value, inline=False)
        tba_field_added = True

    # Set footer before enforcing 6000 char limit (so footer is included in count)
    embed.set_footer(text=f"Last updated {datetime.now(tz).strftime('%H:%M')} AWST")

    # Enforce 6000 char limit: drop/truncate fields from end
    while _count_embed_chars(embed) > MAX_EMBED_CHARS:
        if not embed.fields:
            break

        last_field = embed.fields[-1]
        # If TBA is last and still too large, replace it with ellipsis and break
        if tba_field_added and last_field.name == "Date TBA":
            embed.set_field_at(len(embed.fields) - 1, name="Date TBA",
                               value=f"… and {len(tba)} undated events", inline=False)
            tba_field_added = False  # Don't try to modify it again
            if _count_embed_chars(embed) <= MAX_EMBED_CHARS:
                break

        # Remove field from end using remove_field (which mutates internal state)
        embed.remove_field(len(embed.fields) - 1)

    return embed

