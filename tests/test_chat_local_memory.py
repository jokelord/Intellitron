"""Ordinary Chat freezes local-memory tails independently from UI projections."""
import asyncio
import base64
import io
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from backend.api.chat import ChatCompletionRequest, create_chat_completion, router
from backend.api.chat_session_store import ChatSessionStore
from backend.config import get_config
from backend.llm_service import GenerationResult, abort_request
from backend.compact.types import CompactEvent
from backend.request_lifecycle import RequestLifecycleRegistry
from backend.retrieval.contracts import PreparedUserTurn
from backend.session_hub_runtime import SessionHubRuntime

RAW = "Use my deployment format."
SUFFIX = '\n\n[reference]\n{"text":"version, flags, checks"}'
QUOTED = "version, flags, checks"


class Memory:
    def __init__(self):
        self.calls = []
    async def prepare_user_turn(self, raw, *, enabled, **kwargs):
        self.calls.append((raw, enabled, kwargs))
        if not enabled or not raw.strip():
            return PreparedUserTurn(raw, raw, "disabled")
        return PreparedUserTurn(raw, raw + SUFFIX, "matched", memory_texts=(QUOTED,))


class Service:
    is_ready = True
    active_backend = "sglang"
    def __init__(self):
        self.config = deepcopy(get_config())
        self.calls = []
    async def chat_stream_async(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        yield {"content": "Answer."}
    async def chat_async(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return GenerationResult(text="Answer.")


def app_for(memory):
    app = FastAPI()
    app.include_router(router)
    from backend.api.session_hub import router as hub_router
    from backend.api.compact import router as compact_router
    app.include_router(hub_router)
    app.include_router(compact_router)
    app.state.request_lifecycle_registry = RequestLifecycleRegistry()
    app.state.session_hub_runtime = SessionHubRuntime()
    app.state.chat_session_store = ChatSessionStore()
    app.state.compact_orchestrator = None
    app.state.local_memory_service = memory
    return app


def wire(sid, stream=True, enabled=True, messages=None):
    return {"session_id": sid, "request_id": str(uuid4()), "stream": stream,
        "enable_local_memory": enabled, "messages": messages or [
            {"role": "system", "content": "Stable system."}, {"role": "user", "content": RAW}]}


@pytest.mark.parametrize("value", ["true", "false", 0, 1, None, [], {}])
def test_chat_memory_flag_is_strict_boolean(value):
    with pytest.raises(ValidationError):
        ChatCompletionRequest(messages=[], enable_local_memory=value)


@pytest.mark.parametrize("stream", [False, True])
def test_chat_injects_once_keeps_raw_bubble_and_frozen_historical_ref(stream):
    memory, service = Memory(), Service()
    app = app_for(memory)
    sid = "chat-memory-" + uuid4().hex
    first = wire(sid, stream)
    with patch("backend.api.chat.get_llm_service", return_value=service), TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=first)
        assert response.status_code == 200, response.text
        assert service.calls[0]["messages"][-1].content == RAW + SUFFIX
        assert service.calls[0]["messages"][0].content == "Stable system."
        assert len(memory.calls) == 1
        record = app.state.chat_session_store.get(sid)
        events = record.hub.find_payloads_by_fields({"type": "timeline_event"})
        started = [r.payload["event"] for r in events if r.payload["event"]["kind"] == "turn_started"]
        retrieved = [r.payload["event"] for r in events if r.payload["event"]["kind"] == "local_memory_retrieved"]
        assert started[0]["user_content"] == RAW
        assert len(retrieved) == 1
        assert retrieved[0]["chunks"] == [{"text": QUOTED}]
        assert retrieved[0]["turn_index"] == 1
        assert client.post("/v1/chat/completions", json=first).status_code == 200
        assert len(memory.calls) == 1
        assert client.post("/v1/chat/completions", json={**first, "enable_local_memory": False}).status_code == 409
        second = wire(sid, stream, False, [first["messages"][0],
            {"role": "user", "content": RAW, "metadata": {"chat_turn_ref": f"chat:{sid}:1:user"}},
            {"role": "assistant", "content": "Answer."}, {"role": "user", "content": RAW}])
        assert client.post("/v1/chat/completions", json=second).status_code == 200
        assert service.calls[-1]["messages"][1].content == RAW + SUFFIX
        assert service.calls[-1]["messages"][-1].content == RAW
        replay = client.get(f"/v1/session/{sid}/events")
        assert replay.status_code == 200
        assert replay.text.count('"kind": "local_memory_retrieved"') == 1
        assert app.state.chat_session_store.pop(sid) is record
        assert not record.local_memory_source_turns()
        assert not record.local_memory_bootstrap_snapshot()


@pytest.mark.parametrize("stream", [False, True])
def test_chat_missing_or_duplicate_ref_does_not_silently_lose_accepted_tail(stream):
    memory, service = Memory(), Service()
    app, sid = app_for(memory), "chat-ref-" + uuid4().hex
    with patch("backend.api.chat.get_llm_service", return_value=service), TestClient(app) as client:
        assert client.post("/v1/chat/completions", json=wire(sid, stream)).status_code == 200
        history = [{"role": "user", "content": RAW}, {"role": "assistant", "content": "Answer."},
            {"role": "user", "content": "Next question."}]
        assert client.post("/v1/chat/completions", json=wire(sid, stream, False, history)).status_code == 409
        ref = {"chat_turn_ref": f"chat:{sid}:1:user"}
        history[0]["metadata"] = ref
        history.insert(1, deepcopy(history[0]))
        assert client.post("/v1/chat/completions", json=wire(sid, stream, False, history)).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_pending_stop_during_chat_lookup_prevents_model_and_canonical_commit(stream):
    memory, service = Memory(), Service()
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(raw, **kwargs):
        entered.set()
        await release.wait()
        return PreparedUserTurn(raw, raw + SUFFIX, "matched", memory_texts=(QUOTED,))
    memory.prepare_user_turn = blocked
    app, sid = app_for(memory), "chat-stop-" + uuid4().hex
    payload = wire(sid, stream)
    raw_request = Request({"type": "http", "app": app, "method": "POST", "path": "/v1/chat/completions", "headers": []})
    with patch("backend.api.chat.get_llm_service", return_value=service):
        call = asyncio.create_task(create_chat_completion(ChatCompletionRequest(**payload), raw_request))
        if stream:
            response = await call
        await asyncio.wait_for(entered.wait(), 2)
        abort_request(payload["request_id"])
        release.set()
        if stream:
            _ = [line async for line in response.body_iterator]
        else:
            await call
    assert not service.calls
    record = app.state.chat_session_store.get(sid)
    assert not [r for r in record.hub.find_payloads_by_fields({"type": "timeline_event"})
                if r.payload["event"]["kind"] == "local_memory_retrieved"]


@pytest.mark.parametrize("stream", [False, True])
def test_compact_uses_accepted_memory_and_commits_exact_replacement_boundary(stream):
    memory, service = Memory(), Service()
    app, sid = app_for(memory), "chat-compact-" + uuid4().hex
    captured = []
    class Compact:
        async def run_compact(self, req):
            captured.append(req)
            yield CompactEvent.completed("summary-local-memory", "Summary.")
    app.state.compact_orchestrator = Compact()
    with patch("backend.api.chat.get_llm_service", return_value=service), TestClient(app) as client:
        assert client.post("/v1/chat/completions", json=wire(sid, stream)).status_code == 200
        historical = {"role": "user", "content": RAW, "metadata": {"chat_turn_ref": f"chat:{sid}:1:user"}}
        compact = client.post("/v1/compact/run", json={"mode": "chat", "session_id": sid,
            "messages": [{"role": "system", "content": "Stable system."}, historical,
                         {"role": "assistant", "content": "Answer."}]})
        assert compact.status_code == 200, compact.text
        assert captured[0].body_messages[1].content == RAW + SUFFIX
        missing = wire(sid, stream, False, [{"role": "user", "content": "Unrelated new question."}])
        assert client.post("/v1/chat/completions", json=missing).status_code == 409
        compacted = wire(sid, stream, False, [
            {"role": "system", "content": "Stable system."},
            {**historical, "metadata": {**historical["metadata"], "pre_compact": True}},
            {"role": "system", "content": "Summary.", "metadata": {
                "compact_summary": True, "summary_id": "summary-local-memory"}},
            {"role": "user", "content": "New question."}])
        assert client.post("/v1/chat/completions", json=compacted).status_code == 200
        assert not any(SUFFIX in str(message.content) for message in service.calls[-1]["messages"])
        resurrect = deepcopy(compacted)
        resurrect["request_id"] = str(uuid4())
        resurrect["messages"].insert(-1, historical)
        assert client.post("/v1/chat/completions", json=resurrect).status_code == 409


@pytest.mark.parametrize("frames", [[{}], [{"content": "", "usage": {"prompt_tokens": 1}}], []])
def test_valid_empty_chat_stream_keeps_accepted_tail(frames):
    memory, service = Memory(), Service()
    async def empty(**kwargs):
        service.calls.append(deepcopy(kwargs))
        for frame in frames:
            yield frame
    service.chat_stream_async = empty
    app, sid = app_for(memory), "chat-empty-" + uuid4().hex
    with patch("backend.api.chat.get_llm_service", return_value=service), TestClient(app) as client:
        assert client.post("/v1/chat/completions", json=wire(sid)).status_code == 200
        following = wire(sid, True, False, [
            {"role": "user", "content": RAW, "metadata": {"chat_turn_ref": f"chat:{sid}:1:user"}},
            {"role": "assistant", "content": ""}, {"role": "user", "content": "Next."}])
        assert client.post("/v1/chat/completions", json=following).status_code == 200
        assert service.calls[-1]["messages"][0].content == RAW + SUFFIX


def image_url():
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (48, 32), "blue").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


