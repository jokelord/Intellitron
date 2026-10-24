import asyncio
import base64
import io
from copy import deepcopy
from unittest.mock import MagicMock, patch

from PIL import Image

from backend.api.chat import ChatMessage, _generate_stream_response
from backend.compact.types import CompactEvent
from backend.config import get_config
from backend.llm_service import Message


async def _drain(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


def _set_compact_config():
    cfg = get_config()
    cfg.compact.prompt_shape = "cache_first_tail_with_fallback"
    cfg.compact.prompt_control_enabled = True
    cfg.agent_prompts.compaction.no_tools_preamble = "COMPACT"
    cfg.agent_prompts.compaction.main_prompt = "MAIN"
    cfg.agent_prompts.compaction.no_tools_trailer = "TRAILER"
    cfg.agent_prompts.compaction.summary_envelope_preface = "["
    cfg.agent_prompts.compaction.summary_envelope_suffix = "]"
    cfg.agent_prompts.compaction.compact_control_user_instruction = "summarize prior messages."
    return cfg


def test_chat_auto_compact_passes_reliable_and_cache_first_bodies(monkeypatch):
    _set_compact_config()
    chat_messages = [
        ChatMessage(role="system", content="NORMAL CHAT SYSTEM"),
        ChatMessage(role="user", content="old user"),
        ChatMessage(role="assistant", content="old assistant"),
        ChatMessage(role="user", content="current user"),
    ]
    raw_request = MagicMock()
    captured = {}

    async def fake_run(req):
        captured["body_messages"] = req.body_messages
        captured["cache_first_body_messages"] = req.cache_first_body_messages
        yield CompactEvent.started("sid", "auto", "chat")
        yield CompactEvent.completed("sid", "cleaned", metadata={"trigger": "auto", "mode": "chat"})

    raw_request.app.state.compact_orchestrator.run_compact = fake_run
    service = MagicMock()

    async def fake_chat_stream(messages, **kwargs):
        yield {"content": "ok"}

    service.chat_stream_async = fake_chat_stream

    with patch("backend.api.chat._should_auto_compact_chat", return_value=True):
        asyncio.run(_drain(_generate_stream_response(
            service=service,
            messages=[Message(role=m.role, content=m.content) for m in chat_messages],
            model="qwen",
            max_tokens=None,
            temperature=None,
            top_p=None,
            raw_request=raw_request,
            original_chat_messages=chat_messages,
        )))

    assert captured["body_messages"][0].role == "system"
    assert "COMPACT" in captured["body_messages"][0].content or "Respond with TEXT ONLY" in captured["body_messages"][0].content
    assert captured["cache_first_body_messages"][0].role == "system"
    assert captured["cache_first_body_messages"][0].content == "NORMAL CHAT SYSTEM"
    assert "INTERNAL COMPACT CONTROL RULE" not in captured["cache_first_body_messages"][0].content
    assert all(m.content != "current user" for m in captured["body_messages"])
    assert all(m.content != "current user" for m in captured["cache_first_body_messages"])


def test_chat_auto_compact_uses_service_config_and_exact_prepared_source(monkeypatch):
    cfg = _set_compact_config()
    monkeypatch.setattr(cfg.model, "max_image_dimension", 64)
    service_cfg = deepcopy(cfg)
    service_cfg.model.max_image_dimension = 8
    image = Image.new("RGB", (16, 16), "blue")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    original_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
    chat_messages = [
        ChatMessage(role="system", content="SYSTEM"),
        ChatMessage(role="user", content=[{"type": "image_url", "image_url": {"url": original_url}}]),
        ChatMessage(role="assistant", content="seen"),
        ChatMessage(role="user", content="current"),
    ]
    raw_request = MagicMock()
    captured = {}

    async def fake_run(req):
        captured["source"] = req.cache_first_body_messages
        yield CompactEvent.completed("sid", "cleaned", metadata={"trigger": "auto", "mode": "chat"})

    raw_request.app.state.compact_orchestrator.run_compact = fake_run

    class Service:
        config = service_cfg

        async def chat_stream_async(self, messages, **kwargs):
            captured["provider"] = list(messages)
            captured["provider_kwargs"] = kwargs
            yield {"content": "ok"}

    with patch("backend.api.chat._should_auto_compact_chat", return_value=True):
        asyncio.run(_drain(_generate_stream_response(
            service=Service(),
            messages=[Message(role=m.role, content=m.content) for m in chat_messages],
            model="qwen",
            max_tokens=None,
            temperature=None,
            top_p=None,
            raw_request=raw_request,
            original_chat_messages=chat_messages,
        )))

    source_url = captured["source"][1].content[0]["image_url"]["url"]
    with Image.open(io.BytesIO(base64.b64decode(source_url.split(",", 1)[1]))) as prepared:
        assert prepared.size == (8, 8)
    assert captured["provider"] == [
        Message(role="user", content="[cleaned]"),
        Message(role="user", content="current"),
    ]
    assert captured["provider_kwargs"]["preserve_prompt_shape"] is True


def test_chat_prompt_control_disabled_preserves_no_system_default(monkeypatch):
    cfg = get_config()
    cfg.compact.prompt_control_enabled = False
    chat_messages = [ChatMessage(role="user", content="hello")]
    raw_request = MagicMock()
    raw_request.app.state.compact_orchestrator = MagicMock()
    service = MagicMock()

    captured = {}

    async def fake_chat_stream(messages, **kwargs):
        captured["messages"] = list(messages)
        yield {"content": "ok"}

    service.chat_stream_async = fake_chat_stream

    with patch("backend.api.chat._should_auto_compact_chat", return_value=False):
        asyncio.run(_drain(_generate_stream_response(
            service=service,
            messages=[Message(role="user", content="hello")],
            model="qwen",
            max_tokens=None,
            temperature=None,
            top_p=None,
            raw_request=raw_request,
            original_chat_messages=chat_messages,
        )))

    assert captured["messages"][0].role == "user"
    assert captured["messages"][0].content == "hello"


def test_chat_prompt_control_enabled_does_not_mutate_normal_prompt(monkeypatch):
    _set_compact_config()
    chat_messages = [ChatMessage(role="user", content="hello")]
    raw_request = MagicMock()
    raw_request.app.state.compact_orchestrator = MagicMock()
    service = MagicMock()

    captured = {}

    async def fake_chat_stream(messages, **kwargs):
        captured["messages"] = list(messages)
        yield {"content": "ok"}

    service.chat_stream_async = fake_chat_stream

    with patch("backend.api.chat._should_auto_compact_chat", return_value=False):
        asyncio.run(_drain(_generate_stream_response(
            service=service,
            messages=[Message(role="user", content="hello")],
            model="qwen",
            max_tokens=None,
            temperature=None,
            top_p=None,
            raw_request=raw_request,
            original_chat_messages=chat_messages,
        )))

    assert len(captured["messages"]) == 1
    assert captured["messages"][0].role == "user"
    assert captured["messages"][0].content == "hello"
