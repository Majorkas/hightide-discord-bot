"""Tests for Scryfall /card lookup."""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord import app_commands
import httpx

from src.services.deck_service import (
    AmbiguousCardError, _get_card_image_url, _trim_description_at_boundary, build_card_embed,
    scryfall_card, scryfall_card_by_id, scryfall_card_exact,
)


FIXTURES = Path(__file__).parent / "fixtures"
SFID = "d0d33d52-3d28-4635-b985-51e126289259"
HANDLER_LOGGER = "hightide.discord_ui.commands_roles"


def _resp(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    if status >= 400 and status != 404:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            str(status), request=MagicMock(), response=r)
    return r


def _client(*responses, side_effect=None):
    """Patch AsyncClient so .get returns the responses in order (or runs side_effect)."""
    patcher = patch("src.services.deck_service.httpx.AsyncClient")
    cls = patcher.start()
    client = AsyncMock()
    client.get.side_effect = side_effect if side_effect is not None else list(responses)
    cls.return_value.__aenter__.return_value = client
    return patcher, client


@pytest.fixture
def no_sleep():
    with patch("asyncio.sleep", new_callable=AsyncMock) as s:
        yield s


def _interaction(user_id=1):
    i = AsyncMock(spec=discord.Interaction)
    i.response = AsyncMock()
    i.user = MagicMock(id=user_id)
    return i


@pytest.fixture
def scryfall_card_fixture():
    """Real Lightning Bolt card JSON."""
    with open(FIXTURES / "scryfall_card.json") as f:
        return json.load(f)


@pytest.fixture
def scryfall_dfc_fixture():
    """Real Delver of Secrets (double-faced) card JSON."""
    with open(FIXTURES / "scryfall_dfc.json") as f:
        return json.load(f)


class TestBuildCardEmbed:
    """Test embed building from Scryfall card data."""

    def test_basic_embed_structure(self, scryfall_card_fixture):
        """Embed has title, URL, image, and description."""
        card = scryfall_card_fixture
        embed = build_card_embed(card)

        assert isinstance(embed, discord.Embed)
        assert embed.title == "Lightning Bolt"
        assert embed.url == "https://scryfall.com/card/msc/806/lightning-bolt?utm_source=api"
        assert embed.image.url == card["image_uris"]["normal"]
        assert embed.description == (
            "**{R}**\nInstant\nLightning Bolt deals 3 damage to any target.\n**USD $0.85**")

    def test_embed_includes_cost_type_oracle(self, scryfall_card_fixture):
        """Description includes mana cost, type line, and oracle text."""
        card = scryfall_card_fixture
        embed = build_card_embed(card)
        desc = embed.description

        assert "{R}" in desc  # mana_cost
        assert "Instant" in desc  # type_line
        assert "Lightning Bolt deals 3 damage" in desc  # oracle_text

    def test_embed_includes_price_when_present(self, scryfall_card_fixture):
        """Description includes USD price when available."""
        card = scryfall_card_fixture
        embed = build_card_embed(card)
        desc = embed.description

        assert "**USD $0.85**" in desc

    def test_embed_without_price(self, scryfall_card_fixture):
        """Embed renders without price when prices.usd is None."""
        card = scryfall_card_fixture.copy()
        card["prices"] = {"usd": None}

        embed = build_card_embed(card)

        assert embed.description == "**{R}**\nInstant\nLightning Bolt deals 3 damage to any target."

    def test_image_from_image_uris_normal(self, scryfall_card_fixture):
        """Single-faced card uses image_uris.normal."""
        card = scryfall_card_fixture
        embed = build_card_embed(card)

        assert embed.image.url == "https://cards.scryfall.io/normal/front/7/6/7673784e-db4b-43a1-8d55-1bb9fc1e284f.jpg?1783903008"

    def test_dfc_uses_front_face_image(self, scryfall_dfc_fixture):
        """Double-faced card uses card_faces[0].image_uris.normal (front face)."""
        card = scryfall_dfc_fixture
        embed = build_card_embed(card)

        # Should use front face's image
        front_face = card["card_faces"][0]
        assert embed.image.url == front_face["image_uris"]["normal"]

    def test_oracle_text_trimmed_at_400_chars(self, scryfall_card_fixture):
        """Long oracle text is cut back to the last space within 400 chars, plus an ellipsis."""
        card = scryfall_card_fixture.copy()
        card["oracle_text"] = "word " * 200
        card["prices"] = {"usd": None}

        oracle = build_card_embed(card).description.split("\n")[-1]

        assert oracle == "word " * 79 + "word…"
        assert len(oracle) == 400

    def test_markdown_escaping_in_description(self, scryfall_card_fixture):
        """Dangerous markdown/mention characters in scraped text are escaped exactly."""
        card = scryfall_card_fixture.copy()
        card["oracle_text"] = "Deals [damage] to **any** target with *italics* and _underscores_ @everyone <@123>"

        oracle = build_card_embed(card).description.split("\n")[2]

        assert oracle == ("Deals \\[damage\\] to \\*\\*any\\*\\* target with \\*italics\\* and "
                          "\\_underscores\\_ @\u200beveryone \\<@123\\>")

    def test_oracle_lines_escaped_individually_and_newlines_kept(self, scryfall_card_fixture):
        card = scryfall_card_fixture.copy()
        card["oracle_text"] = "{T}: Add *one* mana.\nDraw a card_"
        card["prices"] = {"usd": None}

        desc = build_card_embed(card).description

        assert desc == "**{R}**\nInstant\n{T}: Add \\*one\\* mana.\nDraw a card\\_"

    def test_type_line_and_mana_cost_escaped(self, scryfall_card_fixture):
        card = scryfall_card_fixture.copy()
        card["mana_cost"] = "*{R}*"
        card["type_line"] = "Instant _x_ @everyone"
        card["oracle_text"] = ""
        card["prices"] = {}

        assert build_card_embed(card).description == (
            "**\\*{R}\\***\nInstant \\_x\\_ @\u200beveryone")