@pytest.mark.parametrize("stream", [False, True])
def test_chat_multimodal_preserves_parts_resizes_before_model_and_keeps_image_only_raw(stream):
    from PIL import Image
    memory, service = Memory(), Service()
    service.config.model.max_image_dimension = 16
    app, sid = app_for(memory), "chat-image-" + uuid4().hex
    parts = [{"type": "image_url", "image_url": {"url": image_url()}}, {"type": "text", "text": RAW}]
    with patch("backend.api.chat.get_llm_service", return_value=service), TestClient(app) as client:
        assert client.post("/v1/chat/completions", json=wire(sid, stream, True,
            [{"role": "user", "content": parts}])).status_code == 200
        sent = service.calls[-1]["messages"][-1].content
        assert sent[1] == {"type": "text", "text": RAW + SUFFIX}
        with Image.open(io.BytesIO(base64.b64decode(sent[0]["image_url"]["url"].split(",", 1)[1]))) as image:
            assert max(image.size) == 16
        only_image = wire("chat-image-only-" + uuid4().hex, stream, True,
            [{"role": "user", "content": [parts[0]]}])
        assert client.post("/v1/chat/completions", json=only_image).status_code == 200
        assert service.calls[-1]["messages"][-1].content[0]["type"] == "image_url"
        assert len(service.calls[-1]["messages"][-1].content) == 1


