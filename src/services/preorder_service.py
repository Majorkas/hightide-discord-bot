"""Persistent preorders board: one full image card per current preorder product."""
import asyncio
import hashlib
import json
import logging

import discord
from sqlalchemy.orm import Session

from src.db.repositories import get_board_post, get_or_create_config, save_board_post
from src.discord_ui.embeds import build_preorder_card
from src.scraping.fetcher import FetchError, fetch_preorder_products
from src.scraping.normalize import clean_title, is_junk_title
from src.services.board_service import (
    _delete_stored_messages, _get_board_lock, _loads_ids,
)
from src.services.product_service import _store_prices
from src.services.role_service import _resolve_channel

log = logging.getLogger("hightide.preorders")

BOARD_TYPE = "preorders"
_PREORDER_TERMS = ("preorder", "pre-order")
_STORE_PRICE_CAP = 80


def _is_preorder(product: dict) -> bool:
    """Title-only preorder match (WP search also matches content; titles avoid the noise)."""
    title = product.get("title", {})
    title = title.get("rendered", "") if isinstance(title, dict) else ""
    if not isinstance(title, str):
        return False
    lowered = title.lower()
    return any(term in lowered for term in _PREORDER_TERMS)


def collect_preorders(base_url: str) -> list[dict]:
    """Current preorder products (title-matched, junk excluded), newest first.

    Raises FetchError / httpx.HTTPError on failure — the caller (scheduler) treats the
    board update as failed rather than wipe the board on a transient error.
    """
    found: dict[int, dict] = {}
    for term in _PREORDER_TERMS:
        for product in fetch_preorder_products(base_url, term):
            if not isinstance(product, dict):
                continue
            pid = product.get("id")
            if not isinstance(pid, int) or isinstance(pid, bool):
                continue
            if not _is_preorder(product) or is_junk_title(
                    product.get("title", {}).get("rendered", "")):
                continue
            link = product.get("link")
            found.setdefault(pid, {
                "id": pid,
                "title": clean_title(product["title"]["rendered"]),
                "url": link if isinstance(link, str) else "",
                "image_url": _image_url(product),
            })

    if not found:
        return []
    prices = _store_prices(base_url)
    items = list(found.values())
    for item in items:
        price = prices.get(f"wp:{item['id']}")
        item["price"] = price[:_STORE_PRICE_CAP] if price else None
    return items


def _image_url(product: dict) -> str | None:
    try:
        media = product["_embedded"]["wp:featuredmedia"][0]
        url = media.get("source_url")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    return url if isinstance(url, str) and url else None


def _item_hash(item: dict) -> str:
    parts = [str(item.get("title")), str(item.get("url")), str(item.get("price")),
             str(item.get("image_url"))]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _header_hash(count: int) -> str:
    return hashlib.sha1(f"preorders:{count}".encode("utf-8")).hexdigest()


def _header_embed(count: int) -> discord.Embed:
    desc = (f"{count} item{'s' if count != 1 else ''} available to pre-order."
            if count else "Nothing available to pre-order right now.")
    return discord.Embed(title="Pre-orders at Hightide", description=desc, color=0x1F8B4C)


async def upsert_preorder_board(bot, session: Session, guild_id: int, base_url: str) -> str:
    """Create/update the preorder board's message set; deletes cards for gone products."""
    async with _get_board_lock(guild_id, BOARD_TYPE):
        cfg = get_or_create_config(session, guild_id)
        if not cfg.preorder_channel_id:
            return "no_channel"
        channel = await _resolve_channel(bot, cfg.preorder_channel_id)
        if channel is None:
            return "no_channel"

        items = await asyncio.to_thread(collect_preorders, base_url)
        keys = [str(i["id"]) for i in items]
        hashes = {"header": _header_hash(len(items))}
        hashes.update({k: _item_hash(i) for k, i in zip(keys, items)})
        no_mentions = discord.AllowedMentions.none()

        post = get_board_post(session, guild_id, BOARD_TYPE)
        stored: dict[str, tuple[int, str | None]] = {}
        extras: list[int] = []
        if post and post.channel_id == channel.id:
            ids = [post.message_id, *_loads_ids(post.message_ids)]
            try:
                data = json.loads(post.render_hash)
                skeys, shashes = data["keys"], data["hashes"]
                if isinstance(skeys, list) and isinstance(shashes, dict) and len(skeys) == len(ids):
                    stored = {k: (mid, shashes.get(k)) for k, mid in zip(skeys, ids)}
            except (ValueError, TypeError, KeyError):
                extras = ids[1:]

        # Edit the header (or note it for recreation).
        header_msg = None
        if "header" in stored:
            try:
                header_msg = await channel.fetch_message(stored["header"][0])
                if stored["header"][1] != hashes["header"]:
                    await header_msg.edit(embed=_header_embed(len(items)), allowed_mentions=no_mentions)
            except discord.NotFound:
                header_msg = None

        # Key-set diff: if the stored card keys don't exactly match the current set, recreate.
        if header_msg is not None and stored:
            stored_cards = [k for k in stored if k != "header"]
            if sorted(stored_cards) != sorted(keys):
                all_msgs = [mid for k, (mid, _) in stored.items()] + extras
                await _delete_stored_messages(bot, channel, all_msgs)
                stored, extras, header_msg = {}, [], None

        if header_msg is None:
            if stored:  # header gone but cards remain: drop them
                await _delete_stored_messages(
                    bot, channel, [mid for k, (mid, _) in stored.items() if k != "header"] + extras)
            stored, extras = {}, []

        # Send / edit cards, tracking newly sent messages for cleanup on failure.
        sent: list = []
        new_ids: dict[str, int] = {}
        try:
            if header_msg is None:
                header_msg = await channel.send(embed=_header_embed(len(items)),
                                                allowed_mentions=no_mentions)
                sent.append(header_msg)
            new_ids["header"] = header_msg.id

            for key, item in zip(keys, items):
                embed = build_preorder_card(item)
                old = stored.get(key)
                if old is not None and old[1] == hashes[key]:
                    new_ids[key] = old[0]
                    continue
                if old is not None:
                    try:
                        card_msg = await channel.fetch_message(old[0])
                        await card_msg.edit(embed=embed, allowed_mentions=no_mentions)
                        new_ids[key] = old[0]
                        continue
                    except discord.NotFound:
                        pass
                msg = await channel.send(embed=embed, allowed_mentions=no_mentions)
                sent.append(msg)
                new_ids[key] = msg.id

            stale = [mid for k, (mid, _) in stored.items()
                     if k not in new_ids] + extras
            save_board_post(session, guild_id, BOARD_TYPE, "preorders", channel.id,
                            new_ids["header"],
                            json.dumps({"keys": ["header", *keys], "hashes": hashes}),
                            json.dumps([new_ids[k] for k in keys]))
        except Exception:
            log.exception("preorder board update failed; removing %d newly sent message(s)",
                          len(sent))
            for msg in sent:
                try:
                    await msg.delete()
                except Exception:
                    log.exception("cleanup of unsaved preorder message failed")
            raise

        await _delete_stored_messages(bot, channel, stale)
        return "created" if not post else "edited"