class TestScryfallCardFuzzy:
    """Test async scryfall_card (fuzzy lookup)."""

    @pytest.mark.asyncio
    async def test_fuzzy_hit_returns_card_dict(self, scryfall_card_fixture):
        """Fuzzy lookup returns card dict on 200 response."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = scryfall_card_fixture
            mock_client.get.return_value = mock_response

            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await scryfall_card("Lightning Bolt")

            assert result == scryfall_card_fixture

    @pytest.mark.asyncio
    async def test_fuzzy_miss_404_no_suggestion(self):
        """Fuzzy miss 404 returns None (Scryfall 404 has no suggestion field)."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.json.return_value = {"object": "error", "code": "not_found", "status": 404, "details": "Not found"}
            mock_client.get.return_value = mock_response

            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await scryfall_card("Nonexistent Card Name")

            assert result is None

    @pytest.mark.asyncio
    async def test_fuzzy_ambiguous_404_raises(self):
        """Real Scryfall ambiguous shape: 404 not_found with type == 'ambiguous'."""
        p, client = _client(_resp(404, {
            "object": "error", "code": "not_found", "status": 404, "type": "ambiguous",
            "details": "Too many cards match ambiguous name “Lightni”. Add more words to refine your search.",
        }))
        try:
            with pytest.raises(AmbiguousCardError):
                await scryfall_card("Lightni")
        finally:
            p.stop()
        assert client.get.call_count == 1

    @pytest.mark.asyncio
    async def test_fuzzy_404_non_json_body_is_plain_miss(self):
        r = _resp(404)
        r.json.side_effect = ValueError("not json")
        p, _ = _client(r)
        try:
            assert await scryfall_card("Nope") is None
        finally:
            p.stop()

    @pytest.mark.asyncio
    async def test_network_error_propagates(self):
        """Network/transport error propagates (with retries=True)."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.TransportError("Network error")
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with pytest.raises(httpx.TransportError):
                await scryfall_card("Lightning Bolt", retries=True)

    @pytest.mark.asyncio
    async def test_5xx_error_propagates(self):
        """5xx errors propagate after retries (with retries=True)."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 503
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "503", request=MagicMock(), response=mock_response
            )
            mock_client.get.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with pytest.raises(httpx.HTTPStatusError):
                await scryfall_card("Lightning Bolt", retries=True)

    @pytest.mark.asyncio
    async def test_ua_header(self):
        """Includes correct User-Agent header."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"name": "Lightning Bolt"}
            mock_client.get.return_value = mock_response

            mock_client_class.return_value.__aenter__.return_value = mock_client

            await scryfall_card("Lightning Bolt")

            # Check headers in call
            call_args = mock_client.get.call_args
            assert "headers" in call_args[1], "headers kwarg must be present"
            assert "hightide-events-bot/0.2" in str(call_args[1]["headers"]), "UA must be present"


class TestScryfallCardExact:
    """Test async scryfall_card_exact (exact lookup for Task 9)."""

    @pytest.mark.asyncio
    async def test_exact_hit_returns_card_dict(self, scryfall_card_fixture):
        """Exact lookup returns card dict on 200 response."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = scryfall_card_fixture
            mock_client.get.return_value = mock_response

            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await scryfall_card_exact("Lightning Bolt")

            assert result == scryfall_card_fixture

    @pytest.mark.asyncio
    async def test_exact_miss_returns_none(self):
        """Exact lookup returns None on 404 (no suggestion needed)."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.json.return_value = {"details": "Not found"}
            mock_client.get.return_value = mock_response

            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await scryfall_card_exact("Lightning Bolt Extra")

            assert result is None


class TestDoubleFacedRichness:
    """Test double-faced card handling in embed."""

    def test_dfc_uses_front_face_mana_cost(self, scryfall_dfc_fixture):
        """DFC with empty mana_cost uses card_faces[0].mana_cost."""
        card = scryfall_dfc_fixture.copy()
        card["mana_cost"] = ""  # Clear main mana cost
        embed = build_card_embed(card)
        desc = embed.description

        assert card["card_faces"][0]["mana_cost"] == "{U}"
        assert desc.split("\n")[0] == "**{U}**"

    def test_dfc_uses_front_face_oracle_text(self, scryfall_dfc_fixture):
        """DFC with empty oracle_text uses card_faces[0].oracle_text."""
        card = scryfall_dfc_fixture.copy()
        card["oracle_text"] = ""  # Clear main oracle text
        embed = build_card_embed(card)
        desc = embed.description

        front_oracle = card["card_faces"][0]["oracle_text"]
        assert front_oracle
        assert desc.split("\n")[2] == front_oracle


class TestRetryPolicies:
    """Test retry behavior with different parameters."""

    @pytest.mark.asyncio
    async def test_404_never_retries(self):
        """404 on first call means exactly 1 GET call (no retry)."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.json.return_value = {"details": "Not found"}
            mock_client.get.return_value = mock_response

            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await scryfall_card("Nonexistent", retries=True)

            assert result is None
            # Should only call once (no retry on 404)
            assert mock_client.get.call_count == 1

    @pytest.mark.asyncio
    async def test_5xx_retries_three_times_when_enabled(self):
        """5xx error with retries=True attempts 3 times."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 503
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "503", request=MagicMock(), response=mock_response
            )
            mock_client.get.return_value = mock_response
            mock_client_class.return_value.__aenter__.return_value = mock_client

            with pytest.raises(httpx.HTTPStatusError):
                await scryfall_card("Lightning Bolt", retries=True)

            assert mock_client.get.call_count == 3

    @pytest.mark.asyncio
    async def test_retries_false_single_attempt_on_5xx(self):
        """5xx error with retries=False attempts only once."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 503
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "503", request=MagicMock(), response=mock_response
            )
            mock_client.get.return_value = mock_response

            mock_client_class.return_value.__aenter__.return_value = mock_client

            with pytest.raises(httpx.HTTPStatusError):
                await scryfall_card("Lightning Bolt", retries=False)

            assert mock_client.get.call_count == 1

    @pytest.mark.asyncio
    async def test_default_timeout_15_seconds(self):
        """Default timeout parameter is 15.0 seconds."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"name": "Lightning Bolt"}
            mock_client.get.return_value = mock_response

            mock_client_class.return_value.__aenter__.return_value = mock_client

            await scryfall_card("Lightning Bolt")

            # Check timeout was passed to get()
            call_args = mock_client.get.call_args
            assert "timeout" in call_args[1]
            assert call_args[1]["timeout"] == 15.0

    @pytest.mark.asyncio
    async def test_custom_timeout_respected(self):
        """Custom timeout parameter is respected."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"name": "Lightning Bolt"}
            mock_client.get.return_value = mock_response

            mock_client_class.return_value.__aenter__.return_value = mock_client

            await scryfall_card("Lightning Bolt", timeout=2.5)

            # Check custom timeout was passed
            call_args = mock_client.get.call_args
            assert "timeout" in call_args[1]
            assert call_args[1]["timeout"] == 2.5


