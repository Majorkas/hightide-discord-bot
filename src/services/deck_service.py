"""Scryfall card and deck lookup services."""
import logging
import re
from dataclasses import dataclass
from time import monotonic as _monotonic
from urllib.parse import urlparse

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


_SCRYFALL_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


async def scryfall_card_by_id(sfid: str, *, retries: bool = True, timeout: float = 15.0) -> dict | None:
    """Fetch a card from Scryfall by its UUID.

    Returns dict on 200; None on 404 or a malformed id (no request is made).
    Raises httpx.TransportError or httpx.HTTPStatusError for network/5xx errors.
    """
    if not _SCRYFALL_ID.match(sfid):
        return None

    @_get_retry_decorator(retries)
    async def _get_with_retry(client: httpx.AsyncClient) -> httpx.Response:
        resp = await client.get(f"{SCRYFALL_API}/cards/{sfid}", headers=UA, timeout=timeout)
        if resp.status_code >= 500 or resp.status_code == 429:
            resp.raise_for_status()
        return resp

    async with httpx.AsyncClient() as client:
        resp = await _get_with_retry(client)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
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


@dataclass
class DeckSummary:
    title: str
    fmt: str
    card_count: int
    image_url: str | None
    url: str
    source: str  # "Moxfield" | "Archidekt"
    commander: str | None = None


_MOXFIELD_HOSTS = {"moxfield.com", "www.moxfield.com"}
_ARCHIDEKT_HOSTS = {"archidekt.com", "www.archidekt.com"}
_MOXFIELD_PATH = re.compile(r"^/decks/([A-Za-z0-9_-]{1,64})(?:/[^/]*)?/?$")
_ARCHIDEKT_PATH = re.compile(r"^/decks/(\d{1,12})(?:/[^/]*)?/?$")
_MIN_IMAGE_BUDGET = 0.25

_MOXFIELD_FORMATS = {
    "commander": "Commander", "standard": "Standard", "modern": "Modern",
    "pioneer": "Pioneer", "legacy": "Legacy", "vintage": "Vintage", "pauper": "Pauper",
    "brawl": "Brawl", "historicbrawl": "Historic Brawl", "standardbrawl": "Standard Brawl",
    "oathbreaker": "Oathbreaker", "duelcommander": "Duel Commander",
    "paupercommander": "Pauper Commander", "premodern": "Premodern",
    "pennydreadful": "Penny Dreadful", "historic": "Historic", "alchemy": "Alchemy",
    "timeless": "Timeless", "explorer": "Explorer", "gladiator": "Gladiator",
}
_ARCHIDEKT_FORMATS = {
    1: "Standard", 2: "Modern", 3: "Commander / EDH", 4: "Legacy", 5: "Vintage",
    6: "Pauper", 7: "Custom", 8: "Frontier", 9: "Future Standard", 10: "Penny Dreadful",
    11: "1v1 Commander", 12: "Dual Commander", 13: "Brawl",
}
_ARCHIDEKT_EXCLUDED = {"maybeboard", "sideboard"}


def parse_deck_url(url: str) -> tuple[str, str] | None:
    """Return (source, deck_id) for a Moxfield/Archidekt deck link, else None."""
    s = url.strip().strip("<>").strip()
    if not s:
        return None
    if "://" not in s:
        s = "https://" + s
    try:
        parsed = urlparse(s)
        host = parsed.hostname
        parsed.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.port:
        return None
    if host in _MOXFIELD_HOSTS:
        m = _MOXFIELD_PATH.match(parsed.path)
        return ("Moxfield", m.group(1)) if m else None
    if host in _ARCHIDEKT_HOSTS:
        m = _ARCHIDEKT_PATH.match(parsed.path)
        return ("Archidekt", m.group(1)) if m else None
    return None


async def _get_json_response(client: httpx.AsyncClient, url: str, retries: bool, timeout: float) -> httpx.Response:
    @_get_retry_decorator(retries)
    async def _get(c: httpx.AsyncClient) -> httpx.Response:
        resp = await c.get(url, headers=UA, timeout=timeout)
        if resp.status_code >= 500 or resp.status_code == 429:
            resp.raise_for_status()
        return resp

    return await _get(client)


def _moxfield_format(raw) -> str:
    key = str(raw or "").strip().lower()
    if key in _MOXFIELD_FORMATS:
        return _MOXFIELD_FORMATS[key]
    return key.replace("_", " ").replace("-", " ").title()[:30] or "Unknown"


def _as_int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


