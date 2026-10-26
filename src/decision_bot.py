"""The decision-tracking Discord bot: a second client that listens for decisions.

Runs in the same process as the events bot (see src/__main__.py). Needs the
Message Content intent enabled on its own Discord application to read message text.
"""
import logging

import discord

from src import config

log = logging.getLogger("hightide.decision_bot")

# Lower than you'd think: short agreements ("yes $90 is good", "deal") are exactly where
# decisions settle, and they must reach the AI. Only truly content-free chatter is skipped.
MIN_MESSAGE_CHARS = 8
# Per-channel detection throttle: a spammer can't drive unbounded OpenAI spend.
_MAX_DETECTIONS_PER_MINUTE = 6
_channel_windows: dict[int, list[float]] = {}


def _throttled(channel_id: int) -> bool:
    """True when the channel has hit the per-minute detection cap."""
    import time
    now = time.monotonic()
    window = [t for t in _channel_windows.get(channel_id, []) if now - t < 60]
    if len(window) >= _MAX_DETECTIONS_PER_MINUTE:
        _channel_windows[channel_id] = window
        return True
    window.append(now)
    _channel_windows[channel_id] = window
    return False


class DecisionBot(discord.Client):
    """The decision bot client; registers its own dynamic items + tables in setup_hook."""

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

    async def setup_hook(self) -> None:
        # This client authors the Confirm/Dismiss cards, so it must register the
        # DecisionAction dynamic item itself (component interactions route to the
        # authoring client). Also create the decision tables here so the listener
        # works even if it starts before the events bot finishes its own setup.
        from src.db.session import ensure_decision_tables, ensure_meeting_tables, get_engine
        engine = get_engine(config.DATABASE_URL)
        ensure_decision_tables(engine)
        ensure_meeting_tables(engine)
        from src.discord_ui.decision_views import DecisionAction
        self.add_dynamic_items(DecisionAction)

    async def on_ready(self) -> None:
        log.info("decision bot online as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        # Route by channel: a recording in the meetings channel goes through the
        # meeting pipeline; anything else (including audio attachments in other
        # channels) falls through to normal decision detection so it isn't lost.
        from src.db.repositories import get_or_create_decision_config
        from src.services import decision_service
        if _is_meeting_upload(message):
            with decision_service._session() as session:
                cfg = get_or_create_decision_config(session, message.guild.id)
                meetings_ch = cfg.meetings_channel_id if cfg.enabled else None
            if meetings_ch == message.channel.id:
                await _process_meeting(self, message)
                return
        await _maybe_detect(self, message)


def create_decision_bot() -> discord.Client:
    """The decision bot client with message-content intent and an on_message listener."""
    return DecisionBot()


CONTEXT_MESSAGES = 10  # how far back to read for back-and-forth context


async def _recent_context(message: discord.Message, bot_user_id: int | None = None) -> list[str]:
    """Recent channel history as 'Name: text' lines, oldest first, ending with this message.

    The current message is always the LAST line (the one being judged). Context stops at the
    bot's own last confirmation card (a new topic shouldn't inherit a settled decision) and
    excludes other bot messages. Falls back to just this message when history can't be read.
    """
    def _line(m) -> str:
        name = getattr(m.author, "display_name", None) or "someone"
        return f"{name}: {(m.content or '').strip()}"

    current = _line(message)
    try:
        history = []
        async for m in message.channel.history(limit=CONTEXT_MESSAGES - 1,
                                               before=message, oldest_first=False):
            # The bot's own confirmation card ends the previous decision's thread:
            # a new topic must not inherit that settled decision as context.
            if m.author.bot and bot_user_id is not None and m.author.id == bot_user_id:
                break
            if m.author.bot or not (m.content or "").strip():
                continue
            history.append(_line(m))
        history.reverse()  # oldest first
        return [*history, current]
    except (discord.HTTPException, AttributeError):
        return [current]


async def _maybe_detect(client: discord.Client, message: discord.Message) -> None:
    """Detect a decision in a monitored channel and post a confirmation card."""
    # Never react to bots (incl. ourselves), DMs, or empty/short text.
    if message.author.bot or message.guild is None:
        return
    content = (message.content or "").strip()
    if len(content) < MIN_MESSAGE_CHARS:
        return
    if _throttled(message.channel.id):
        log.info("decision detection throttled in channel %s", message.channel.id)
        return

    from src.db.repositories import (
        get_or_create_decision_config, has_pending_decision_in_channel,
        list_decision_channels,
    )
    from src.services import decision_service

    with decision_service._session() as session:
        cfg = get_or_create_decision_config(session, message.guild.id)
        if not cfg.enabled:
            return
        if message.channel.id not in list_decision_channels(session, message.guild.id):
            return
        # One pending card per channel: a back-and-forth resolving over several messages
        # shouldn't stack duplicate confirmation cards for the same decision.
        if has_pending_decision_in_channel(session, message.guild.id, message.channel.id):
            return

    # Pull recent history so a back-and-forth agreement ("ok, 175 it is") is judged
    # with the proposals it resolves, not in isolation. Context stops at the bot's last
    # confirmation card so a new topic starts fresh.
    bot_user_id = client.user.id if getattr(client, "user", None) else None
    conversation = await _recent_context(message, bot_user_id)
    result = await decision_service.detect_decision(conversation)
    if result is None or not result.is_decision:
        return

    with decision_service._session() as session:
        decision = decision_service.create_pending_decision(
            session, message.guild.id, message.channel.id, message.id,
            message.author.id, content, result.summary or content[:200])

    from src.discord_ui.decision_views import DecisionView, build_candidate_embed
    embed = build_candidate_embed(decision.summary, message.author.id, decision.content)
    try:
        await message.channel.send(embed=embed, view=DecisionView(decision.id),
                                   allowed_mentions=discord.AllowedMentions.none(),
                                   reference=message)
    except discord.HTTPException:
        log.exception("could not post decision confirmation for decision %s", decision.id)


def _is_meeting_upload(message: discord.Message) -> bool:
    """True when the message carries an audio attachment in a guild (meetings channel checked
    by the caller against config)."""
    if message.author.bot or message.guild is None or not message.attachments:
        return False
    from src.services.meeting_service import is_audio_attachment
    return any(
        is_audio_attachment(a.filename, getattr(a, "content_type", None))
        for a in message.attachments
    )


async def _process_meeting(client: discord.Client, message: discord.Message) -> None:
    """Transcribe an uploaded meeting recording, post notes, and raise decision cards."""
    from src.db.repositories import add_meeting
    from src.discord_ui.decision_views import DecisionView, build_candidate_embed
    from src.discord_ui.decision_views import _esc
    from src.services import decision_service, meeting_service

    # The meetings channel is checked by the caller (on_message); only staff may
    # trigger a paid Whisper/GPT run, not anyone who can post an attachment here.
    author = message.author
    if not getattr(getattr(author, "guild_permissions", None), "manage_guild", False):
        await message.channel.send(
            "⚠️ Only staff can have a recording transcribed here.",
            reference=message, allowed_mentions=discord.AllowedMentions.none())
        return

    attachment = next(
        (a for a in message.attachments
         if meeting_service.is_audio_attachment(a.filename, getattr(a, "content_type", None))),
        None)
    if attachment is None:
        return
    # Pre-download size guard (attachment.size is Discord-reported bytes).
    if getattr(attachment, "size", 0) > meeting_service._MAX_DOWNLOAD_BYTES:
        await message.channel.send(
            "⚠️ That file is far too large to process — trim it to a shorter clip.",
            reference=message, allowed_mentions=discord.AllowedMentions.none())
        return

    async def _reply(text: str) -> None:
        await message.channel.send(text, reference=message,
                                   allowed_mentions=discord.AllowedMentions.none())

    try:
        data = await meeting_service.download_attachment(attachment.url)
        transcript = await meeting_service.transcribe_audio(data, attachment.filename)
        notes = await meeting_service.extract_notes(transcript.text)
    except meeting_service.MeetingError as e:
        await _reply(f"⚠️ Couldn't process that recording: {_esc(str(e))}")
        return
    except Exception:
        log.exception("meeting processing failed for %s", attachment.filename)
        await _reply("⚠️ Something went wrong processing that recording — try again.")
        return

    notes_embed = discord.Embed(
        title=f"Meeting notes — {_esc(attachment.filename[:80])}",
        description=_esc(notes.notes)[:4000] or "(no key points extracted)",
        color=0x1F8B4C)

    # Two-phase: write the meeting row + all decision rows first (single session,
    # closed before any Discord send), then post the notes card and decision cards.
    # This avoids partial DB state if a send fails mid-loop.
    decisions_to_post: list = []
    with decision_service._session() as session:
        add_meeting(session, message.guild.id, message.channel.id, attachment.filename,
                    notes.notes[:1500], transcript.text[:500])
        for summary in notes.decisions:
            d = decision_service.create_pending_decision(
                session, message.guild.id, message.channel.id, message.id,
                message.author.id, summary, summary)
            decisions_to_post.append(d)

    try:
        await message.channel.send(embed=notes_embed,
                                   allowed_mentions=discord.AllowedMentions.none(),
                                   reference=message)
    except discord.HTTPException:
        log.exception("could not post meeting notes for %s", attachment.filename)

    for d in decisions_to_post:
        try:
            await message.channel.send(
                embed=build_candidate_embed(d.summary, message.author.id, d.content),
                view=DecisionView(d.id),
                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("could not post meeting decision card")


async def run_decision_bot() -> None:
    """Run the decision bot (called by the launcher)."""
    client = create_decision_bot()
    await client.start(config.DECISION_BOT_TOKEN)
