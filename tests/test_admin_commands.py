from src.bot import create_bot


def test_bot_has_admin_commands():
    bot = create_bot()
    names = {c.name for c in bot.tree.get_commands()}
    assert "config" in names
    assert "source" in names
    assert "board" in names
