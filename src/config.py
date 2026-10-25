import os

DISCORD_TOKEN: str = os.environ.get("DISCORD_TOKEN", "")
DATABASE_URL: str = os.environ.get("DATABASE_URL", "sqlite:///./hightide.db")
DEFAULT_TIMEZONE: str = os.environ.get("DEFAULT_TIMEZONE", "Australia/Perth")
_guild = os.environ.get("GUILD_ID")
GUILD_ID: int | None = int(_guild) if _guild else None

# Decision-tracking bot (second Discord application; optional — omit to disable it)
DECISION_BOT_TOKEN: str = os.environ.get("DECISION_BOT_TOKEN", "")
OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
TRELLO_API_KEY: str = os.environ.get("TRELLO_API_KEY", "")
TRELLO_API_TOKEN: str = os.environ.get("TRELLO_API_TOKEN", "")
