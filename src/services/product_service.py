import logging
from decimal import Decimal

import discord
import httpx
from sqlalchemy.orm import Session

from src.db.repositories import (
    get_alerted_product_ids, get_or_create_config, record_product_alerts,
)
from src.discord_ui.embeds import _link, _md_escape, _safe_url
from src.scraping.fetcher import (
    FetchError, fetch_event_category_ids, fetch_latest_products, fetch_store_products_latest,
)
from src.scraping.hightide_parser import (
    MAX_PRICE_CHARS, MAX_TITLE_CHARS, MAX_URL_CHARS, _image_url,
)
from src.scraping.normalize import clean_title, extract_price, is_junk_title
from src.services.role_service import _resolve_channel

log = logging.getLogger("hightide.products")

# Global marker (seeded even when no products matched); can't collide with "wp:N" ids.
# Suffixed for this watch model so pre-existing DBs re-seed once instead of flooding.
_SEED_MARKER = "seeded:new-in"
_MAX_MINOR_UNIT = 4
_MAX_AFFIX_CHARS = 8
MAX_ALERTS_PER_RUN = 10
_MAX_IMAGE_URL_CHARS = 2000


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


def _store_prices(base_url: str) -> dict[str, str]:
    """wp:{id} -> formatted price from the Store API; failures degrade to no entries."""
    try:
        store = fetch_store_products_latest(base_url)
    except (FetchError, httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        log.warning("store price fetch failed: %s", e)
        return {}
    prices: dict[str, str] = {}
    for sp in store:
        if not isinstance(sp, dict):
            continue
        price = format_store_price(sp.get("prices"))
        if price and sp.get("id") is not None:
            prices.setdefault(f"wp:{sp['id']}", price)
    return prices


def check_new_products(session: Session, guild_id: int, base_url: str, tz) -> list[dict]:
    """Newly added shop products (New In), minus event tickets; seeds silently on first run."""
    cfg = get_or_create_config(session, guild_id)
    if not cfg.new_stock_channel_id:
        return []
    alerted = get_alerted_product_ids(session, guild_id)
    seeded = _SEED_MARKER in alerted

    try:
        event_cats = fetch_event_category_ids(base_url)
        products = fetch_latest_products(base_url)
    except (FetchError, httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        # Without the category list event tickets can't be excluded; never risk posting those.
        if not seeded:
            raise FetchError(f"cannot seed; product fetch failed: {e}") from e
        log.warning("latest-products fetch failed (seeded run, skipping): %s", e)
        return []
    if not event_cats:
        log.warning("no event categories found; event tickets will not be excluded")

    found: dict[str, dict] = {}
    for product in products:
        if not isinstance(product, dict):
            continue
        cats = product.get("product_cat")
        if isinstance(cats, list) and any(
                isinstance(c, int) and not isinstance(c, bool) and c in event_cats
                for c in cats):
            continue  # event tickets are covered by the boards
        title = product.get("title", {})
        title = title.get("rendered", "") if isinstance(title, dict) else ""
        if isinstance(title, str) and is_junk_title(title):
            continue
        alert = _to_alert(product)
        if alert is not None:
            found.setdefault(alert["external_id"], alert)

    if not seeded:
        # First run: record everything currently in the shop without alerting.
        record_product_alerts(session, guild_id, [*found, _SEED_MARKER])
        log.info("seeded %d products for guild %s", len(found), guild_id)
        return []

    pending = {xid: a for xid, a in found.items() if xid not in alerted}
    if pending:
        store_prices = _store_prices(base_url)
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
