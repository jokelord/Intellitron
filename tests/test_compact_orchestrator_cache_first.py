from __future__ import annotations

import base64
import copy
import io

import pytest
from PIL import Image

from backend.compact.orchestrator import CompactOrchestrator
from backend.compact.types import CompactEventKind, CompactRequest
from backend.config import CompactConfig, CompactionPromptsConfig, get_config
from backend.llm_service import LLMService, Message, prepare_multimodal_messages


VALID = """<summary>
1. Primary Request and Intent:
   Plan work.
2. Key Technical Concepts:
   Prefix cache.
3. Files and Code Sections:
   backend/compact/orchestrator.py.
4. Errors and fixes:
   None.
5. Problem Solving:
   Fallback.
6. All user messages:
   User asked.
7. Pending Tasks:
   Implement.
8. Current Work:
   Tests.
9. Optional Next Step:
   Run pytest.
</summary>"""


class SequencedLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def chat_stream_async(
        self,
        messages,
        max_new_tokens,
        temperature,
        top_p,
        tools=None,
        request_id=None,
        extra_body=None,
        preserve_prompt_shape=False,
    ):
        self.calls.append(
            {
                "messages": list(messages),
                "extra_body": extra_body,
                "preserve_prompt_shape": preserve_prompt_shape,
            }
        )
        output = self.outputs.pop(0)
        yield {"content": output[: len(output) // 2]}
        yield {"content": output[len(output) // 2 :]}
        yield {"usage": {"prompt_tokens": 1000, "completion_tokens": 100, "cached_tokens": 900}}


class ExceptionThenValidLLM:
    def __init__(self):
        self.calls = []

    async def chat_stream_async(
        self,
        messages,
        max_new_tokens,
        temperature,
        top_p,
        tools=None,
        request_id=None,
        extra_body=None,
    ):
        self.calls.append({"messages": list(messages), "extra_body": extra_body})
        if len(self.calls) == 1:
            yield {"content": "hidden cache-first partial"}
            raise ValueError("bad cache-first prompt shape")
        yield {"content": VALID}
        yield {"usage": {"prompt_tokens": 200, "completion_tokens": 100, "cached_tokens": 0}}


def prompts():
    return CompactionPromptsConfig(
        no_tools_preamble="PREAMBLE",
        main_prompt="MAIN",
        no_tools_trailer="TRAILER",
        compact_user_instruction="Summarize messages that follow.",
        compact_control_user_instruction="[[SENTINEL]] summarize prior messages.",
    )


def cfg(shape="cache_first_tail_with_fallback"):
    return CompactConfig(
        prompt_shape=shape,
        prompt_control_enabled=True,
        reliable_fallback_enabled=True,
        validation_min_summary_chars=20,
        validation_min_summary_prompt_ratio=0.0,
        validation_require_summary_sections=True,
        max_summary_tokens=2000,
        temperature=0.3,
        top_p=0.9,
    )


class CharCounter:
    def count_messages(self, messages, image_count: int = 0) -> int:
        return sum(len(str(getattr(message, "content", ""))) for message in messages)


async def collect(agen):
    out = []
    async for event in agen:
        out.append(event)
    return out


@pytest.mark.asyncio
async def test_cache_first_invalid_output_is_not_streamed_and_falls_back():
    llm = SequencedLLM(["<action>switch_agent</action><target>coordinator_2</target>", VALID])
    applied = []
    orch = CompactOrchestrator(llm, cfg(), prompts(), apply_compaction=lambda req, sid, text: applied.append(text))
    req = CompactRequest(
        mode="agent",
        body_messages=[Message(role="system", content="COMPACT"), Message(role="user", content="history")],
        cache_first_body_messages=[Message(role="system", content="ACTIVE"), Message(role="user", content="history")],
        reference_messages=[Message(role="system", content="ACTIVE"), Message(role="user", content="history")],
        trigger="manual",
    )

    events = await collect(orch.run_compact(req))

    assert len(llm.calls) == 2
    assert all("switch_agent" not in (event.text or "") for event in events)
    assert [event.kind for event in events].count(CompactEventKind.COMPLETED) == 1
    assert len(applied) == 1
    assert "Primary Request" in applied[0]


@pytest.mark.asyncio
async def test_cache_first_valid_output_applies_without_fallback():
    llm = SequencedLLM([VALID])
    applied = []
    orch = CompactOrchestrator(llm, cfg(), prompts(), apply_compaction=lambda req, sid, text: applied.append(text))
    req = CompactRequest(
        mode="agent",
        body_messages=[Message(role="system", content="COMPACT"), Message(role="user", content="history")],
        cache_first_body_messages=[Message(role="system", content="ACTIVE"), Message(role="user", content="history")],
        reference_messages=[Message(role="system", content="ACTIVE"), Message(role="user", content="history")],
        trigger="manual",
    )

    events = await collect(orch.run_compact(req))

    assert len(llm.calls) == 1
    assert any(event.kind == CompactEventKind.COMPLETED for event in events)
    completed = [event for event in events if event.kind == CompactEventKind.COMPLETED][0]
    assert completed.metadata["prompt_shape"] == "cache_first_tail"
    assert completed.metadata["fallback_used"] is False
    assert completed.metadata["same_as_reference"] is False
    assert completed.metadata["first_divergence_char"] is not None
    assert len(applied) == 1


@pytest.mark.asyncio
async def test_cache_first_preserves_image_bearing_source_shape():
    llm = SequencedLLM([VALID])
    orch = CompactOrchestrator(llm, cfg(), prompts())
    image = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}
    cache_source = [
        Message(role="system", content="ACTIVE"),
        Message(role="user", content=[image, {"type": "text", "text": "history"}]),
    ]
    original_cache_source = copy.deepcopy(cache_source)
    req = CompactRequest(
        mode="agent",
        body_messages=[Message(role="system", content="COMPACT"), Message(role="user", content="history")],
        cache_first_body_messages=cache_source,
        reference_messages=cache_source,
        trigger="manual",
    )

    await collect(orch.run_compact(req))

    assert llm.calls[0]["preserve_prompt_shape"] is True
    assert llm.calls[0]["messages"][:-1] == cache_source
    assert cache_source == original_cache_source
    assert cache_source[1].content[0]["image_url"]["url"] == "data:image/jpeg;base64,AAAA"


@pytest.mark.asyncio
async def test_cache_first_bypasses_a_second_valid_oversized_image_preparation(monkeypatch):
    class RecordingProvider:
        def __init__(self):
            self.calls = []

        def is_loaded(self):
            return True

        def generate(self, messages, stream=False, **kwargs):
            self.calls.append(list(messages))
            if stream:
                return iter([{"content": VALID}])
            return object()

    config = get_config()
    monkeypatch.setattr(config.model, "max_image_dimension", 8)
    source_image = Image.new("RGB", (16, 16), "red")
    image_buffer = io.BytesIO()
    source_image.save(image_buffer, format="JPEG")
    original_url = "data:image/jpeg;base64," + base64.b64encode(image_buffer.getvalue()).decode()
    source = [
        Message(role="system", content="ACTIVE"),
        Message(
            role="user",
            content=[{"type": "image_url", "image_url": {"url": original_url}}],
        )
    ]
    provider = RecordingProvider()
    service = LLMService(config=config)
    service._provider = provider
    real_prepare = prepare_multimodal_messages
    prepare_calls = []

    def record_prepare(*args, **kwargs):
        prepare_calls.append(args[0])
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr("backend.llm_service.prepare_multimodal_messages", record_prepare)
    service.chat(source)
    prepared_source = provider.calls[0]
    prepared_url = prepared_source[1].content[0]["image_url"]["url"]
    orch = CompactOrchestrator(service, cfg(), prompts())
    req = CompactRequest(
        mode="agent",
        body_messages=[Message(role="system", content="COMPACT"), Message(role="user", content="history")],
        cache_first_body_messages=prepared_source,
        reference_messages=prepared_source,
        trigger="manual",
    )

    await collect(orch.run_compact(req))

    assert prepared_url != original_url
    assert provider.calls[1][:-1] == prepared_source
    assert provider.calls[1][1].content[0]["image_url"]["url"] == prepared_url
    assert len(prepare_calls) == 1
    assert service._stream_usage_requests == {}
    assert not hasattr(service, "_last_stream_request")


@pytest.mark.asyncio
async def test_reliable_fallback_normalizes_deterministically_through_llm_service(monkeypatch):
    class RecordingProvider:
        def __init__(self):
            self.calls = []

        def is_loaded(self):
            return True

        def generate(self, messages, stream=False, preserve_prompt_shape=False, **kwargs):
            self.calls.append(
                {
                    "messages": list(messages),
                    "preserve_prompt_shape": preserve_prompt_shape,
                }
            )
            return iter([{"content": "bad" if preserve_prompt_shape else VALID}])

    config = get_config()
    monkeypatch.setattr(config.model, "max_image_dimension", 8)
    source_image = Image.new("RGB", (16, 16), "red")
    image_buffer = io.BytesIO()
    source_image.save(image_buffer, format="JPEG")
    original_url = "data:image/jpeg;base64," + base64.b64encode(image_buffer.getvalue()).decode()
    source = [
        Message(role="system", content="COMPACT"),
        Message(
            role="user",
            content=[{"type": "image_url", "image_url": {"url": original_url}}],
        ),
    ]
    original_source = copy.deepcopy(source)
    provider = RecordingProvider()
    service = LLMService(config=config)
    service._provider = provider
    orch = CompactOrchestrator(service, cfg(), prompts())

    for _ in range(2):
        req = CompactRequest(
            mode="agent",
            body_messages=source,
            cache_first_body_messages=source,
            reference_messages=source,
            trigger="manual",
        )
        await collect(orch.run_compact(req))

    def image_url(call):
        image_message = next(
            message for message in call["messages"] if isinstance(message.content, list)
        )
        return image_message.content[0]["image_url"]["url"]

    assert [call["preserve_prompt_shape"] for call in provider.calls] == [
        True,
        False,
        True,
        False,
    ]
    assert image_url(provider.calls[0]) == original_url
    assert image_url(provider.calls[2]) == original_url
    normalized_urls = [image_url(provider.calls[1]), image_url(provider.calls[3])]
    assert normalized_urls[0] == normalized_urls[1]
    assert normalized_urls[0] != original_url
    assert source == original_source
    assert source[1].content[0]["image_url"]["url"] == original_url


@pytest.mark.asyncio
async def test_reliable_default_keeps_streaming_delta_behavior():
    llm = SequencedLLM([VALID])
    orch = CompactOrchestrator(llm, cfg("reliable_position1"), prompts())
    req = CompactRequest(
        mode="agent",
        body_messages=[Message(role="system", content="COMPACT"), Message(role="user", content="history")],
        trigger="manual",
    )

    events = await collect(orch.run_compact(req))

    assert len(llm.calls) == 1
    assert any(event.kind == CompactEventKind.DELTA for event in events)
    assert any(event.kind == CompactEventKind.COMPLETED for event in events)


@pytest.mark.asyncio
async def test_compact_llm_call_passes_configured_extra_body():
    llm = SequencedLLM([VALID])
    compact_cfg = cfg()
    compact_cfg.extra_body = {"chat_template_kwargs": {"enable_thinking": False}}
    orch = CompactOrchestrator(llm, compact_cfg, prompts())
    req = CompactRequest(
        mode="agent",
        body_messages=[Message(role="system", content="COMPACT"), Message(role="user", content="history")],
        cache_first_body_messages=[Message(role="system", content="ACTIVE"), Message(role="user", content="history")],
        trigger="manual",
    )

    events = await collect(orch.run_compact(req))

    assert any(event.kind == CompactEventKind.COMPLETED for event in events)
    assert llm.calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.asyncio
async def test_cache_first_is_attempted_when_untrimmed_source_exceeds_budget(monkeypatch):
    from backend.compact import source_budget as source_budget_module

    monkeypatch.setattr(source_budget_module.TokenCounter, "get", staticmethod(lambda: CharCounter()))
    llm = SequencedLLM([VALID])
    compact_cfg = cfg()
    compact_cfg.source_token_budget = 55
    orch = CompactOrchestrator(llm, compact_cfg, prompts())
    req = CompactRequest(
        mode="agent",
        body_messages=[
            Message(role="system", content="COMPACT"),
            Message(role="user", content="old user message that should be removed"),
            Message(role="assistant", content="old assistant message that should be removed"),
            Message(role="user", content="recent user"),
            Message(role="assistant", content="recent assistant"),
        ],
        cache_first_body_messages=[
            Message(role="system", content="ACTIVE"),
            Message(role="user", content="old user message that should be removed"),
            Message(role="assistant", content="old assistant message that should be removed"),
            Message(role="user", content="recent user"),
            Message(role="assistant", content="recent assistant"),
        ],
        reference_messages=[
            Message(role="system", content="ACTIVE"),
            Message(role="user", content="old user message that should be removed"),
            Message(role="assistant", content="old assistant message that should be removed"),
            Message(role="user", content="recent user"),
            Message(role="assistant", content="recent assistant"),
        ],
        trigger="auto",
    )

    events = await collect(orch.run_compact(req))

    assert any(event.kind == CompactEventKind.COMPLETED for event in events)
    assert len(llm.calls) == 1

    cache_first_contents = [message.content for message in llm.calls[0]["messages"]]
    assert "old user message that should be removed" in cache_first_contents
    assert "old assistant message that should be removed" in cache_first_contents
    assert "recent user" in cache_first_contents
    assert "recent assistant" in cache_first_contents

    completed = [event for event in events if event.kind == CompactEventKind.COMPLETED][0]
    assert completed.metadata["prompt_shape"] == "cache_first_tail"
    assert completed.metadata["fallback_used"] is False
    assert "cache_first_skipped_reason" not in completed.metadata


@pytest.mark.asyncio
async def test_cache_first_attempt_preserves_oversized_recent_message(monkeypatch):
    from backend.compact import source_budget as source_budget_module

    monkeypatch.setattr(source_budget_module.TokenCounter, "get", staticmethod(lambda: CharCounter()))
    llm = SequencedLLM([VALID])
    compact_cfg = cfg()
    compact_cfg.source_token_budget = 20
    huge_recent_message = "x" * 80
    orch = CompactOrchestrator(llm, compact_cfg, prompts())
    req = CompactRequest(
        mode="agent",
        body_messages=[
            Message(role="system", content="COMPACT"),
            Message(role="user", content=huge_recent_message),
        ],
        cache_first_body_messages=[
            Message(role="system", content="ACTIVE"),
            Message(role="user", content=huge_recent_message),
        ],
        reference_messages=[
            Message(role="system", content="ACTIVE"),
            Message(role="user", content=huge_recent_message),
        ],
        trigger="auto",
    )

    events = await collect(orch.run_compact(req))

    assert len(llm.calls) == 1
    cache_first_contents = [message.content for message in llm.calls[0]["messages"]]
    assert huge_recent_message in cache_first_contents
    completed = [event for event in events if event.kind == CompactEventKind.COMPLETED][0]
    assert completed.metadata["prompt_shape"] == "cache_first_tail"
    assert completed.metadata["fallback_used"] is False


@pytest.mark.asyncio
async def test_over_budget_cache_first_falls_back_to_budgeted_reliable_when_invalid(monkeypatch):
    from backend.compact import source_budget as source_budget_module

    monkeypatch.setattr(source_budget_module.TokenCounter, "get", staticmethod(lambda: CharCounter()))
    huge_recent_message = "x" * 80
    llm = SequencedLLM(["bad", VALID])
    compact_cfg = cfg()
    compact_cfg.source_token_budget = 20
    compact_cfg.validation_min_summary_chars = 20
    orch = CompactOrchestrator(llm, compact_cfg, prompts())
    req = CompactRequest(
        mode="agent",
        body_messages=[
            Message(role="system", content="COMPACT"),
            Message(role="user", content=huge_recent_message),
        ],
        cache_first_body_messages=[
            Message(role="system", content="ACTIVE"),
            Message(role="user", content=huge_recent_message),
        ],
        reference_messages=[
            Message(role="system", content="ACTIVE"),
            Message(role="user", content=huge_recent_message),
        ],
        trigger="auto",
    )

    events = await collect(orch.run_compact(req))

    assert len(llm.calls) == 2
    cache_first_contents = [message.content for message in llm.calls[0]["messages"]]
    reliable_contents = [message.content for message in llm.calls[1]["messages"]]
    assert huge_recent_message in cache_first_contents
    assert huge_recent_message not in reliable_contents
    assert llm.calls[0]["preserve_prompt_shape"] is True
    assert llm.calls[1]["preserve_prompt_shape"] is False
    completed = [event for event in events if event.kind == CompactEventKind.COMPLETED][0]
    assert completed.metadata["prompt_shape"] == "reliable_position1"
    assert completed.metadata["fallback_used"] is True


@pytest.mark.asyncio
async def test_compact_omits_preservation_policy_when_signature_inspection_fails(monkeypatch):
    class OpaqueSignatureLLM:
        async def chat_stream_async(
            self,
            messages,
            max_new_tokens,
            temperature,
            top_p,
            tools=None,
            request_id=None,
        ):
            yield {"content": VALID}

    monkeypatch.setattr("backend.compact.orchestrator.inspect.signature", lambda _: (_ for _ in ()).throw(ValueError()))
    orch = CompactOrchestrator(OpaqueSignatureLLM(), cfg(), prompts())
    req = CompactRequest(
        mode="agent",
        body_messages=[Message(role="system", content="COMPACT"), Message(role="user", content="history")],
        cache_first_body_messages=[Message(role="system", content="ACTIVE"), Message(role="user", content="history")],
        trigger="manual",
    )

    events = await collect(orch.run_compact(req))

    assert any(event.kind == CompactEventKind.COMPLETED for event in events)


@pytest.mark.asyncio
async def test_cache_first_exception_falls_back_to_reliable_without_streaming_partial_delta():
    llm = ExceptionThenValidLLM()
    orch = CompactOrchestrator(llm, cfg(), prompts())
    req = CompactRequest(
        mode="agent",
        body_messages=[Message(role="system", content="COMPACT"), Message(role="user", content="history")],
        cache_first_body_messages=[Message(role="system", content="ACTIVE"), Message(role="user", content="history")],
        reference_messages=[Message(role="system", content="ACTIVE"), Message(role="user", content="history")],
        trigger="manual",
    )

    events = await collect(orch.run_compact(req))

    assert len(llm.calls) == 2
    assert all("hidden cache-first partial" not in (event.text or "") for event in events)
    completed = [event for event in events if event.kind == CompactEventKind.COMPLETED][0]
    assert completed.metadata["prompt_shape"] == "reliable_position1"
    assert completed.metadata["fallback_used"] is True


@pytest.mark.asyncio
async def test_cache_first_invalid_output_fails_without_reliable_fallback():
    llm = SequencedLLM(["bad"])
    compact_cfg = cfg()
    compact_cfg.reliable_fallback_enabled = False
    compact_cfg.validation_min_summary_chars = 20
    orch = CompactOrchestrator(llm, compact_cfg, prompts())
    req = CompactRequest(
        mode="agent",
        body_messages=[Message(role="system", content="COMPACT"), Message(role="user", content="history")],
        cache_first_body_messages=[Message(role="system", content="ACTIVE"), Message(role="user", content="history")],
        reference_messages=[Message(role="system", content="ACTIVE"), Message(role="user", content="history")],
        trigger="manual",
    )

    events = await collect(orch.run_compact(req))

    assert len(llm.calls) == 1
    assert events[-1].kind == CompactEventKind.FAILED
    assert events[-1].metadata["reason"] == "too_short"
