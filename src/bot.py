from dotenv import load_dotenv

load_dotenv()  # must run before src.config reads the environment

import logging  # noqa: E402

import discord  # noqa: E402
from discord import app_commands  # noqa: E402
from discord.ext import commands  # noqa: E402

from sqlalchemy import select  # noqa: E402

from src import config  # noqa: E402
from src.db.models import Base  # noqa: E402
from src.db.session import (  # noqa: E402
    ensure_board_post_message_ids, ensure_card_wants_printing_columns,
    ensure_guild_config_v2_columns,
    ensure_guild_config_weekly_image_url, ensure_guild_config_weekly_seeded,
    get_engine, get_session_factory,
)

log = logging.getLogger("hightide")


def register_persistent_views(bot: commands.Bot, session_factory) -> None:
    """Re-attach the roles panel view for every guild that has a posted panel."""
    from src.db.models import GuildConfig
    from src.db.repositories import list_interest_roles
    from src.discord_ui.role_views import RolesPanelView
    with session_factory() as session:
        rows = session.execute(
            select(GuildConfig).where(GuildConfig.roles_message_id.is_not(None))).scalars().all()
        for cfg in rows:
            bot.add_view(RolesPanelView(list_interest_roles(session, cfg.guild_id)),
                         message_id=cfg.roles_message_id)


async def _reply_cooldown(interaction: discord.Interaction) -> None:
    msg = "Slow down — try again in a moment."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)

    # Add AdminCog immediately (sync, for tests and CLI)
    from src.discord_ui.commands_admin import AdminCog
    admin_cog = AdminCog(bot)
    # Register groups directly on tree at creation so command surface is inspectable
    # without a Discord connection; AdminCog is intentionally NOT re-added in setup_hook
    bot.tree.add_command(admin_cog.config_group)
    bot.tree.add_command(admin_cog.source_group)
    bot.tree.add_command(admin_cog.board_group)

    # Same as AdminCog: group registered directly on the tree, so UserCog is NOT add_cog'd
    # in setup_hook (that would double-register the command).
    from src.discord_ui.commands_user import UserCog
    user_cog = UserCog(bot)
    bot.tree.add_command(user_cog.events_group)

    # Same pattern: RolesCog group lives on the tree, so it is NOT add_cog'd (would double-register).
    from src.discord_ui.commands_roles import RolesCog
    roles_cog = RolesCog(bot)
    bot.tree.add_command(roles_cog.roles_group)
    bot.tree.add_command(roles_cog.card_lookup)  # Top-level /card command
    bot.tree.add_command(roles_cog.deck_lookup)  # Top-level /deck command
    bot.tree.add_command(roles_cog.want_group)  # Public /want group

    default_on_error = bot.tree.on_error

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction,
                                   error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await _reply_cooldown(interaction)
            return
        await default_on_error(interaction, error)

    @bot.event
    async def setup_hook() -> None:
        engine = get_engine(config.DATABASE_URL)
        Base.metadata.create_all(engine)
        ensure_board_post_message_ids(engine)
        ensure_guild_config_weekly_image_url(engine)
        ensure_guild_config_weekly_seeded(engine)
        ensure_guild_config_v2_columns(engine)
        ensure_card_wants_printing_columns(engine)
        register_persistent_views(bot, get_session_factory(config.DATABASE_URL))
        from src.discord_ui.role_views import SignupButton
        bot.add_dynamic_items(SignupButton)  # custom_id carries the event id
        from src.services.scheduler import RefreshScheduler
        RefreshScheduler(bot).start()
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
        log.info("commands synced")

    return bot


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set — copy .env.example to .env and fill it in")
    bot = create_bot()
    bot.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()
