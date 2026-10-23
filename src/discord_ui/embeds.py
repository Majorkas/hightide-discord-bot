from collections import defaultdict
from datetime import datetime

import discord

from src.db.models import Event


def build_board_embed(events: list[Event], title: str, tz,
                      include_tba: bool) -> discord.Embed:
    embed = discord.Embed(title=title, color=0x1F8B4C)
    dated = [e for e in events if e.starts_at is not None]
    tba = [e for e in events if e.starts_at is None]

    by_day: dict[datetime.date, list[Event]] = defaultdict(list)
    for e in dated:
        local = e.starts_at.astimezone(tz)
        by_day[local.date()].append(e)

    if not dated and not (include_tba and tba):
        embed.description = "No events found for this period."

    for day in sorted(by_day):
        lines = []
        for e in sorted(by_day[day], key=lambda x: x.starts_at):
            local = e.starts_at.astimezone(tz)
            time_part = local.strftime("%H:%M") if e.time_known else "time TBC"
            price_part = f" · {e.price_text}" if e.price_text else ""
            lines.append(f"`{time_part}` [{e.title}]({e.url}){price_part}")
        embed.add_field(name=day.strftime("%a %d %b %Y"),
                        value="\n".join(lines), inline=False)

    if include_tba and tba:
        lines = [f"[{e.title}]({e.url})" for e in tba]
        embed.add_field(name="Date TBA", value="\n".join(lines), inline=False)

    embed.set_footer(text=f"Last updated {datetime.now(tz).strftime('%H:%M')} AWST")
    return embed
