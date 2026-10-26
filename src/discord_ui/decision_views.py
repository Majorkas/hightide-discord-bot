"""Persistent Confirm/Dismiss buttons for decision candidates."""
import logging
import re

import discord

log = logging.getLogger("hightide.decision_views")

_MD_EXTRA = re.compile(r"([\[\]()<>])")


def _esc(s: str) -> str:
    s = discord.utils.escape_markdown(s)
    s = _MD_EXTRA.sub(r"\\\1", s)
    return discord.utils.escape_mentions(s)


def build_candidate_embed(summary: str, decider_id: int, excerpt: str) -> discord.Embed:
    """The confirmation card the bot posts for an AI-detected decision.

    The excerpt (original message text) is only shown when it adds context beyond the
    summary — meeting-sourced decisions store content == summary, so we'd duplicate.
    """
    if excerpt.strip() and excerpt.strip() != summary.strip():
        desc = f"**{_esc(summary)}**\n\n{_esc(excerpt)}\n\n— <@{decider_id}>"
    else:
        desc = f"**{_esc(summary)}**\n\n— <@{decider_id}>"
    embed = discord.Embed(title="Decision detected — confirm?", description=desc,
                          color=0xF0A832)
    return embed


class DecisionAction(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"decision:(?P<action>confirm|dismiss):(?P<decision_id>[0-9]{1,18})"):
    """Confirm/Dismiss a pending decision; the decision id lives in the custom_id."""

    def __init__(self, action: str, decision_id: int):
        self.action = action
        self.decision_id = decision_id
        label, style, emoji = (
            ("Confirm", discord.ButtonStyle.success, "✅") if action == "confirm"
            else ("Dismiss", discord.ButtonStyle.secondary, "✖️"))
        super().__init__(discord.ui.Button(
            label=label, emoji=emoji, style=style,
            custom_id=f"decision:{action}:{decision_id}"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item,
                             match) -> "DecisionAction":
        return cls(match["action"], int(match["decision_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        from src.services.decision_service import handle_decision_action
        await handle_decision_action(interaction, self.action, self.decision_id)


class DecisionView(discord.ui.View):
    def __init__(self, decision_id: int):
        super().__init__(timeout=None)
        self.add_item(DecisionAction("confirm", decision_id))
        self.add_item(DecisionAction("dismiss", decision_id))
