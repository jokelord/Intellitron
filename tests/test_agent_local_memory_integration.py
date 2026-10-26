"""Local memory belongs to one admitted top-level user turn, never UI history."""
import asyncio
import json
import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from smolagents.memory import TaskStep

from backend.agent.agent_runner import AgentRunner
from backend.api.agent import AgentChatRequest, router
from backend.config import get_config
from backend.cycle.executor import CycleExecutor
from backend.cycle.models import CycleStartRequest, CycleStepRequest
from backend.llm_service import GenerationResult
from backend.request_lifecycle import RequestLifecycleRegistry
from backend.session_hub_runtime import SessionHubRuntime
from backend.retrieval.contracts import MemorySource


RAW = "Use our deployment format."
MEMORY_TEXT = "version, flags, checks"
ENRICHED = RAW + '\n\n[local reference]\n{"text":"version, flags, checks"}'


class MemoryService:
    def __init__(self):
        self.calls = []

    async def prepare_user_turn(self, raw, *, enabled, **kwargs):
        self.calls.append((raw, enabled, kwargs))
        return SimpleNamespace(raw_user_text=raw, model_user_content=ENRICHED if enabled else raw,
            status="matched" if enabled else "disabled",
            sources=(MemorySource("preferences.md", 3, 5),) if enabled else (),
            memory_texts=(MEMORY_TEXT,) if enabled else ())


def make_runner(profile="basic_agent", protocol="legacy_text"):
    cfg = deepcopy(get_config())
    cfg.model.model_type = "text"
    cfg.agent.entry_agent_name = profile
    cfg.agent.enabled_agent_profiles = ["coordinator_1", "coordinator_2", "basic_agent", "manager_agent"]
    cfg.inference.tool_protocol = protocol
    cfg.compact.enabled = False
    service = MagicMock(config=cfg, active_backend="sglang")
    service.get_stream_usage.return_value = {}

    def chat(**kwargs):
        if kwargs.get("stream"):
            return iter([{"content": "Saved answer."}])
        return GenerationResult(text="Saved answer.")

    service.chat.side_effect = chat
    runner = AgentRunner(service, cfg, session_hub_runtime=SessionHubRuntime())
    runner.local_memory_service = MemoryService()
    return runner, service


def app_for(runner, registry):
    app = FastAPI()
    app.include_router(router)
    from backend.api.session_hub import router as hub_router
    app.include_router(hub_router)
    app.state.agent_runner = runner
    app.state.request_lifecycle_registry = registry
    app.state.session_hub_runtime = runner.session_hub_runtime
    return app


@pytest.mark.parametrize("value", ["true", "false", 0, 1, None, [], {}])
@pytest.mark.parametrize("model,payload", [(AgentChatRequest, {"message": RAW}),
                                         (CycleStartRequest, {"steps": []})])
