import discord

from src.db.models import InterestRole

MAX_PANEL_ROLES = 25  # Discord allows 5 rows x 5 buttons per view


def build_roles_panel_embed(roles: list[InterestRole]) -> discord.Embed:
    roles = [r for r in roles if r.enabled is not False]  # unflushed rows have enabled=None
    lines = [f"{(r.emoji + ' ') if r.emoji else ''}**{r.label}** — <@&{r.role_id}>"
             for r in roles]
    return discord.Embed(
        title="Get event pings",
        description=("Click a button to add or remove that game's role. "
                     "You'll be pinged when a new event in that game appears.\n\n"
                     + ("\n".join(lines) if lines else "No roles configured yet.")),
        color=0x1F8B4C)


class RolesPanelView(discord.ui.View):
    def __init__(self, roles: list[InterestRole]):
        super().__init__(timeout=None)
        for r in roles:
            if r.enabled is False:
                continue
            btn = discord.ui.Button(
                label=r.label, emoji=r.emoji or None,
                style=discord.ButtonStyle.secondary,
                custom_id=f"role_toggle:{r.id}")
            btn.callback = self._make_callback(r.id)
            self.add_item(btn)

    def _make_callback(self, role_row_id: int):
        async def _cb(interaction: discord.Interaction):
            from src.services.role_service import handle_role_toggle
            await handle_role_toggle(interaction, role_row_id)
        return _cb


class SignupButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"signup:(?P<event_id>[0-9]+)"):
    """Going button; the event id lives in the custom_id so it survives restarts."""

    def __init__(self, event_id: int, count: int = 0):
        self.event_id = event_id
        super().__init__(discord.ui.Button(
            label=f"✅ Going ({count})", style=discord.ButtonStyle.success,
            custom_id=f"signup:{event_id}"))

    @property
    def label(self) -> str | None:
        return self.item.label

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item,
                             match) -> "SignupButton":
        return cls(int(match["event_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        from src.services.signup_service import handle_signup_toggle
        await handle_signup_toggle(interaction, self.event_id)


class SignupView(discord.ui.View):
    def __init__(self, event_id: int, count: int = 0):
        super().__init__(timeout=None)
        self.add_item(SignupButton(event_id, count))