class TestEndpointParamValidation:
    """Test that correct endpoint params are used."""

    @pytest.mark.asyncio
    async def test_fuzzy_param_exact_value(self):
        """Fuzzy endpoint uses fuzzy=name (not exact=name)."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"name": "Lightning Bolt"}
            mock_client.get.return_value = mock_response

            mock_client_class.return_value.__aenter__.return_value = mock_client

            await scryfall_card("Lightning Bolt", retries=False)

            # Verify params dict
            call_args = mock_client.get.call_args
            params = call_args[1].get("params", {})
            assert "fuzzy" in params, f"Expected 'fuzzy' param, got: {params}"
            assert params["fuzzy"] == "Lightning Bolt"
            assert "exact" not in params

    @pytest.mark.asyncio
    async def test_exact_param_exact_value(self):
        """Exact endpoint uses exact=name (not fuzzy=name)."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"name": "Lightning Bolt"}
            mock_client.get.return_value = mock_response

            mock_client_class.return_value.__aenter__.return_value = mock_client

            await scryfall_card_exact("Lightning Bolt")

            # Verify params dict
            call_args = mock_client.get.call_args
            params = call_args[1].get("params", {})
            assert "exact" in params, f"Expected 'exact' param, got: {params}"
            assert params["exact"] == "Lightning Bolt"
            assert "fuzzy" not in params


class TestCommandSurface:
    """Test that /card command is registered properly."""

    def test_card_command_in_tree(self):
        """The bot tree includes a /card top-level command."""
        from src.bot import create_bot
        bot = create_bot()
        commands = {c.name: c for c in bot.tree.get_commands()}

        assert "card" in commands, f"Expected 'card' command, got: {list(commands.keys())}"

    def test_card_command_is_public(self):
        """The /card command has no default_permissions (public)."""
        from src.bot import create_bot
        bot = create_bot()
        commands = {c.name: c for c in bot.tree.get_commands()}

        card_cmd = commands.get("card")
        assert card_cmd is not None
        assert card_cmd.default_permissions is None, "card command should be public (no manage_guild required)"

    def test_card_command_guild_only(self):
        """The /card command is guild_only."""
        from src.bot import create_bot
        bot = create_bot()
        commands = {c.name: c for c in bot.tree.get_commands()}

        card_cmd = commands.get("card")
        assert card_cmd is not None
        assert card_cmd.guild_only is True