def test_wire_flag_rejects_non_boolean_values(model, payload, value):
    with pytest.raises(ValidationError):
        model(**payload, enable_local_memory=value)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("registry_enabled", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_public_api_forwards_frozen_flag_and_keeps_user_display_raw(stream, registry_enabled, enabled):
    runner, service = make_runner()
    registry = RequestLifecycleRegistry() if registry_enabled else None
    with TestClient(app_for(runner, registry)) as client:
        response = client.post("/v1/agent/chat", json={"session_id": "123e4567-e89b-12d3-a456-426614174110", "message": RAW,
            "stream": stream, "enable_local_memory": enabled})
    assert response.status_code == 200, response.text
    assert runner.local_memory_service.calls[0][:2] == (RAW, enabled)
    assert len(runner.local_memory_service.calls) == 1
    session = runner.sessions.get("123e4567-e89b-12d3-a456-426614174110")
    tasks = [s.task for s in session.agent.memory.steps if isinstance(s, TaskStep)]
    assert tasks == [ENRICHED if enabled else RAW]
    if registry_enabled and stream:
        assert ENRICHED not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("profile", ["basic_agent", "manager_agent", "coordinator_1"])
@pytest.mark.parametrize("protocol", ["legacy_text", "qwen35-native"])
async def test_runner_retains_exact_prepared_tail_for_profiles_and_next_turn(stream, profile, protocol):
    runner, _ = make_runner(profile, protocol)
    if stream:
        _ = [e async for e in runner.run_stream("123e4567-e89b-12d3-a456-426614174113", RAW, enable_local_memory=True)]
    else:
        await runner.run_sync("123e4567-e89b-12d3-a456-426614174113", RAW, enable_local_memory=True)
    session = runner.sessions.get("123e4567-e89b-12d3-a456-426614174113")
    tasks = [s.task for s in session.agent.memory.steps if isinstance(s, TaskStep)]
    assert len(tasks) == 1
    assert ENRICHED in tasks[0]
    if profile != "coordinator_1" or protocol == "legacy_text":
        assert tasks[0] == ENRICHED
    frozen = tasks[0]
    # Public producer admission awaits the coordinator's owned worker cleanup
    # before admitting the next turn; direct runner callers do the same here.
    await asyncio.wait_for(asyncio.to_thread(session.wait_for_run_scopes,
        session.capture_active_run_scopes()), 2)
    await runner.run_sync("123e4567-e89b-12d3-a456-426614174113", "Next ordinary turn.")
    assert [s.task for s in session.agent.memory.steps if isinstance(s, TaskStep)][0] == frozen
    assert len(runner.local_memory_service.calls) == 2
    assert runner.local_memory_service.calls[-1][1] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_stop_during_lookup_never_calls_model_or_appends_late_task(stream):
    runner, service = make_runner()
    sid = str(uuid4())
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(model_user_content=ENRICHED, status="matched", memory_texts=(MEMORY_TEXT,))

    runner.local_memory_service.prepare_user_turn = blocked
    async def run():
        if stream:
            return [e async for e in runner.run_stream(sid, RAW, enable_local_memory=True)]
        return await runner.run_sync(sid, RAW, enable_local_memory=True)
    task = asyncio.create_task(run())
    await asyncio.wait_for(started.wait(), 2)
    session = runner.sessions.get(sid)
    assert session.run_in_progress
    runner.abort_session(session.session_id)
    release.set()
    await task
    assert not service.chat.called
    assert not [s for s in session.agent.memory.steps if isinstance(s, TaskStep)]
    assert not session.run_in_progress
    assert not [e for e in session.timeline_events if e.get("kind") == "local_memory_retrieved"]


@pytest.mark.asyncio
async def test_cycle_freezes_flag_and_prepares_each_ordinary_message():
    runner, _ = make_runner()
    executor = CycleExecutor(runner, MagicMock())
    request = CycleStartRequest(cycle_run_id="memory-cycle", agent_mode=True, enable_local_memory=True,
        steps=[CycleStepRequest(type="message", message=RAW), CycleStepRequest(type="message", message=RAW)])
    record = executor.ensure_run(request)
    request.enable_local_memory = False
    _ = [e async for e in record.events(0, heartbeat_seconds=1)]
    assert [call[:2] for call in runner.local_memory_service.calls] == [(RAW, True), (RAW, True)]
    await executor.shutdown()


def test_context_validator_uses_frozen_native_schema_template_and_provider_budget(monkeypatch):
    from backend.compact.tokenizer import TokenCounter
    runner, _ = make_runner("basic_agent", "qwen35-native")
    runner.config.sglang.max_context_length = 100000
    session = runner.get_or_create_session("123e4567-e89b-12d3-a456-426614174111", None)
    seen = []
    class Tokenizer:
        def apply_chat_template(self, messages, **kwargs):
            seen.append((messages, kwargs))
            return [0] * (200000 if "oversized" in str(messages) else 100)
    monkeypatch.setattr(TokenCounter, "get", lambda **_: SimpleNamespace(
        _use_real_tokenizer=True, _tokenizer=Tokenizer()))
    validator = runner._local_memory_context_validator(session, None, True)
    assert validator(ENRICHED) is True
    assert validator("oversized") is False
    assert seen[0][1]["tools"]
    assert seen[0][1]["add_generation_prompt"] is True
    assert seen[0][1]["enable_thinking"] is True
    assert seen[0][0][-1]["role"] == "user"
    assert seen[0][0][-1]["content"] == [{"type": "text", "text": "New task:\n" + ENRICHED}]
    assert not session.agent.memory.steps


def test_context_validator_refuses_uncertified_budget(monkeypatch):
    from backend.compact.tokenizer import TokenCounter
    runner, _ = make_runner()
    session = runner.get_or_create_session("123e4567-e89b-12d3-a456-426614174112", None)
    monkeypatch.setattr(TokenCounter, "get", lambda **_: SimpleNamespace(_use_real_tokenizer=False))
    assert runner._local_memory_context_validator(session, None, None)(ENRICHED) is False


@pytest.mark.parametrize("backend", ["sglang", "vllm"])
@pytest.mark.parametrize("use_tilde", [False, True])
def test_context_budget_uses_active_provider_custom_template_snapshot(tmp_path, monkeypatch, backend, use_tilde):
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from transformers import PreTrainedTokenizerFast
    from backend.compact.tokenizer import TokenCounter
    engine = Tokenizer(WordLevel({"[UNK]": 0, "x": 1}, unk_token="[UNK]"))
    engine.pre_tokenizer = Whitespace()
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=engine, unk_token="[UNK]")
    tokenizer.chat_template = "{{ messages[-1].content[0].text }}"
    # The serving template expands the tail five times. Counting with the
    # tokenizer's default would incorrectly admit the ten-token candidate.
    serving_template = "{{ 'x ' * 20 }}{% for n in range(5) %}{{ messages[-1].content[0].text }} {% endfor %}"
    template_file = tmp_path / "serving.jinja"
    template_file.write_text(serving_template, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir(exist_ok=True)
    (tmp_path / "Project").mkdir(exist_ok=True)
    runner, service = make_runner("basic_agent", "qwen35-native")
    service.active_backend = backend
    runner.config.sglang.max_context_length = 60
    runner.config.vllm.max_model_len = 60
    runner.config.inference.max_tokens = 5
    getattr(runner.config, backend).chat_template = (
        f"~/{template_file.name}" if use_tilde else str(template_file)
    )
    other_backend = "vllm" if backend == "sglang" else "sglang"
    getattr(runner.config, other_backend).chat_template = str(tmp_path / "missing-other.jinja")
    session = runner.get_or_create_session(str(uuid4()), None)
    monkeypatch.setattr(TokenCounter, "get", lambda **_: SimpleNamespace(
        _use_real_tokenizer=True, _tokenizer=tokenizer))
    validator = runner._local_memory_context_validator(session, None, False)
    assert validator("x") is True
    assert validator("x " * 10) is False
    # The frozen serving snapshot must survive edits while this runner lives.
    template_file.write_text(tokenizer.chat_template, encoding="utf-8")
    assert runner._local_memory_context_validator(session, None, False)("x " * 10) is False


def test_missing_configured_serving_template_never_uses_tokenizer_default(tmp_path, monkeypatch):
    from backend.compact.tokenizer import TokenCounter
    runner, _ = make_runner()
    runner.config.sglang.chat_template = str(tmp_path / "absent.jinja")
    session = runner.get_or_create_session(str(uuid4()), None)
    tokenizer = SimpleNamespace(apply_chat_template=lambda *args, **kwargs: [1])
    monkeypatch.setattr(TokenCounter, "get", lambda **_: SimpleNamespace(
        _use_real_tokenizer=True, _tokenizer=tokenizer))
    assert runner._local_memory_context_validator(session, None, None)(ENRICHED) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["legacy_text", "qwen35-native"])
