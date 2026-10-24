from types import SimpleNamespace
import base64
import io

from PIL import Image
import pytest
from unittest.mock import MagicMock, patch
from smolagents.memory import AgentMemory, TaskStep
from smolagents.models import ChatMessage, MessageRole

from backend.agent.agent_runner import SmartToolCallingAgent
from backend.agent.local_llm_model import LocalLLMModel
from backend.compact.prompt_diagnostics import make_prompt_snapshot
from backend.config import get_config
from backend.llm_service import GenerationResult, LLMService


class FakeLLMService:
    def __init__(self, text="ok", tool_calls=None):
        self.text = text
        self.tool_calls = tool_calls

    def chat(self, **kwargs):
        self.kwargs = kwargs
        if not kwargs.get("stream"):
            return GenerationResult(text=self.text, tool_calls=self.tool_calls)
        return iter([{"content": self.text}])


def _memory_only_agent():
    with patch.object(SmartToolCallingAgent, "__init__", lambda self, **kwargs: None):
        agent = SmartToolCallingAgent.__new__(SmartToolCallingAgent)
    agent.memory = AgentMemory(system_prompt="You are a helpful assistant.")
    agent.logger = MagicMock()
    return agent


def test_generate_stream_stores_exact_non_routing_payload_for_compact_cache_first():
    session = SimpleNamespace(
        current_agent_name="basic_agent",
        last_llm_prompt_snapshot=None,
        last_compactable_llm_messages=None,
        last_compactable_agent_name=None,
    )
    llm_service = FakeLLMService()
    model = LocalLLMModel(llm_service=llm_service, runtime_context="RUNTIME", session=session)
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content="BASIC SYSTEM"),
        ChatMessage(role=MessageRole.USER, content="large observation already in memory"),
    ]

    stream = list(model.generate_stream(messages))

    assert session.last_compactable_agent_name == "basic_agent"
    assert [m.role for m in session.last_compactable_llm_messages] == ["system", "user", "assistant"]
    assert session.last_compactable_llm_messages[0].content == "BASIC SYSTEM\n\nRUNTIME"
    assert session.last_compactable_llm_messages[1].content == "large observation already in memory"
    assert session.last_compactable_llm_messages[2].content == "ok"
    assert session.last_compactable_llm_messages is not llm_service.kwargs["messages"]
    assert llm_service.kwargs["stream_usage_id"] == llm_service.kwargs["request_id"]
    assert stream[0].content == "ok"


def test_generate_stream_does_not_overwrite_compactable_payload_for_coordinator_routing():
    existing_payload = [SimpleNamespace(role="system", content="BASIC SYSTEM")]
    session = SimpleNamespace(
        current_agent_name="coordinator_1",
        last_llm_prompt_snapshot=None,
        last_compactable_llm_messages=existing_payload,
        last_compactable_agent_name="basic_agent",
    )
    model = LocalLLMModel(llm_service=FakeLLMService(), runtime_context="ROUTING", session=session)

    list(model.generate_stream([ChatMessage(role=MessageRole.SYSTEM, content="ROUTER SYSTEM")]))

    assert session.last_compactable_llm_messages is existing_payload
    assert session.last_compactable_agent_name == "basic_agent"


def test_generate_stores_exact_non_routing_payload_for_compact_cache_first():
    session = SimpleNamespace(
        current_agent_name="coordinator_2",
        last_llm_prompt_snapshot=None,
        last_compactable_llm_messages=None,
        last_compactable_agent_name=None,
    )
    llm_service = FakeLLMService()
    model = LocalLLMModel(llm_service=llm_service, runtime_context="RUNTIME", session=session)

    model.generate([ChatMessage(role=MessageRole.SYSTEM, content="COORDINATOR TWO SYSTEM")])

    assert session.last_compactable_agent_name == "coordinator_2"
    assert session.last_compactable_llm_messages[0].content == "COORDINATOR TWO SYSTEM\n\nRUNTIME"
    assert session.last_compactable_llm_messages[1].content == "ok"
    assert session.last_compactable_llm_messages is not llm_service.kwargs["messages"]


def test_generate_empty_output_clears_existing_compactable_payload():
    existing_payload = [SimpleNamespace(role="system", content="OLD PAYLOAD")]
    session = SimpleNamespace(
        current_agent_name="basic_agent",
        last_llm_prompt_snapshot=None,
        last_compactable_llm_messages=existing_payload,
        last_compactable_agent_name="basic_agent",
    )
    model = LocalLLMModel(llm_service=FakeLLMService(text=""), session=session)

    list(model.generate_stream([ChatMessage(role=MessageRole.SYSTEM, content="BASIC SYSTEM")]))

    assert session.last_compactable_llm_messages is None
    assert session.last_compactable_agent_name is None


