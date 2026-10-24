"""Scryfall card and deck lookup services."""
import logging

import discord
import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src.discord_ui.embeds import _md_escape, _safe_url

log = logging.getLogger("hightide.deck_service")

UA = {"User-Agent": "hightide-events-bot/0.2"}
SCRYFALL_API = "https://api.scryfall.com"


def _is_retryable(exc: BaseException) -> bool:
    """Determine if an exception is retryable."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code >= 500 or code == 429
    return False


def _get_retry_decorator(retries: bool):
    """Return retry decorator or identity based on retries flag."""
    if retries:
        return retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(min=1, max=8),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )
    else:
        # No retry: identity decorator
        def identity(func):
            return func
        return identity


async def scryfall_card(name: str, *, retries: bool = True, timeout: float = 15.0) -> dict | None:
    """Fetch a card from Scryfall using fuzzy name matching.

    Args:
        name: Card name to look up
        retries: Whether to retry on transient errors (default True)
        timeout: Request timeout in seconds (default 15.0)

    Returns dict with card data on 200.
    Returns None on 404 (card not found).
    Raises httpx.TransportError or httpx.HTTPStatusError for network/5xx errors.
    """
    @_get_retry_decorator(retries)
    async def _get_with_retry(client: httpx.AsyncClient) -> httpx.Response:
        resp = await client.get(f"{SCRYFALL_API}/cards/named", params={"fuzzy": name}, headers=UA, timeout=timeout)
        if resp.status_code >= 500 or resp.status_code == 429:
            resp.raise_for_status()
        return resp

    async with httpx.AsyncClient() as client:
        resp = await _get_with_retry(client)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 404:
            return None
        resp.raise_for_status()
        return None


async def scryfall_card_exact(name: str) -> dict | None:
    """Fetch a card from Scryfall using exact name matching.

    Args:
        name: Card name to look up (exact match required)

    Returns:
        dict with card data on 200, None on 404.

    Raises:
        httpx.TransportError: Network/connectivity errors (propagate to caller).
        httpx.HTTPStatusError: 5xx and 429 errors (propagate to caller after retries).

    Used by Task 9 for wants board card resolution. Callers must handle exceptions.
    """
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    async def _get_with_retry(client: httpx.AsyncClient) -> httpx.Response:
        resp = await client.get(f"{SCRYFALL_API}/cards/named", params={"exact": name}, headers=UA, timeout=15.0)
        if resp.status_code >= 500 or resp.status_code == 429:
            resp.raise_for_status()
        return resp

    async with httpx.AsyncClient() as client:
        resp = await _get_with_retry(client)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 404:
            return None
        resp.raise_for_status()
        return None


def _get_card_image_url(card: dict) -> str | None:
    """Extract image URL from card data, handling double-faced cards."""
    # Single-faced card: use image_uris.normal
    if card.get("image_uris") and card["image_uris"].get("normal"):
        url = card["image_uris"]["normal"]
        return _safe_url(url)

    # Double-faced card: use card_faces[0].image_uris.normal (front face)
    if card.get("card_faces") and len(card["card_faces"]) > 0:
        front_face = card["card_faces"][0]
        if front_face.get("image_uris") and front_face["image_uris"].get("normal"):
            url = front_face["image_uris"]["normal"]
            return _safe_url(url)

    return None


def _trim_description_at_boundary(text: str, max_chars: int = 400) -> str:
    """Trim text to max_chars at a word boundary."""
    if len(text) <= max_chars:
        return text

    # Find last space before max_chars
    trimmed = text[:max_chars]
    last_space = trimmed.rfind(" ")
    if last_space > 0:
        return trimmed[:last_space].rstrip() + "…"
    return trimmed.rstrip() + "…"


def build_card_embed(card: dict) -> discord.Embed:
    """Build a Discord embed for a Scryfall card.

    Args:
        card: Card dict from Scryfall API

    Returns:
        discord.Embed with card name, image, and formatted description
    """
    name = card.get("name", "Unknown")
    scryfall_uri = card.get("scryfall_uri", "")

    # Validate and safe URL
    safe_uri = _safe_url(scryfall_uri)

    # Build description parts
    parts = []

    # Mana cost (use front face if main cost is empty)
    mana_cost = card.get("mana_cost") or ""
    if not mana_cost and card.get("card_faces") and len(card["card_faces"]) > 0:
        mana_cost = card["card_faces"][0].get("mana_cost", "")
    if mana_cost:
        parts.append(f"**{_md_escape(mana_cost)}**")

    # Type line
    if card.get("type_line"):
        parts.append(_md_escape(card["type_line"]))

    # Oracle text (use front face if main text is empty)
    oracle_text = card.get("oracle_text") or ""
    if not oracle_text and card.get("card_faces") and len(card["card_faces"]) > 0:
        oracle_text = card["card_faces"][0].get("oracle_text", "")
    if oracle_text:
        oracle = _md_escape(oracle_text)
        oracle = _trim_description_at_boundary(oracle, 400)
        parts.append(oracle)

    # Price
    if card.get("prices") and card["prices"].get("usd"):
        usd = float(card["prices"]["usd"])
        parts.append(f"**USD ${usd:.2f}**")

    description = "\n".join(parts)

    # Get image
    image_url = _get_card_image_url(card)

    # Create embed
    embed = discord.Embed(
        title=name,
        url=safe_uri,
        description=description,
        color=0x1F8B4C
    )

    if image_url:
        embed.set_image(url=image_url)

    return embed