@pytest.mark.asyncio
async def test_chat_async_forwards_optional_request_id_to_real_worker():
    from backend.llm_service import LLMService
    service = object.__new__(LLMService)
    captured = []
    def chat(**kwargs):
        captured.append(kwargs)
        return GenerationResult(text="ok")
    service.chat = chat
    await service.chat_async(messages=[], request_id="sync-request-id", preserve_prompt_shape=True)
    assert captured[0]["request_id"] == "sync-request-id"
    assert captured[0]["preserve_prompt_shape"] is True


def test_vision_omission_then_hub_replay_retains_frozen_memory_and_canonical_placeholder():
    memory, service = Memory(), Service()
    app, sid = app_for(memory), "chat-vision-" + uuid4().hex
    original = [{"type": "image_url", "image_url": {"url": image_url()}}, {"type": "text", "text": RAW}]
    placeholder = "[historical images omitted: count=1; sources=camera_1; relative_range=0.00..0.00s]"
    ref = {"chat_turn_ref": f"chat:{sid}:1:user"}
    metadata = {"history_image_policy": "aggregate_prior_image_blocks", "manual_image_count": 0,
        "source_count": 0, "retained_tick_count": 0, "binding": {"page_generation": 7,
        "mode": "chat", "top_level_context_id": sid, "automation_task_id": "task_01J0123456789ABCDEFGH",
        "capture_cycle_id": "cycle_01J0123456789ABCDEFG"}}
    with patch("backend.api.chat.get_llm_service", return_value=service), TestClient(app) as client:
        assert client.post("/v1/chat/completions", json=wire(sid, True, True,
            [{"role": "user", "content": original}])).status_code == 200
        second = wire(sid, True, False, [{"role": "user", "metadata": ref, "content": [
            {"type": "text", "text": placeholder}, {"type": "text", "text": RAW}]},
            {"role": "assistant", "content": "Answer."}, {"role": "user", "content": "Next."}])
        second["vision_automation"] = metadata
        assert client.post("/v1/chat/completions", json=second).status_code == 200
        canonical = deepcopy(service.calls[-1]["messages"][0].content)
        replay = wire(sid, True, False, [{"role": "user", "metadata": ref, "content": [
            {"type": "text", "text": RAW}, {"type": "text", "text":
             "[historical images omitted: count=1; sources=none; relative_range=unavailable]"}]},
            {"role": "assistant", "content": "Answer."}, {"role": "user", "content": "Next."},
            {"role": "assistant", "content": "Answer."}, {"role": "user", "content": "After reload."}])
        response = client.post("/v1/chat/completions", json=replay)
        assert response.status_code == 200, response.text
        assert service.calls[-1]["messages"][0].content == canonical


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("first_role", ["user", "assistant"])
def test_chat_history_window_anchor_is_validated_and_echoed_without_entering_prompt(stream, first_role):
    memory, service = Memory(), Service()
    app, sid = app_for(memory), "chat-anchor-" + uuid4().hex
    with patch("backend.api.chat.get_llm_service", return_value=service), TestClient(app) as client:
        assert client.post("/v1/chat/completions", json=wire(sid, stream, False)).status_code == 200
        anchor = f"chat:{sid}:1:{first_role}"
        previous = [{"role": first_role, "content": RAW if first_role == "user" else "Answer.",
            "metadata": {"chat_turn_ref" if first_role == "user" else "chat_message_ref": anchor}}]
        if first_role == "user":
            previous.append({"role": "assistant", "content": "Answer."})
        next_request = wire(sid, stream, True, [*previous, {"role": "user", "content": "Next."}])
        next_request["chat_history_start_ref"] = anchor
        assert client.post("/v1/chat/completions", json=next_request).status_code == 200
        record = app.state.chat_session_store.get(sid)
        retrieved = [r.payload["event"] for r in record.hub.find_payloads_by_fields({"type": "timeline_event"})
            if r.payload["event"]["kind"] == "local_memory_retrieved"]
        assert retrieved[0]["history_start_ref"] == anchor
        assert "chat_history_start_ref" not in str(service.calls[-1]["messages"])
        invalid = deepcopy(next_request)
        invalid["request_id"] = str(uuid4())
        invalid["chat_history_start_ref"] = f"chat:foreign-session:1:{first_role}"
        assert client.post("/v1/chat/completions", json=invalid).status_code == 409
        assert len(memory.calls) == 2