def test_generate_framed_tool_call_clears_existing_compactable_payload_without_storing_new_one():
    existing_payload = [SimpleNamespace(role="system", content="OLD PAYLOAD")]
    session = SimpleNamespace(
        current_agent_name="basic_agent",
        last_llm_prompt_snapshot=None,
        last_compactable_llm_messages=existing_payload,
        last_compactable_agent_name="basic_agent",
    )
    tool_call = '''<<TOOL_CALL>>
{
  "name": "read_file",
  "arguments": {
  "file_path": "/tmp/a"
}
}
<<END_TOOL_CALL>>'''
    model = LocalLLMModel(llm_service=FakeLLMService(text=tool_call), session=session)

    list(model.generate_stream([ChatMessage(role=MessageRole.SYSTEM, content="BASIC SYSTEM")]))

    assert session.last_compactable_llm_messages is None
    assert session.last_compactable_agent_name is None


@pytest.mark.parametrize(
    "control_text",
    [
        "Calling tools:\n[]",
        "<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{\"file_path\":}}\n<<END_TOOL_CALL>>",
    ],
)
def test_rejected_control_text_never_becomes_compactable_payload(control_text):
    session = SimpleNamespace(
        current_agent_name="basic_agent",
        last_llm_prompt_snapshot=None,
        last_compactable_llm_messages=None,
        last_compactable_agent_name=None,
    )
    model = LocalLLMModel(
        llm_service=FakeLLMService(text=control_text),
        session=session,
    )

    list(
        model.generate_stream(
            [ChatMessage(role=MessageRole.SYSTEM, content="BASIC SYSTEM")]
        )
    )

    assert session.last_compactable_llm_messages is None
    assert session.last_compactable_agent_name is None


def test_generate_structured_tool_calls_clear_existing_payload_without_storing_new_one():
    existing_payload = [SimpleNamespace(role="system", content="OLD PAYLOAD")]
    session = SimpleNamespace(
        current_agent_name="basic_agent",
        last_llm_prompt_snapshot=None,
        last_compactable_llm_messages=existing_payload,
        last_compactable_agent_name="basic_agent",
    )
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": {"file_path": "/tmp/a"}},
        }
    ]
    model = LocalLLMModel(llm_service=FakeLLMService(text="I will inspect the file.", tool_calls=tool_calls), session=session)
    read_file_tool = SimpleNamespace(
        name="read_file",
        description="Read one file.",
        inputs={
            "file_path": {
                "type": "string",
                "description": "Absolute file path.",
            }
        },
    )

    model.generate(
        [ChatMessage(role=MessageRole.SYSTEM, content="BASIC SYSTEM")],
        tools_to_call_from=[read_file_tool],
    )

    assert session.last_compactable_llm_messages is None
    assert session.last_compactable_agent_name is None


def test_empty_stream_fallback_hidden_native_final_answer_is_not_compactable():
    class EmptyThenNativeFinalAnswerService:
        def chat(self, **kwargs):
            if kwargs["stream"]:
                return iter([])
            return GenerationResult(
                text="Natural accompanying answer.",
                tool_calls=[
                    {
                        "id": "provider_final",
                        "type": "function",
                        "function": {
                            "name": "final_answer",
                            "arguments": {"answer": "done"},
                        },
                    }
                ],
            )

    session = SimpleNamespace(
        current_agent_name="basic_agent",
        last_llm_prompt_snapshot=None,
        last_compactable_llm_messages=None,
        last_compactable_agent_name=None,
    )
    model = LocalLLMModel(
        llm_service=EmptyThenNativeFinalAnswerService(),
        session=session,
    )

    deltas = list(
        model.generate_stream(
            [ChatMessage(role=MessageRole.SYSTEM, content="BASIC SYSTEM")]
        )
    )

    assert [delta.content for delta in deltas] == ["Natural accompanying answer."]
    assert session.last_compactable_llm_messages is None
    assert session.last_compactable_agent_name is None


def test_generate_stream_normal_call_replaces_existing_compactable_payload():
    existing_payload = [SimpleNamespace(role="system", content="OLD PAYLOAD")]
    session = SimpleNamespace(
        current_agent_name="basic_agent",
        last_llm_prompt_snapshot=None,
        last_compactable_llm_messages=existing_payload,
        last_compactable_agent_name="basic_agent",
    )
    model = LocalLLMModel(llm_service=FakeLLMService(), session=session)

    list(model.generate_stream([ChatMessage(role=MessageRole.SYSTEM, content="PROFILE SYSTEM")]))

    assert session.last_compactable_llm_messages is not existing_payload
    assert session.last_compactable_llm_messages[0].content == "PROFILE SYSTEM"
    assert session.last_compactable_agent_name == "basic_agent"


