import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from valentine.models import AgentTask, RoutingDecision, AgentName, IncomingMessage, ContentType


@pytest.fixture
def mock_bus():
    bus = AsyncMock()
    bus.check_health = AsyncMock(return_value=True)
    bus.get_history = AsyncMock(return_value=[])
    bus.append_history = AsyncMock()
    bus.stream_name = MagicMock(return_value="valentine:cortex:task")
    bus.ROUTER_STREAM = "valentine:router:task"
    bus.close = AsyncMock()
    return bus


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.provider_name = "groq"
    llm.chat_completion = AsyncMock(return_value="nothing to extract")
    return llm


def _make_task(text="Hello", intent="search_memory"):
    msg = IncomingMessage(
        message_id="1", chat_id="c1", user_id="u1",
        platform="telegram", content_type=ContentType.TEXT, text=text,
    )
    return AgentTask(
        task_id="t1", agent=AgentName.CORTEX,
        routing=RoutingDecision(intent=intent, agent=AgentName.CORTEX),
        message=msg,
    )


class TestCortexGracefulDegradation:
    def test_cortex_survives_no_memory_lib(self, mock_llm, mock_bus):
        """Cortex should not crash when mem0 import fails."""
        with patch.dict("sys.modules", {"mem0": None}):
            # Force re-import
            import importlib
            import valentine.agents.cortex as cortex_mod
            try:
                importlib.reload(cortex_mod)
            except Exception:
                pass
            # Just constructing should not crash
            agent = cortex_mod.CortexAgent(llm=mock_llm, bus=mock_bus)
            assert agent.memory is None

    @pytest.mark.asyncio
    async def test_process_task_without_memory_returns_success(self, mock_llm, mock_bus):
        """process_task should return success=True with a friendly message when memory is None."""
        from valentine.agents.cortex import CortexAgent
        agent = CortexAgent(llm=mock_llm, bus=mock_bus)
        agent.memory = None  # Simulate failed init
        task = _make_task(intent="search_memory")
        result = await agent.process_task(task)
        assert result.success is True
        assert "unavailable" in result.text.lower() or "not available" in result.text.lower() or "temporarily" in result.text.lower()

    @pytest.mark.asyncio
    async def test_store_memory_without_memory_returns_success(self, mock_llm, mock_bus):
        """store_memory intent should degrade gracefully, not error."""
        from valentine.agents.cortex import CortexAgent
        agent = CortexAgent(llm=mock_llm, bus=mock_bus)
        agent.memory = None
        task = _make_task(intent="store_memory")
        result = await agent.process_task(task)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_fetch_context_without_memory(self, mock_llm, mock_bus):
        """fetch_context_for_routing should return empty list when memory is None."""
        from valentine.agents.cortex import CortexAgent
        agent = CortexAgent(llm=mock_llm, bus=mock_bus)
        agent.memory = None
        msg = IncomingMessage(
            message_id="1", chat_id="c1", user_id="u1",
            platform="telegram", content_type=ContentType.TEXT, text="test",
        )
        result = await agent.fetch_context_for_routing(msg)
        assert result == []