def test_unidentifiable_historical_window_skips_enrichment_without_rewriting_legacy_history():
    memory, service = Memory(), Service()
    app, sid = app_for(memory), "chat-unanchored-" + uuid4().hex
    history = [{"role": "user", "content": "Old unreferenced user."},
        {"role": "assistant", "content": "Old answer."}, {"role": "user", "content": RAW}]
    with patch("backend.api.chat.get_llm_service", return_value=service), TestClient(app) as client:
        assert client.post("/v1/chat/completions", json=wire(sid, True, True, history)).status_code == 200
        assert not memory.calls
        assert [message.content for message in service.calls[0]["messages"]] == [item["content"] for item in history]


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_first_chat_bootstrap_history_can_retrieve_with_opaque_window_anchor(role):
    memory, service = Memory(), Service()
    app, sid = app_for(memory), "chat-bootstrap-" + uuid4().hex
    anchor = f"chat:{sid}:bootstrap:old-ui-message:{role}"
    history = [{"role": role, "content": "Imported raw history.", "metadata": {"chat_message_ref": anchor}},
        {"role": "user", "content": RAW}]
    request = {**wire(sid, True, True, history), "chat_history_start_ref": anchor}
    with patch("backend.api.chat.get_llm_service", return_value=service), TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=request)
        assert response.status_code == 200
        assert service.calls[0]["messages"][0].content == "Imported raw history."
        assert service.calls[0]["messages"][-1].content == RAW + SUFFIX
        assert len(memory.calls) == 1
        assert anchor in response.text