class TestCardCommandHandler:
    """Test /card command handler behavior."""

    @pytest.mark.asyncio
    async def test_card_success_sends_public_embed(self, scryfall_card_fixture):
        """Success case sends embed non-ephemeral (public)."""
        from src.discord_ui.commands_roles import RolesCog
        
        mock_interaction = AsyncMock(spec=discord.Interaction)
        mock_interaction.response = AsyncMock()
        
        cog = RolesCog(AsyncMock())
        
        with patch("src.discord_ui.commands_roles.scryfall_card") as mock_card:
            mock_card.return_value = scryfall_card_fixture
            
            # Get the callback from the Command object
            callback = cog.card_lookup.callback
            await callback(cog, mock_interaction, "Lightning Bolt")
            
            # Should call scryfall_card with retries=False and timeout=2.5
            mock_card.assert_called_once_with("Lightning Bolt", retries=False, timeout=2.5)
            
            # Should call send_message with embed and ephemeral=False
            mock_interaction.response.send_message.assert_called_once()
            call_kwargs = mock_interaction.response.send_message.call_args[1]
            assert "embed" in call_kwargs
            assert call_kwargs.get("ephemeral") is False

    @pytest.mark.asyncio
    async def test_card_404_sends_ephemeral_error(self):
        """404 (None result) sends ephemeral error message."""
        from src.discord_ui.commands_roles import RolesCog
        
        mock_interaction = AsyncMock(spec=discord.Interaction)
        mock_interaction.response = AsyncMock()
        
        cog = RolesCog(AsyncMock())
        
        with patch("src.discord_ui.commands_roles.scryfall_card") as mock_card:
            mock_card.return_value = None
            
            callback = cog.card_lookup.callback
            await callback(cog, mock_interaction, "Nonexistent Card")
            
            # Should send ephemeral error
            mock_interaction.response.send_message.assert_called_once()
            call_args = mock_interaction.response.send_message.call_args
            assert "No card found" in str(call_args)
            assert call_args[1].get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_card_transport_error_sends_ephemeral_error(self):
        """TransportError sends ephemeral error message."""
        from src.discord_ui.commands_roles import RolesCog
        
        mock_interaction = AsyncMock(spec=discord.Interaction)
        mock_interaction.response = AsyncMock()
        
        cog = RolesCog(AsyncMock())
        
        with patch("src.discord_ui.commands_roles.scryfall_card") as mock_card:
            mock_card.side_effect = httpx.TransportError("Network down")
            
            callback = cog.card_lookup.callback
            await callback(cog, mock_interaction, "Lightning Bolt")
            
            # Should send ephemeral error
            mock_interaction.response.send_message.assert_called_once()
            call_args = mock_interaction.response.send_message.call_args
            assert "having trouble" in str(call_args).lower()
            assert call_args[1].get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_card_http_error_sends_ephemeral_error(self):
        """HTTPStatusError sends ephemeral error message."""
        from src.discord_ui.commands_roles import RolesCog
        
        mock_interaction = AsyncMock(spec=discord.Interaction)
        mock_interaction.response = AsyncMock()
        
        cog = RolesCog(AsyncMock())
        
        with patch("src.discord_ui.commands_roles.scryfall_card") as mock_card:
            mock_response = MagicMock()
            mock_card.side_effect = httpx.HTTPStatusError("500", request=MagicMock(), response=mock_response)
            
            callback = cog.card_lookup.callback
            await callback(cog, mock_interaction, "Lightning Bolt")
            
            # Should send ephemeral error
            mock_interaction.response.send_message.assert_called_once()
            call_args = mock_interaction.response.send_message.call_args
            assert "having trouble" in str(call_args).lower()
            assert call_args[1].get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_card_name_too_long_sends_ephemeral_error(self):
        """Name > 100 chars sends ephemeral error without calling service."""
        from src.discord_ui.commands_roles import RolesCog
        
        mock_interaction = AsyncMock(spec=discord.Interaction)
        mock_interaction.response = AsyncMock()
        
        cog = RolesCog(AsyncMock())
        
        with patch("src.discord_ui.commands_roles.scryfall_card") as mock_card:
            long_name = "A" * 101
            
            callback = cog.card_lookup.callback
            await callback(cog, mock_interaction, long_name)
            
            # Should NOT call scryfall_card
            mock_card.assert_not_called()
            
            # Should send ephemeral error
            mock_interaction.response.send_message.assert_called_once()
            call_args = mock_interaction.response.send_message.call_args
            assert "100" in str(call_args)
            assert call_args[1].get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_card_wait_for_timeout_reply(self, monkeypatch):
        """Test that /card handler wraps scryfall_card in asyncio.wait_for and replies quickly."""
        import asyncio
        from src.discord_ui.commands_roles import RolesCog
        
        # Patch the timeout constant to 0.05s for fast test execution
        monkeypatch.setattr("src.discord_ui.commands_roles._LOOKUP_TIMEOUT", 0.05)
        
        mock_interaction = AsyncMock(spec=discord.Interaction)
        mock_interaction.response = AsyncMock()
        
        cog = RolesCog(AsyncMock())
        
        async def hang(*args, **kwargs):
            await asyncio.sleep(10)  # Simulate hanging lookup
        
        with patch("src.discord_ui.commands_roles.scryfall_card", new_callable=AsyncMock) as f:
            f.side_effect = hang
            start = asyncio.get_event_loop().time()
            callback = cog.card_lookup.callback
            await callback(cog, mock_interaction, "Lightning Bolt")
            elapsed = asyncio.get_event_loop().time() - start
        
        # wait_for should cancel after ~0.05s, reply should be fast
        assert elapsed < 0.5
        args, kw = mock_interaction.response.send_message.call_args
        assert "too long" in args[0] and kw["ephemeral"] is True