@pytest.mark.parametrize("profile", ["basic_agent", "manager_agent"])
async def test_prospective_task_budget_matches_actual_model_task_envelope(tmp_path, monkeypatch, protocol, profile):
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from transformers import PreTrainedTokenizerFast
    from backend.compact.tokenizer import TokenCounter
    from backend.agent.canonical_messages import message_to_template
    engine = Tokenizer(WordLevel({"[UNK]": 0, "x": 1, "New": 2, "task": 3, ":": 4}, unk_token="[UNK]"))
    engine.pre_tokenizer = Whitespace()
    tokenizer = PreTrainedTokenizerFast(tokenizer_object=engine, unk_token="[UNK]")
    template_file = tmp_path / "task.jinja"
    template_file.write_text("{{ messages[-1].content[0].text }}", encoding="utf-8")
    runner, service = make_runner(profile, protocol)
    runner.config.sglang.chat_template = str(template_file)
    runner.config.sglang.max_context_length = 8
    runner.config.inference.max_tokens = 5
    session = runner.get_or_create_session(str(uuid4()), None)
    seen = []
    apply_template = tokenizer.apply_chat_template
    def record(messages, **kwargs):
        seen.append(deepcopy(messages))
        return apply_template(messages, **kwargs)
    tokenizer.apply_chat_template = record
    monkeypatch.setattr(TokenCounter, "get", lambda **_: SimpleNamespace(
        _use_real_tokenizer=True, _tokenizer=tokenizer))
    validator = runner._local_memory_context_validator(session, None, False)
    fits = validator("x")
    assert not session.agent.memory.steps
    await runner.run_sync(session.session_id, "x", enable_local_memory=False)
    actual = [message_to_template(message) for message in service.chat.call_args.kwargs["messages"]]
    assert actual[-1]["content"] == [{"type": "text", "text": "New task:\nx"}]
    assert seen[0] == actual
    # Bare x plus five reserved output tokens fits, but the actual TaskStep
    # envelope brings the request to nine tokens and must reject enrichment.
    assert fits is False


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["coordinator_1", "coordinator_2"])
@pytest.mark.parametrize("protocol", ["legacy_text", "qwen35-native"])
async def test_routed_memory_budget_certifies_target_prefix_before_freezing(monkeypatch, source, protocol):
    from pathlib import Path
    from transformers import AutoTokenizer
    from backend.compact.tokenizer import TokenCounter, _extract_token_count
    from backend.agent.canonical_messages import message_to_template
    from backend.agent.agent_runner import ALL_TOOLS
    cfg = get_config()
    if not Path(cfg.model.model_path, "tokenizer_config.json").is_file():
        pytest.skip("configured local inference tokenizer artifact is unavailable")
    tokenizer = AutoTokenizer.from_pretrained(cfg.model.model_path, local_files_only=True, trust_remote_code=False)
    monkeypatch.setattr("backend.agent.agent_runner._build_runtime_time_context", lambda: "Fixed test clock")
    monkeypatch.setattr(TokenCounter, "get", lambda **_: SimpleNamespace(
        _use_real_tokenizer=True, _tokenizer=tokenizer))
    enriched = RAW + "\n\n[reference]\n" + "记忆：部署记录顺序固定为版本、启动参数、验证结果。" * 100
    runner, service = make_runner(source, protocol)
    runner.config.inference.max_tokens = 128
    runner.config.agent.coordinator_1_routing_max_tokens = 128
    runner.config.sglang.max_context_length = 131072
    async def prepare(raw, **kwargs):
        return SimpleNamespace(model_user_content=enriched)
    runner.local_memory_service.prepare_user_turn = prepare
    switch = ('<tool_call>\n<function=switch_agent>\n<parameter=target_agent>\nbasic_agent\n'
              '</parameter>\n</function>\n</tool_call>' if protocol == "qwen35-native" else
              '<<SWITCH_AGENT>> basic_agent')
    outputs = iter([switch, "Saved answer."])
    requests = []
    def chat(**kwargs):
        requests.append(kwargs)
        text = next(outputs)
        return iter([{"content": text}]) if kwargs.get("stream") else GenerationResult(text=text)
    service.chat.side_effect = chat
    sid = str(uuid4())
    await runner.run_sync(sid, RAW, enable_local_memory=True)
    target = requests[-1]
    assert len(requests) == 2
    configured = Path(runner.config.sglang.chat_template)
    if not configured.is_absolute():
        from backend.config import PROJECT_ROOT
        configured = PROJECT_ROOT / configured
    template = configured.read_text(encoding="utf-8")
    messages = [message_to_template(m) for m in target["messages"]]
    raw_messages = deepcopy(messages)
    for message in raw_messages:
        if isinstance(message["content"], str):
            message["content"] = message["content"].replace(enriched, RAW)
        else:
            for part in message["content"]:
                if part.get("type") == "text":
                    part["text"] = part["text"].replace(enriched, RAW)
    def count(body):
        return _extract_token_count(tokenizer.apply_chat_template(body, tools=target["tools"],
            chat_template=template, tokenize=True, add_generation_prompt=True, return_dict=False))
    raw_tokens, enriched_tokens = count(raw_messages), count(messages)
    limit = raw_tokens + 256
    assert raw_tokens + 128 <= limit < enriched_tokens + 128

    pristine, _ = make_runner(source, protocol)
    pristine.config.inference.max_tokens = 128
    pristine.config.agent.coordinator_1_routing_max_tokens = 128
    pristine.config.sglang.max_context_length = limit
    session = pristine.get_or_create_session(str(uuid4()), None)
    before = (session.agent, session.active_agent, session.runtime_context, session.capability,
              session.event_hub, list(session.agent.memory.steps), dict(session.behavior_records))
    all_tool_bindings = [dict(vars(tool)) for tool in ALL_TOOLS]
    live_tool_bindings = [dict(vars(tool)) for tool in session.agent.tools.values()]
    budget_requests = []
    apply_template = tokenizer.apply_chat_template
    def record_template(body, **kwargs):
        budget_requests.append((deepcopy(body), deepcopy(kwargs.get("tools"))))
        return apply_template(body, **kwargs)
    tokenizer.apply_chat_template = record_template
    with patch("backend.agent.agent_runner.get_tools_from_config", side_effect=AssertionError("budget must not initialize tools")):
        validator = pristine._local_memory_context_validator(session, None, None)
        assert validator(enriched) is False
        pristine.config.sglang.max_context_length = 131072
        assert pristine._local_memory_context_validator(session, None, None)(enriched) is True
    # The production target's system/runtime and schema are present verbatim
    # in a detached target shape, rather than estimated from router metadata.
    assert any(body[0] == messages[0] and tools == target["tools"]
               for body, tools in budget_requests)
    assert before == (session.agent, session.active_agent, session.runtime_context, session.capability,
                      session.event_hub, list(session.agent.memory.steps), dict(session.behavior_records))
    assert [dict(vars(tool)) for tool in ALL_TOOLS] == all_tool_bindings
    assert [dict(vars(tool)) for tool in session.agent.tools.values()] == live_tool_bindings
    assert [s.task for s in runner.sessions.get(sid).agent.memory.steps if isinstance(s, TaskStep)]