@pytest.mark.parametrize("backend", ["sglang", "vllm"])
@pytest.mark.parametrize("use_tilde", [False, True])
def test_chat_budget_uses_actual_configured_template_and_output_reserve(tmp_path, monkeypatch, backend, use_tilde):
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from transformers import PreTrainedTokenizerFast
    from backend.api.chat_local_memory import _context_validator
    from backend.compact.tokenizer import TokenCounter
    from backend.llm_service import Message
    engine = Tokenizer(WordLevel({"[UNK]": 0, "x": 1}, unk_token="[UNK]"))
    engine.pre_tokenizer = Whitespace()
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=engine, unk_token="[UNK]")
    tokenizer.chat_template = "{{ messages[-1].content }}"
    template = tmp_path / "serving.jinja"
    template.write_text("{{ 'x ' * 20 }}{% for n in range(5) %}{{ messages[-1].content }} {% endfor %}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    service = Service()
    service.active_backend = backend
    service.config.sglang.max_context_length = 60
    service.config.vllm.max_model_len = 60
    getattr(service.config, backend).chat_template = (
        f"~/{template.name}" if use_tilde else str(template)
    )
    monkeypatch.setattr(TokenCounter, "get", lambda: SimpleNamespace(
        _use_real_tokenizer=True, _tokenizer=tokenizer))
    fits = _context_validator(service, [Message(role="user", content="x")], None, 5, False)
    assert fits("x") is True
    assert fits("x " * 10) is False
    template.write_text(tokenizer.chat_template, encoding="utf-8")
    assert _context_validator(service, [Message(role="user", content="x")], None, 5, False)("x " * 10) is False


@pytest.mark.parametrize("stream", [False, True])
def test_preaccept_transport_failure_does_not_commit_memory_tail(stream):
    memory, service = Memory(), Service()
    async def fail_stream(**kwargs):
        raise RuntimeError("rejected before provider acceptance")
        yield {}
    async def fail_sync(**kwargs):
        raise RuntimeError("rejected before provider acceptance")
    service.chat_stream_async, service.chat_async = fail_stream, fail_sync
    app, sid = app_for(memory), "chat-failure-" + uuid4().hex
    with patch("backend.api.chat.get_llm_service", return_value=service), TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=wire(sid, stream))
        assert response.status_code == (200 if stream else 500)
        assert app.state.chat_session_store.get(sid).local_memory_source_turns() == frozenset()


@pytest.mark.parametrize("backend", ["sglang", "vllm"])
@pytest.mark.parametrize("stream", [False, True])
def test_real_provider_http_rejection_never_commits_chat_memory(monkeypatch, backend, stream):
    from backend.llm_service import LLMService, SGLangProvider, VLLMProvider
    class Rejected:
        status_code = 400
        text = "fixture rejection before inference"
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return self.text.encode()
        def json(self): return {"error": {"message": self.text}}
        def close(self): pass
    class Transport:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def stream(self, *args, **kwargs): return Rejected()
        def post(self, *args, **kwargs): return Rejected()
    monkeypatch.setattr("httpx.Client", Transport)
    memory = Memory()
    service = LLMService(deepcopy(get_config()))
    service._provider = (SGLangProvider if backend == "sglang" else VLLMProvider)(service.config)
    service._provider._loaded = True
    service._active_backend = backend
    app, sid = app_for(memory), "chat-http-reject-" + uuid4().hex
    with patch("backend.api.chat.get_llm_service", return_value=service), TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=wire(sid, stream))
        assert response.status_code == (200 if stream else 500)
        record = app.state.chat_session_store.get(sid)
        assert record.local_memory_source_turns() == frozenset()
        assert record.finish_reason == "error"


