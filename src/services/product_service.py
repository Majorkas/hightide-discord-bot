import logging
import re

import discord
import httpx
from sqlalchemy.orm import Session

from src.db.repositories import (
    get_alerted_product_ids, get_or_create_config, record_product_alerts,
)
from src.discord_ui.embeds import _link, _md_escape, _safe_url
from src.scraping.fetcher import FetchError, fetch_category_products
from src.scraping.hightide_parser import (
    MAX_PRICE_CHARS, MAX_TITLE_CHARS, MAX_URL_CHARS, _image_url,
)
from src.scraping.normalize import clean_title, extract_price
from src.services.role_service import _resolve_channel

log = logging.getLogger("hightide.products")

MAX_SLUGS = 10
MAX_SLUG_LEN = 50
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Marks a guild as seeded even when the watched categories were empty at seed time.
_SEED_MARKER = "seeded"


def parse_category_slugs(raw: str) -> list[str]:
    """Normalise a comma-separated slug list; raises ValueError when invalid."""
    slugs: list[str] = []
    for part in raw.split(","):
        slug = part.strip().lower()
        if not slug:
            continue
        if len(slug) > MAX_SLUG_LEN or not _SLUG_RE.match(slug):
            raise ValueError(f"invalid category slug: {slug[:MAX_SLUG_LEN]!r}")
        if slug not in slugs:
            slugs.append(slug)
    if not slugs:
        raise ValueError("no category slugs given")
    if len(slugs) > MAX_SLUGS:
        raise ValueError(f"at most {MAX_SLUGS} categories")
    return slugs


def _to_alert(product: dict) -> dict | None:
    try:
        price = extract_price(product.get("content", {}).get("rendered", ""))
        return {
            "external_id": f"wp:{product['id']}",
            "title": clean_title(product["title"]["rendered"])[:MAX_TITLE_CHARS],
            "url": product["link"][:MAX_URL_CHARS],
            "price": price[:MAX_PRICE_CHARS] if price else None,
            "image_url": _image_url(product),
        }
    except (KeyError, TypeError, AttributeError):
        log.warning("skipping malformed product id=%s",
                    product.get("id") if isinstance(product, dict) else None)
        return None


def check_new_products(session: Session, guild_id: int, base_url: str, tz) -> list[dict]:
    """Products in watched categories not yet alerted; the first run seeds silently."""
    cfg = get_or_create_config(session, guild_id)
    if not cfg.new_stock_channel_id:
        return []
    slugs = parse_category_slugs(cfg.new_stock_cat_slugs or "pre-orders")
    alerted = get_alerted_product_ids(session, guild_id)
    first_run = not alerted

    found: dict[str, dict] = {}
    failures: list[str] = []
    for slug in slugs:
        try:
            products = fetch_category_products(base_url, slug)
        except (FetchError, httpx.HTTPError) as e:
            log.warning("watched category %s fetch failed: %s", slug, e)
            failures.append(slug)
            continue
        for product in products:
            alert = _to_alert(product)
            if alert is not None:
                found.setdefault(alert["external_id"], alert)

    if first_run:
        if failures:
            # A partial seed would flood the missing category's products later.
            raise FetchError(f"cannot seed; categories failed: {', '.join(failures)}")
        record_product_alerts(session, guild_id, [*found, _SEED_MARKER])
        log.info("seeded %d products for guild %s", len(found), guild_id)
        return []
    return [a for xid, a in found.items() if xid not in alerted]


def _alert_embed(product: dict) -> discord.Embed:
    price = _md_escape(product["price"]) if product.get("price") else "—"
    embed = discord.Embed(
        description=f"{_link(product['title'], product['url'])} · {price}")
    image = _safe_url(product.get("image_url"))
    if image:
        embed.set_thumbnail(url=image)
    return embed


async def post_product_alerts(bot, session: Session, guild_id: int, products: list[dict]) -> int:
    """Post one alert per product; each is recorded only after its send succeeds."""
    cfg = get_or_create_config(session, guild_id)
    if not products or not cfg.new_stock_channel_id:
        return 0
    channel = await _resolve_channel(bot, cfg.new_stock_channel_id)
    if channel is None:
        return 0
    sent = 0
    for product in products:
        try:
            await channel.send(embed=_alert_embed(product),
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("product alert failed for %s", product["external_id"])
            continue
        record_product_alerts(session, guild_id, [product["external_id"]])
        sent += 1
    return sent