class TestOracleTrim:
    def test_at_or_below_cap_untouched(self):
        text = ("ab " * 200)[:400]
        assert _trim_description_at_boundary(text, 400) == text
        assert _trim_description_at_boundary("short", 400) == "short"

    def test_one_over_cap_is_trimmed(self):
        text = "word " * 80 + "x"  # 401 chars
        assert len(text) == 401
        assert _trim_description_at_boundary(text, 400) == "word " * 79 + "word…"

    def test_no_space_text_hard_cut(self):
        assert _trim_description_at_boundary("a" * 500, 400) == "a" * 400 + "…"

    def test_space_exactly_at_cut(self):
        text = "a" * 399 + " " + "b" * 10
        assert _trim_description_at_boundary(text, 400) == "a" * 399 + "…"

    def test_trailing_spaces_stripped_before_ellipsis(self):
        text = "a" * 390 + " " * 20 + "b" * 20
        assert _trim_description_at_boundary(text, 400) == "a" * 390 + "…"

    def test_embed_cap_is_400_not_larger(self, scryfall_card_fixture):
        card = {**scryfall_card_fixture, "oracle_text": "word " * 300, "prices": {}}
        oracle = build_card_embed(card).description.split("\n")[-1]
        assert len(oracle) == 400 and oracle.endswith("word…")

    def test_embed_text_of_exactly_400_not_trimmed(self, scryfall_card_fixture):
        text = ("word " * 80)[:400]
        card = {**scryfall_card_fixture, "oracle_text": text, "prices": {}}
        assert build_card_embed(card).description.split("\n")[-1] == text

    def test_escaping_happens_before_trim_so_cap_applies_to_final_text(self, scryfall_card_fixture):
        card = {**scryfall_card_fixture, "oracle_text": "*" * 300, "prices": {}}
        oracle = build_card_embed(card).description.split("\n")[-1]
        assert oracle == "\\*" * 200 + "\u2026"


class TestCardEmbedEdges:
    @staticmethod
    def _card(base, **kw):
        return {**base, **kw}

    @pytest.mark.parametrize("uris", [
        {"normal": "javascript:alert(1)"},
        {"normal": "ftp://x.example/a.jpg"},
        {"normal": 123},
        {"normal": ""},
        {"small": "https://cards.scryfall.io/small.jpg"},
        {},
        None,
        "https://cards.scryfall.io/normal.jpg",
        ["https://cards.scryfall.io/normal.jpg"],
    ])
    def test_unusable_image_uris_give_no_image(self, scryfall_card_fixture, uris):
        card = self._card(scryfall_card_fixture, image_uris=uris)
        assert _get_card_image_url(card) is None
        embed = build_card_embed(card)
        assert embed.image.url is None

    def test_missing_image_uris_key(self, scryfall_card_fixture):
        card = dict(scryfall_card_fixture)
        del card["image_uris"]
        assert _get_card_image_url(card) is None

    def test_unsafe_top_level_image_does_not_fall_through_to_faces(self):
        card = {"image_uris": {"normal": "javascript:x"},
                "card_faces": [{"image_uris": {"normal": "https://cards.scryfall.io/f.jpg"}}]}
        assert _get_card_image_url(card) is None

    def test_top_level_without_normal_falls_back_to_front_face(self):
        card = {"image_uris": {"small": "https://cards.scryfall.io/s.jpg"},
                "card_faces": [{"image_uris": {"normal": "https://cards.scryfall.io/f.jpg"}}]}
        assert _get_card_image_url(card) == "https://cards.scryfall.io/f.jpg"

    @pytest.mark.parametrize("faces", [
        [{"name": "front"}],
        [{"image_uris": None}],
        [{"image_uris": "https://cards.scryfall.io/f.jpg"}],
        [{"image_uris": {"normal": "javascript:x"}}],
        ["not a dict"],
        [],
        None,
        "not a list",
        {"image_uris": {"normal": "https://cards.scryfall.io/f.jpg"}},
    ])
    def test_unusable_card_faces_give_no_image(self, faces):
        assert _get_card_image_url({"card_faces": faces}) is None

    def test_only_front_face_is_used(self):
        card = {"card_faces": [{"name": "front"},
                               {"image_uris": {"normal": "https://cards.scryfall.io/back.jpg"}}]}
        assert _get_card_image_url(card) is None

    def test_single_face_image_exact(self, scryfall_card_fixture):
        assert _get_card_image_url(scryfall_card_fixture) == scryfall_card_fixture["image_uris"]["normal"]

    def test_unsafe_characters_in_image_url_are_percent_encoded(self):
        url = _get_card_image_url({"image_uris": {"normal": "https://cards.scryfall.io/a b.jpg"}})
        assert url == "https://cards.scryfall.io/a%20b.jpg"

    @pytest.mark.parametrize("usd", ["abc", "", "nan", "inf", "-inf", "1,50", [], {}, "$1.00"])
    def test_non_numeric_price_is_omitted_not_raised(self, scryfall_card_fixture, usd):
        card = self._card(scryfall_card_fixture, prices={"usd": usd})
        desc = build_card_embed(card).description
        assert desc == "**{R}**\nInstant\nLightning Bolt deals 3 damage to any target."

    @pytest.mark.parametrize("prices", ["1.00", ["usd"], 5, None])
    def test_non_dict_prices_omitted(self, scryfall_card_fixture, prices):
        card = self._card(scryfall_card_fixture, prices=prices)
        assert "USD" not in build_card_embed(card).description

    @pytest.mark.parametrize("usd,shown", [("0.5", "$0.50"), (3, "$3.00"), ("1234.5", "$1234.50"), (0.999, "$1.00")])
    def test_numeric_prices_formatted(self, scryfall_card_fixture, usd, shown):
        card = self._card(scryfall_card_fixture, prices={"usd": usd})
        assert build_card_embed(card).description.endswith(f"\n**USD {shown}**")

    def test_dfc_embed_content(self, scryfall_dfc_fixture):
        embed = build_card_embed(scryfall_dfc_fixture)
        front = scryfall_dfc_fixture["card_faces"][0]
        assert embed.title == "Delver of Secrets // Insectile Aberration"
        assert embed.url == scryfall_dfc_fixture["scryfall_uri"]
        assert embed.image.url == front["image_uris"]["normal"]
        lines = embed.description.split("\n")
        assert lines[0] == "**{U}**"
        assert lines[1] == "Creature \u2014 Human Wizard // Creature \u2014 Human Insect"
        assert lines[-1] == "**USD $0.32**"

    def test_unsafe_scryfall_uri_dropped(self, scryfall_card_fixture):
        card = self._card(scryfall_card_fixture, scryfall_uri="javascript:alert(1)")
        assert not build_card_embed(card).url

    def test_missing_fields_do_not_raise(self):
        embed = build_card_embed({})
        assert embed.title == "Unknown" and not embed.description and not embed.url