@pytest.mark.parametrize("profile,expected", [("coordinator_1", False), ("coordinator_2", False), ("basic_agent", True)])
def test_budget_never_discovers_mcp_for_future_routed_profile(monkeypatch, profile, expected):
    from backend.compact.tokenizer import TokenCounter
    runner, _ = make_runner(profile, "qwen35-native")
    session = runner.get_or_create_session(str(uuid4()), None)
    runner.config.mcp.servers = [SimpleNamespace(command="must-not-run", args=[])]
    monkeypatch.setattr(TokenCounter, "get", lambda **_: SimpleNamespace(
        _use_real_tokenizer=True, _tokenizer=SimpleNamespace(apply_chat_template=lambda *a, **k: [1])))
    with patch("backend.agent.agent_runner.get_tools_from_config", side_effect=AssertionError("must not discover MCP")):
        assert runner._local_memory_context_validator(session, None, None)(ENRICHED) is expected


@pytest.mark.asyncio
async def test_lifespan_binds_one_started_memory_service_and_config_uses_actual_snapshot(monkeypatch):
    import backend.main as main
    events = []
    runtime_status = {"default_enabled": False, "available": False, "status": "degraded",
        "indexed_files": 2, "indexed_chunks": 3, "last_sync": None,
        "error_code": "encoder_unavailable", "applied": {"min_similarity": .91, "top_k": 2}}
    class Service:
        active_backend = "sglang"
        def initialize(self):
            return False
        def shutdown(self):
            events.append("llm_shutdown")
    class LocalMemory:
        def __init__(self, config, *, project_root):
            self.config = config
            self.project_root = project_root
        async def start(self):
            events.append("memory_started")
        async def shutdown(self):
            events.append("memory_shutdown")
        def status_snapshot(self):
            return deepcopy(runtime_status)
    class Runner:
        def __init__(self, **kwargs):
            events.append("runner_created")
        def cleanup_expired_sessions(self, _ttl):
            pass
    class Asr:
        def __init__(self, _cfg):
            pass
        def validate_vad(self):
            pass
        async def shutdown(self):
            pass
    cfg = deepcopy(get_config())
    cfg.cycle.enabled = False
    test_app = FastAPI()
    monkeypatch.setattr(main, "app", test_app)
    monkeypatch.setattr(main, "config", cfg)
    monkeypatch.setattr(main, "reload_config", lambda: deepcopy(cfg))
    monkeypatch.setattr(main, "get_llm_service", lambda: Service())
    monkeypatch.setattr(main, "_validate_ocr_worker_count", lambda *_: None)
    monkeypatch.setattr("backend.compact.tokenizer.TokenCounter.get", lambda **_: None)
    monkeypatch.setattr("backend.agent.agent_runner.AgentRunner", Runner)
    monkeypatch.setattr("backend.asr.service.AsrService", Asr)
    monkeypatch.setitem(sys.modules, "backend.retrieval.local_memory", SimpleNamespace(LocalMemoryService=LocalMemory))
    async with main.lifespan(test_app):
        memory = test_app.state.local_memory_service
        assert test_app.state.agent_runner.local_memory_service is memory
        assert events.index("memory_started") < events.index("runner_created")
        result = await main.get_current_config()
        assert result["local_memory"] == runtime_status
    assert events.index("memory_shutdown") < events.index("llm_shutdown")


