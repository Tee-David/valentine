# tests/test_models.py
from valentine.models import (
    IncomingMessage, RoutingDecision, AgentTask, TaskResult,
    ContentType, AgentName, Priority,
)


class TestIncomingMessage:
    def test_to_dict_roundtrip(self, sample_text_message):
        data = sample_text_message.to_dict()
        restored = IncomingMessage.from_dict(data)
        assert restored.message_id == sample_text_message.message_id
        assert restored.content_type == ContentType.TEXT
        assert restored.text == "Hello Valentine"

    def test_from_dict_with_media(self, sample_photo_message):
        data = sample_photo_message.to_dict()
        restored = IncomingMessage.from_dict(data)
        assert restored.content_type == ContentType.PHOTO
        assert restored.media_path == "/tmp/photo.jpg"


class TestRoutingDecision:
    def test_to_dict_roundtrip(self):
        rd = RoutingDecision(
            intent="code_generation",
            agent=AgentName.CODESMITH,
            chain=[AgentName.IRIS, AgentName.CODESMITH],
            params={"language": "python"},
            memory_context=["user likes concise code"],
        )
        data = rd.to_dict()
        restored = RoutingDecision.from_dict(data)
        assert restored.agent == AgentName.CODESMITH
        assert restored.chain == [AgentName.IRIS, AgentName.CODESMITH]
        assert restored.memory_context == ["user likes concise code"]


class TestAgentTask:
    def test_auto_generates_task_id(self, sample_text_message):
        rd = RoutingDecision(intent="chat", agent=AgentName.ORACLE)
        task = AgentTask(task_id="", agent=AgentName.ORACLE, routing=rd, message=sample_text_message)
        assert task.task_id != ""

    def test_to_dict_roundtrip(self, sample_text_message):
        rd = RoutingDecision(intent="chat", agent=AgentName.ORACLE)
        task = AgentTask(task_id="t1", agent=AgentName.ORACLE, routing=rd, message=sample_text_message)
        data = task.to_dict()
        restored = AgentTask.from_dict(data)
        assert restored.task_id == "t1"
        assert restored.agent == AgentName.ORACLE


class TestTaskResult:
    def test_success_result(self):
        result = TaskResult(
            task_id="t1", agent=AgentName.ORACLE, success=True,
            text="Here is my answer", processing_time_ms=150,
        )
        data = result.to_dict()
        restored = TaskResult.from_dict(data)
        assert restored.success is True
        assert restored.text == "Here is my answer"

    def test_error_result(self):
        result = TaskResult(
            task_id="t2", agent=AgentName.CODESMITH, success=False,
            error="API rate limited", processing_time_ms=50,
        )
        assert result.error == "API rate limited"
        assert result.text is None
