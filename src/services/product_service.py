import logging
import re
from decimal import Decimal

import discord
import httpx
from sqlalchemy.orm import Session

from src.db.repositories import (
    get_alerted_product_ids, get_or_create_config, record_product_alerts,
)
from src.discord_ui.embeds import _link, _md_escape, _safe_url
from src.scraping.fetcher import (
    FetchError, fetch_category_products, fetch_store_products, resolve_category_id,
)
from src.scraping.hightide_parser import (
    MAX_PRICE_CHARS, MAX_TITLE_CHARS, MAX_URL_CHARS, _image_url,
)
from src.scraping.normalize import clean_title, extract_price
from src.services.role_service import _resolve_channel

log = logging.getLogger("hightide.products")

MAX_SLUGS = 10
MAX_SLUG_LEN = 50
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Per-slug marker (seeded even when the category was empty); can't collide with "wp:N" ids.
_SEED_PREFIX = "seeded:"
# Pre-per-slug global marker; honoured once, then migrated to per-slug markers.
_LEGACY_SEED_MARKER = "seeded"
_MAX_MINOR_UNIT = 4
_MAX_AFFIX_CHARS = 8
MAX_ALERTS_PER_RUN = 10
_MAX_IMAGE_URL_CHARS = 2000


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
        pid, link = product["id"], product["link"]
        if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(link, str):
            raise TypeError("bad id/link type")
        price = extract_price(product.get("content", {}).get("rendered", ""))
        image = _image_url(product)
        if not isinstance(image, str) or len(image) > _MAX_IMAGE_URL_CHARS:
            image = None
        return {
            "external_id": f"wp:{pid}",
            "title": clean_title(product["title"]["rendered"])[:MAX_TITLE_CHARS],
            "url": link[:MAX_URL_CHARS],
            "price": price[:MAX_PRICE_CHARS] if price else None,
            "image_url": image,
        }
    except (KeyError, TypeError, AttributeError, ValueError):
        log.warning("skipping malformed product id=%s",
                    product.get("id") if isinstance(product, dict) else None)
        return None


def format_store_price(prices) -> str | None:
    """Format a Store API `prices` object; None when absent, zero or malformed."""
    if not isinstance(prices, dict):
        return None
    raw = prices.get("price")
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit() or len(raw) > 12:
        return None
    try:
        minor = int(prices.get("currency_minor_unit", 2))
    except (TypeError, ValueError):
        return None
    if not 0 <= minor <= _MAX_MINOR_UNIT:
        return None
    amount = Decimal(raw).scaleb(-minor)
    if amount == 0:  # WooCommerce uses 0 for "no price set"
        return None
    affixes = [prices.get(k) if isinstance(prices.get(k), str) else ""
               for k in ("currency_prefix", "currency_suffix")]
    prefix, suffix = (a[:_MAX_AFFIX_CHARS] for a in affixes)
    return f"{prefix}{amount:,.{minor}f}{suffix}"


def _store_prices(base_url: str, slugs: list[str]) -> dict[str, str]:
    """wp:{id} -> formatted price from the Store API; failures degrade to no entries."""
    prices: dict[str, str] = {}
    for slug in slugs:
        try:
            store = fetch_store_products(base_url, resolve_category_id(base_url, slug))
        except (FetchError, httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("store price fetch for %s failed: %s", slug, e)
            continue
        for sp in store:
            if not isinstance(sp, dict):
                continue
            price = format_store_price(sp.get("prices"))
            if price and sp.get("id") is not None:
                prices.setdefault(f"wp:{sp['id']}", price)
    return prices


def _seeded_slugs(session: Session, guild_id: int, slugs: list[str],
                  alerted: set[str]) -> set[str]:
    seeded = {s for s in slugs if f"{_SEED_PREFIX}{s}" in alerted}
    has_slug_markers = any(x.startswith(_SEED_PREFIX) for x in alerted)
    if _LEGACY_SEED_MARKER in alerted and not has_slug_markers:
        # Old global marker covered every slug configured at the time.
        record_product_alerts(session, guild_id, [f"{_SEED_PREFIX}{s}" for s in slugs])
        return set(slugs)
    return seeded


def check_new_products(session: Session, guild_id: int, base_url: str, tz) -> list[dict]:
    """New products in watched categories; each slug seeds silently on first sight."""
    cfg = get_or_create_config(session, guild_id)
    if not cfg.new_stock_channel_id:
        return []
    slugs = parse_category_slugs(cfg.new_stock_cat_slugs or "pre-orders")
    alerted = get_alerted_product_ids(session, guild_id)
    seeded = _seeded_slugs(session, guild_id, slugs, alerted)

    per_slug: dict[str, dict[str, dict]] = {}
    unseeded_failures: list[str] = []
    for slug in slugs:
        try:
            products = fetch_category_products(base_url, slug)
        except (FetchError, httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("watched category %s fetch failed: %s", slug, e)
            if slug not in seeded:
                unseeded_failures.append(slug)
            continue
        found: dict[str, dict] = {}
        for product in products:
            alert = _to_alert(product)
            if alert is not None:
                found.setdefault(alert["external_id"], alert)
        per_slug[slug] = found

    if unseeded_failures and not per_slug:
        raise FetchError(f"cannot seed; categories failed: {', '.join(unseeded_failures)}")

    pending: dict[str, dict] = {}
    pending_slugs: list[str] = []
    for slug in slugs:
        if slug not in seeded:
            continue
        new = {x: a for x, a in per_slug.get(slug, {}).items() if x not in alerted}
        if new:
            pending_slugs.append(slug)
            for xid, a in new.items():
                pending.setdefault(xid, a)

    for slug in slugs:
        if slug in seeded or slug not in per_slug:
            continue
        # Products already queued to alert via a seeded slug must stay unrecorded.
        ids = [x for x in per_slug[slug] if x not in pending]
        record_product_alerts(session, guild_id, [*ids, f"{_SEED_PREFIX}{slug}"])
        log.info("seeded %d products for slug %s guild %s", len(ids), slug, guild_id)

    if pending:
        store_prices = _store_prices(base_url, pending_slugs)
        for xid, alert in pending.items():
            price = store_prices.get(xid) or alert["price"]
            alert["price"] = price[:MAX_PRICE_CHARS] if price else None
    return list(pending.values())


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
    for product in products[:MAX_ALERTS_PER_RUN]:
        try:
            await channel.send(embed=_alert_embed(product),
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("product alert failed for %s", product.get("external_id"))
            continue
        except Exception:
            # Poison pill: record it so a permanently failing product can't block the feed.
            log.exception("product alert unsendable for %s", product.get("external_id"))
            record_product_alerts(session, guild_id, [product["external_id"]])
            continue
        record_product_alerts(session, guild_id, [product["external_id"]])
        sent += 1
    overflow = len(products) - MAX_ALERTS_PER_RUN
    if overflow > 0:
        # Overflow stays unrecorded so later ticks drain it.
        plural = "s" if overflow != 1 else ""
        try:
            await channel.send(f"➕ {overflow} more new product{plural} — check the shop.",
                               allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            log.exception("product overflow digest failed")
    return sent
