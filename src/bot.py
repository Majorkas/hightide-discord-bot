import logging

import discord
from discord.ext import commands

from src import config
from src.db.models import Base
from src.db.session import get_engine

log = logging.getLogger("hightide")


def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)

    # Add AdminCog immediately (sync, for tests and CLI)
    from src.discord_ui.commands_admin import AdminCog
    admin_cog = AdminCog(bot)
    # Manually register cog in internal structure
    bot._connection._get_websocket = lambda: None  # Placeholder for cog context
    # Add groups to tree
    bot.tree.add_command(admin_cog.config_group)
    bot.tree.add_command(admin_cog.source_group)
    bot.tree.add_command(admin_cog.board_group)

    @bot.event
    async def setup_hook() -> None:
        engine = get_engine(config.DATABASE_URL)
        Base.metadata.create_all(engine)
        from src.discord_ui.commands_user import UserCog
        await bot.add_cog(UserCog(bot))
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
    bot = create_bot()
    bot.run(config.DISCORD_TOKEN)


if __name__ == "__main__":
    main()
