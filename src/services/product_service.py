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
    FetchError, fetch_products_by_search, fetch_store_products_by_search,
)
from src.scraping.hightide_parser import (
    MAX_PRICE_CHARS, MAX_TITLE_CHARS, MAX_URL_CHARS, _image_url,
)
from src.scraping.normalize import clean_title, extract_price
from src.services.role_service import _resolve_channel

log = logging.getLogger("hightide.products")

MAX_TERMS = 10
MAX_TERM_LEN = 50
_TERM_RE = re.compile(r"^[\w][\w .&+'-]*\Z", re.UNICODE)
# Global marker (seeded even when no products matched); can't collide with "wp:N" ids.
_SEED_MARKER = "seeded"
_MAX_MINOR_UNIT = 4
_MAX_AFFIX_CHARS = 8
MAX_ALERTS_PER_RUN = 10
_MAX_IMAGE_URL_CHARS = 2000


def parse_search_terms(raw: str) -> list[str]:
    """Normalise a comma-separated search-term list; raises ValueError when invalid."""
    terms: list[str] = []
    for part in raw.split(","):
        term = part.strip().lower()
        if not term:
            continue
        if len(term) > MAX_TERM_LEN or not _TERM_RE.match(term):
            raise ValueError(f"invalid search term: {term[:MAX_TERM_LEN]!r}")
        if term not in terms:
            terms.append(term)
    if not terms:
        raise ValueError("no search terms given")
    if len(terms) > MAX_TERMS:
        raise ValueError(f"at most {MAX_TERMS} search terms")
    return terms


def _title_matches(product: dict, terms: list[str]) -> bool:
    """True when the product title contains any search term (title-only matching)."""
    title = product.get("title", {}).get("rendered", "")
    if not isinstance(title, str):
        return False
    title = title.lower()
    return any(term in title for term in terms)


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


def _store_prices(base_url: str, terms: list[str]) -> dict[str, str]:
    """wp:{id} -> formatted price from the Store API; failures degrade to no entries."""
    prices: dict[str, str] = {}
    for term in terms:
        try:
            store = fetch_store_products_by_search(base_url, term)
        except (FetchError, httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("store price fetch for %r failed: %s", term, e)
            continue
        for sp in store:
            if not isinstance(sp, dict):
                continue
            price = format_store_price(sp.get("prices"))
            if price and sp.get("id") is not None:
                prices.setdefault(f"wp:{sp['id']}", price)
    return prices


def check_new_products(session: Session, guild_id: int, base_url: str, tz) -> list[dict]:
    """New products whose title matches a watched search term; seeds silently on first run."""
    cfg = get_or_create_config(session, guild_id)
    if not cfg.new_stock_channel_id:
        return []
    terms = parse_search_terms(cfg.new_stock_terms or "preorder,pre-order")
    alerted = get_alerted_product_ids(session, guild_id)
    seeded = _SEED_MARKER in alerted

    # Union + dedupe every term's hits, keeping only title matches (search also
    # matches descriptions, which we deliberately ignore to avoid noise).
    found: dict[str, dict] = {}
    failures = 0
    for term in terms:
        try:
            products = fetch_products_by_search(base_url, term)
        except (FetchError, httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("product search for %r failed: %s", term, e)
            failures += 1
            continue
        for product in products:
            if not _title_matches(product, terms):
                continue
            alert = _to_alert(product)
            if alert is not None:
                found.setdefault(alert["external_id"], alert)

    if not seeded:
        if failures == len(terms):
            raise FetchError(f"cannot seed; all {failures} search terms failed")
        # First run: record everything currently matching without alerting.
        record_product_alerts(session, guild_id, [*found, _SEED_MARKER])
        log.info("seeded %d products for guild %s", len(found), guild_id)
        return []

    pending = {xid: a for xid, a in found.items() if xid not in alerted}
    if pending:
        store_prices = _store_prices(base_url, terms)
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
