from dotenv import load_dotenv

load_dotenv()  # must run before src.config reads the environment

import logging  # noqa: E402

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

from src import config  # noqa: E402
from src.db.models import Base  # noqa: E402
from src.db.session import get_engine  # noqa: E402

log = logging.getLogger("hightide")


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

    @bot.event
    async def setup_hook() -> None:
        engine = get_engine(config.DATABASE_URL)
        Base.metadata.create_all(engine)
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
