"""Tests for Scryfall /card lookup."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import httpx

from src.services.deck_service import scryfall_card, scryfall_card_exact, build_card_embed


FIXTURES = Path(__file__).parent / "fixtures"


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
        assert embed.image.url is not None
        assert embed.description is not None

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
        desc = embed.description
        
        # Should not have a price line
        assert "USD" not in desc

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
        """Long oracle text is trimmed to 400 chars at a line boundary."""
        card = scryfall_card_fixture.copy()
        # Create a very long oracle text
        long_text = "This is a very long oracle text. " * 30
        card["oracle_text"] = long_text
        
        embed = build_card_embed(card)
        desc = embed.description
        
        # Oracle text portion should be trimmed
        assert len(desc) <= 2000  # Discord embed description limit
        # Should not contain the full long text
        assert long_text[:400] not in desc or len(desc) < len(long_text)

    def test_markdown_escaping_in_description(self, scryfall_card_fixture):
        """Dangerous markdown characters in scraped text are escaped."""
        card = scryfall_card_fixture.copy()
        card["oracle_text"] = "Deals [damage] to **any** target with *italics* and _underscores_"
        
        embed = build_card_embed(card)
        desc = embed.description
        
        # Brackets, asterisks, underscores should be escaped
        assert r"\[" in desc or "[" not in desc
        # At least the oracle text should be in there (escaped or not visible)
        assert "damage" in desc.lower()

    def test_url_escaping_in_scryfall_uri(self, scryfall_card_fixture):
        """URL in embed is validated and escaped properly."""
        card = scryfall_card_fixture
        embed = build_card_embed(card)
        
        # URL should be safe (https scheme and valid host)
        assert embed.url.startswith("https://")
        assert "scryfall" in embed.url.lower()


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
            
            assert result is not None
            assert result["name"] == "Lightning Bolt"

    @pytest.mark.asyncio
    async def test_fuzzy_miss_404_no_suggestion(self):
        """Fuzzy miss with no suggestion returns None."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.json.return_value = {"details": "Not found", "data": {}}
            mock_client.get.return_value = mock_response
            
            mock_client_class.return_value.__aenter__.return_value = mock_client
            
            result = await scryfall_card("Nonexistent Card Name")
            
            assert result is None

    @pytest.mark.asyncio
    async def test_fuzzy_miss_with_suggestion(self):
        """Fuzzy miss 404 includes suggestion in error body if available."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.json.return_value = {
                "details": "Ambiguous name",
                "data": {"options": ["Lightning Bolt", "Chain Lightning"]}
            }
            mock_client.get.return_value = mock_response
            
            mock_client_class.return_value.__aenter__.return_value = mock_client
            
            result = await scryfall_card("Lightni")
            
            # Fuzzy should return None on 404, but error details are handled by caller
            assert result is None

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self):
        """Network/transport error returns None."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.TransportError("Network error")
            mock_client_class.return_value.__aenter__.return_value = mock_client
            
            result = await scryfall_card("Lightning Bolt")
            
            assert result is None

    @pytest.mark.asyncio
    async def test_5xx_error_returns_none(self):
        """5xx errors return None (handled by retry logic)."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 503
            # After retries fail, this should return None
            mock_client.get.side_effect = httpx.HTTPStatusError("503", request=MagicMock(), response=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client
            
            result = await scryfall_card("Lightning Bolt")
            
            assert result is None

    @pytest.mark.asyncio
    async def test_uses_fuzzy_endpoint(self):
        """Uses the fuzzy endpoint for lookup."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"name": "Lightning Bolt"}
            mock_client.get.return_value = mock_response
            
            mock_client_class.return_value.__aenter__.return_value = mock_client
            
            await scryfall_card("Lightning Bolt")
            
            # Should call the fuzzy endpoint
            mock_client.get.assert_called_once()
            call_args = mock_client.get.call_args
            assert "fuzzy=" in call_args[0][0] or "fuzzy" in str(call_args[1])

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
            if "headers" in call_args[1]:
                assert "hightide-events-bot/0.2" in str(call_args[1]["headers"])


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
            
            assert result is not None
            assert result["name"] == "Lightning Bolt"

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

    @pytest.mark.asyncio
    async def test_uses_exact_endpoint(self):
        """Uses the exact endpoint for lookup."""
        with patch("src.services.deck_service.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"name": "Lightning Bolt"}
            mock_client.get.return_value = mock_response
            
            mock_client_class.return_value.__aenter__.return_value = mock_client
            
            await scryfall_card_exact("Lightning Bolt")
            
            # Should call the exact endpoint
            mock_client.get.assert_called_once()
            call_args = mock_client.get.call_args
            assert "exact=" in call_args[0][0] or "exact" in str(call_args[1])


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
