from unittest.mock import MagicMock

from smolagents.models import ChatMessage, MessageRole

from backend.agent.local_llm_model import LocalLLMModel
from backend.llm_service import GenerationResult


def test_generate_stream_does_not_truncate_or_retry_repeated_content():
    repeated_chunk = "x" * 200
    chunks = [{"content": repeated_chunk} for _ in range(5)]

    llm_service = MagicMock()
    llm_service.chat.return_value = iter(chunks)
    model = LocalLLMModel(llm_service)

    deltas = list(
        model.generate_stream(
            [ChatMessage(role=MessageRole.USER, content="repeat exactly")]
        )
    )

    assert "".join(delta.content for delta in deltas) == repeated_chunk * 5
    assert model.get_stream_state()["content"] == repeated_chunk * 5
    llm_service.chat.assert_called_once()


def test_local_llm_model_does_not_log_vlm_instruction_diagnostics(caplog):
    llm_service = MagicMock()
    llm_service.chat.return_value = GenerationResult(text="ok")
    model = LocalLLMModel(llm_service)

    caplog.set_level("INFO", logger="backend.agent.local_llm_model")

    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content="plain system prompt"),
        ChatMessage(role=MessageRole.USER, content="hello"),
    ]
    model.generate(messages)

    assert "包含VLM指令" not in caplog.text
    assert "未包含 VLM 身份指令" not in caplog.text


def test_generate_stream_does_not_log_vlm_instruction_diagnostics(caplog):
    llm_service = MagicMock()
    llm_service.chat.return_value = iter([{"content": "ok"}])
    model = LocalLLMModel(llm_service)

    caplog.set_level("INFO", logger="backend.agent.local_llm_model")

    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content="plain system prompt"),
        ChatMessage(role=MessageRole.USER, content="hello"),
    ]
    list(model.generate_stream(messages))

    assert "包含VLM指令" not in caplog.text
    assert "未包含 VLM 身份指令" not in caplog.text
