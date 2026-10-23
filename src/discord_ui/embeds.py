import re
from collections import defaultdict
from datetime import date, datetime
from urllib.parse import urlparse

import discord

from src.db.models import Event

# Discord embed limits
MAX_EMBED_FIELDS = 25
MAX_FIELD_VALUE_CHARS = 1024
MAX_EMBED_CHARS = 6000

_MD_EXTRA = re.compile(r"([\[\]()<>])")
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")
_URL_UNSAFE = re.compile(r"[()<>\s\x00-\x1f\x7f]")


def _md_escape(s: str) -> str:
    """Neutralise markdown, links and mentions in scraped text."""
    s = _BIDI.sub("", s)
    s = re.sub(r"\s*[\r\n]+\s*", " ", s)
    s = discord.utils.escape_markdown(s)
    s = _MD_EXTRA.sub(r"\\\1", s)
    return discord.utils.escape_mentions(s)


def _safe_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return _URL_UNSAFE.sub(lambda m: f"%{ord(m.group(0)):02X}", url)


def _link(title: str, url: str | None) -> str:
    text = _md_escape(title)
    safe = _safe_url(url)
    return f"[{text}]({safe})" if safe else text


def _event_line(e: Event, tz, with_day: bool = False) -> str:
    local = e.starts_at.astimezone(tz)
    time_part = local.strftime("%H:%M") if e.time_known else "time TBC"
    day_part = f"{local.strftime('%a %d %b')} " if with_day else ""
    price_part = f" · {_md_escape(e.price_text)}" if e.price_text else ""
    return f"{day_part}`{time_part}` {_link(e.title, e.url)}{price_part}"


def _truncate_lines(lines: list[str], max_chars: int) -> tuple[str, int]:
    """Join lines within max_chars, appending '… +N more'; return (text, dropped)."""
    if len("\n".join(lines)) <= max_chars:
        return "\n".join(lines), 0
    reserve = len(f"\n… +{len(lines)} more")
    budget = max_chars - reserve
    kept: list[str] = []
    used = 0
    for line in lines:
        needed = len(line) + (1 if kept else 0)
        if used + needed > budget:
            break
        kept.append(line)
        used += needed
    dropped = len(lines) - len(kept)
    if not kept:
        return f"… +{dropped} more", dropped
    return "\n".join(kept) + f"\n… +{dropped} more", dropped


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
    tba = sorted((e for e in events if e.starts_at is None), key=lambda e: e.title)

    by_day: dict[date, list[Event]] = defaultdict(list)
    for e in dated:
        by_day[e.starts_at.astimezone(tz).date()].append(e)

    will_render_tba = include_tba and len(tba) > 0
    if not dated and not will_render_tba:
        embed.description = "No events found for this period."

    # (name, value, event_count) — built off-embed so the 6000 guard can trim cleanly.
    fields: list[tuple[str, str, int]] = []
    later_lines: list[str] = []
    # Reserve slots: Later (1) and TBA (1 when rendered) within the 25-field cap.
    max_day_fields = 23 if will_render_tba else 24

    for day in sorted(by_day):
        day_events = sorted(by_day[day], key=lambda x: (x.starts_at, x.title))
        if len(fields) >= max_day_fields:
            later_lines.extend(_event_line(e, tz, with_day=True) for e in day_events)
            continue
        lines = [_event_line(e, tz) for e in day_events]
        value, _ = _truncate_lines(lines, MAX_FIELD_VALUE_CHARS)
        fields.append((day.strftime("%a %d %b %Y"), value, len(lines)))

    if later_lines:
        value, _ = _truncate_lines(later_lines, MAX_FIELD_VALUE_CHARS)
        fields.append(("Later", value, len(later_lines)))

    if will_render_tba:
        tba_lines = [_link(e.title, e.url) for e in tba]
        value, _ = _truncate_lines(tba_lines, MAX_FIELD_VALUE_CHARS)
        fields.append(("Date TBA", value, len(tba_lines)))

    footer = f"Last updated {datetime.now(tz).strftime('%H:%M')} AWST"
    total = (len(embed.title or "") + len(embed.description or "") + len(footer)
             + sum(len(n) + len(v) for n, v, _ in fields))

    if total > MAX_EMBED_CHARS:
        total_events = sum(c for _, _, c in fields)
        # Worst-case indicator size: N can't exceed the total event count.
        reserve = len("…") + len(f"… +{total_events} more events")
        removed = 0
        while fields and (total + reserve > MAX_EMBED_CHARS
                          or len(fields) >= MAX_EMBED_FIELDS):
            name, value, count = fields.pop()
            total -= len(name) + len(value)
            removed += count
        fields.append(("…", f"… +{removed} more events", removed))

    for name, value, _ in fields:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text=footer)
    return embed