class TestScryfallFailurePaths:
    """Shared contract for scryfall_card, scryfall_card_exact and scryfall_card_by_id."""

    CALLERS = {
        "fuzzy": lambda **kw: scryfall_card("Bolt", **kw),
        "exact": lambda **kw: scryfall_card_exact("Bolt", **kw),
        "by_id": lambda **kw: scryfall_card_by_id(SFID, **kw),
    }

    @pytest.fixture(params=list(CALLERS))
    def call(self, request):
        return self.CALLERS[request.param]

    async def test_5xx_retried_three_times_then_raises(self, call, no_sleep):
        p, client = _client(side_effect=lambda *a, **k: _resp(503))
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await call()
        finally:
            p.stop()
        assert client.get.call_count == 3
        assert no_sleep.await_count == 2

    async def test_429_retried_then_raises(self, call, no_sleep):
        p, client = _client(side_effect=lambda *a, **k: _resp(429))
        try:
            with pytest.raises(httpx.HTTPStatusError) as ei:
                await call()
        finally:
            p.stop()
        assert ei.value.response.status_code == 429
        assert client.get.call_count == 3

    @pytest.mark.parametrize("status", [429, 500, 503])
    async def test_success_after_retry(self, call, no_sleep, status):
        p, client = _client(_resp(status), _resp(200, {"name": "Lightning Bolt"}))
        try:
            assert await call() == {"name": "Lightning Bolt"}
        finally:
            p.stop()
        assert client.get.call_count == 2
        assert no_sleep.await_count == 1

    async def test_transport_error_retried_then_success(self, call, no_sleep):
        p, client = _client(httpx.ConnectError("x"), _resp(200, {"name": "B"}))
        try:
            assert await call() == {"name": "B"}
        finally:
            p.stop()
        assert client.get.call_count == 2

    async def test_transport_error_exhausts_and_propagates(self, call, no_sleep):
        p, client = _client(side_effect=httpx.ReadTimeout("slow"))
        try:
            with pytest.raises(httpx.TransportError):
                await call()
        finally:
            p.stop()
        assert client.get.call_count == 3

    @pytest.mark.parametrize("status", [400, 401, 403, 422])
    async def test_other_4xx_propagate_without_retry(self, call, no_sleep, status):
        p, client = _client(_resp(status, {"object": "error"}))
        try:
            with pytest.raises(httpx.HTTPStatusError) as ei:
                await call()
        finally:
            p.stop()
        assert ei.value.response.status_code == status
        assert client.get.call_count == 1
        no_sleep.assert_not_awaited()

    async def test_retries_false_single_attempt(self, call, no_sleep):
        p, client = _client(side_effect=lambda *a, **k: _resp(503))
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await call(retries=False)
        finally:
            p.stop()
        assert client.get.call_count == 1
        no_sleep.assert_not_awaited()

    async def test_retries_false_transport_error_single_attempt(self, call, no_sleep):
        p, client = _client(side_effect=httpx.ConnectError("x"))
        try:
            with pytest.raises(httpx.ConnectError):
                await call(retries=False)
        finally:
            p.stop()
        assert client.get.call_count == 1

    async def test_404_is_none_without_retry(self, call, no_sleep):
        p, client = _client(_resp(404, {"object": "error", "code": "not_found"}))
        try:
            assert await call() is None
        finally:
            p.stop()
        assert client.get.call_count == 1

    async def test_default_and_custom_timeout(self, call):
        p, client = _client(_resp(200, {}), _resp(200, {}))
        try:
            await call()
            await call(timeout=2.5)
        finally:
            p.stop()
        assert [c[1]["timeout"] for c in client.get.call_args_list] == [15.0, 2.5]