@pytest.mark.parametrize("stream", [False, True])
def test_request_replay_never_prepares_twice_and_changed_flag_conflicts(stream):
    runner, _ = make_runner()
    payload = {"session_id": "123e4567-e89b-12d3-a456-426614174115", "message": RAW,
        "request_id": "018f6f44-75f4-7def-b746-01a31bc3c115", "stream": stream,
        "enable_local_memory": True}
    with TestClient(app_for(runner, RequestLifecycleRegistry())) as client:
        assert client.post("/v1/agent/chat", json=payload).status_code == 200
        assert client.post("/v1/agent/chat", json=payload).status_code == 200
        assert client.post("/v1/agent/chat", json={**payload, "enable_local_memory": False}).status_code == 409
    assert len(runner.local_memory_service.calls) == 1
    session = runner.sessions.get(payload["session_id"])
    records = session.event_hub.find_payloads_by_fields({"type": "timeline_event"})
    assert len([record for record in records
        if record.payload.get("event", {}).get("kind") == "local_memory_retrieved"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("protocol", ["legacy_text", "qwen35-native"])
async def test_handoff_reuses_same_prepared_snapshot(stream, protocol):
    runner, service = make_runner("coordinator_1", protocol)
    switch = ('<tool_call>\n<function=switch_agent>\n<parameter=target_agent>\n'
              'basic_agent\n</parameter>\n</function>\n</tool_call>' if protocol == "qwen35-native" else
              '<<SWITCH_AGENT>> basic_agent')
    outputs = iter([switch, "Saved answer."])
    requests = []
    def chat(**kwargs):
        requests.append(kwargs)
        text = next(outputs)
        return iter([{"content": text}]) if kwargs.get("stream") else GenerationResult(text=text)
    service.chat.side_effect = chat
    sid = "123e4567-e89b-12d3-a456-426614174116"
    if stream:
        frames = [e async for e in runner.run_stream(sid, RAW, enable_local_memory=True)]
    else:
        await runner.run_sync(sid, RAW, enable_local_memory=True)
    session = runner.sessions.get(sid)
    assert session.current_agent_name == "basic_agent"
    assert len(runner.local_memory_service.calls) == 1
    tasks = [s.task for s in session.agent.memory.steps if isinstance(s, TaskStep)]
    assert tasks and all(ENRICHED in task for task in tasks)
    assert len(requests) == 2
    assert len([event for event in session.timeline_events
        if event.get("kind") == "local_memory_retrieved"]) == 1


@pytest.mark.asyncio
async def test_empty_stream_fallback_keeps_same_injected_prefix_without_retrieval():
    runner, service = make_runner()
    requests = []
    def chat(**kwargs):
        requests.append(kwargs)
        return iter([{}]) if kwargs.get("stream") else GenerationResult(text="Fallback answer.")
    service.chat.side_effect = chat
    sid = "123e4567-e89b-12d3-a456-426614174117"
    frames = [e async for e in runner.run_stream(sid, RAW, enable_local_memory=True)]
    assert any("Fallback answer." in frame for frame in frames)
    assert len(runner.local_memory_service.calls) == 1
    assert len(requests) == 2
    assert requests[0]["messages"] == requests[1]["messages"]
    assert requests[0]["tools"] == requests[1]["tools"]
    assert len(memory_events("".join(frames))) == 1


@pytest.mark.asyncio
async def test_bootstrap_uses_raw_current_text_and_does_not_duplicate_current_bubble():
    runner, _ = make_runner()
    sid = "123e4567-e89b-12d3-a456-426614174118"
    await runner.run_sync(sid, RAW, enable_local_memory=True, context_messages=[
        {"role": "user", "content": "Earlier question."},
        {"role": "assistant", "content": "Earlier answer."},
        {"role": "user", "content": RAW}])
    session = runner.sessions.get(sid)
    assert [s.task for s in session.agent.memory.steps if isinstance(s, TaskStep)] == ["Earlier question.", ENRICHED]


@pytest.mark.parametrize("stream", [False, True])
def test_pre_admission_rejection_never_prepares_or_creates_history(stream):
    runner, service = make_runner()
    with TestClient(app_for(runner, RequestLifecycleRegistry())) as client:
        response = client.post("/v1/agent/chat", json={"message": "", "stream": stream,
            "enable_local_memory": True})
    assert response.status_code == 400
    assert not runner.local_memory_service.calls
    assert not service.chat.called
    assert runner.sessions.active_count == 0


def memory_events(body):
    payloads = [json.loads(line[6:]) for line in body.splitlines()
                if line.startswith("data: ") and line[6:] != "[DONE]"]
    return [p for p in payloads if p.get("type") == "timeline_event"
            and p.get("event", {}).get("kind") == "local_memory_retrieved"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("profile", ["basic_agent", "coordinator_1"])
@pytest.mark.parametrize("protocol", ["legacy_text", "qwen35-native"])
def test_public_memory_showcase_is_frozen_bound_to_raw_user_turn_and_replayable(stream, profile, protocol):
    runner, _ = make_runner(profile, protocol)
    sid = str(uuid4())
    with TestClient(app_for(runner, RequestLifecycleRegistry())) as client:
        response = client.post("/v1/agent/chat", json={"session_id": sid, "message": RAW,
            "stream": stream, "enable_local_memory": True})
        assert response.status_code == 200, response.text
        expected = {"kind": "local_memory_retrieved", "session_id": sid, "turn_index": 1,
            "chunks": [{"text": MEMORY_TEXT}]}
        if stream:
            assert [p["event"] for p in memory_events(response.text)] == [expected]
        else:
            assert response.json()["local_memory"] == {k: v for k, v in expected.items() if k != "kind"}
        session = runner.sessions.get(sid)
        raw_turns = session.event_hub.find_payloads_by_fields({"type": "timeline_event"})
        started = [record.payload["event"] for record in raw_turns
            if record.payload.get("event", {}).get("kind") == "turn_started"]
        assert started[0]["user_content"] == RAW
        assert "preferences.md" not in json.dumps(expected)
        replay = client.get(f"/v1/session/{sid}/events")
        assert replay.status_code == 200
        assert [p["event"] for p in memory_events(replay.text)] == [expected]
        assert len(runner.local_memory_service.calls) == 1
        second = client.post("/v1/agent/chat", json={"session_id": sid, "message": "Next user turn.",
            "stream": stream, "enable_local_memory": False})
        assert second.status_code == 200
        if stream:
            assert not memory_events(second.text)
        else:
            assert "local_memory" not in second.json()
        replay_after = client.get(f"/v1/session/{sid}/events")
        assert [p["event"] for p in memory_events(replay_after.text)] == [expected]
        assert len(runner.local_memory_service.calls) == 2


@pytest.mark.asyncio
async def test_cycle_memory_showcase_survives_hidden_steps_with_message_path():
    runner, _ = make_runner()
    executor = CycleExecutor(runner, MagicMock())
    record = executor.ensure_run(CycleStartRequest(cycle_run_id=str(uuid4()),
        agent_mode=True, show_agent_steps=False, enable_local_memory=True,
        steps=[CycleStepRequest(type="message", message=RAW), CycleStepRequest(type="message", message=RAW)]))
    frames = [frame async for frame in record.events(0, heartbeat_seconds=1)]
    events = memory_events("".join(frames))
    assert len(events) == 2
    assert [event["path"] for event in events] == [[0], [1]]
    assert all(event["event"]["chunks"] == [{"text": MEMORY_TEXT}] for event in events)
    assert all(event["event"]["turn_index"] == 0 for event in events)
    replay = [frame async for frame in record.events(0, heartbeat_seconds=1)]
    assert memory_events("".join(replay)) == events
    assert len(runner.local_memory_service.calls) == 2
    await executor.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["disabled", "unready", "no_match", "timeout", "context_budget_unavailable"])
@pytest.mark.parametrize("stream", [False, True])
async def test_no_selected_memory_never_creates_showcase(status, stream):
    runner, _ = make_runner()
    async def prepare(raw, **kwargs):
        return SimpleNamespace(raw_user_text=raw, model_user_content=raw,
            status=status, sources=(), memory_texts=())
    runner.local_memory_service.prepare_user_turn = prepare
    sid = str(uuid4())
    if stream:
        frames = [frame async for frame in runner.run_stream(sid, RAW, enable_local_memory=True)]
        assert not memory_events("".join(frames))
    else:
        result = await runner.run_sync(sid, RAW, enable_local_memory=True)
        assert "local_memory" not in result
    assert not [e for e in runner.sessions.get(sid).timeline_events
                if e.get("kind") == "local_memory_retrieved"]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["legacy_text", "qwen35-native"])
async def test_new_text_only_memory_does_not_rewrite_accepted_source_bearing_history(protocol):
    runner, _ = make_runner("basic_agent", protocol)
    session = runner.get_or_create_session(str(uuid4()), None)
    old_task = 'Old accepted question.\n[reference]\n{"source":"private/preferences.md","lines":[3,5],"text":"old quoted text"}'
    session.agent.memory.steps.append(TaskStep(task=old_task))
    session.has_run = True
    await runner.run_sync(session.session_id, RAW, enable_local_memory=True)
    tasks = [step.task for step in session.agent.memory.steps if isinstance(step, TaskStep)]
    assert tasks == [old_task, ENRICHED]