MAX_EVENT_CARDS = 7
MAX_MESSAGE_EMBEDS = 10
_COLOR = 0x1F8B4C
_COMPACT_RESERVE = 800  # chars kept free per compact embed when sizing cards


def _card_description(e: Event, tz, summary_cap: int | None) -> str:
    local = e.starts_at.astimezone(tz)
    time_part = local.strftime("%H:%M") if e.time_known else "time TBC"
    info = f"{local.strftime('%a %d %b')} · {time_part}"
    if e.price_text:
        info += f" · {_md_escape(e.price_text)}"
    summary = e.summary or ""
    if summary_cap is not None and len(summary) > summary_cap:
        summary = summary[:summary_cap].rstrip() + "…" if summary_cap else ""
    return f"{info}\n{_md_escape(summary)}" if summary else info


def _event_card(e: Event, tz, summary_cap: int | None) -> discord.Embed:
    card = discord.Embed(title=e.title, url=_safe_url(e.url),
                         description=_card_description(e, tz, summary_cap), color=_COLOR)
    image = _safe_url(e.image_url)
    if image:
        card.set_image(url=image)
    return card


def _compact_embed(title: str, lines: list[str], max_chars: int) -> discord.Embed:
    budget = max(0, min(4096, max_chars - len(title)))
    text, _ = _truncate_lines(lines, budget)
    return discord.Embed(title=title, description=text, color=_COLOR)


def build_rich_board_embeds(events: list[Event], title: str, tz,
                            include_tba: bool) -> list[discord.Embed]:
    """One card per dated event (max 7), then 'Also this week' overflow and 'Date TBA'."""
    dated = sorted((e for e in events if e.starts_at is not None),
                   key=lambda e: (e.starts_at, e.title))
    tba = sorted((e for e in events if e.starts_at is None), key=lambda e: e.title)
    show_tba = include_tba and bool(tba)
    footer = f"Last updated {datetime.now(tz).strftime('%H:%M')} AWST"

    if not dated and not show_tba:
        empty = discord.Embed(title=title, description="No events found for this period.",
                              color=_COLOR)
        empty.set_footer(text=footer)
        return [empty]

    card_events, overflow = dated[:MAX_EVENT_CARDS], dated[MAX_EVENT_CARDS:]
    compact_count = bool(overflow) + show_tba

    # The 6000-char cap spans the whole message: shrink summaries until cards fit.
    fixed = len(title) + len(footer)
    cards: list[discord.Embed] = []
    for cap in (None, 140, 60, 0):
        cards = [_event_card(e, tz, cap) for e in card_events]
        used = fixed + sum(_count_embed_chars(c) for c in cards)
        if used + _COMPACT_RESERVE * compact_count <= MAX_EMBED_CHARS:
            break
    embeds = cards
    used = fixed + sum(_count_embed_chars(c) for c in cards)

    if overflow:
        by_day: dict[date, list[Event]] = defaultdict(list)
        for e in overflow:
            by_day[e.starts_at.astimezone(tz).date()].append(e)
        lines: list[str] = []
        for day in sorted(by_day):
            lines.append(f"**{day.strftime('%a %d %b %Y')}**")
            lines.extend(_event_line(e, tz) for e in by_day[day])
        budget = MAX_EMBED_CHARS - used - (_COMPACT_RESERVE if show_tba else 0)
        embed = _compact_embed("Also this week", lines, budget)
        used += _count_embed_chars(embed)
        embeds.append(embed)

    if show_tba:
        embeds.append(_compact_embed("Date TBA", [_link(e.title, e.url) for e in tba],
                                     MAX_EMBED_CHARS - used))

    assert len(embeds) <= MAX_MESSAGE_EMBEDS
    if cards:
        embeds[0].set_author(name=title)
    embeds[0].set_footer(text=footer)
    return embeds
