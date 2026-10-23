import os

DISCORD_TOKEN: str = os.environ.get("DISCORD_TOKEN", "")
DATABASE_URL: str = os.environ.get("DATABASE_URL", "sqlite:///./hightide.db")
DEFAULT_TIMEZONE: str = os.environ.get("DEFAULT_TIMEZONE", "Australia/Perth")
_guild = os.environ.get("GUILD_ID")
GUILD_ID: int | None = int(_guild) if _guild else None
