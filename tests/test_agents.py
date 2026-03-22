import pytest
from unittest.mock import AsyncMock
from valentine.models import AgentName, ContentType


class TestOracleAgent:
    @pytest.mark.asyncio
    async def test_basic_chat(self, mock_llm, mock_bus):
        mock_llm.chat_completion = AsyncMock(return_value="Hey there!")
        from valentine.agents.oracle import OracleAgent
        agent = OracleAgent(llm=mock_llm, bus=mock_bus)
        from tests.conftest import make_task_for
        task = make_task_for(AgentName.ORACLE, text="Hi Valentine")
        result = await agent.process_task(task)
        assert result.success is True
        assert result.text == "Hey there!"

    @pytest.mark.asyncio
    async def test_saves_history(self, mock_llm, mock_bus):
        mock_llm.chat_completion = AsyncMock(return_value="Response")
        from valentine.agents.oracle import OracleAgent
        agent = OracleAgent(llm=mock_llm, bus=mock_bus)
        from tests.conftest import make_task_for
        task = make_task_for(AgentName.ORACLE, text="Hello")
        await agent.process_task(task)
        assert mock_bus.append_history.call_count == 2  # user + assistant

    @pytest.mark.asyncio
    async def test_handles_llm_failure(self, mock_llm, mock_bus):
        mock_llm.chat_completion = AsyncMock(side_effect=Exception("API error"))
        from valentine.agents.oracle import OracleAgent
        agent = OracleAgent(llm=mock_llm, bus=mock_bus)
        from tests.conftest import make_task_for
        task = make_task_for(AgentName.ORACLE, text="Hello")
        result = await agent.process_task(task)
        assert result.success is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_reply_context_included(self, mock_llm, mock_bus):
        mock_llm.chat_completion = AsyncMock(return_value="About that...")
        from valentine.agents.oracle import OracleAgent
        agent = OracleAgent(llm=mock_llm, bus=mock_bus)
        from tests.conftest import make_task_for
        task = make_task_for(
            AgentName.ORACLE, text="What about this?",
            reply_to_text="The weather is nice today",
        )
        result = await agent.process_task(task)
        call_args = mock_llm.chat_completion.call_args
        messages = call_args[0][0]
        user_msg = messages[-1]["content"]
        assert "weather is nice" in user_msg


class TestZeroClawRouter:
    @pytest.mark.asyncio
    async def test_photo_routes_to_iris(self, mock_llm, mock_bus):
        mock_llm.chat_completion = AsyncMock(
            return_value='{"intent": "describe", "agent": "oracle", "priority": "normal"}'
        )
        from valentine.orchestrator.zeroclaw import ZeroClawRouter
        router = ZeroClawRouter(llm=mock_llm, bus=mock_bus)
        from tests.conftest import make_task_for
        task = make_task_for(
            AgentName.ZEROCLAW, intent="incoming", text="What's this?",
            content_type=ContentType.PHOTO, media_path="/tmp/photo.jpg",
        )
        result = await router.process_task(task)
        last_add_task = mock_bus.add_task.call_args_list[-1]
        routed_stream = last_add_task[0][0]
        assert "iris" in routed_stream

    @pytest.mark.asyncio
    async def test_voice_routes_to_echo(self, mock_llm, mock_bus):
        mock_llm.chat_completion = AsyncMock(
            return_value='{"intent": "transcribe", "agent": "oracle", "priority": "normal"}'
        )
        from valentine.orchestrator.zeroclaw import ZeroClawRouter
        router = ZeroClawRouter(llm=mock_llm, bus=mock_bus)
        from tests.conftest import make_task_for
        task = make_task_for(
            AgentName.ZEROCLAW, intent="incoming",
            content_type=ContentType.VOICE, media_path="/tmp/voice.ogg",
        )
        result = await router.process_task(task)
        last_add_task = mock_bus.add_task.call_args_list[-1]
        routed_stream = last_add_task[0][0]
        assert "echo" in routed_stream

    @pytest.mark.asyncio
    async def test_bad_json_falls_back_to_oracle(self, mock_llm, mock_bus):
        mock_llm.chat_completion = AsyncMock(return_value="Not JSON at all")
        from valentine.orchestrator.zeroclaw import ZeroClawRouter
        router = ZeroClawRouter(llm=mock_llm, bus=mock_bus)
        from tests.conftest import make_task_for
        task = make_task_for(AgentName.ZEROCLAW, intent="incoming", text="Hello")
        result = await router.process_task(task)
        last_add_task = mock_bus.add_task.call_args_list[-1]
        routed_stream = last_add_task[0][0]
        assert "oracle" in routed_stream

    @pytest.mark.asyncio
    async def test_invalid_priority_no_crash(self, mock_llm, mock_bus):
        mock_llm.chat_completion = AsyncMock(
            return_value='{"intent": "chat", "agent": "oracle", "priority": "SUPER_URGENT"}'
        )
        from valentine.orchestrator.zeroclaw import ZeroClawRouter
        router = ZeroClawRouter(llm=mock_llm, bus=mock_bus)
        from tests.conftest import make_task_for
        task = make_task_for(AgentName.ZEROCLAW, intent="incoming", text="Hello")
        result = await router.process_task(task)
        assert result.success is True
