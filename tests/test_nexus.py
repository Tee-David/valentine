import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_bus():
    bus = AsyncMock()
    bus.get_history = AsyncMock(return_value=[])
    bus.append_history = AsyncMock()
    bus.close = AsyncMock()
    return bus


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.provider_name = "groq"
    return llm


class TestNexusToolExecution:
    @pytest.mark.asyncio
    async def test_weather_no_mock_data(self, mock_llm, mock_bus):
        """Weather should NOT return hardcoded mock data."""
        from valentine.agents.nexus import NexusAgent
        agent = NexusAgent(llm=mock_llm, bus=mock_bus)
        result = await agent._execute_tool("get_weather", {"location": "London"})
        assert "Mock" not in result
        # Should contain temperature info or an error about network
        assert "°" in result or "error" in result.lower() or "couldn" in result.lower()

    @pytest.mark.asyncio
    async def test_crypto_no_mock_data(self, mock_llm, mock_bus):
        """Crypto should NOT return hardcoded mock data."""
        from valentine.agents.nexus import NexusAgent
        agent = NexusAgent(llm=mock_llm, bus=mock_bus)
        result = await agent._execute_tool("get_crypto_price", {"symbol": "BTC"})
        assert "Mock" not in result
        assert "$" in result or "error" in result.lower() or "couldn" in result.lower()

    @pytest.mark.asyncio
    async def test_unknown_tool(self, mock_llm, mock_bus):
        from valentine.agents.nexus import NexusAgent
        agent = NexusAgent(llm=mock_llm, bus=mock_bus)
        result = await agent._execute_tool("nonexistent_tool", {})
        assert "not available" in result.lower() or "not found" in result.lower()