async def _moxfield_summary(data: dict, deck_id: str, started: float, *, retries: bool, timeout: float) -> DeckSummary:
    commander_name = None
    sfid = None
    commanders = data.get("commanders")
    if isinstance(commanders, dict):
        for entry in commanders.values():
            card = entry.get("card") if isinstance(entry, dict) else None
            if isinstance(card, dict):
                name, sfid_raw = card.get("name"), card.get("scryfall_id")
                commander_name = name if isinstance(name, str) and name else None
                sfid = sfid_raw if isinstance(sfid_raw, str) else None
                break

    image_url = None
    remaining = timeout - (_monotonic() - started)
    if sfid and remaining >= _MIN_IMAGE_BUDGET:
        try:
            card = await scryfall_card_by_id(sfid, retries=retries, timeout=remaining)
            if card:
                image_url = _get_card_image_url(card)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as e:
            log.warning("Scryfall image lookup failed for deck %s: %s", deck_id, e)

    title = data.get("name")
    # Cap commander at 100 chars and format at 40 chars
    capped_commander = (commander_name[:100] if commander_name else None)
    capped_format = _moxfield_format(data.get("format"))[:40]
    return DeckSummary(
        title=title if isinstance(title, str) and title.strip() else "Untitled deck",
        fmt=capped_format,
        card_count=_as_int(data.get("mainboardCount")) + _as_int(data.get("commandersCount")),
        image_url=image_url,
        url=_safe_url(f"https://moxfield.com/decks/{deck_id}") or "",
        source="Moxfield",
        commander=capped_commander,
    )


def _archidekt_summary(data: dict, deck_id: str) -> DeckSummary:
    count = 0
    commander = None
    for entry in data.get("cards") or []:
        if not isinstance(entry, dict):
            continue
        cats = {str(c).lower() for c in (entry.get("categories") or [])}
        if cats & _ARCHIDEKT_EXCLUDED:
            continue
        count += _as_int(entry.get("quantity"))
        if commander is None and "commander" in cats:
            oracle = (entry.get("card") or {}).get("oracleCard") or {}
            name = oracle.get("name")
            commander = name if isinstance(name, str) and name else None

    image_url = None
    for key in ("customFeatured", "featured"):
        raw_url = data.get(key)
        if isinstance(raw_url, str):
            # Require https for Archidekt image URLs
            if raw_url.startswith("https://"):
                image_url = _safe_url(raw_url)
                if image_url:
                    break

    title = data.get("name")
    # Cap commander at 100 chars
    capped_commander = (commander[:100] if commander else None)
    return DeckSummary(
        title=title if isinstance(title, str) and title.strip() else "Untitled deck",
        fmt=_ARCHIDEKT_FORMATS.get(data.get("deckFormat"), "Unknown"),
        card_count=count,
        image_url=image_url,
        url=_safe_url(f"https://archidekt.com/decks/{deck_id}") or "",
        source="Archidekt",
        commander=capped_commander,
    )


async def fetch_deck(url: str, *, retries: bool = True, timeout: float = 15.0) -> DeckSummary | None:
    """Fetch a deck summary from a Moxfield or Archidekt link.

    `timeout` is a total budget: the commander-image lookup only gets what the deck
    request left over, and is skipped (no image) when too little remains.

    Returns None for an unknown host, bad URL, 404/other non-200 or unreadable body.
    Raises httpx.TransportError or httpx.HTTPStatusError for network/5xx/429 errors
    on the deck request (the image lookup is best-effort and never raises).
    """
    parsed = parse_deck_url(url)
    if parsed is None:
        return None
    source, deck_id = parsed
    api = (f"https://api2.moxfield.com/v2/decklists/{deck_id}" if source == "Moxfield"
           else f"https://archidekt.com/api/decks/{deck_id}/")

    started = _monotonic()
    async with httpx.AsyncClient() as client:
        resp = await _get_json_response(client, api, retries, timeout)

    # Log non-200 responses for Moxfield (except 404)
    if source == "Moxfield" and resp.status_code != 200 and resp.status_code != 404:
        log.warning("Moxfield API returned %d for deck %s", resp.status_code, deck_id)

    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    if source == "Moxfield":
        return await _moxfield_summary(data, deck_id, started, retries=retries, timeout=timeout)
    return _archidekt_summary(data, deck_id)


def build_deck_embed(d: DeckSummary) -> discord.Embed:
    """Build a Discord embed for a deck summary."""
    noun = "card" if d.card_count == 1 else "cards"
    description = f"{d.source} · {_md_escape(d.fmt)} · {d.card_count} {noun}"
    if d.commander:
        description += f"\nCommander: {_md_escape(d.commander)}"

    embed = discord.Embed(
        title=_md_escape(d.title[:100]),
        url=_safe_url(d.url),
        description=description,
        color=0x1F8B4C,
    )
    image = _safe_url(d.image_url)
    if image:
        embed.set_image(url=image)
    return embed
