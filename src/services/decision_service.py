"""AI decision detection and the pending -> confirmed/rejected lifecycle."""
import json
import logging
from dataclasses import dataclass

import httpx
import discord
from sqlalchemy.orm import Session
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src import config
from src.db.models import Decision
from src.db.repositories import (
    add_decision, get_confirmed_unsynced_decisions, set_decision_status,
)
from src.services import trello_service

log = logging.getLogger("hightide.decisions")

# Session factory seam (tests monkeypatch this).
_session_factory = None


def _session():
    global _session_factory
    if _session_factory is None:
        from src.db.session import get_session_factory
        _session_factory = get_session_factory(config.DATABASE_URL)
    return _session_factory()


OPENAI_API = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o-mini"
MAX_CONTENT_CHARS = 500   # stored excerpt cap
MAX_SUMMARY_CHARS = 200   # AI one-liner cap
_TIMEOUT = 8.0

_SYSTEM = (
    "You detect decisions in a small hobby-store staff Discord. You are given the most recent "
    "messages in a channel, oldest first, as 'Name: text' lines. The LAST line is the message "
    "being judged; earlier lines are context. A decision is a concrete choice the team has "
    "settled on — including a resolution reached after back-and-forth (e.g. one person says 150, "
    "another 200, then they agree on 175 — the LAST line confirming it is the decision). "
    "Casual chat, questions, and proposals with no agreement are NOT decisions. "
    "The summary MUST name both the subject (what was decided about — resolve pronouns like "
    "'them'/'it' to the thing named earlier, e.g. 'the new deckboxes') AND the settled "
    "outcome/value. Never write a summary that is just a bare value like 'Price set at 30' — "
    "always include the subject: 'Price set at 30 for the new deckboxes'. "
    "Reply with strict JSON only: "
    '{"is_decision": true|false, "summary": "<=200 char one-line summary naming the subject and '
    'the outcome", "decider_hint": "who settled it, if clear, else empty"}.'
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


async def detect_decision(conversation: list[str] | str) -> DetectionResult | None:
    """Detect whether the latest line of `conversation` settles a decision.

    `conversation` is a list of 'Name: text' lines (oldest first; the LAST line is the
    message being judged) or a single bare string. None on API failure (never raises).
    """
    if isinstance(conversation, str):
        conversation = [conversation]
    lines = [c for c in conversation if (c or "").strip()]
    if not config.OPENAI_API_KEY or not lines:
        return None
    text = "\n".join(lines)
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


def message_link(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


async def sync_decision_to_trello(session: Session, decision: Decision, *,
                                  decider_name: str, channel_name: str) -> bool:
    """Create the Trello card for a confirmed decision and record the sync.

    Returns True on success; False (logged, no exception) when Trello is unconfigured
    or the create fails. Idempotent: an existing sync row short-circuits.
    """
    from src.db.repositories import (
        get_or_create_decision_config, get_trello_member, get_trello_sync,
        record_trello_sync,
    )
    from zoneinfo import ZoneInfo

    if get_trello_sync(session, decision.id) is not None:
        return True  # already synced
    cfg = get_or_create_decision_config(session, decision.guild_id)
    if not (cfg.enabled and cfg.trello_list_id):
        log.info("decision %s not synced: trello not configured/enabled", decision.id)
        return False
    when = decision.created_at.astimezone(ZoneInfo(config.DEFAULT_TIMEZONE)).strftime("%b %d %Y, %I:%M %p")
    desc = trello_service.build_card_description(
        decider_name=decider_name, when=when, channel_name=channel_name,
        message_url=message_link(decision.guild_id, decision.channel_id, decision.message_id),
        excerpt=decision.content)
    member = get_trello_member(session, decision.guild_id, decision.user_id)
    card = await trello_service.create_card(cfg.trello_list_id, decision.summary, desc,
                                            member_id=member)
    if card is None:
        return False
    record_trello_sync(session, decision.id, card.card_id, card.url)
    return True


async def handle_decision_action(interaction, action: str, decision_id: int) -> None:
    """Confirm/Dismiss callback: staff-only; resolves the decision, syncs to Trello on
    confirm, and edits the card to reflect the outcome."""
    # Staff-only: anyone who can see a monitored channel could otherwise push message
    # content to Trello or dismiss decisions.
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "Only staff can confirm or dismiss decisions.", ephemeral=True)
        return

    await interaction.response.defer()
    with _session() as session:
        if action == "confirm":
            d = confirm_decision(session, interaction.guild_id, decision_id,
                                 confirmed_by=interaction.user.id)
            if d is None:
                await interaction.followup.send(
                    "That decision is no longer pending.", ephemeral=True)
                return
            member = interaction.guild.get_member(d.user_id) if interaction.guild else None
            decider_name = (member.display_name if member else f"User {d.user_id}")
            channel = interaction.guild.get_channel(d.channel_id) if interaction.guild else None
            channel_name = f"#{channel.name}" if channel else "a staff channel"
            synced = await sync_decision_to_trello(
                session, d, decider_name=decider_name, channel_name=channel_name)
            status_note = " and synced to Trello" if synced else (
                " — Trello sync failed or isn't configured (an admin can retry with /decision sync)")
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            new_embed = None
            if embed is not None:
                new_embed = discord.Embed(title="Decision confirmed",
                                          description=embed.description, color=0x1F8B4C)
            try:
                await interaction.edit_original_response(embed=new_embed, view=None)
            except discord.HTTPException:
                log.info("could not edit confirmed decision card %s", decision_id)
            await interaction.followup.send(
                f"Decision confirmed{status_note}.", ephemeral=True)
        else:  # dismiss
            d = dismiss_decision(session, interaction.guild_id, decision_id)
            if d is None:
                await interaction.followup.send(
                    "That decision is no longer pending.", ephemeral=True)
                return
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            new_embed = None
            if embed is not None:
                new_embed = discord.Embed(title="Decision dismissed",
                                          description=embed.description, color=0x95A5A6)
            try:
                await interaction.edit_original_response(embed=new_embed, view=None)
            except discord.HTTPException:
                log.info("could not edit dismissed decision card %s", decision_id)
            await interaction.followup.send("Dismissed.", ephemeral=True)