class TestScryfallExactSignature:
    async def test_exact_params_and_headers(self):
        p, client = _client(_resp(200, {"name": "Bolt"}))
        try:
            await scryfall_card_exact("Bolt", timeout=1.0)
        finally:
            p.stop()
        args, kw = client.get.call_args
        assert args[0] == "https://api.scryfall.com/cards/named"
        assert kw["params"] == {"exact": "Bolt"}
        assert kw["timeout"] == 1.0
        assert kw["headers"]["User-Agent"] == "hightide-events-bot/0.2"

    def test_keyword_only_options(self):
        with pytest.raises(TypeError):
            scryfall_card_exact("Bolt", False)


class TestScryfallByIdValidation:
    @pytest.mark.parametrize("bad", [
        SFID + "\n", "\n" + SFID, SFID + " ", " " + SFID, SFID[:-1], SFID + "0", "", "not-a-uuid",
        SFID.replace("-", ""), "../../cards/random", SFID + "/../x",
    ])
    async def test_malformed_ids_make_no_request(self, bad):
        p, client = _client()
        try:
            assert await scryfall_card_by_id(bad) is None
        finally:
            p.stop()
        client.get.assert_not_called()

    async def test_uppercase_uuid_accepted(self):
        p, client = _client(_resp(200, {"name": "X"}))
        try:
            assert await scryfall_card_by_id(SFID.upper()) == {"name": "X"}
        finally:
            p.stop()
        client.get.assert_called_once()


class TestCardHandlerEdges:
    def _cog(self):
        from src.discord_ui.commands_roles import RolesCog
        return RolesCog(AsyncMock())

    async def _run(self, name, **patch_kw):
        cog, i = self._cog(), _interaction()
        with patch("src.discord_ui.commands_roles.scryfall_card", new_callable=AsyncMock, **patch_kw) as f:
            await cog.card_lookup.callback(cog, i, name)
        return f, i

    async def test_generic_exception_is_ephemeral_trouble_and_logged_error(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=HANDLER_LOGGER):
            f, i = await self._run("Lightning Bolt", side_effect=RuntimeError("boom"))
        args, kw = i.response.send_message.call_args
        assert args[0] == "Scryfall is having trouble \u2014 try again shortly."
        assert kw == {"ephemeral": True}
        i.response.send_message.assert_called_once()
        (rec,) = [r for r in caplog.records if r.name == HANDLER_LOGGER]
        assert rec.levelname == "ERROR"
        assert "'Lightning Bolt'" in rec.getMessage() and "boom" in rec.getMessage()

    async def test_transport_error_logs_warning(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=HANDLER_LOGGER):
            await self._run("Bolt", side_effect=httpx.ConnectError("down"))
        (rec,) = [r for r in caplog.records if r.name == HANDLER_LOGGER]
        assert rec.levelname == "WARNING"
        assert "transport" in rec.getMessage() and "'Bolt'" in rec.getMessage()

    async def test_http_status_error_logs_warning(self, caplog):
        exc = httpx.HTTPStatusError("503", request=MagicMock(), response=MagicMock())
        with caplog.at_level(logging.DEBUG, logger=HANDLER_LOGGER):
            _, i = await self._run("Bolt", side_effect=exc)
        (rec,) = [r for r in caplog.records if r.name == HANDLER_LOGGER]
        assert rec.levelname == "WARNING" and "HTTP error" in rec.getMessage()
        assert i.response.send_message.call_args[1]["ephemeral"] is True

    async def test_miss_does_not_log(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=HANDLER_LOGGER):
            await self._run("Nope", return_value=None)
        assert [r for r in caplog.records if r.name == HANDLER_LOGGER] == []

    async def test_log_input_is_repr_quoted_so_newlines_cannot_forge_lines(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=HANDLER_LOGGER):
            await self._run("a\nWARNING forged", side_effect=RuntimeError("x"))
        (rec,) = [r for r in caplog.records if r.name == HANDLER_LOGGER]
        assert "\n" not in rec.getMessage()
        assert "'a\\nWARNING forged'" in rec.getMessage()

    async def test_miss_message_escapes_echoed_name(self):
        _, i = await self._run("@everyone *bold*", return_value=None)
        args, kw = i.response.send_message.call_args
        assert args[0] == "No card found for '@\u200beveryone \\*bold\\*'."
        assert kw == {"ephemeral": True}

    async def test_ambiguous_message(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=HANDLER_LOGGER):
            _, i = await self._run("Lightni", side_effect=AmbiguousCardError("Lightni"))
        args, kw = i.response.send_message.call_args
        assert args[0] == "Too many cards match 'Lightni' \u2014 add more words."
        assert kw == {"ephemeral": True}
        assert [r for r in caplog.records if r.name == HANDLER_LOGGER] == []

    async def test_ambiguous_message_escapes_name(self):
        _, i = await self._run("@everyone", side_effect=AmbiguousCardError("@everyone"))
        assert i.response.send_message.call_args[0][0] == (
            "Too many cards match '@\u200beveryone' \u2014 add more words.")

    async def test_name_of_exactly_100_chars_is_looked_up(self):
        name = "A" * 100
        f, i = await self._run(name, return_value=None)
        f.assert_called_once_with(name, retries=False, timeout=2.5)
        assert i.response.send_message.call_args[0][0] == f"No card found for '{name}'."

    async def test_name_of_101_chars_rejected(self):
        f, i = await self._run("A" * 101)
        f.assert_not_called()
        args, kw = i.response.send_message.call_args
        assert args[0] == "Card name must be \u2264100 characters." and kw == {"ephemeral": True}

    async def test_padding_does_not_count_toward_100(self):
        name = "A" * 100
        f, _ = await self._run("  " + name + "  ", return_value=None)
        f.assert_called_once_with(name, retries=False, timeout=2.5)

    @pytest.mark.parametrize("blank", ["", " ", "   ", "\t\n ", " " * 300])
    async def test_blank_name_never_reaches_scryfall(self, blank):
        f, i = await self._run(blank)
        f.assert_not_called()
        args, kw = i.response.send_message.call_args
        assert args[0] == "Give me a card name." and kw == {"ephemeral": True}

    async def test_name_is_stripped_before_lookup(self):
        f, _ = await self._run("  Lightning Bolt \n", return_value=None)
        f.assert_called_once_with("Lightning Bolt", retries=False, timeout=2.5)

    async def test_service_timeout_is_below_wait_for_cap(self, scryfall_card_fixture):
        from src.discord_ui import commands_roles
        f, _ = await self._run("Bolt", return_value=scryfall_card_fixture)
        service_timeout = f.call_args[1]["timeout"]
        assert service_timeout == 2.5
        assert service_timeout < commands_roles._LOOKUP_TIMEOUT == 2.6
        assert f.call_args[1]["retries"] is False


