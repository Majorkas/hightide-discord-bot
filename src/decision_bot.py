"""The decision-tracking Discord bot: a second client that listens for decisions.

Runs in the same process as the events bot (see src/__main__.py). Needs the
Message Content intent enabled on its own Discord application to read message text.
"""
import logging

import discord

from src import config

log = logging.getLogger("hightide.decision_bot")

MIN_MESSAGE_CHARS = 25  # ignore very short messages ("ok", "lol") — no decision there


def create_decision_bot() -> discord.Client:
    """The decision bot client with message-content intent and an on_message listener."""
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        log.info("decision bot online as %s", client.user)

    @client.event
    async def on_message(message: discord.Message) -> None:
        await _maybe_detect(client, message)

    return client


async def _maybe_detect(client: discord.Client, message: discord.Message) -> None:
    """Detect a decision in a monitored channel and post a confirmation card."""
    # Never react to bots (incl. ourselves), DMs, or empty/short text.
    if message.author.bot or message.guild is None:
        return
    content = (message.content or "").strip()
    if len(content) < MIN_MESSAGE_CHARS:
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
