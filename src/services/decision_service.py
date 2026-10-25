"""AI decision detection and the pending -> confirmed/rejected lifecycle."""
import json
import logging
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src import config
from src.db.models import Decision
from src.db.repositories import (
    add_decision, get_confirmed_unsynced_decisions, set_decision_status,
)

log = logging.getLogger("hightide.decisions")

OPENAI_API = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o-mini"
MAX_CONTENT_CHARS = 500   # stored excerpt cap
MAX_SUMMARY_CHARS = 200   # AI one-liner cap
_TIMEOUT = 8.0

_SYSTEM = (
    "You detect decisions in a small hobby-store staff Discord. A decision is a concrete "
    "choice the team has settled on (e.g. \"we'll run Friday drafts at 7pm\", \"ordering the "
    "new set\", \"cancel the league\"). Casual chat, questions, and proposals with no agreement "
    "are NOT decisions. Reply with strict JSON only: "
    '{"is_decision": true|false, "summary": "<=200 char one-line summary of the decision", '
    '"decider_hint": "who settled it, if clear, else empty"}.'
)


@dataclass
class DetectionResult:
    is_decision: bool
    summary: str
    decider_hint: str


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return False


def _parse_detection(raw: str) -> DetectionResult | None:
    """Parse the model's JSON reply; None on any malformed/unsafe shape."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("is_decision"), bool):
        return None
    summary = data.get("summary")
    summary = summary.strip()[:MAX_SUMMARY_CHARS] if isinstance(summary, str) else ""
    hint = data.get("decider_hint")
    hint = hint.strip()[:100] if isinstance(hint, str) else ""
    return DetectionResult(data["is_decision"], summary, hint)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=6),
       retry=retry_if_exception(_is_retryable), reraise=True)
async def _call_openai(client: httpx.AsyncClient, text: str) -> httpx.Response:
    resp = await client.post(
        OPENAI_API,
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": text[:MAX_CONTENT_CHARS]},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 150,
        },
        timeout=_TIMEOUT)
    if resp.status_code >= 500 or resp.status_code == 429:
        resp.raise_for_status()
    return resp


async def detect_decision(text: str) -> DetectionResult | None:
    """Detect whether `text` contains a decision; None on API failure (never raises)."""
    if not config.OPENAI_API_KEY or not (text or "").strip():
        return None
    try:
        async with httpx.AsyncClient() as client:
            resp = await _call_openai(client, text)
        if resp.status_code != 200:
            log.warning("OpenAI detection returned %d", resp.status_code)
            return None
        content = resp.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
        log.warning("OpenAI detection failed: %r", e)
        return None
    return _parse_detection(content)


def create_pending_decision(session: Session, guild_id: int, channel_id: int,
                            message_id: int, user_id: int, content: str,
                            summary: str) -> Decision:
    """Store a pending decision (excerpt + summary capped)."""
    return add_decision(session, guild_id, channel_id, message_id, user_id,
                        content[:MAX_CONTENT_CHARS], summary[:MAX_SUMMARY_CHARS])


def confirm_decision(session: Session, guild_id: int, decision_id: int,
                     confirmed_by: int) -> Decision | None:
    """Mark a pending decision confirmed. Returns None if unknown or already resolved."""
    d = set_decision_status(session, guild_id, decision_id, "confirmed", confirmed_by)
    return d


def dismiss_decision(session: Session, guild_id: int, decision_id: int) -> Decision | None:
    """Mark a pending decision rejected (dismissed)."""
    return set_decision_status(session, guild_id, decision_id, "rejected")


def pending_decisions(session: Session, guild_id: int) -> list[Decision]:
    from src.db.repositories import list_decisions
    return list_decisions(session, guild_id, "pending")


def unsynced_confirmed(session: Session, guild_id: int) -> list[Decision]:
    return get_confirmed_unsynced_decisions(session, guild_id)