def test_agent_prepared_image_matches_provider_snapshot_and_compactable_payload(monkeypatch):
    class RecordingProvider:
        def __init__(self):
            self.calls = []

        def is_loaded(self):
            return True

        def generate(self, messages, preserve_prompt_shape=False, **kwargs):
            self.calls.append((messages, preserve_prompt_shape))
            return GenerationResult(text="ok")

    config = get_config()
    monkeypatch.setattr(config.model, "max_image_dimension", 8)
    image = Image.new("RGB", (16, 16), "red")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    original_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
    provider = RecordingProvider()
    service = LLMService(config=config)
    service._provider = provider
    session = SimpleNamespace(
        current_agent_name="basic_agent",
        last_llm_prompt_snapshot=None,
        last_compactable_llm_messages=None,
        last_compactable_agent_name=None,
    )
    model = LocalLLMModel(llm_service=service, session=session)

    model.generate(
        [
            ChatMessage(
                role=MessageRole.USER,
                content=[{"type": "image_url", "image_url": {"url": original_url}}],
            )
        ]
    )

    provider_messages, preserve_prompt_shape = provider.calls[0]
    provider_url = provider_messages[0].content[0]["image_url"]["url"]
    payload_url = session.last_compactable_llm_messages[0].content[0]["image_url"]["url"]
    provider_snapshot = make_prompt_snapshot("normal_agent", provider_messages)
    assert provider_url != original_url
    assert preserve_prompt_shape is True
    assert payload_url == provider_url
    assert session.last_llm_prompt_snapshot.content_hash == provider_snapshot.content_hash


def test_historical_task_image_reaches_provider_and_compactable_payload_with_same_url():
    image = Image.new("RGB", (4, 3), "blue")
    historical_task = TaskStep(task="Inspect this image", task_images=[image])
    current_task = TaskStep(task="Continue with the next turn", task_images=None)
    agent = _memory_only_agent()
    agent.memory.steps = [historical_task, current_task]
    serialized_messages = agent.write_memory_to_messages()
    service = FakeLLMService()
    session = SimpleNamespace(
        current_agent_name="basic_agent",
        last_llm_prompt_snapshot=None,
        last_compactable_llm_messages=None,
        last_compactable_agent_name=None,
    )

    LocalLLMModel(llm_service=service, session=session).generate(serialized_messages)

    provider_image_part = next(
        part
        for message in service.kwargs["messages"]
        if isinstance(message.content, list)
        for part in message.content
        if part.get("type") == "image_url"
    )
    compact_image_part = next(
        part
        for message in session.last_compactable_llm_messages
        if isinstance(message.content, list)
        for part in message.content
        if part.get("type") == "image_url"
    )
    provider_url = provider_image_part["image_url"]["url"]
    assert provider_url.startswith("data:image/jpeg;base64,")
    assert compact_image_part["image_url"]["url"] == provider_url
    assert historical_task.task_images == [image]
    assert historical_task.task_images[0] is image


def test_agent_image_preparation_uses_injected_service_config(monkeypatch):
    global_config = get_config()
    monkeypatch.setattr(global_config.model, "max_image_dimension", 64)
    service_config = __import__("copy").deepcopy(global_config)
    service_config.model.max_image_dimension = 8

    image = Image.new("RGB", (16, 16), "red")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    original_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
    service = FakeLLMService()
    service.config = service_config
    session = SimpleNamespace(
        current_agent_name="basic_agent",
        last_llm_prompt_snapshot=None,
        last_compactable_llm_messages=None,
        last_compactable_agent_name=None,
    )

    LocalLLMModel(llm_service=service, session=session).generate(
        [ChatMessage(role=MessageRole.USER, content=[
            {"type": "image_url", "image_url": {"url": original_url}}
        ])]
    )

    provider_url = service.kwargs["messages"][0].content[0]["image_url"]["url"]
    payload_url = session.last_compactable_llm_messages[0].content[0]["image_url"]["url"]
    with Image.open(io.BytesIO(base64.b64decode(provider_url.split(",", 1)[1]))) as prepared:
        assert prepared.size == (8, 8)
    assert payload_url == provider_url


def test_streaming_agent_empty_fallback_reuses_exact_prepared_image_payload(monkeypatch):
    global_config = get_config()
    monkeypatch.setattr(global_config.model, "max_image_dimension", 64)
    service_config = __import__("copy").deepcopy(global_config)
    service_config.model.max_image_dimension = 8

    image = Image.new("RGB", (16, 16), "green")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    original_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()

    class EmptyThenFallbackService:
        config = service_config

        def __init__(self):
            self.calls = []

        def chat(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["stream"]:
                return iter([])
            return GenerationResult(text="fallback")

    service = EmptyThenFallbackService()
    session = SimpleNamespace(
        current_agent_name="basic_agent",
        last_llm_prompt_snapshot=None,
        last_compactable_llm_messages=None,
        last_compactable_agent_name=None,
    )
    model = LocalLLMModel(llm_service=service, session=session)

    deltas = list(model.generate_stream([
        ChatMessage(role=MessageRole.USER, content=[
            {"type": "image_url", "image_url": {"url": original_url}}
        ])
    ]))

    assert [delta.content for delta in deltas] == ["fallback"]
    assert len(service.calls) == 2
    assert service.calls[0]["messages"] is service.calls[1]["messages"]
    assert service.calls[0]["preserve_prompt_shape"] is True
    assert service.calls[1]["preserve_prompt_shape"] is True
    provider_url = service.calls[0]["messages"][0].content[0]["image_url"]["url"]
    payload_url = session.last_compactable_llm_messages[0].content[0]["image_url"]["url"]
    with Image.open(io.BytesIO(base64.b64decode(provider_url.split(",", 1)[1]))) as prepared:
        assert prepared.size == (8, 8)
    assert payload_url == provider_url