class TestCooldowns:
    """/card and /deck are rate-limited per user (1 use / 5s).

    Handler tests call `command.callback` directly, which bypasses checks; these tests
    run the command's checks. The cooldown buckets live on the shared command objects,
    so every test uses a fresh user id instead of resetting state.
    """

    @staticmethod
    def _user():
        return uuid.uuid4().int >> 64

    @staticmethod
    def _at(i, offset):
        i.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset)
        return i

    @pytest.fixture(params=["card_lookup", "deck_lookup"])
    def command(self, request):
        from src.discord_ui.commands_roles import RolesCog
        return getattr(RolesCog, request.param)

    async def test_second_call_in_window_is_blocked(self, command):
        uid = self._user()
        assert await command._check_can_run(self._at(_interaction(uid), 0)) is True
        with pytest.raises(app_commands.CommandOnCooldown) as ei:
            await command._check_can_run(self._at(_interaction(uid), 1))
        assert ei.value.cooldown.per == 5.0 and ei.value.cooldown.rate == 1
        assert 0 < ei.value.retry_after <= 5.0

    async def test_allowed_again_after_window(self, command):
        uid = self._user()
        await command._check_can_run(self._at(_interaction(uid), 0))
        assert await command._check_can_run(self._at(_interaction(uid), 6)) is True

    async def test_other_users_unaffected(self, command):
        await command._check_can_run(self._at(_interaction(self._user()), 0))
        assert await command._check_can_run(self._at(_interaction(self._user()), 0.1)) is True

    async def test_card_and_deck_cooldowns_are_independent(self):
        from src.discord_ui.commands_roles import RolesCog
        uid = self._user()
        await RolesCog.card_lookup._check_can_run(self._at(_interaction(uid), 0))
        assert await RolesCog.deck_lookup._check_can_run(self._at(_interaction(uid), 0.1)) is True

    async def test_cooldown_error_gets_ephemeral_reply(self):
        from src.bot import create_bot
        bot = create_bot()
        i = _interaction()
        i.response.is_done = MagicMock(return_value=False)
        err = app_commands.CommandOnCooldown(app_commands.Cooldown(1, 5.0), 3.2)
        await bot.tree.on_error(i, err)
        i.response.send_message.assert_awaited_once_with(
            "Slow down \u2014 try again in a moment.", ephemeral=True)

    async def test_cooldown_reply_uses_followup_when_already_responded(self):
        from src.bot import create_bot
        bot = create_bot()
        i = _interaction()
        i.response.is_done = MagicMock(return_value=True)
        i.followup = AsyncMock()
        err = app_commands.CommandOnCooldown(app_commands.Cooldown(1, 5.0), 3.2)
        await bot.tree.on_error(i, err)
        i.followup.send.assert_awaited_once_with(
            "Slow down \u2014 try again in a moment.", ephemeral=True)
        i.response.send_message.assert_not_called()

    async def test_other_errors_still_logged_by_default_handler(self, caplog):
        from src.bot import create_bot
        bot = create_bot()
        i = _interaction()
        i.command = None
        with caplog.at_level(logging.ERROR):
            await bot.tree.on_error(i, app_commands.AppCommandError("kaboom"))
        assert any(r.levelname == "ERROR" and "Ignoring exception" in r.getMessage()
                   for r in caplog.records)
        i.response.send_message.assert_not_called()

    async def test_direct_callback_invocation_bypasses_cooldown(self):
        from src.discord_ui.commands_roles import RolesCog
        cog = RolesCog(AsyncMock())
        i = _interaction(self._user())
        with patch("src.discord_ui.commands_roles.scryfall_card", new_callable=AsyncMock) as f:
            f.return_value = None
            await cog.card_lookup.callback(cog, i, "a")
            await cog.card_lookup.callback(cog, i, "b")
        assert f.await_count == 2