@pytest.mark.parametrize("stream", [False, True])
def test_registered_bootstrap_prefix_survives_hub_replay_without_display_rows(stream):
    memory, service = Memory(), Service()
    app, sid = app_for(memory), "chat-bootstrap-replay-" + uuid4().hex
    anchor = f"chat:{sid}:bootstrap:imported:user"
    prefix = [{"role": "system", "content": "Pinned system."},
        {"role": "user", "content": "Imported raw.", "metadata": {"chat_message_ref": anchor}},
        {"role": "assistant", "content": "Imported response."}]
    first = {**wire(sid, stream, True, prefix + [{"role": "user", "content": RAW}]), "chat_history_start_ref": anchor}
    with patch("backend.api.chat.get_llm_service", return_value=service), TestClient(app) as client:
        assert client.post("/v1/chat/completions", json=first).status_code == 200
        expected_prefix = deepcopy(service.calls[-1]["messages"])
        replayed = [{"role": "user", "content": RAW, "metadata": {"chat_turn_ref": f"chat:{sid}:1:user"}},
            {"role": "assistant", "content": "Answer.", "metadata": {"chat_message_ref": f"chat:{sid}:1:assistant"}},
            {"role": "user", "content": "Next."}]
        next_request = {**wire(sid, stream, False, replayed), "chat_history_start_ref": anchor}
        response = client.post("/v1/chat/completions", json=next_request)
        assert response.status_code == 200, response.text
        assert service.calls[-1]["messages"][:len(expected_prefix)] == expected_prefix
        assert service.calls[-1]["messages"][-1].content == "Next."
        forged = deepcopy(next_request)
        forged["request_id"] = str(uuid4())
        forged["messages"][0]["metadata"] = {"chat_turn_ref": "chat:foreign:1:user"}
        assert client.post("/v1/chat/completions", json=forged).status_code == 409


def test_bootstrap_replay_compact_uses_exact_prefix_then_retires_it():
    memory, service = Memory(), Service()
    app, sid = app_for(memory), "chat-bootstrap-compact-" + uuid4().hex
    anchor = f"chat:{sid}:bootstrap:imported:user"
    prefix = [{"role": "system", "content": "Pinned system."},
        {"role": "user", "content": "Imported raw.", "metadata": {"chat_message_ref": anchor}},
        {"role": "assistant", "content": "Imported response."}]
    captured = []
    class Compact:
        async def run_compact(self, req):
            captured.append(req)
            yield CompactEvent.completed("bootstrap-summary", "Summary.")
    app.state.compact_orchestrator = Compact()
    with patch("backend.api.chat.get_llm_service", return_value=service), TestClient(app) as client:
        first = {**wire(sid, True, True, prefix + [{"role": "user", "content": RAW}]), "chat_history_start_ref": anchor}
        assert client.post("/v1/chat/completions", json=first).status_code == 200
        source = [{"role": "user", "content": RAW, "metadata": {"chat_turn_ref": f"chat:{sid}:1:user"}},
            {"role": "assistant", "content": "Answer.", "metadata": {"chat_message_ref": f"chat:{sid}:1:assistant"}}]
        compact = client.post("/v1/compact/run", json={"mode": "chat", "session_id": sid,
            "messages": source, "chat_history_start_ref": anchor})
        assert compact.status_code == 200, compact.text
        assert [message.content for message in captured[0].cache_first_body_messages] == [
            "Pinned system.", "Imported raw.", "Imported response.", RAW + SUFFIX, "Answer."]
        missing = wire(sid, True, False, source + [{"role": "user", "content": "Next."}])
        assert client.post("/v1/chat/completions", json=missing).status_code == 409
        replaced = wire(sid, True, False, [{"role": "system", "content": "Summary.",
            "metadata": {"compact_summary": True, "summary_id": "bootstrap-summary"}},
            {"role": "user", "content": "Next."}])
        response = client.post("/v1/chat/completions", json=replaced)
        assert response.status_code == 200, response.text
        record = app.state.chat_session_store.get(sid)
        assert not record.local_memory_bootstrap_snapshot()
        assert not any("Imported" in str(message.content) or SUFFIX in str(message.content)
            for message in service.calls[-1]["messages"])
        resurrect = {**wire(sid, True, False, source + [{"role": "user", "content": "Return old?"}]),
            "chat_history_start_ref": anchor}
        assert client.post("/v1/chat/completions", json=resurrect).status_code == 409


def test_typed_sync_transport_error_does_not_commit_chat_tail():
    memory, service = Memory(), Service()
    async def rejected(**kwargs):
        return GenerationResult(text="error display", transport_error="upstream rejected")
    service.chat_async = rejected
    app, sid = app_for(memory), "chat-typed-error-" + uuid4().hex
    with patch("backend.api.chat.get_llm_service", return_value=service), TestClient(app) as client:
        assert client.post("/v1/chat/completions", json=wire(sid, False)).status_code == 500
        record = app.state.chat_session_store.get(sid)
        assert not record.local_memory_source_turns()
        assert record.finish_reason == "error"


