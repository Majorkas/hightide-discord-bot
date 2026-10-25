"""The decision-tracking Discord bot: a second client that listens for decisions.

Runs in the same process as the events bot (see src/__main__.py). Needs the
Message Content intent enabled on its own Discord application to read message text.
"""
import logging

import discord

from src import config

log = logging.getLogger("hightide.decision_bot")

MIN_MESSAGE_CHARS = 25  # ignore very short messages ("ok", "lol") — no decision there
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
        from src.db.session import ensure_decision_tables, get_engine
        ensure_decision_tables(get_engine(config.DATABASE_URL))
        from src.discord_ui.decision_views import DecisionAction
        self.add_dynamic_items(DecisionAction)

    async def on_ready(self) -> None:
        log.info("decision bot online as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        await _maybe_detect(self, message)


def create_decision_bot() -> discord.Client:
    """The decision bot client with message-content intent and an on_message listener."""
    return DecisionBot()


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

    from src.db.repositories import get_or_create_decision_config, list_decision_channels
    from src.services import decision_service

    with decision_service._session() as session:
        cfg = get_or_create_decision_config(session, message.guild.id)
        if not cfg.enabled:
            return
        if message.channel.id not in list_decision_channels(session, message.guild.id):
            return

        result = await decision_service.detect_decision(content)
        if result is None or not result.is_decision:
            return

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


async def run_decision_bot() -> None:
    """Run the decision bot (called by the launcher)."""
    client = create_decision_bot()
    await client.start(config.DECISION_BOT_TOKEN)
