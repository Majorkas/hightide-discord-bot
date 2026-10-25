"""Trello REST integration: create cards for confirmed decisions, assign mapped members."""
import logging
import re
from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src import config

log = logging.getLogger("hightide.trello")

_MD_SPECIAL = re.compile(r"([\\`*_\[\]()])")


def _md(text: str) -> str:
    """Escape Trello-markdown metacharacters in user-controlled text."""
    return _MD_SPECIAL.sub(r"\\\1", text or "")

API = "https://api.trello.com/1"
_TIMEOUT = 10.0


class TrelloError(Exception):
    """Trello call failed (bad auth, missing list, network) — never raised to callers."""


@dataclass
class TrelloCard:
    card_id: str
    url: str


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return False


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=6),
       retry=retry_if_exception(_is_retryable), reraise=True)
async def _post(client: httpx.AsyncClient, url: str, **params) -> httpx.Response:
    resp = await client.post(url, params=params, timeout=_TIMEOUT)
    if resp.status_code >= 500 or resp.status_code == 429:
        resp.raise_for_status()
    return resp


async def create_card(list_id: str, name: str, desc: str,
                      member_id: str | None = None) -> TrelloCard | None:
    """Create a Trello card on `list_id`; assign `member_id` when given.

    Returns a TrelloCard on success; None on any failure (logged, never raises).
    """
    if not (config.TRELLO_API_KEY and config.TRELLO_API_TOKEN and list_id):
        log.warning("Trello not configured (missing key/token/list) — skipping card create")
        return None
    params = {
        "key": config.TRELLO_API_KEY, "token": config.TRELLO_API_TOKEN,
        "idList": list_id, "name": name[:16384], "desc": desc[:16384],
    }
    if member_id:
        params["idMembers"] = member_id
    try:
        async with httpx.AsyncClient() as client:
            resp = await _post(client, f"{API}/cards", **params)
        if resp.status_code != 200:
            # Never log the response/exception text: the request URL carries the API
            # key+token and the card content in its query string.
            log.warning("Trello create card returned HTTP %d", resp.status_code)
            return None
        data = resp.json()
        card_id, url = data.get("id"), data.get("url") or data.get("shortUrl")
        if not isinstance(card_id, str) or not card_id:
            log.warning("Trello create card returned no usable id")
            return None
        return TrelloCard(card_id, url if isinstance(url, str) else None)
    except httpx.HTTPStatusError as e:
        log.warning("Trello create card failed: HTTP %d", e.response.status_code)
        return None
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as e:
        log.warning("Trello create card failed: %s", type(e).__name__)
        return None


def build_card_description(*, decider_name: str, when: str, channel_name: str,
                           message_url: str | None, excerpt: str) -> str:
    """Markdown body for the decision's Trello card; user content is markdown-escaped here."""
    lines = [
        f"**Decided by:** {_md(decider_name)}",
        f"**When:** {_md(when)}",
        f"**Channel:** {_md(channel_name)}",
    ]
    if message_url:
        lines.append(f"**Source:** {message_url}")
    lines += ["", "**Excerpt:**", _md(excerpt)]
    return "\n".join(lines)
