import discord

from src.db.models import InterestRole

MAX_PANEL_ROLES = 25  # Discord allows 5 rows x 5 buttons per view


def build_roles_panel_embed(roles: list[InterestRole]) -> discord.Embed:
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
