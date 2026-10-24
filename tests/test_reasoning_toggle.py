from smolagents.models import ChatMessage, MessageRole

from backend.agent.local_llm_model import LocalLLMModel, _normal_turn_enable_thinking
from backend.llm_service import GenerationResult


class _FakeLLMService:
    def __init__(self):
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return GenerationResult(text="ok")


def test_local_llm_model_uses_normal_turn_enable_thinking_context():
    service = _FakeLLMService()
    model = LocalLLMModel(service)  # type: ignore[arg-type]
    token = _normal_turn_enable_thinking.set(True)
    try:
        model.generate([ChatMessage(role=MessageRole.USER, content="hello")])
    finally:
        _normal_turn_enable_thinking.reset(token)

    assert service.calls[0]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": True}
    }


def test_local_llm_model_does_not_override_explicit_extra_body():
    service = _FakeLLMService()
    model = LocalLLMModel(service)  # type: ignore[arg-type]
    explicit = {"chat_template_kwargs": {"enable_thinking": False}}
    token = _normal_turn_enable_thinking.set(True)
    try:
        model.generate(
            [ChatMessage(role=MessageRole.USER, content="route")],
            extra_body=explicit,
        )
    finally:
        _normal_turn_enable_thinking.reset(token)

    assert service.calls[0]["extra_body"] == explicit


def test_local_llm_model_omits_extra_body_when_toggle_is_unspecified():
    service = _FakeLLMService()
    model = LocalLLMModel(service)  # type: ignore[arg-type]

    model.generate([ChatMessage(role=MessageRole.USER, content="hello")])

    assert "extra_body" not in service.calls[0]