def test_auto_compact_retires_bootstrap_projection_before_new_tail_commit():
    memory, service = Memory(), Service()
    app, sid = app_for(memory), "chat-bootstrap-auto-" + uuid4().hex
    anchor = f"chat:{sid}:bootstrap:imported:user"
    prefix = [{"role": "system", "content": "Pinned system."},
        {"role": "user", "content": "Imported.", "metadata": {"chat_message_ref": anchor}}]
    class Compact:
        async def run_compact(self, req):
            yield CompactEvent.completed("bootstrap-auto-summary", "Summary.")
    app.state.compact_orchestrator = Compact()
    with patch("backend.api.chat.get_llm_service", return_value=service), TestClient(app) as client:
        first = {**wire(sid, True, True, prefix + [{"role": "user", "content": RAW}]), "chat_history_start_ref": anchor}
        assert client.post("/v1/chat/completions", json=first).status_code == 200
        later = prefix + [{"role": "user", "content": RAW, "metadata": {"chat_turn_ref": f"chat:{sid}:1:user"}},
            {"role": "assistant", "content": "Answer."}, {"role": "user", "content": "New question."}]
        next_request = {**wire(sid, True, True, later), "chat_history_start_ref": anchor}
        with patch("backend.api.chat._should_auto_compact_chat", return_value=True):
            response = client.post("/v1/chat/completions", json=next_request)
        assert response.status_code == 200, response.text
        record = app.state.chat_session_store.get(sid)
        assert not record.local_memory_bootstrap_snapshot()
        assert record.local_memory_source_turns() == frozenset({2})
        assert service.calls[-1]["messages"][-1].content == "New question." + SUFFIX
        assert f'chat:{sid}:2:user' in response.text


@pytest.mark.parametrize("outcome", ["rejected", "stopped", "no_match", "unavailable", "off"])
def test_admitted_bootstrap_is_replayable_without_committing_failed_or_empty_memory(outcome):
    memory, service = Memory(), Service()
    app, sid = app_for(memory), "chat-bootstrap-admission-" + uuid4().hex
    anchor = f"chat:{sid}:bootstrap:imported:user"
    prefix = [{"role": "user", "content": "Imported.", "metadata": {"chat_message_ref": anchor}}]
    if outcome == "rejected":
        async def rejected(**kwargs):
            yield {"transport_error": "HTTP400", "content": "error"}
        service.chat_stream_async = rejected
    elif outcome == "stopped":
        async def stopped(raw, **kwargs):
            if not kwargs["enabled"]:
                return PreparedUserTurn(raw, raw, "disabled")
            abort_request(first["request_id"])
            return PreparedUserTurn(raw, raw + SUFFIX, "matched", memory_texts=(QUOTED,))
        memory.prepare_user_turn = stopped
    elif outcome == "no_match":
        async def no_match(raw, **kwargs):
            return PreparedUserTurn(raw, raw, "no_match")
        memory.prepare_user_turn = no_match
    elif outcome == "unavailable":
        app.state.local_memory_service = None
    with patch("backend.api.chat.get_llm_service", return_value=service), TestClient(app) as client:
        first = {**wire(sid, True, outcome != "off", prefix + [{"role": "user", "content": RAW}]),
            "chat_history_start_ref": anchor}
        assert client.post("/v1/chat/completions", json=first).status_code == 200
        record = app.state.chat_session_store.get(sid)
        assert not record.local_memory_source_turns()
        async def accepted(**kwargs):
            service.calls.append(deepcopy(kwargs))
            yield {"content": "Answer."}
        service.chat_stream_async = accepted
        replayed = [{"role": "user", "content": RAW, "metadata": {"chat_turn_ref": f"chat:{sid}:1:user"}},
            {"role": "assistant", "content": ""}, {"role": "user", "content": "Next."}]
        response = client.post("/v1/chat/completions", json={**wire(sid, True, False, replayed),
            "chat_history_start_ref": anchor})
        assert response.status_code == 200, response.text
        assert [message.content for message in service.calls[-1]["messages"]][:2] == ["Imported.", RAW]
        assert not record.local_memory_source_turns()
