import json
import asyncio
import pytest
from copy import deepcopy
from unittest.mock import MagicMock, patch

from smolagents.memory import ActionStep
from smolagents.monitoring import Timing
from smolagents.models import (
    ChatMessageStreamDelta,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    get_tool_json_schema,
)

from backend.agent.agent_runner import (
    AgentRunner,
    AgentSession,
    SecondaryHandoffRequest,
    SessionBusyError,
)
from backend.config import get_config


def _make_runner() -> AgentRunner:
    config = deepcopy(get_config())
    config.inference.tool_protocol = "legacy_text"
    config.model.model_type = "text"
    config.agent.entry_agent_name = "coordinator_1"
    config.agent.enabled_agent_profiles = ["coordinator_1", "basic_agent"]
    return AgentRunner(MagicMock(), config)


def test_config_exposes_coordinator_1_timeout_seconds():
    config = get_config()

    assert config.agent.coordinator_1_timeout_seconds == 20


def test_config_exposes_ripgrep_timeout_seconds():
    config = get_config()

    assert config.agent.ripgrep_timeout_seconds == 15


def test_config_exposes_coordinator_1_routing_only_overrides():
    config = get_config()

    assert config.agent.coordinator_1_routing_temperature == 0.0
    assert config.agent.coordinator_1_routing_max_tokens == 150
    assert config.agent.coordinator_1_routing_extra_body == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_config_uses_supported_enabled_entry_profile():
    config = get_config()

    assert config.agent.entry_agent_name == "manager_agent"
    assert config.agent.entry_agent_name in config.agent.enabled_agent_profiles
    assert "manager_agent" in config.agent.enabled_agent_profiles
    assert "basic_agent" in config.agent.enabled_agent_profiles


def test_coordinator_1_and_coordinator_2_prompts_exist():
    runner = _make_runner()

    assert runner.config.agent_prompts.coordinator_1["instructions"]
    assert runner.config.agent_prompts.coordinator_2["instructions"]


def test_runner_config_preserves_prompt_compatibility_keys():
    runner = _make_runner()

    assert runner.config.agent_prompts.coordinator_1["instructions"]
    assert runner.config.agent_prompts.basic_agent["instructions"]
    assert runner.config.agent_prompts.vlm_instructions


def test_runner_can_build_runtime_context_for_coordinator_2_and_basic_agent():
    runner = _make_runner()
    assert runner._build_runtime_context_for_profile("coordinator_1") is None
    assert runner._build_runtime_context_for_profile("coordinator_2") is not None
    assert runner._build_runtime_context_for_profile("basic_agent") is not None


def test_runner_builds_runtime_context_when_enabled_for_supported_profiles():
    runner = _make_runner()
    runner.config.runtime_prompts.enable_runtime_time_context = True

    assert runner._build_runtime_context_for_profile("coordinator_1") is None
    assert runner._build_runtime_context_for_profile("coordinator_2") is not None
    assert runner._build_runtime_context_for_profile("basic_agent") is not None


def test_real_config_enables_runtime_time_context_for_supported_profiles():
    config = get_config()

    assert config.runtime_prompts.enable_runtime_time_context is True


def test_runtime_context_is_not_built_for_coordinator_2_when_disabled():
    runner = _make_runner()
    runner.config.runtime_prompts.enable_runtime_time_context = False

    assert runner._build_runtime_context_for_profile("coordinator_2") is None
    assert runner._build_runtime_context_for_profile("basic_agent") is None


def test_runtime_context_is_pinned_per_session():
    runner = _make_runner()
    runner.config.runtime_prompts.enable_runtime_time_context = True
    session = runner.get_or_create_session(None)

    first = runner._build_runtime_context_for_profile("coordinator_2", session=session)
    second = runner._build_runtime_context_for_profile("coordinator_2", session=session)

    assert first == second
    assert session.runtime_context == first


def test_get_or_create_session_initializes_entry_profile_session_state():
    runner = _make_runner()
    coordinator_agent = MagicMock()

    with patch.object(
        runner, "_build_profile_agent", return_value=coordinator_agent
    ) as mock_build_profile:
        session = runner.get_or_create_session(None)

    assert mock_build_profile.call_count == 1
    assert mock_build_profile.call_args.kwargs == {"session": None, "runtime_context": None}
    assert mock_build_profile.call_args.args == ("coordinator_1",)
    assert session.agent is coordinator_agent
    assert session.active_agent is coordinator_agent
    assert session.current_agent_name == "coordinator_1"
    assert session.turn_switch_count == 0
    assert session.has_shared_history is False


def test_agent_session_defaults_to_coordinator_1_current_agent_name():
    session = AgentSession(
        session_id="default-current-agent-session",
        agent=MagicMock(),
        mode="tool_calling",
    )

    assert session.current_agent_name == "coordinator_1"


def test_prepare_session_for_new_turn_prefers_active_agent_memory_and_state():
    runner = _make_runner()
    original_agent = MagicMock()
    original_agent.memory.steps = ["primary-step"]
    original_agent.state = {"value": 1}
    session = AgentSession(
        session_id="shared-history-session",
        agent=original_agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
        turn_switch_count=2,
        has_shared_history=True,
    )
    switched_agent = MagicMock()
    switched_agent.memory.steps = ["runtime-step"]
    switched_agent.state = {"value": 2}
    session.active_agent = switched_agent
    runner.sessions.put(session)

    rebuilt_entry_agent = MagicMock()
    rebuilt_entry_agent.memory.steps = []

    with patch.object(
        runner, "_build_profile_agent", return_value=rebuilt_entry_agent
    ) as mock_build_profile:
        prepared = runner.prepare_session_for_new_turn("shared-history-session")

    assert prepared is session
    assert mock_build_profile.call_count == 1
    assert mock_build_profile.call_args.kwargs == {
        "session": session,
        "runtime_context": None,
    }
    assert mock_build_profile.call_args.args == ("coordinator_1",)
    assert session.agent is rebuilt_entry_agent
    assert session.active_agent is rebuilt_entry_agent
    assert session.current_agent_name == "coordinator_1"
    assert session.turn_switch_count == 0
    assert session.has_shared_history is True
    assert session.agent.memory.steps == ["runtime-step"]
    assert session.agent.state == {"value": 2}


def test_prepare_session_for_new_turn_preserves_shared_state_without_strip():
    """v2 契约：prepare_session_for_new_turn 不再主动 strip 任何 state 字段
    （v1 时代曾 strip context_pack；v2 改写为单纯 deepcopy 透传）。
    """
    runner = _make_runner()
    original_agent = MagicMock()
    original_agent.memory.steps = ["primary-step"]
    original_agent.state = {"value": 1}
    session = AgentSession(
        session_id="shared-history-context-pack-session",
        agent=original_agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
        turn_switch_count=2,
        has_shared_history=True,
    )
    switched_agent = MagicMock()
    switched_agent.memory.steps = ["runtime-step"]
    switched_agent.state = {"value": 2}
    session.active_agent = switched_agent
    runner.sessions.put(session)

    rebuilt_entry_agent = MagicMock()
    rebuilt_entry_agent.memory.steps = []

    with patch.object(
        runner, "_build_profile_agent", return_value=rebuilt_entry_agent
    ):
        prepared = runner.prepare_session_for_new_turn(
            "shared-history-context-pack-session"
        )

    assert prepared is session
    assert session.agent.memory.steps == ["runtime-step"]
    assert session.agent.state == {"value": 2}


def test_get_or_create_session_reuse_preserves_shared_history_semantics():
    runner = _make_runner()
    original_agent = MagicMock()
    original_agent.memory.steps = ["stale-primary-step"]
    original_agent.state = {"shared": False}
    session = AgentSession(
        session_id="reused-session",
        agent=original_agent,
        mode="tool_calling",
        has_shared_history=True,
    )
    runtime_agent = MagicMock()
    runtime_agent.memory.steps = ["shared-step"]
    runtime_agent.state = {"shared": True}
    session.active_agent = runtime_agent
    session.current_agent_name = "basic_agent"
    session.turn_switch_count = 1
    session.aborted = True
    session.answer_now = True
    runner.sessions.put(session)

    rebuilt_entry_agent = MagicMock()
    rebuilt_entry_agent.memory.steps = []

    with patch.object(
        runner, "_build_profile_agent", return_value=rebuilt_entry_agent
    ) as mock_build_profile:
        reused = runner.get_or_create_session("reused-session")

    assert reused is session
    assert mock_build_profile.call_count == 1
    assert mock_build_profile.call_args.kwargs == {
        "session": session,
        "runtime_context": None,
    }
    assert mock_build_profile.call_args.args == ("coordinator_1",)
    assert reused.agent is rebuilt_entry_agent
    assert reused.active_agent is rebuilt_entry_agent
    assert reused.agent.memory.steps == ["shared-step"]
    assert reused.agent.state == {"shared": True}
    assert reused.current_agent_name == "coordinator_1"
    assert reused.turn_switch_count == 0
    assert reused.has_shared_history is True
    assert reused.aborted is False
    assert reused.answer_now is False


def test_get_or_create_session_does_not_prepare_in_flight_session():
    runner = _make_runner()
    original_agent = MagicMock()
    original_agent.memory.steps = ["primary-step"]
    runtime_agent = MagicMock()
    runtime_agent.memory.steps = ["runtime-step"]
    runtime_agent.state = {"shared": True}
    session = AgentSession(
        session_id="active-session",
        agent=original_agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
        has_shared_history=True,
    )
    session.active_agent = runtime_agent
    session.aborted = True
    session.answer_now = True
    runner.sessions.put(session)

    with patch.object(runner, "get_current_request_id", return_value="req-123"), patch.object(
        runner, "_build_profile_agent", return_value=MagicMock()
    ) as mock_build_profile:
        try:
            runner.get_or_create_session("active-session")
        except SessionBusyError as exc:
            assert str(exc) == "Session active-session already has an active run"
        else:
            raise AssertionError("expected SessionBusyError for in-flight session")

    mock_build_profile.assert_not_called()
    assert session.agent is original_agent
    assert session.active_agent is runtime_agent
    assert session.current_agent_name == "basic_agent"
    assert session.aborted is True
    assert session.answer_now is True


def test_get_or_create_session_does_not_prepare_session_while_run_flag_is_active():
    runner = _make_runner()
    original_agent = MagicMock()
    original_agent.memory.steps = ["primary-step"]
    runtime_agent = MagicMock()
    runtime_agent.memory.steps = ["runtime-step"]
    runtime_agent.state = {"shared": True}
    session = AgentSession(
        session_id="run-flag-active-session",
        agent=original_agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
        has_shared_history=True,
    )
    session.active_agent = runtime_agent
    session.run_in_progress = True
    runner.sessions.put(session)

    with patch.object(runner, "get_current_request_id", return_value=None), patch.object(
        runner, "_build_profile_agent", return_value=MagicMock()
    ) as mock_build_profile:
        try:
            runner.get_or_create_session("run-flag-active-session")
        except SessionBusyError as exc:
            assert str(exc) == "Session run-flag-active-session already has an active run"
        else:
            raise AssertionError("expected SessionBusyError for run-flag-active session")

    mock_build_profile.assert_not_called()
    assert session.agent is original_agent
    assert session.active_agent is runtime_agent
    assert session.current_agent_name == "basic_agent"
    assert session.run_in_progress is True


def test_get_or_create_session_rejects_new_turn_while_existing_run_is_active():
    runner = _make_runner()
    session = AgentSession(
        session_id="busy-session",
        agent=MagicMock(),
        mode="tool_calling",
    )
    session.run_in_progress = True
    runner.sessions.put(session)

    with patch.object(runner, "get_current_request_id", return_value=None):
        try:
            runner.get_or_create_session("busy-session", mode="tool_calling")
        except SessionBusyError as exc:
            assert str(exc) == "Session busy-session already has an active run"
        else:
            raise AssertionError("expected RuntimeError for concurrent turn")


def test_activate_profile_agent_does_not_write_context_pack_for_target_agent():
    """v2 契约：_activate_profile_agent 不再调用 _compile_context_pack，
    也不再向 next_agent.state 注入 context_pack。"""
    runner = _make_runner()
    current_agent = MagicMock()
    current_agent.memory.steps = []
    current_agent.state = {"shared": True}
    session = AgentSession(
        session_id="handoff-context-session",
        agent=current_agent,
        mode="tool_calling",
    )
    next_agent = MagicMock()
    next_agent.memory.steps = []
    next_agent.state = {}

    with patch.object(
        runner, "_build_profile_agent", return_value=next_agent
    ), patch.object(
        runner, "_compile_context_pack", create=True
    ) as mock_compile:
        runner._activate_profile_agent(session, "coordinator_2")

    mock_compile.assert_not_called()
    assert "context_pack" not in (next_agent.state or {}), (
        "v2 禁止向 next_agent.state 注入 context_pack；该字段从未被 LLM 消费。"
    )
    assert session.current_agent_name == "coordinator_2"


def test_activate_profile_agent_passes_user_goal_through_to_next_agent():
    """v2 契约：_activate_profile_agent 仍接受 user_goal 参数（用于审计/log），
    但不写入 next_agent.state；user_goal 由调用方在 run_stream/run_sync 路径中
    通过 _derive_current_user_goal 派生并使用。"""
    runner = _make_runner()
    current_agent = MagicMock()
    current_agent.memory.steps = []
    current_agent.state = {"shared": True}
    session = AgentSession(
        session_id="handoff-current-turn-goal-session",
        agent=current_agent,
        mode="tool_calling",
    )
    next_agent = MagicMock()
    next_agent.memory.steps = []
    next_agent.state = {}

    with patch.object(runner, "_build_profile_agent", return_value=next_agent):
        runner._activate_profile_agent(
            session,
            "coordinator_2",
            user_goal="当前问题",
        )

    assert "context_pack" not in (next_agent.state or {})
    assert "user_goal" not in (next_agent.state or {}), (
        "v2 禁止把 user_goal 注入 next_agent.state。"
    )
    assert session.current_agent_name == "coordinator_2"


def test_activate_secondary_handoff_preserves_route_receipt_and_handoff_ledger():
    """v2 契约：_activate_secondary_handoff 保留 route_receipt 与 handoff_ledger
    作为 Tier 2 审计；不再向 next_agent.state 注入 context_pack；
    不再维护 working_memory 字段。"""
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_1",
        "coordinator_2",
        "basic_agent",
    ]
    current_agent = MagicMock()
    current_agent.memory.steps = []
    current_agent.state = {"shared": True}
    session = AgentSession(
        session_id="secondary-handoff-context-session",
        agent=current_agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )
    next_agent = MagicMock()
    next_agent.memory.steps = []
    next_agent.state = {}

    with patch.object(runner, "_build_profile_agent", return_value=next_agent):
        handoff_message, timeline_event = runner._activate_secondary_handoff(
            session,
            SecondaryHandoffRequest(
                target_agent="basic_agent",
                task="联网搜索 Jetson Thor 最新消息",
            ),
            source_agent="coordinator_2",
            fallback_user_goal="原始用户问题",
        )

    assert handoff_message == "联网搜索 Jetson Thor 最新消息"
    assert timeline_event == {
        "kind": "agent_switched",
        "source": "coordinator_2",
        "target_agent": "basic_agent",
    }
    assert session.current_agent_name == "basic_agent"
    assert session.behavior_records["route_receipt"] == {
        "decider": "coordinator_2",
        "decision": "basic_agent",
        "reason_summary": "secondary_handoff",
        "timeout_fallback": False,
    }
    assert session.behavior_records["handoff_ledger"] == [
        "coordinator_2 -> basic_agent"
    ]
    assert "context_pack" not in (next_agent.state or {}), (
        "v2 禁止向 next_agent.state 注入 context_pack。"
    )
    assert session.timeline_events == [
        {
            "kind": "agent_switched",
            "source": "coordinator_2",
            "target_agent": "basic_agent",
        }
    ]


def test_run_sync_returns_coordinator_direct_answer_without_entering_basic_path():
    runner = _make_runner()
    coordinator_agent = MagicMock()
    coordinator_agent.memory.steps = []
    coordinator_agent.tools = {}
    coordinator_agent.run.return_value = "旧单代理路径答案"
    session = AgentSession(
        session_id="sync-direct-answer-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_agent",
    )

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_pass",
        create=True,
        return_value={"kind": "final", "answer": "协调器直接回复"},
    ) as mock_route, patch.object(
        runner, "_commit_direct_answer_to_shared_memory", create=True
    ) as mock_commit, patch.object(
        runner,
        "_run_basic_sync",
        create=True,
        return_value={"session_id": session.session_id, "answer": "不应进入 basic", "steps": []},
    ) as mock_basic:
        result = asyncio.run(runner.run_sync(session.session_id, "直接回答这个问题"))

    assert result == {
        "session_id": session.session_id,
        "answer": "协调器直接回复",
        "steps": [],
    }
    mock_route.assert_called_once_with(session, "直接回答这个问题", None)
    mock_commit.assert_called_once_with(
        session,
        "直接回答这个问题",
        "协调器直接回复",
        None,
    )
    mock_basic.assert_not_called()
    coordinator_agent.run.assert_not_called()


def test_run_stream_with_basic_agent_entry_skips_coordinator_routing_and_dispatch_card():
    runner = _make_runner()
    runner.config.agent.entry_agent_name = "basic_agent"
    runner.config.agent.enabled_agent_profiles = ["basic_agent"]
    basic_agent = MagicMock()
    basic_agent.memory.steps = []
    basic_agent.tools = {}
    session = AgentSession(
        session_id="basic-entry-stream-session",
        agent=basic_agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    async def _basic_stream():
        yield 'data: {"type": "session_start", "session_id": "basic-entry-stream-session"}\n\n'
        yield 'data: {"type": "agent_step", "step_type": "thought", "content": "basic thought"}\n\n'
        yield 'data: {"type": "content", "content": "basic answer"}\n\n'
        yield 'data: {"type": "done", "session_id": "basic-entry-stream-session"}\n\n'

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner, "_run_coordinator_routing_stream", create=True
    ) as mock_route, patch.object(
        runner, "_run_basic_stream", create=True, return_value=_basic_stream()
    ) as mock_basic:
        events = asyncio.run(
            _collect_stream_events(runner.run_stream(session.session_id, "hello"))
        )

    payloads = [json.loads(event[6:]) for event in events]
    mock_route.assert_not_called()
    mock_basic.assert_called_once_with(session, "hello", None, None)
    assert payloads == [
        {"type": "session_start", "session_id": "basic-entry-stream-session"},
        {"type": "agent_step", "step_type": "thought", "content": "basic thought"},
        {"type": "content", "content": "basic answer"},
        {"type": "done", "session_id": "basic-entry-stream-session"},
    ]
    assert all(
        payload.get("type") != "timeline_event"
        or payload.get("event", {}).get("kind") not in {"routing_started", "routing_delta", "dispatch_delta"}
        for payload in payloads
    )


def test_run_sync_with_basic_agent_entry_skips_coordinator_routing():
    runner = _make_runner()
    runner.config.agent.entry_agent_name = "basic_agent"
    runner.config.agent.enabled_agent_profiles = ["basic_agent"]
    basic_agent = MagicMock()
    basic_agent.memory.steps = []
    basic_agent.tools = {}
    session = AgentSession(
        session_id="basic-entry-sync-session",
        agent=basic_agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner, "_run_coordinator_routing_pass", create=True
    ) as mock_route, patch.object(
        runner,
        "_run_basic_sync",
        create=True,
        return_value={"session_id": session.session_id, "answer": "basic answer", "steps": []},
    ) as mock_basic:
        result = asyncio.run(runner.run_sync(session.session_id, "hello"))

    assert result == {
        "session_id": "basic-entry-sync-session",
        "answer": "basic answer",
        "steps": [],
    }
    mock_route.assert_not_called()
    mock_basic.assert_called_once_with(session, "hello", None, None)


def test_run_sync_invalid_coordinator_route_returns_stable_fallback_response():
    runner = _make_runner()
    coordinator_agent = MagicMock()
    coordinator_agent.memory.steps = []
    coordinator_agent.tools = {}
    session = AgentSession(
        session_id="sync-invalid-route-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_agent",
    )

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_pass",
        create=True,
        return_value={"kind": "invalid", "error": "Unsupported coordinator target: missing_agent"},
    ):
        result = asyncio.run(runner.run_sync(session.session_id, "route this"))

    assert result == {
        "session_id": session.session_id,
        "answer": "Unsupported coordinator target: missing_agent",
        "steps": [],
    }


def test_classify_coordinator_output_does_not_route_bare_json_demo():
    runner = _make_runner()
    chat_message = MagicMock()
    chat_message.content = '{"switch_agent":"basic_agent"}'
    chat_message.tool_calls = None

    result = runner._classify_coordinator_output(chat_message)

    assert result == {
        "kind": "final",
        "answer": '{"switch_agent":"basic_agent"}',
    }


def test_build_coordinator_turn_message_uses_coordinator_1_prompt_bundle():
    runner = _make_runner()
    runner.config.agent_prompts.coordinator_1["instructions"] = "router instructions"
    legacy_templates = deepcopy(runner.config.agent_prompts.legacy_prompt_compatibility["profiles"]["coordinator_1"])
    legacy_templates["routing_turn_prompt"] = "ROUTER {{raw_task}} {{custom_instructions}}"
    runner.config.agent_prompts.coordinator_1["legacy_prompt_templates"] = legacy_templates

    turn_message = runner._build_coordinator_turn_message("请帮我查一下天气")

    assert turn_message == "ROUTER 请帮我查一下天气 router instructions"


def test_classify_coordinator_output_accepts_switch_agent_command_for_coordinator_2():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_agent",
        "basic_agent",
        "coordinator_2",
    ]
    chat_message = MagicMock()
    chat_message.content = "<<SWITCH_AGENT>> coordinator_2"
    chat_message.tool_calls = None

    result = runner._classify_coordinator_output(chat_message)

    assert result == {"kind": "switch", "target_agent": "coordinator_2"}


@pytest.mark.parametrize(
    "content",
    [
        "<<SWITCH_AGENT>> basic_agent\nextra prose",
        "<<SWITCH_AGENT>> basic_agent\n<<SWITCH_AGENT>> coordinator_2",
        (
            "<<TOOL_CALL>>\n"
            '{"name":"read_file","arguments":{"file_path":"/tmp/a"}}\n'
            "<<END_TOOL_CALL>>"
        ),
    ],
)
def test_classify_coordinator_output_rejects_non_atomic_control_response(content):
    runner = _make_runner()
    chat_message = MagicMock(content=content, tool_calls=None)

    result = runner._classify_coordinator_output(chat_message)

    assert result["kind"] == "invalid"
    assert "No tool was executed" in result["error"]


def test_classify_coordinator_output_rejects_unsupported_switch_agent_target():
    runner = _make_runner()
    chat_message = MagicMock()
    chat_message.content = "<<SWITCH_AGENT>> missing_agent"
    chat_message.tool_calls = None

    result = runner._classify_coordinator_output(chat_message)

    assert result["kind"] == "invalid"
    assert "missing_agent" in result["error"]
    assert "basic_agent" in result["error"]


def test_classify_coordinator_output_accepts_enabled_future_profile_target():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_agent",
        "basic_agent",
        "auditor_agent",
    ]
    chat_message = MagicMock()
    chat_message.content = "<<SWITCH_AGENT>> auditor_agent"
    chat_message.tool_calls = None

    result = runner._classify_coordinator_output(chat_message)

    assert result == {"kind": "switch", "target_agent": "auditor_agent"}


def test_classify_coordinator_output_accepts_structured_switch_tool_call_for_enabled_target():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_agent",
        "basic_agent",
        "coordinator_2",
    ]
    chat_message = MagicMock()
    chat_message.content = ""
    chat_message.tool_calls = [
        ChatMessageToolCall(
            id="call_0",
            type="function",
            function=ChatMessageToolCallFunction(
                name="switch_agent",
                arguments={"target_agent": "coordinator_2"},
            ),
        )
    ]

    result = runner._classify_coordinator_output(chat_message)

    assert result == {"kind": "switch", "target_agent": "coordinator_2"}


def test_classify_coordinator_output_accepts_structured_switch_tool_call_with_irrelevant_content():
    runner = _make_runner()
    chat_message = MagicMock()
    chat_message.content = "Coordinator selecting next agent"
    chat_message.tool_calls = [
        ChatMessageToolCall(
            id="call_0",
            type="function",
            function=ChatMessageToolCallFunction(
                name="switch_agent",
                arguments={"target_agent": "basic_agent"},
            ),
        )
    ]

    result = runner._classify_coordinator_output(chat_message)

    assert result == {"kind": "switch", "target_agent": "basic_agent"}


def test_classify_coordinator_output_rejects_structured_switch_tool_call_for_unsupported_target():
    runner = _make_runner()
    chat_message = MagicMock()
    chat_message.content = ""
    chat_message.tool_calls = [
        ChatMessageToolCall(
            id="call_0",
            type="function",
            function=ChatMessageToolCallFunction(
                name="switch_agent",
                arguments={"target_agent": "missing_agent"},
            ),
        )
    ]

    result = runner._classify_coordinator_output(chat_message)

    assert result["kind"] == "invalid"
    assert "missing_agent" in result["error"]
    assert "basic_agent" in result["error"]


def test_classify_coordinator_output_rejects_mixed_structured_tool_calls_with_switch_first():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_agent",
        "basic_agent",
        "coordinator_2",
    ]
    chat_message = MagicMock()
    chat_message.content = ""
    chat_message.tool_calls = [
        ChatMessageToolCall(
            id="call_0",
            type="function",
            function=ChatMessageToolCallFunction(
                name="switch_agent",
                arguments={"target_agent": "coordinator_2"},
            ),
        ),
        ChatMessageToolCall(
            id="call_1",
            type="function",
            function=ChatMessageToolCallFunction(
                name="read_file",
                arguments={"path": "/tmp/a.txt"},
            ),
        ),
    ]

    result = runner._classify_coordinator_output(chat_message)

    assert result["kind"] == "invalid"
    assert "cannot be mixed" in result["error"]


def test_classify_coordinator_output_rejects_mixed_structured_tool_calls_with_switch_last():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_agent",
        "basic_agent",
        "coordinator_2",
    ]
    chat_message = MagicMock()
    chat_message.content = ""
    chat_message.tool_calls = [
        ChatMessageToolCall(
            id="call_0",
            type="function",
            function=ChatMessageToolCallFunction(
                name="read_file",
                arguments={"path": "/tmp/a.txt"},
            ),
        ),
        ChatMessageToolCall(
            id="call_1",
            type="function",
            function=ChatMessageToolCallFunction(
                name="switch_agent",
                arguments={"target_agent": "coordinator_2"},
            ),
        ),
    ]

    result = runner._classify_coordinator_output(chat_message)

    assert result["kind"] == "invalid"
    assert "cannot be mixed" in result["error"]


def test_classify_coordinator_output_keeps_bare_tool_json_as_natural_text():
    runner = _make_runner()
    chat_message = MagicMock()
    chat_message.content = '[{"name":"read_file","arguments":{"path":"/tmp/a.txt"}}]'
    chat_message.tool_calls = None

    result = runner._classify_coordinator_output(chat_message)

    assert result == {"kind": "final", "answer": chat_message.content}


def test_classify_coordinator_output_keeps_bare_mixed_json_as_natural_text():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_agent",
        "basic_agent",
        "coordinator_2",
    ]
    chat_message = MagicMock()
    chat_message.content = (
        "["
        '{"id":"call_0","type":"agent","function":'
        '{"name":"switch_agent","arguments":"coordinator_2"}},'
        '{"name":"read_file","arguments":{"path":"/tmp/a.txt"}}'
        "]"
    )
    chat_message.tool_calls = None

    result = runner._classify_coordinator_output(chat_message)

    assert result == {"kind": "final", "answer": chat_message.content}


def test_classify_coordinator_output_rejects_content_switch_when_structured_tool_calls_conflict():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_agent",
        "basic_agent",
        "coordinator_2",
    ]
    chat_message = MagicMock()
    chat_message.content = "<<SWITCH_AGENT>> coordinator_2"
    chat_message.tool_calls = [
        ChatMessageToolCall(
            id="call_0",
            type="function",
            function=ChatMessageToolCallFunction(
                name="read_file",
                arguments={"path": "/tmp/a.txt"},
            ),
        )
    ]

    result = runner._classify_coordinator_output(chat_message)

    assert result["kind"] == "invalid"
    assert "cannot coexist" in result["error"]


def test_classify_coordinator_output_rejects_unrecoverable_content_only_tool_wrapper():
    runner = _make_runner()
    chat_message = MagicMock()
    chat_message.content = 'Calling tools: [{"name":"read_file","arguments":'
    chat_message.tool_calls = None

    result = runner._classify_coordinator_output(chat_message)

    assert result["kind"] == "invalid"
    assert "legacy" in result["error"].lower()


def test_classify_coordinator_output_rejects_conflicting_content_and_structured_switch_commands():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_agent",
        "basic_agent",
        "coordinator_2",
    ]
    chat_message = MagicMock()
    chat_message.content = "<<SWITCH_AGENT>> coordinator_2"
    chat_message.tool_calls = [
        ChatMessageToolCall(
            id="call_0",
            type="function",
            function=ChatMessageToolCallFunction(
                name="switch_agent",
                arguments={"target_agent": "basic_agent"},
            ),
        )
    ]

    result = runner._classify_coordinator_output(chat_message)

    assert result["kind"] == "invalid"
    assert "cannot coexist" in result["error"]


def test_classify_coordinator_output_rejects_structured_switch_when_content_has_malformed_tool_wrapper():
    runner = _make_runner()
    chat_message = MagicMock()
    chat_message.content = 'Calling tools: [{"name":"read_file","arguments":'
    chat_message.tool_calls = [
        ChatMessageToolCall(
            id="call_0",
            type="function",
            function=ChatMessageToolCallFunction(
                name="switch_agent",
                arguments={"target_agent": "basic_agent"},
            ),
        )
    ]

    result = runner._classify_coordinator_output(chat_message)

    assert result["kind"] == "invalid"
    assert "legacy" in result["error"].lower()


def test_build_coordinator_turn_message_uses_yaml_template_and_short_reasoning_constraint():
    runner = _make_runner()

    prompt = runner._build_coordinator_turn_message("使用浏览器 打开 example.com")

    assert "Route this user turn." not in prompt
    assert "你必须从以下 agent 中选择一个并输出切换动作" in prompt
    assert "coordinator_2" in prompt
    assert "basic_agent" in prompt
    assert "原始用户任务" in prompt
    assert "Original user task" not in prompt


def test_build_coordinator_turn_message_preserves_switch_agent_output_contract_examples():
    runner = _make_runner()

    prompt = runner._build_coordinator_turn_message("使用浏览器 打开 example.com")

    assert "<<SWITCH_AGENT>> basic_agent" in prompt
    assert "<<SWITCH_AGENT>> coordinator_2" in prompt
    assert '"name":"switch_agent"' not in prompt


def test_coordinator_2_managed_basic_agent_proxy_exposes_schema_and_prompt_fields():
    runner = _make_runner()

    proxy = runner._build_managed_basic_agent_proxy()

    assert proxy.name == "basic_agent"
    assert proxy.description
    assert proxy.inputs == {}
    assert proxy.output_type == "string"
    assert proxy.to_tool_calling_prompt() == (
        "basic_agent: 切换到 basic_agent 来执行外部搜索、文件检查、代码编辑、命令运行或其他工具能力。\n"
        "    Takes inputs: {}\n"
        "    Returns an output of type: string"
    )
    assert get_tool_json_schema(proxy)["function"]["name"] == "basic_agent"


def test_process_tool_calls_rejects_mixed_basic_agent_handoff_with_other_tools():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_1",
        "coordinator_2",
        "basic_agent",
    ]
    agent = runner._build_profile_agent("coordinator_2")
    chat_message = MagicMock()
    chat_message.tool_calls = [
        ChatMessageToolCall(
            id="call_0",
            type="function",
            function=ChatMessageToolCallFunction(
                name="basic_agent",
                arguments={"task": "联网搜索 Jetson Thor 最新消息"},
            ),
        ),
        ChatMessageToolCall(
            id="call_1",
            type="function",
            function=ChatMessageToolCallFunction(
                name="search_and_fetch",
                arguments={"query": "Jetson Thor 最新消息"},
            ),
        ),
    ]
    memory_step = ActionStep(step_number=1, timing=Timing(start_time=0.0, end_time=0.1))

    with patch.object(agent.orchestrator, "analyze") as mock_analyze:
        with pytest.raises(
            ValueError,
            match="coordinator_2 must emit either a basic_agent handoff or tool calls, not both",
        ):
            list(agent.process_tool_calls(chat_message, memory_step))

    mock_analyze.assert_not_called()


def test_run_stream_surfaces_mixed_basic_agent_handoff_as_invalid_coordinator_route():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_1",
        "coordinator_2",
        "basic_agent",
    ]
    session = AgentSession(
        session_id="coord2-mixed-tool-call-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    coordinator_2_agent = MagicMock()
    coordinator_2_agent.memory.steps = []
    coordinator_2_agent.tools = {}

    async def _invalid_coordinator_2_stream():
        yield 'data: {"type": "session_start", "session_id": "coord2-mixed-tool-call-session"}\n\n'
        yield 'data: {"type": "error", "error": "coordinator_2 must emit either a basic_agent handoff or tool calls, not both"}\n\n'
        yield 'data: {"type": "done", "session_id": "coord2-mixed-tool-call-session"}\n\n'

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {
                "kind": "result",
                "route": {"kind": "switch", "target_agent": "coordinator_2"},
            },
        ]),
    ), patch.object(
        runner, "_build_profile_agent", return_value=coordinator_2_agent
    ), patch.object(
        runner,
        "_run_basic_stream",
        create=True,
        return_value=_invalid_coordinator_2_stream(),
    ):
        events = asyncio.run(
            _collect_stream_events(runner.run_stream(session.session_id, "请继续分析并联网搜索"))
        )

    payloads = [json.loads(event[6:]) for event in events]
    assert payloads[0] == {"type": "session_start", "session_id": "coord2-mixed-tool-call-session"}
    assert payloads[1]["event"]["kind"] == "routing_delta"
    assert payloads[2]["event"]["kind"] == "dispatch_delta"
    assert payloads[3]["dispatch_id"] == "dispatch-coord2-mixed-tool-call-session-routing"
    assert payloads[4]["event"] == {
        "kind": "agent_switched",
        "source": "coordinator_1",
        "target_agent": "coordinator_2",
    }
    assert payloads[5]["dispatch_target_agent"] == "coordinator_2"
    assert payloads[6] == {
        "type": "error",
        "error": "coordinator_2 must emit either a basic_agent handoff or tool calls, not both",
    }
    assert payloads[7] == {"type": "done", "session_id": "coord2-mixed-tool-call-session"}


def test_run_sync_surfaces_mixed_basic_agent_handoff_as_stable_error_response():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_1",
        "coordinator_2",
        "basic_agent",
    ]
    session = AgentSession(
        session_id="coord2-mixed-tool-call-sync-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    coordinator_2_agent = MagicMock()
    coordinator_2_agent.memory.steps = []
    coordinator_2_agent.tools = {}

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_pass",
        create=True,
        return_value={"kind": "switch", "target_agent": "coordinator_2"},
    ), patch.object(
        runner, "_build_profile_agent", return_value=coordinator_2_agent
    ), patch.object(
        runner,
        "_run_basic_sync",
        create=True,
        side_effect=ValueError(
            "coordinator_2 must emit either a basic_agent handoff or tool calls, not both"
        ),
    ):
        result = asyncio.run(
            runner.run_sync(session.session_id, "请继续分析并联网搜索")
        )

    assert result == {
        "session_id": "coord2-mixed-tool-call-sync-session",
        "answer": "coordinator_2 must emit either a basic_agent handoff or tool calls, not both",
        "steps": [],
    }


def test_build_coordinator_turn_message_renders_custom_instructions_placeholder():
    runner = _make_runner()
    runner.config.agent_prompts.coordinator_1["instructions"] = "router instructions"
    legacy_templates = deepcopy(runner.config.agent_prompts.legacy_prompt_compatibility["profiles"]["coordinator_1"])
    legacy_templates["routing_turn_prompt"] = "{{custom_instructions}}\n任务: {{raw_task}}"
    runner.config.agent_prompts.coordinator_1["legacy_prompt_templates"] = legacy_templates

    prompt = runner._build_coordinator_turn_message("你好")

    assert runner.config.agent_prompts.coordinator_1["instructions"] in prompt
    assert "{{custom_instructions}}" not in prompt
    assert "{%- if custom_instructions %}" not in prompt
    assert "{% endif %}" not in prompt


def test_run_sync_switches_to_basic_agent_and_reuses_raw_user_task():
    runner = _make_runner()
    coordinator_agent = MagicMock()
    coordinator_agent.memory.steps = ["shared-step"]
    coordinator_agent.state = {"foo": "bar"}
    coordinator_agent.tools = {}
    coordinator_agent.run.return_value = "旧路径不应被使用"
    session = AgentSession(
        session_id="sync-switch-basic-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_agent",
    )
    session.timeline_events = [
        {
            "kind": "routing_started",
            "source": "coordinator_1",
            "message_id": "routing-card-sync-switch-basic-session",
        },
        {
            "kind": "routing_decision",
            "source": "coordinator_1",
            "message_id": "routing-card-sync-switch-basic-session",
            "target_agent": "basic_agent",
        },
    ]
    decoded_images = [object()]
    basic_agent = MagicMock()
    basic_agent.memory.steps = []
    basic_agent.tools = {}

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=decoded_images
    ), patch.object(
        runner,
        "_run_coordinator_routing_pass",
        create=True,
        return_value={"kind": "switch", "target_agent": "basic_agent"},
    ) as mock_route, patch.object(
        runner, "_build_profile_agent", return_value=basic_agent
    ) as mock_build, patch.object(
        runner,
        "_run_basic_sync",
        create=True,
        return_value={
            "session_id": session.session_id,
            "answer": "basic agent 答案",
            "steps": [{"step_number": 1, "thought": "搜索"}],
        },
    ) as mock_basic:
        result = asyncio.run(
            runner.run_sync(
                session.session_id,
                "帮我搜索这张图的信息",
                images=["data:image/jpeg;base64,abc"],
            )
        )

    assert result == {
        "session_id": session.session_id,
        "answer": "basic agent 答案",
        "steps": [{"step_number": 1, "thought": "搜索"}],
    }
    mock_route.assert_called_once_with(session, "帮我搜索这张图的信息", decoded_images)
    assert mock_build.call_count == 1
    assert mock_build.call_args.kwargs == {
        "session": session,
        "runtime_context": None,
    }
    assert mock_build.call_args.args == ("basic_agent",)
    mock_basic.assert_called_once_with(
        session, "帮我搜索这张图的信息", decoded_images, None
    )
    assert session.agent is basic_agent
    assert session.active_agent is basic_agent
    assert basic_agent.memory.steps == ["shared-step"]
    assert basic_agent.state == {"foo": "bar"}, (
        "v2 禁止 _activate_profile_agent 向 next_agent.state 注入 context_pack；"
        "state 应保持为 prior_agent 传入的 deepcopy 值（仅 strip 掉 transient，v2 已无 transient）。"
    )
    assert "context_pack" not in (basic_agent.state or {}), (
        "v2 禁止 context_pack 出现在 basic_agent.state 中。"
    )
    assert session.current_agent_name == "basic_agent"
    assert session.turn_switch_count == 1
    assert session.timeline_events == [
        {
            "kind": "routing_started",
            "source": "coordinator_1",
            "message_id": "routing-card-sync-switch-basic-session",
        },
        {
            "kind": "routing_decision",
            "source": "coordinator_1",
            "message_id": "routing-card-sync-switch-basic-session",
            "target_agent": "basic_agent",
        },
        {
            "kind": "agent_switched",
            "source": "coordinator_1",
            "target_agent": "basic_agent",
        },
    ]
    coordinator_agent.run.assert_not_called()


def test_run_coordinator_routing_pass_appends_sync_timeline_events_for_switch_route():
    runner = _make_runner()
    coordinator_agent = runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []
    session = AgentSession(
        session_id="sync-routing-timeline-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    chat_message = MagicMock()
    chat_message.content = "<<SWITCH_AGENT>> basic_agent"
    chat_message.tool_calls = None

    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate",
        return_value=chat_message,
    ):
        route = runner._run_coordinator_routing_pass(session, "帮我搜索资料", None)

    assert route == {"kind": "switch", "target_agent": "basic_agent"}
    assert session.timeline_events == [
        {
            "kind": "routing_started",
            "source": "coordinator_1",
            "message_id": "routing-card-sync-routing-timeline-session",
        },
        {
            "kind": "routing_decision",
            "source": "coordinator_1",
            "message_id": "routing-card-sync-routing-timeline-session",
            "target_agent": "basic_agent",
        },
    ]


def test_run_coordinator_routing_pass_uses_routing_only_overrides():
    runner = _make_runner()
    coordinator_agent = runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []
    session = AgentSession(
        session_id="sync-routing-overrides-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    chat_message = MagicMock()
    chat_message.content = "直接回复"
    chat_message.tool_calls = None

    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate",
        return_value=chat_message,
    ) as mock_generate:
        route = runner._run_coordinator_routing_pass(session, "帮我总结", None)

    assert route == {"kind": "final", "answer": "直接回复"}
    call_kwargs = mock_generate.call_args.kwargs
    assert call_kwargs["temperature"] == 0.0
    assert call_kwargs["max_tokens"] == 150
    assert call_kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_run_stream_invalid_coordinator_route_keeps_session_start_error_done_protocol():
    runner = _make_runner()
    coordinator_agent = MagicMock()
    coordinator_agent.memory.steps = []
    coordinator_agent.tools = {}
    session = AgentSession(
        session_id="stream-invalid-route-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_agent",
    )

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {"kind": "dispatch", "dispatch_status": "running", "dispatch_stage": "routing"},
            {
                "kind": "result",
                "route": {"kind": "invalid", "error": "Unsupported coordinator target: missing_agent"},
            },
        ]),
    ):
        events = asyncio.run(
            _collect_stream_events(runner.run_stream(session.session_id, "route this"))
        )

    payloads = [json.loads(event[6:]) for event in events]
    assert payloads == [
        {"type": "session_start", "session_id": session.session_id},
        {
            "type": "agent_step",
            "step_type": "dispatch",
            "dispatch_status": "running",
            "dispatch_stage": "routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
        },
        {
            "type": "agent_step",
            "step_type": "dispatch",
            "dispatch_status": "error",
            "dispatch_stage": "routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "dispatch_id": "dispatch-stream-invalid-route-session-routing",
            "dispatch_message": "Unsupported coordinator target: missing_agent",
        },
        {
            "type": "error",
            "error": "Unsupported coordinator target: missing_agent",
        },
        {"type": "done", "session_id": session.session_id},
    ]


def test_run_stream_shorthand_switch_routes_into_basic_stream_instead_of_returning_json():
    runner = _make_runner()
    coordinator_agent = MagicMock()
    coordinator_agent.memory.steps = ["shared-step"]
    coordinator_agent.state = {"foo": "bar"}
    coordinator_agent.tools = {}
    session = AgentSession(
        session_id="stream-switch-basic-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_agent",
    )
    basic_agent = MagicMock()
    basic_agent.memory.steps = []
    basic_agent.tools = {}

    async def _basic_stream():
        yield 'data: {"type": "session_start", "session_id": "stream-switch-basic-session"}\n\n'
        yield 'data: {"type": "agent_step", "step_type": "thought", "content": "我来打开浏览器", "partial": true}\n\n'
        yield 'data: {"type": "done", "session_id": "stream-switch-basic-session"}\n\n'

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {"kind": "dispatch", "dispatch_status": "running", "dispatch_stage": "routing"},
            {
                "kind": "result",
                "route": {"kind": "switch", "target_agent": "basic_agent"},
            },
        ]),
    ) as mock_route, patch.object(
        runner, "_build_profile_agent", return_value=basic_agent
    ) as mock_build, patch.object(
        runner,
        "_run_basic_stream",
        create=True,
        return_value=_basic_stream(),
    ) as mock_basic:
        events = asyncio.run(
            _collect_stream_events(runner.run_stream(session.session_id, "使用浏览器 打开 example.com"))
        )

    payloads = [json.loads(event[6:]) for event in events]
    assert payloads == [
        {"type": "session_start", "session_id": "stream-switch-basic-session"},
        {
            "type": "agent_step",
            "step_type": "dispatch",
            "dispatch_status": "running",
            "dispatch_stage": "routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
        },
        {
            "type": "timeline_event",
                "event": {
                    "kind": "agent_switched",
                    "source": "coordinator_1",
                    "target_agent": "basic_agent",
                },
        },
        {
            "type": "agent_step",
            "step_type": "dispatch",
            "dispatch_status": "completed",
            "dispatch_stage": "handoff",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "dispatch_id": "dispatch-stream-switch-basic-session-routing",
            "dispatch_target_agent": "basic_agent",
        },
        {
            "type": "agent_step",
            "step_type": "thought",
            "content": "我来打开浏览器",
            "partial": True,
        },
        {"type": "done", "session_id": "stream-switch-basic-session"},
    ]
    mock_route.assert_called_once_with(session, "使用浏览器 打开 example.com", None)
    assert mock_build.call_count == 1
    assert mock_build.call_args.kwargs == {
        "session": session,
        "runtime_context": None,
    }
    assert mock_build.call_args.args == ("basic_agent",)
    mock_basic.assert_called_once_with(
        session, "使用浏览器 打开 example.com", None, None
    )


def test_run_stream_emits_dispatch_running_before_direct_answer_content():
    runner = _make_runner()
    coordinator_agent = MagicMock()
    coordinator_agent.memory.steps = []
    coordinator_agent.tools = {}
    session = AgentSession(
        session_id="stream-direct-answer-dispatch-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_agent",
    )

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {
                "kind": "timeline",
                "event": {
                    "kind": "routing_decision",
                    "source": "coordinator_1",
                    "message_id": "routing-card-direct-answer",
                    "decision": "direct_answer",
                },
            },
            {
                "kind": "timeline",
                "event": {
                    "kind": "routing_delta",
                    "source": "coordinator_1",
                    "message_id": "routing-card-direct-answer",
                    "text": "正在分析是否需要切换执行代理",
                    "partial": True,
                },
            },
            {"kind": "dispatch", "dispatch_status": "running", "dispatch_stage": "routing", "dispatch_message": "正在分析是否需要切换执行代理"},
            {
                "kind": "result",
                "route": {"kind": "final", "answer": "协调器直接回复"},
            },
        ]),
    ) as mock_route, patch.object(
        runner, "_commit_direct_answer_to_shared_memory", create=True
    ) as mock_commit:
        events = asyncio.run(
            _collect_stream_events(runner.run_stream(session.session_id, "直接回答这个问题"))
        )

    payloads = [json.loads(event[6:]) for event in events]
    assert payloads[0] == {"type": "session_start", "session_id": "stream-direct-answer-dispatch-session"}
    assert payloads[1]["event"]["kind"] == "routing_decision"
    assert payloads[2]["event"]["kind"] == "routing_delta"
    assert payloads[3]["dispatch_kind"] == "routing"
    assert payloads[4]["dispatch_id"] == "dispatch-stream-direct-answer-dispatch-session-routing"
    assert payloads[5] == {"type": "content", "content": "协调器直接回复"}
    assert payloads[6] == {"type": "done", "session_id": "stream-direct-answer-dispatch-session"}
    mock_route.assert_called_once_with(session, "直接回答这个问题", None)
    mock_commit.assert_called_once_with(session, "直接回答这个问题", "协调器直接回复", None)


def test_run_stream_does_not_emit_agent_switched_when_activation_fails():
    runner = _make_runner()
    coordinator_agent = MagicMock()
    coordinator_agent.memory.steps = []
    coordinator_agent.tools = {}
    session = AgentSession(
        session_id="stream-switch-activation-failure-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_agent",
    )

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {"kind": "dispatch", "dispatch_status": "running", "dispatch_stage": "routing"},
            {
                "kind": "result",
                "route": {"kind": "switch", "target_agent": "basic_agent"},
            },
        ]),
    ), patch.object(
        runner, "_activate_profile_agent", side_effect=RuntimeError("build failed")
    ):
        async def _collect_partial_events():
            collected = []
            with pytest.raises(RuntimeError, match="build failed"):
                async for event in runner.run_stream(session.session_id, "切换失败"):
                    collected.append(json.loads(event[6:]))
            return collected

        payloads = asyncio.run(_collect_partial_events())

    assert payloads == [
        {"type": "session_start", "session_id": "stream-switch-activation-failure-session"},
        {
            "type": "agent_step",
            "step_type": "dispatch",
            "dispatch_status": "running",
            "dispatch_stage": "routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
        },
    ]


def test_run_stream_merges_dispatch_message_deltas_into_single_line():
    runner = _make_runner()
    coordinator_agent = MagicMock()
    coordinator_agent.memory.steps = []
    coordinator_agent.tools = {}
    session = AgentSession(
        session_id="stream-dispatch-merge-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_agent",
    )

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {"kind": "dispatch", "dispatch_status": "running", "dispatch_stage": "routing", "dispatch_message": "正在"},
            {"kind": "dispatch", "dispatch_status": "running", "dispatch_stage": "routing", "dispatch_message": "分析是否"},
            {"kind": "dispatch", "dispatch_status": "running", "dispatch_stage": "routing", "dispatch_message": "需要切换执行代理"},
            {
                "kind": "result",
                "route": {"kind": "final", "answer": "协调器直接回复"},
            },
        ]),
    ), patch.object(
        runner, "_commit_direct_answer_to_shared_memory", create=True
    ):
        events = asyncio.run(
            _collect_stream_events(runner.run_stream(session.session_id, "直接回答这个问题"))
        )

    payloads = [json.loads(event[6:]) for event in events]
    dispatch_payloads = [p for p in payloads if p.get("step_type") == "dispatch"]
    assert dispatch_payloads[0]["dispatch_message"] == "正在"
    assert dispatch_payloads[1]["dispatch_message"] == "分析是否"
    assert dispatch_payloads[2]["dispatch_message"] == "需要切换执行代理"


def test_run_stream_emits_timeline_event_for_routing_delta():
    runner = _make_runner()
    session = AgentSession(
        session_id="timeline-routing-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events(
            [
                {
                    "kind": "timeline",
                    "event": {
                        "kind": "routing_delta",
                        "source": "coordinator_1",
                        "message_id": "routing-card-1",
                        "text": "正在分析",
                        "partial": True,
                    },
                },
                {
                    "kind": "result",
                    "route": {"kind": "switch", "target_agent": "basic_agent"},
                },
            ]
        ),
    ), patch.object(
        runner, "_build_profile_agent", return_value=MagicMock(memory=MagicMock(steps=[]), tools={})
    ), patch.object(
        runner,
        "_run_basic_stream",
        create=True,
        return_value=_async_events([]),
    ):
        events = asyncio.run(_collect_stream_events(runner.run_stream(session.session_id, "你好")))

    payloads = [json.loads(event[6:]) for event in events]
    assert payloads[1] == {
        "type": "timeline_event",
        "event": {
            "kind": "routing_delta",
            "source": "coordinator_1",
            "message_id": "routing-card-1",
            "text": "正在分析",
            "partial": True,
        },
    }


def test_run_stream_emits_generic_dispatch_delta_for_routing_before_body():
    runner = _make_runner()
    session = AgentSession(
        session_id="timeline-generic-routing-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events(
            [
                {
                    "kind": "timeline",
                    "event": {
                        "kind": "dispatch_delta",
                        "source": "coordinator_1",
                        "display": "dispatch_card",
                        "message_id": "assistant-msg-1",
                        "dispatch_id": "dispatch-run-1-routing",
                        "dispatch_kind": "routing",
                        "display_slot": "before_body",
                        "text": "正在分析",
                        "partial": True,
                    },
                },
                {
                    "kind": "result",
                    "route": {"kind": "switch", "target_agent": "basic_agent"},
                },
            ]
        ),
    ), patch.object(
        runner, "_build_profile_agent", return_value=MagicMock(memory=MagicMock(steps=[]), tools={})
    ), patch.object(
        runner,
        "_run_basic_stream",
        create=True,
        return_value=_async_events([]),
    ):
        events = asyncio.run(_collect_stream_events(runner.run_stream(session.session_id, "你好")))

    payloads = [json.loads(event[6:]) for event in events]
    assert payloads[1] == {
        "type": "timeline_event",
        "event": {
            "kind": "dispatch_delta",
            "source": "coordinator_1",
            "display": "dispatch_card",
            "message_id": "assistant-msg-1",
            "dispatch_id": "dispatch-run-1-routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "text": "正在分析",
            "partial": True,
        },
    }


def test_coordinator_routing_stream_timeout_also_emits_timeline_event():
    runner = _make_runner()
    coordinator_agent = runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []
    session = AgentSession(
        session_id="timeline-timeout-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    runner.config.agent.coordinator_1_timeout_seconds = 1

    async def _collect():
        return [
            item
            async for item in runner._run_coordinator_routing_stream(
                session, "帮我搜索 Jetson Thor 部署方案", None
            )
        ]

    monotonic_values = iter([100.0, 100.2, 101.3])
    last_monotonic = 101.3

    def _fake_monotonic():
        nonlocal last_monotonic
        try:
            last_monotonic = next(monotonic_values)
        except StopIteration:
            pass
        return last_monotonic

    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate_stream",
        return_value=iter(
            [
                ChatMessageStreamDelta(content=""),
                ChatMessageStreamDelta(content="这条增量不应被消费"),
            ]
        ),
    ), patch("backend.agent.agent_runner.time.monotonic", side_effect=_fake_monotonic):
        outputs = asyncio.run(_collect())

    assert outputs == [
        {
            "kind": "dispatch",
            "dispatch_status": "running",
            "dispatch_stage": "routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "dispatch_id": "dispatch-timeline-timeout-session-routing",
        },
        {
            "kind": "timeline",
            "event": {
                "kind": "routing_started",
                "source": "coordinator_1",
                "message_id": "routing-card-timeline-timeout-session",
            },
        },
        {
            "kind": "timeline",
            "event": {
                "kind": "routing_timeout_fallback",
                "source": "coordinator_1",
                "message_id": "routing-card-timeline-timeout-session",
                "from_agent": "coordinator_1",
                "target_agent": "basic_agent",
                "reason": "timeout_fallback",
            },
        },
        {
            "kind": "timeline",
            "event": {
                "kind": "dispatch_delta",
                "source": "coordinator_1",
                "message_id": "routing-card-timeline-timeout-session",
                "dispatch_id": "dispatch-timeline-timeout-session-routing",
                "dispatch_kind": "routing",
                "display": "dispatch_card",
                "display_slot": "before_body",
                "target_agent": "basic_agent",
                "text": "coordinator_1 路由超时，回退到 basic_agent",
                "partial": True,
            },
        },
        {
            "kind": "dispatch",
            "dispatch_status": "running",
            "dispatch_stage": "routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "dispatch_id": "dispatch-timeline-timeout-session-routing",
            "dispatch_message": "coordinator_1 路由超时，回退到 basic_agent",
        },
        {
            "kind": "result",
            "route": {
                "kind": "switch",
                "target_agent": "basic_agent",
                "reason": "timeout_fallback",
            },
        },
    ]


def test_run_coordinator_routing_stream_uses_routing_only_overrides():
    runner = _make_runner()
    coordinator_agent = runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []
    session = AgentSession(
        session_id="stream-routing-overrides-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    async def _collect():
        return [
            item
            async for item in runner._run_coordinator_routing_stream(
                session, "帮我总结", None
            )
        ]

    with patch.object(coordinator_agent, "write_memory_to_messages", return_value=[]), patch.object(
        coordinator_agent.model,
        "generate_stream",
        return_value=iter([ChatMessageStreamDelta(content="直接回复")]),
    ) as mock_generate_stream, patch.object(
        coordinator_agent.model,
        "get_stream_state",
        return_value={"content": "直接回复", "reasoning": ""},
    ):
        outputs = asyncio.run(_collect())

    assert outputs[-1] == {"kind": "result", "route": {"kind": "final", "answer": "直接回复"}}
    call_kwargs = mock_generate_stream.call_args.kwargs
    assert call_kwargs["temperature"] == 0.0
    assert call_kwargs["max_tokens"] == 150
    assert call_kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_run_coordinator_routing_stream_uses_raw_split_marker_without_leaking_delta():
    runner = _make_runner()
    coordinator_agent = runner._build_profile_agent("coordinator_1")
    coordinator_agent.memory.steps = []
    session = AgentSession(
        session_id="stream-routing-split-marker-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    marker = "<<SWITCH_AGENT>> basic_agent"

    async def _collect():
        return [
            item
            async for item in runner._run_coordinator_routing_stream(
                session, "帮我搜索", None
            )
        ]

    with patch.object(
        coordinator_agent, "write_memory_to_messages", return_value=[]
    ), patch.object(
        coordinator_agent.model,
        "generate_stream",
        return_value=iter(()),
    ), patch.object(
        coordinator_agent.model,
        "get_stream_state",
        return_value={"content": marker, "reasoning": ""},
    ):
        outputs = asyncio.run(_collect())

    assert outputs[-1] == {
        "kind": "result",
        "route": {"kind": "switch", "target_agent": "basic_agent"},
    }
    assert all(marker not in str(output) for output in outputs[:-1])


def test_run_stream_keeps_timeout_timeline_event_before_fallback_handoff():
    runner = _make_runner()
    coordinator_agent = MagicMock()
    coordinator_agent.memory.steps = []
    coordinator_agent.tools = {}
    session = AgentSession(
        session_id="stream-timeout-fallback-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    basic_agent = MagicMock()
    basic_agent.memory.steps = []
    basic_agent.tools = {}

    async def _basic_stream():
        yield 'data: {"type": "session_start", "session_id": "stream-timeout-fallback-session"}\n\n'
        yield 'data: {"type": "agent_step", "step_type": "thought", "content": "basic fallback thought", "partial": true}\n\n'
        yield 'data: {"type": "done", "session_id": "stream-timeout-fallback-session"}\n\n'

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {
                "kind": "timeline",
                "event": {
                    "kind": "routing_timeout_fallback",
                    "source": "coordinator_1",
                    "target_agent": "basic_agent",
                    "reason": "timeout_fallback",
                },
            },
            {
                "kind": "dispatch",
                "dispatch_status": "running",
                "dispatch_stage": "routing",
                "dispatch_message": "coordinator_1 路由超时，回退到 basic_agent",
            },
            {
                "kind": "result",
                "route": {
                    "kind": "switch",
                    "target_agent": "basic_agent",
                    "reason": "timeout_fallback",
                },
            },
        ]),
    ), patch.object(
        runner, "_build_profile_agent", return_value=basic_agent
    ), patch.object(
        runner,
        "_run_basic_stream",
        create=True,
        return_value=_basic_stream(),
    ):
        events = asyncio.run(
            _collect_stream_events(runner.run_stream(session.session_id, "帮我搜索 Jetson Thor 部署方案"))
        )

    payloads = [json.loads(event[6:]) for event in events]
    assert payloads[0] == {"type": "session_start", "session_id": "stream-timeout-fallback-session"}
    assert payloads[1]["event"]["kind"] == "routing_timeout_fallback"
    assert payloads[2]["dispatch_message"] == "coordinator_1 路由超时，回退到 basic_agent"
    assert payloads[3]["event"] == {
        "kind": "agent_switched",
        "source": "coordinator_1",
        "target_agent": "basic_agent",
        "reason": "timeout_fallback",
    }
    assert payloads[4]["dispatch_id"] == "dispatch-stream-timeout-fallback-session-routing"
    assert payloads[5]["step_type"] == "thought"
    assert payloads[5]["content"] == "basic fallback thought"
    assert payloads[6] == {"type": "done", "session_id": "stream-timeout-fallback-session"}


def test_run_stream_preserves_coordinator_2_message_timeline_events_from_basic_stream():
    runner = _make_runner()
    session = AgentSession(
        session_id="coordinator-2-stream-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    coordinator_2_agent = MagicMock()
    coordinator_2_agent.memory.steps = []
    coordinator_2_agent.tools = {}

    async def _basic_stream():
        yield 'data: {"type": "session_start", "session_id": "coordinator-2-stream-session"}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_started", "source": "coordinator_2", "message_id": "assistant-msg-42", "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_delta", "source": "coordinator_2", "message_id": "assistant-msg-42", "text": "我们先从边界条件开始", "partial": true, "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_completed", "source": "coordinator_2", "message_id": "assistant-msg-42", "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "done", "session_id": "coordinator-2-stream-session"}\n\n'

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {
                "kind": "result",
                "route": {"kind": "switch", "target_agent": "coordinator_2"},
            },
        ]),
    ), patch.object(
        runner, "_build_profile_agent", return_value=coordinator_2_agent
    ), patch.object(
        runner,
        "_run_basic_stream",
        create=True,
        return_value=_basic_stream(),
    ):
        events = asyncio.run(
            _collect_stream_events(runner.run_stream(session.session_id, "请详细分析这个问题"))
        )

    payloads = [json.loads(event[6:]) for event in events]
    assert payloads[0] == {"type": "session_start", "session_id": "coordinator-2-stream-session"}
    assert payloads[1]["event"]["kind"] == "routing_delta"
    assert payloads[2]["event"]["kind"] == "dispatch_delta"
    assert payloads[3]["dispatch_id"] == "dispatch-coordinator-2-stream-session-routing"
    assert payloads[4]["event"] == {
        "kind": "agent_switched",
        "source": "coordinator_1",
        "target_agent": "coordinator_2",
    }
    assert payloads[5]["dispatch_target_agent"] == "coordinator_2"
    assert payloads[6]["event"]["kind"] == "message_started"
    assert payloads[7]["event"]["kind"] == "message_delta"
    assert payloads[8]["event"]["kind"] == "message_completed"
    assert payloads[9] == {"type": "done", "session_id": "coordinator-2-stream-session"}


def test_run_stream_coordinator_2_secondary_handoff_switches_to_basic_agent():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_1",
        "coordinator_2",
        "basic_agent",
    ]
    session = AgentSession(
        session_id="coord2-secondary-handoff-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    # v2: pre-populate behavior_records 以模拟 _activate_secondary_handoff
    # 在 _run_basic_stream 之前的副作用。v1 时代这些信息会写进
    # next_agent.state["context_pack"]；v2 改为 session.behavior_records。
    session.behavior_records = {
        "route_receipt": {
            "decider": "coordinator_2",
            "decision": "basic_agent",
            "reason_summary": "secondary_handoff",
            "timeout_fallback": False,
        },
        "handoff_ledger": ["coordinator_2 -> basic_agent"],
    }
    coordinator_2_agent = MagicMock()
    coordinator_2_agent.memory.steps = []
    coordinator_2_agent.tools = {"final_answer": MagicMock(name="final_answer")}
    coordinator_2_agent.managed_agents = {
        "basic_agent": MagicMock(name="basic_agent", description="delegate to basic agent")
    }
    basic_agent = MagicMock()
    basic_agent.memory.steps = []
    basic_agent.tools = {}
    basic_agent.state = {}

    async def _coordinator_2_stream():
        yield 'data: {"type": "session_start", "session_id": "coord2-secondary-handoff-session"}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_started", "source": "coordinator_2", "message_id": "assistant-msg-42", "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_delta", "source": "coordinator_2", "message_id": "assistant-msg-42", "text": "需要联网搜索", "partial": true, "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "secondary_handoff", "target_agent": "basic_agent", "task": "联网搜索 Jetson Thor 最新消息"}\n\n'

    async def _basic_agent_stream():
        yield 'data: {"type": "session_start", "session_id": "coord2-secondary-handoff-session"}\n\n'
        yield 'data: {"type": "agent_step", "step_type": "tool_call", "name": "search_and_fetch", "input": {"query": "Jetson Thor 最新消息"}}\n\n'
        yield 'data: {"type": "done", "session_id": "coord2-secondary-handoff-session"}\n\n'

    def _run_basic_stream_side_effect(
        active_session, active_message, active_images, _enable_thinking=None
    ):
        if active_session.current_agent_name == "coordinator_2":
            assert active_session.agent is coordinator_2_agent
            assert active_message == "帮我查 Jetson Thor 最新消息"
            assert active_images is None
            return _coordinator_2_stream()

        assert active_session.current_agent_name == "basic_agent"
        assert active_session.agent is basic_agent
        assert active_message == "联网搜索 Jetson Thor 最新消息"
        assert active_images is None
        assert "context_pack" not in (active_session.agent.state or {}), (
            "v2 禁止 next_agent.state 注入 context_pack。"
            "routing_receipt / handoff_ledger 改由 session.behavior_records 承担。"
        )
        assert active_session.behavior_records["route_receipt"] == {
            "decider": "coordinator_2",
            "decision": "basic_agent",
            "reason_summary": "secondary_handoff",
            "timeout_fallback": False,
        }
        assert "coordinator_2 -> basic_agent" in (
            active_session.behavior_records.get("handoff_ledger") or []
        ), (
            "v2 契约：handoff_ledger 是 append-only 的 Tier 2 审计，"
            "应至少包含本次 secondary_handoff 触发的条目。"
        )
        return _basic_agent_stream()

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {
                "kind": "timeline",
                "event": {
                    "kind": "routing_delta",
                    "source": "coordinator_1",
                    "message_id": "routing-card-coordinator-2-stream-session",
                    "text": "正在分析是否需要切换执行代理",
                    "partial": True,
                },
            },
            {
                "kind": "dispatch",
                "dispatch_status": "running",
                "dispatch_stage": "routing",
                "dispatch_message": "正在分析是否需要切换执行代理",
            },
            {
                "kind": "result",
                "route": {"kind": "switch", "target_agent": "coordinator_2"},
            },
        ]),
    ), patch.object(
        runner,
        "_build_profile_agent",
        side_effect=[coordinator_2_agent, basic_agent],
    ) as mock_build, patch.object(
        runner,
        "_run_basic_stream",
        create=True,
        side_effect=_run_basic_stream_side_effect,
    ) as mock_basic:
        events = asyncio.run(
            _collect_stream_events(
                runner.run_stream(session.session_id, "帮我查 Jetson Thor 最新消息")
            )
        )

    payloads = [json.loads(event[6:]) for event in events]
    assert payloads == [
        {"type": "session_start", "session_id": "coord2-secondary-handoff-session"},
        {
            "type": "timeline_event",
            "event": {
                "kind": "routing_delta",
                "source": "coordinator_1",
                "message_id": "routing-card-coordinator-2-stream-session",
                "text": "正在分析是否需要切换执行代理",
                "partial": True,
            },
        },
        {
            "type": "agent_step",
            "step_type": "dispatch",
            "dispatch_status": "running",
            "dispatch_stage": "routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "dispatch_message": "正在分析是否需要切换执行代理",
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "agent_switched",
                "source": "coordinator_1",
                "target_agent": "coordinator_2",
            },
        },
        {
            "type": "agent_step",
            "step_type": "dispatch",
            "dispatch_status": "completed",
            "dispatch_stage": "handoff",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "dispatch_id": "dispatch-coord2-secondary-handoff-session-routing",
            "dispatch_target_agent": "coordinator_2",
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_started",
                "source": "coordinator_2",
                "message_id": "assistant-msg-42",
                "display": "chat_bubble",
            },
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_delta",
                "source": "coordinator_2",
                "message_id": "assistant-msg-42",
                "text": "需要联网搜索",
                "partial": True,
                "display": "chat_bubble",
            },
        },
        {
            "type": "agent_step",
            "step_type": "dispatch",
            "dispatch_status": "completed",
            "dispatch_stage": "handoff",
            "dispatch_kind": "secondary_handoff",
            "display_slot": "after_body",
            "dispatch_id": "dispatch-coord2-secondary-handoff-session-secondary-handoff",
            "dispatch_target_agent": "basic_agent",
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "agent_switched",
                "source": "coordinator_2",
                "target_agent": "basic_agent",
            },
        },
        {
            "type": "agent_step",
            "step_type": "tool_call",
            "name": "search_and_fetch",
            "input": {"query": "Jetson Thor 最新消息"},
        },
        {"type": "done", "session_id": "coord2-secondary-handoff-session"},
    ]
    assert mock_build.call_args_list[0].args == ("coordinator_2",)
    assert mock_build.call_args_list[1].args == ("basic_agent",)
    assert mock_basic.call_count == 2
    assert session.behavior_records["route_receipt"] == {
        "decider": "coordinator_2",
        "decision": "basic_agent",
        "reason_summary": "secondary_handoff",
        "timeout_fallback": False,
    }
    assert "coordinator_2 -> basic_agent" in (
        session.behavior_records.get("handoff_ledger") or []
    ), (
        "v2 契约：handoff_ledger 是 append-only 的 Tier 2 审计，"
        "应至少包含本次 secondary_handoff 触发的条目。"
    )


def test_run_stream_coordinator_2_secondary_handoff_keeps_trailing_message_completed_before_basic_agent():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_1",
        "coordinator_2",
        "basic_agent",
    ]
    session = AgentSession(
        session_id="coord2-secondary-handoff-trailing-event-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    coordinator_2_agent = MagicMock()
    coordinator_2_agent.memory.steps = []
    coordinator_2_agent.tools = {}
    basic_agent = MagicMock()
    basic_agent.memory.steps = []
    basic_agent.tools = {}
    basic_agent.state = {}

    async def _coordinator_2_stream():
        yield 'data: {"type": "session_start", "session_id": "coord2-secondary-handoff-trailing-event-session"}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_started", "source": "coordinator_2", "message_id": "assistant-msg-99", "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_delta", "source": "coordinator_2", "message_id": "assistant-msg-99", "text": "需要先切换给 basic_agent", "partial": true, "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "secondary_handoff", "target_agent": "basic_agent", "task": "联网搜索 Jetson Thor 最新消息"}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_completed", "source": "coordinator_2", "message_id": "assistant-msg-99", "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "done", "session_id": "coord2-secondary-handoff-trailing-event-session"}\n\n'

    async def _basic_agent_stream():
        yield 'data: {"type": "session_start", "session_id": "coord2-secondary-handoff-trailing-event-session"}\n\n'
        yield 'data: {"type": "agent_step", "step_type": "tool_call", "name": "search_and_fetch", "input": {"query": "Jetson Thor 最新消息"}}\n\n'
        yield 'data: {"type": "done", "session_id": "coord2-secondary-handoff-trailing-event-session"}\n\n'

    def _run_basic_stream_side_effect(
        active_session, active_message, active_images, _enable_thinking=None
    ):
        if active_session.current_agent_name == "coordinator_2":
            return _coordinator_2_stream()
        return _basic_agent_stream()

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {
                "kind": "result",
                "route": {"kind": "switch", "target_agent": "coordinator_2"},
            },
        ]),
    ), patch.object(
        runner,
        "_build_profile_agent",
        side_effect=[coordinator_2_agent, basic_agent],
    ), patch.object(
        runner,
        "_run_basic_stream",
        create=True,
        side_effect=_run_basic_stream_side_effect,
    ):
        events = asyncio.run(
            _collect_stream_events(runner.run_stream(session.session_id, "帮我查 Jetson Thor 最新消息"))
        )

    payloads = [json.loads(event[6:]) for event in events]
    event_kinds = [
        payload["event"]["kind"]
        for payload in payloads
        if payload.get("type") == "timeline_event"
    ]
    assert event_kinds == [
        "routing_delta",
        "dispatch_delta",
        "agent_switched",
        "message_started",
        "message_delta",
        "message_completed",
        "agent_switched",
    ]
    timeline_payloads = [
        payload
        for payload in payloads
        if payload.get("type") == "timeline_event"
    ]
    assert timeline_payloads[5] == {
        "type": "timeline_event",
        "event": {
            "kind": "message_completed",
            "source": "coordinator_2",
            "message_id": "assistant-msg-99",
            "display": "chat_bubble",
        },
    }
    assert timeline_payloads[6] == {
        "type": "timeline_event",
        "event": {
            "kind": "agent_switched",
            "source": "coordinator_2",
            "target_agent": "basic_agent",
        },
    }


def test_run_stream_coordinator_2_secondary_handoff_emits_dispatch_before_message_completed():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_1",
        "coordinator_2",
        "basic_agent",
    ]
    session = AgentSession(
        session_id="coord2-handoff-order-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    coordinator_2_agent = MagicMock()
    coordinator_2_agent.memory.steps = []
    coordinator_2_agent.tools = {}
    basic_agent = MagicMock()
    basic_agent.memory.steps = []
    basic_agent.tools = {}
    basic_agent.state = {}

    async def _coordinator_2_stream():
        yield 'data: {"type": "session_start", "session_id": "coord2-handoff-order-session"}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_started", "source": "coordinator_2", "message_id": "assistant-msg-order", "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_delta", "source": "coordinator_2", "message_id": "assistant-msg-order", "text": "先分析是否需要联网", "partial": true, "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "secondary_handoff", "target_agent": "basic_agent", "task": "联网搜索 Jetson Thor 最新消息"}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_completed", "source": "coordinator_2", "message_id": "assistant-msg-order", "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "done", "session_id": "coord2-handoff-order-session"}\n\n'

    async def _basic_agent_stream():
        yield 'data: {"type": "session_start", "session_id": "coord2-handoff-order-session"}\n\n'
        yield 'data: {"type": "agent_step", "step_type": "tool_call", "name": "search_and_fetch", "input": {"query": "Jetson Thor 最新消息"}}\n\n'
        yield 'data: {"type": "done", "session_id": "coord2-handoff-order-session"}\n\n'

    def _run_basic_stream_side_effect(
        active_session, active_message, active_images, _enable_thinking=None
    ):
        if active_session.current_agent_name == "coordinator_2":
            return _coordinator_2_stream()
        return _basic_agent_stream()

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {
                "kind": "result",
                "route": {"kind": "switch", "target_agent": "coordinator_2"},
            },
        ]),
    ), patch.object(
        runner,
        "_build_profile_agent",
        side_effect=[coordinator_2_agent, basic_agent],
    ), patch.object(
        runner,
        "_run_basic_stream",
        create=True,
        side_effect=_run_basic_stream_side_effect,
    ):
        events = asyncio.run(
            _collect_stream_events(
                runner.run_stream(session.session_id, "帮我查 Jetson Thor 最新消息")
            )
        )

    payloads = [json.loads(event[6:]) for event in events]
    dispatch_index = next(
        index
        for index, payload in enumerate(payloads)
        if payload.get("type") == "agent_step"
        and payload.get("step_type") == "dispatch"
        and payload.get("dispatch_kind") == "secondary_handoff"
    )
    message_completed_index = next(
        index
        for index, payload in enumerate(payloads)
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "message_completed"
        and payload.get("event", {}).get("source") == "coordinator_2"
    )
    switched_index = next(
        index
        for index, payload in enumerate(payloads)
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "agent_switched"
        and payload.get("event", {}).get("source") == "coordinator_2"
    )

    assert dispatch_index < message_completed_index < switched_index


def test_run_stream_coordinator_2_secondary_handoff_stops_when_trailing_event_is_aborted():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_1",
        "coordinator_2",
        "basic_agent",
    ]
    session = AgentSession(
        session_id="coord2-secondary-handoff-trailing-abort-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    coordinator_2_agent = MagicMock()
    coordinator_2_agent.memory.steps = []
    coordinator_2_agent.tools = {}
    basic_agent = MagicMock()
    basic_agent.memory.steps = []
    basic_agent.tools = {}

    async def _coordinator_2_stream():
        yield 'data: {"type": "session_start", "session_id": "coord2-secondary-handoff-trailing-abort-session"}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_started", "source": "coordinator_2", "message_id": "assistant-msg-abort", "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "secondary_handoff", "target_agent": "basic_agent", "task": "联网搜索 Jetson Thor 最新消息"}\n\n'
        yield 'data: {"type": "aborted", "session_id": "coord2-secondary-handoff-trailing-abort-session"}\n\n'
        yield 'data: {"type": "done", "session_id": "coord2-secondary-handoff-trailing-abort-session"}\n\n'

    def _run_basic_stream_side_effect(
        active_session, active_message, active_images, _enable_thinking=None
    ):
        if active_session.current_agent_name == "coordinator_2":
            return _coordinator_2_stream()
        raise AssertionError("basic_agent stream should not start after trailing abort")

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {
                "kind": "result",
                "route": {"kind": "switch", "target_agent": "coordinator_2"},
            },
        ]),
    ), patch.object(
        runner,
        "_build_profile_agent",
        side_effect=[coordinator_2_agent, basic_agent],
    ) as mock_build, patch.object(
        runner,
        "_run_basic_stream",
        create=True,
        side_effect=_run_basic_stream_side_effect,
    ) as mock_basic:
        events = asyncio.run(
            _collect_stream_events(runner.run_stream(session.session_id, "帮我查 Jetson Thor 最新消息"))
        )

    payloads = [json.loads(event[6:]) for event in events]
    assert payloads == [
        {"type": "session_start", "session_id": "coord2-secondary-handoff-trailing-abort-session"},
        {
            "type": "timeline_event",
            "event": {
                "kind": "routing_delta",
                "source": "coordinator_1",
                "message_id": "routing-card-coord2-secondary-handoff-trailing-abort-session",
                "text": "正在分析是否需要切换执行代理",
                "partial": True,
            },
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "dispatch_delta",
                "source": "coordinator_1",
                "message_id": "routing-card-coord2-secondary-handoff-trailing-abort-session",
                "dispatch_id": "dispatch-coord2-secondary-handoff-trailing-abort-session-routing",
                "dispatch_kind": "routing",
                "display": "dispatch_card",
                "display_slot": "before_body",
                "text": "正在分析是否需要切换执行代理",
                "partial": True,
            },
        },
        {
            "type": "agent_step",
            "step_type": "dispatch",
            "dispatch_status": "running",
            "dispatch_stage": "routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "dispatch_id": "dispatch-coord2-secondary-handoff-trailing-abort-session-routing",
            "dispatch_message": "正在分析是否需要切换执行代理",
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "agent_switched",
                "source": "coordinator_1",
                "target_agent": "coordinator_2",
            },
        },
        {
            "type": "agent_step",
            "step_type": "dispatch",
            "dispatch_status": "completed",
            "dispatch_stage": "handoff",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "dispatch_id": "dispatch-coord2-secondary-handoff-trailing-abort-session-routing",
            "dispatch_target_agent": "coordinator_2",
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_started",
                "source": "coordinator_2",
                "message_id": "assistant-msg-abort",
                "display": "chat_bubble",
            },
        },
        {
            "type": "aborted",
            "session_id": "coord2-secondary-handoff-trailing-abort-session",
        },
        {"type": "done", "session_id": "coord2-secondary-handoff-trailing-abort-session"},
    ]
    assert mock_build.call_args_list[0].args == ("coordinator_2",)
    assert mock_basic.call_count == 1


def test_run_basic_stream_emits_coordinator_2_chat_bubble_timeline_instead_of_thought_steps():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    def _agent_stream():
        yield ChatMessageStreamDelta(content="我们先从边界条件开始")

    agent.run.side_effect = lambda *args, **kwargs: _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-basic-stream-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "请详细分析这个问题", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    message_id = payloads[1]["event"]["message_id"]
    assert message_id.startswith("assistant-msg-")
    assert payloads == [
        {"type": "session_start", "session_id": "coordinator-2-basic-stream-session"},
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_started",
                "source": "coordinator_2",
                "message_id": message_id,
                "display": "chat_bubble",
            },
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_delta",
                "source": "coordinator_2",
                "message_id": message_id,
                "text": "我们先从边界条件开始",
                "partial": True,
                "reasoning": False,
                "display": "chat_bubble",
            },
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_completed",
                "source": "coordinator_2",
                "message_id": message_id,
                "display": "chat_bubble",
            },
        },
        {"type": "done", "session_id": "coordinator-2-basic-stream-session"},
    ]
    assert session.timeline_events == [
        {
            "kind": "message_started",
            "source": "coordinator_2",
            "message_id": message_id,
            "display": "chat_bubble",
        },
        {
            "kind": "message_delta",
            "source": "coordinator_2",
            "message_id": message_id,
            "text": "我们先从边界条件开始",
            "partial": True,
            "reasoning": False,
            "display": "chat_bubble",
        },
        {
            "kind": "message_completed",
            "source": "coordinator_2",
            "message_id": message_id,
            "display": "chat_bubble",
        },
    ]


def test_run_basic_stream_uses_distinct_coordinator_2_message_id_per_run():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    def _agent_stream():
        yield ChatMessageStreamDelta(content="我们先从边界条件开始")

    agent.run.side_effect = lambda *args, **kwargs: _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-repeat-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    first_events = asyncio.run(
        _collect_stream_events(runner._run_basic_stream(session, "第一次", None))
    )
    second_events = asyncio.run(
        _collect_stream_events(runner._run_basic_stream(session, "第二次", None))
    )

    def _coordinator_message_ids(events):
        payloads = [json.loads(event[6:]) for event in events]
        ids = [
            payload["event"]["message_id"]
            for payload in payloads
            if payload.get("type") == "timeline_event"
            and payload["event"].get("source") == "coordinator_2"
        ]
        return payloads, ids

    first_payloads, first_ids = _coordinator_message_ids(first_events)
    second_payloads, second_ids = _coordinator_message_ids(second_events)

    assert first_ids == [first_ids[0], first_ids[0], first_ids[0]]
    assert second_ids == [second_ids[0], second_ids[0], second_ids[0]]
    assert first_ids[0] != second_ids[0]
    assert [payload["event"]["kind"] for payload in first_payloads if payload.get("type") == "timeline_event"] == [
        "message_started",
        "message_delta",
        "message_completed",
    ]
    assert [payload["event"]["kind"] for payload in second_payloads if payload.get("type") == "timeline_event"] == [
        "message_started",
        "message_delta",
        "message_completed",
    ]


def test_run_basic_stream_does_not_duplicate_coordinator_2_message_delta_on_action_step_flush():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    def _agent_stream():
        yield ChatMessageStreamDelta(content="我们先从边界条件开始")
        yield ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0, end_time=0.1),
            model_output="我们先从边界条件开始",
            tool_calls=[],
            observations=None,
        )

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-dedup-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "请详细分析这个问题", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    message_id = payloads[1]["event"]["message_id"]
    assert message_id.startswith("assistant-msg-")
    assert payloads == [
        {"type": "session_start", "session_id": "coordinator-2-dedup-session"},
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_started",
                "source": "coordinator_2",
                "message_id": message_id,
                "display": "chat_bubble",
            },
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_delta",
                "source": "coordinator_2",
                "message_id": message_id,
                "text": "我们先从边界条件开始",
                "partial": True,
                "reasoning": False,
                "display": "chat_bubble",
            },
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_completed",
                "source": "coordinator_2",
                "message_id": message_id,
                "display": "chat_bubble",
            },
        },
        {"type": "done", "session_id": "coordinator-2-dedup-session"},
    ]


def test_run_basic_stream_does_not_emit_legacy_content_for_coordinator_2_final_answer():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    def _agent_stream():
        yield ChatMessageStreamDelta(content="我们先从边界条件开始")
        yield ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0, end_time=0.1),
            model_output="我们先从边界条件开始",
            tool_calls=[],
            observations=None,
        )
        from smolagents.agents import ActionOutput

        yield ActionOutput(output="最终结论", is_final_answer=True)

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-final-answer-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "请详细分析这个问题", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    message_id = payloads[1]["event"]["message_id"]
    assert message_id.startswith("assistant-msg-")
    assert payloads == [
        {"type": "session_start", "session_id": "coordinator-2-final-answer-session"},
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_started",
                "source": "coordinator_2",
                "message_id": message_id,
                "display": "chat_bubble",
            },
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_delta",
                "source": "coordinator_2",
                "message_id": message_id,
                "text": "我们先从边界条件开始",
                "partial": True,
                "reasoning": False,
                "display": "chat_bubble",
            },
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_delta",
                "source": "coordinator_2",
                "message_id": message_id,
                "text": "最终结论",
                "partial": False,
                "reasoning": False,
                "display": "chat_bubble",
            },
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_completed",
                "source": "coordinator_2",
                "message_id": message_id,
                "display": "chat_bubble",
            },
        },
        {"type": "done", "session_id": "coordinator-2-final-answer-session"},
    ]


def test_run_basic_stream_deduplicates_coordinator_2_final_answer_when_stream_already_includes_reasoning_and_answer():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    final_text = "你好！很高兴见到你！请问今天有什么我可以帮你的吗？"
    streamed_text = (
        '用户只是简单打了个招呼"你好"，这是一个简单的问候，不需要进行复杂推理或调用任何工具。'
        f"\n\n{final_text}"
    )

    def _agent_stream():
        yield ChatMessageStreamDelta(content=streamed_text)
        yield ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0, end_time=0.1),
            model_output=final_text,
            tool_calls=[],
            observations=None,
        )
        from smolagents.agents import ActionOutput

        yield ActionOutput(output=final_text, is_final_answer=True)

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-streamed-final-dedup-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "你好", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    message_id = payloads[1]["event"]["message_id"]
    assert payloads == [
        {"type": "session_start", "session_id": "coordinator-2-streamed-final-dedup-session"},
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_started",
                "source": "coordinator_2",
                "message_id": message_id,
                "display": "chat_bubble",
            },
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_delta",
                "source": "coordinator_2",
                "message_id": message_id,
                "text": streamed_text,
                "partial": True,
                "reasoning": False,
                "display": "chat_bubble",
            },
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_completed",
                "source": "coordinator_2",
                "message_id": message_id,
                "display": "chat_bubble",
            },
        },
        {"type": "done", "session_id": "coordinator-2-streamed-final-dedup-session"},
    ]


def test_run_basic_stream_coordinator_2_tool_event_has_no_control_text_in_chat_bubble():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    from smolagents.memory import ToolCall

    def _agent_stream():
        yield ToolCall(
            name="search_and_fetch",
            arguments={"query": "Jetson Thor 最新消息"},
            id="call_0",
        )

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-filter-tool-transcript-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "帮我查 Jetson Thor 最新消息", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    bubble_texts = [
        payload["event"]["text"]
        for payload in payloads
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "message_delta"
    ]

    assert bubble_texts == []
    assert any(
        payload.get("type") == "agent_step"
        and payload.get("step_type") == "tool_call"
        and payload.get("name") == "search_and_fetch"
        for payload in payloads
    )


def test_run_basic_stream_coordinator_2_handoff_has_no_control_text_in_chat_bubble():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    def _agent_stream():
        yield SecondaryHandoffRequest(target_agent="basic_agent")

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-filter-handoff-json-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "帮我查 Jetson Thor 最新消息", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    bubble_texts = [
        payload["event"]["text"]
        for payload in payloads
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "message_delta"
    ]

    assert bubble_texts == []
    assert any(payload.get("type") == "secondary_handoff" for payload in payloads)


def test_run_basic_stream_coordinator_2_handoff_marker_never_leaks_from_step_stream():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    def _agent_stream():
        yield SecondaryHandoffRequest(target_agent="basic_agent")

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-filter-split-handoff-json-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "帮我查 Agentsociety Omphalos", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    bubble_texts = [
        payload["event"]["text"]
        for payload in payloads
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "message_delta"
    ]

    assert bubble_texts == []
    assert any(payload.get("type") == "secondary_handoff" for payload in payloads)


def test_run_basic_stream_coordinator_2_does_not_route_from_reasoning_marker():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    def _agent_stream():
        delta = ChatMessageStreamDelta(content="<<SWITCH_AGENT>> basic_agent")
        delta.reasoning = True
        yield delta

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-think-switch-array-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "帮我验证这个平台信息", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    bubble_texts = [
        payload["event"]["text"]
        for payload in payloads
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "message_delta"
    ]

    assert bubble_texts == ["<<SWITCH_AGENT>> basic_agent"]
    assert not any(payload.get("type") == "secondary_handoff" for payload in payloads)


def test_run_basic_stream_coordinator_2_consumes_secondary_handoff_request():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    def _agent_stream():
        yield SecondaryHandoffRequest(target_agent="basic_agent")

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-string-final-switch-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "帮我查 Jetson Thor 最新消息", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    bubble_texts = [
        payload["event"]["text"]
        for payload in payloads
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "message_delta"
    ]

    assert bubble_texts == []
    assert any(payload.get("type") == "secondary_handoff" for payload in payloads)


def test_run_basic_stream_execution_steps_use_after_body_display_slot():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    from smolagents.memory import ToolCall
    from smolagents.agents import ToolOutput

    def _agent_stream():
        yield ToolCall(
            name="search_and_fetch",
            arguments={"query": "Jetson Thor 最新消息"},
            id="call_0",
        )
        yield ToolOutput(
            id="call_0",
            output="done",
            observation="找到了最新消息",
            is_final_answer=False,
            tool_call=ToolCall(
                name="search_and_fetch",
                arguments={"query": "Jetson Thor 最新消息"},
                id="call_0",
            ),
        )

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="basic-agent-step-slot-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "帮我查 Jetson Thor 最新消息", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    tool_call_payload = next(
        payload for payload in payloads if payload.get("type") == "agent_step" and payload.get("step_type") == "tool_call"
    )
    observation_payload = next(
        payload for payload in payloads if payload.get("type") == "agent_step" and payload.get("step_type") == "observation"
    )

    assert tool_call_payload["display_slot"] == "after_body"
    assert observation_payload["display_slot"] == "after_body"


def test_run_basic_stream_coordinator_2_does_not_receive_gated_tool_control_text():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    from smolagents.memory import ToolCall

    def _agent_stream():
        yield ToolCall(
            name="search_and_fetch",
            arguments={"query": "Jetson Thor 最新消息"},
            id="call_0",
        )

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-buffer-early-prefix-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "帮我查 Jetson Thor 最新消息", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    bubble_texts = [
        payload["event"]["text"]
        for payload in payloads
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "message_delta"
    ]

    assert bubble_texts == []
    assert any(
        payload.get("type") == "agent_step"
        and payload.get("step_type") == "tool_call"
        and payload.get("name") == "search_and_fetch"
        for payload in payloads
    )


def test_run_basic_stream_coordinator_2_preserves_plain_protocol_explanation():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    prose = "解释工具调用协议时可以使用自然语言，但这轮不会真的调用工具。"

    def _agent_stream():
        yield ChatMessageStreamDelta(content=prose)

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-preserve-prose-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "解释工具调用格式", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    bubble_texts = [
        payload["event"]["text"]
        for payload in payloads
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "message_delta"
    ]

    assert bubble_texts == [prose]


def test_run_basic_stream_coordinator_2_preserves_fenced_control_example_as_prose():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    example = "```text\n<<SWITCH_AGENT>> basic_agent\n```"

    def _agent_stream():
        yield ChatMessageStreamDelta(content=example)

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-preserve-suffix-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "帮我查 Jetson Thor 最新消息", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    bubble_texts = [
        payload["event"]["text"]
        for payload in payloads
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "message_delta"
    ]

    assert bubble_texts == [example]
    assert not any(payload.get("type") == "secondary_handoff" for payload in payloads)


def test_run_basic_stream_coordinator_2_valid_handoff_has_no_prose_or_marker_delta():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    def _agent_stream():
        yield SecondaryHandoffRequest(target_agent="basic_agent")

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-prose-split-handoff-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "请帮我查 Samson Video Japan", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    bubble_texts = [
        payload["event"]["text"]
        for payload in payloads
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "message_delta"
    ]

    assert any(payload.get("type") == "secondary_handoff" for payload in payloads)
    assert bubble_texts == []


def test_run_basic_stream_coordinator_2_hides_complete_switch_marker():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    def _agent_stream():
        yield SecondaryHandoffRequest(target_agent="basic_agent")

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-complete-switch-json-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "请帮我查 Samson Video Japan", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    bubble_texts = [
        payload["event"]["text"]
        for payload in payloads
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "message_delta"
    ]

    assert bubble_texts == []
    assert any(payload.get("type") == "secondary_handoff" for payload in payloads)


def test_run_basic_stream_coordinator_2_does_not_reappend_cleared_switch_marker():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    def _agent_stream():
        yield SecondaryHandoffRequest(target_agent="basic_agent")
        yield ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0, end_time=0.1),
            model_output=None,
            tool_calls=[],
            observations=None,
        )

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-action-step-switch-json-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "请帮我查 Samson Video Japan", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    bubble_texts = [
        payload["event"]["text"]
        for payload in payloads
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "message_delta"
    ]

    assert bubble_texts == []
    assert any(payload.get("type") == "secondary_handoff" for payload in payloads)


def test_run_basic_stream_coordinator_2_preserves_latex_brace_text_without_name_error():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    first_delta = ChatMessageStreamDelta(content="我来推导旋转矩阵。")
    second_delta = ChatMessageStreamDelta(content="{bmatrix} a & b \\\\ c & d \\end{bmatrix}")

    def _agent_stream():
        yield first_delta
        yield second_delta

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-latex-brace-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "推导旋转中心法矩阵", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    bubble_texts = [
        payload["event"]["text"]
        for payload in payloads
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "message_delta"
    ]

    assert bubble_texts == [
        "我来推导旋转矩阵。",
        "{bmatrix} a & b \\\\ c & d \\end{bmatrix}",
    ]


def test_run_basic_stream_marks_coordinator_2_reasoning_deltas_for_chat_bubbles():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    reasoning_text = '用户只是简单打了个招呼"你好"，这是一个普通问候，不需要调用工具。'
    final_text = "你好！很高兴见到你。请问有什么我可以帮你的吗？"

    reasoning_delta = ChatMessageStreamDelta(content=reasoning_text)
    reasoning_delta.reasoning = True

    def _agent_stream():
        yield reasoning_delta
        yield ChatMessageStreamDelta(content=final_text)
        yield ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0, end_time=0.1),
            model_output=final_text,
            tool_calls=[],
            observations=None,
        )
        from smolagents.agents import ActionOutput

        yield ActionOutput(output=final_text, is_final_answer=True)

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-reasoning-flag-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "你好", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    message_id = payloads[1]["event"]["message_id"]
    assert payloads == [
        {"type": "session_start", "session_id": "coordinator-2-reasoning-flag-session"},
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_started",
                "source": "coordinator_2",
                "message_id": message_id,
                "display": "chat_bubble",
            },
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_delta",
                "source": "coordinator_2",
                "message_id": message_id,
                "text": reasoning_text,
                "partial": True,
                "reasoning": True,
                "display": "chat_bubble",
            },
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_delta",
                "source": "coordinator_2",
                "message_id": message_id,
                "text": final_text,
                "partial": True,
                "reasoning": False,
                "display": "chat_bubble",
            },
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_completed",
                "source": "coordinator_2",
                "message_id": message_id,
                "display": "chat_bubble",
            },
        },
        {"type": "done", "session_id": "coordinator-2-reasoning-flag-session"},
    ]


def test_run_basic_stream_emits_coordinator_2_final_answer_when_only_action_output_is_available():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    def _agent_stream():
        from smolagents.agents import ActionOutput

        yield ActionOutput(output="最终结论", is_final_answer=True)

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="coordinator-2-action-output-only-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "请直接给出结论", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    message_id = payloads[1]["event"]["message_id"]
    assert message_id.startswith("assistant-msg-")
    assert payloads == [
        {"type": "session_start", "session_id": "coordinator-2-action-output-only-session"},
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_started",
                "source": "coordinator_2",
                "message_id": message_id,
                "display": "chat_bubble",
            },
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_delta",
                "source": "coordinator_2",
                "message_id": message_id,
                "text": "最终结论",
                "partial": False,
                "reasoning": False,
                "display": "chat_bubble",
            },
        },
        {
            "type": "timeline_event",
            "event": {
                "kind": "message_completed",
                "source": "coordinator_2",
                "message_id": message_id,
                "display": "chat_bubble",
            },
        },
        {"type": "done", "session_id": "coordinator-2-action-output-only-session"},
    ]
    assert session.timeline_events == [
        {
            "kind": "message_started",
            "source": "coordinator_2",
            "message_id": message_id,
            "display": "chat_bubble",
        },
        {
            "kind": "message_delta",
            "source": "coordinator_2",
            "message_id": message_id,
            "text": "最终结论",
            "partial": False,
            "reasoning": False,
            "display": "chat_bubble",
        },
        {
            "kind": "message_completed",
            "source": "coordinator_2",
            "message_id": message_id,
            "display": "chat_bubble",
        },
    ]


def test_run_stream_coordinator_2_secondary_handoff_never_surfaces_switch_marker_as_content_event():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_1",
        "coordinator_2",
        "basic_agent",
    ]
    session = AgentSession(
        session_id="coord2-handoff-no-content-json-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    coordinator_2_agent = MagicMock()
    coordinator_2_agent.memory.steps = []
    coordinator_2_agent.tools = {}
    basic_agent = MagicMock()
    basic_agent.memory.steps = []
    basic_agent.tools = {}
    basic_agent.state = {}

    async def _coordinator_2_stream():
        yield 'data: {"type": "session_start", "session_id": "coord2-handoff-no-content-json-session"}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_started", "source": "coordinator_2", "message_id": "assistant-msg-no-content-json", "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_delta", "source": "coordinator_2", "message_id": "assistant-msg-no-content-json", "text": "我先解释一下为什么需要转交。", "partial": true, "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "secondary_handoff", "target_agent": "basic_agent", "task": "联网搜索 Jetson Thor 最新消息"}\n\n'
        yield 'data: {"type": "content", "content": "<<SWITCH_AGENT>> basic_agent"}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_completed", "source": "coordinator_2", "message_id": "assistant-msg-no-content-json", "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "done", "session_id": "coord2-handoff-no-content-json-session"}\n\n'

    async def _basic_agent_stream():
        yield 'data: {"type": "session_start", "session_id": "coord2-handoff-no-content-json-session"}\n\n'
        yield 'data: {"type": "agent_step", "step_type": "tool_call", "name": "search_and_fetch", "input": {"query": "Jetson Thor 最新消息"}}\n\n'
        yield 'data: {"type": "done", "session_id": "coord2-handoff-no-content-json-session"}\n\n'

    def _run_basic_stream_side_effect(
        active_session, active_message, active_images, _enable_thinking=None
    ):
        if active_session.current_agent_name == "coordinator_2":
            return _coordinator_2_stream()
        return _basic_agent_stream()

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {
                "kind": "result",
                "route": {"kind": "switch", "target_agent": "coordinator_2"},
            },
        ]),
    ), patch.object(
        runner,
        "_build_profile_agent",
        side_effect=[coordinator_2_agent, basic_agent],
    ), patch.object(
        runner,
        "_run_basic_stream",
        create=True,
        side_effect=_run_basic_stream_side_effect,
    ):
        events = asyncio.run(
            _collect_stream_events(
                runner.run_stream(session.session_id, "帮我查 Jetson Thor 最新消息")
            )
        )

    payloads = [json.loads(event[6:]) for event in events]
    leaked_content_payloads = [
        payload for payload in payloads
        if payload.get("type") == "content"
        and payload.get("content") == "<<SWITCH_AGENT>> basic_agent"
    ]
    assert leaked_content_payloads == []


def test_run_stream_coordinator_2_secondary_handoff_ignores_lone_agent_switched_timeline_signal():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_1",
        "coordinator_2",
        "basic_agent",
    ]
    session = AgentSession(
        session_id="coord2-no-fallback-handoff-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    coordinator_2_agent = MagicMock()
    coordinator_2_agent.memory.steps = []
    coordinator_2_agent.tools = {}
    basic_agent = MagicMock()
    basic_agent.memory.steps = []
    basic_agent.tools = {}

    async def _coordinator_2_stream():
        yield 'data: {"type": "session_start", "session_id": "coord2-no-fallback-handoff-session"}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_started", "source": "coordinator_2", "message_id": "assistant-msg-no-fallback", "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_delta", "source": "coordinator_2", "message_id": "assistant-msg-no-fallback", "text": "我继续直接回答。", "partial": true, "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "agent_switched", "source": "coordinator_2", "target_agent": "basic_agent"}}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_completed", "source": "coordinator_2", "message_id": "assistant-msg-no-fallback", "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "done", "session_id": "coord2-no-fallback-handoff-session"}\n\n'

    def _run_basic_stream_side_effect(
        active_session, active_message, active_images, _enable_thinking=None
    ):
        if active_session.current_agent_name == "coordinator_2":
            return _coordinator_2_stream()
        raise AssertionError("basic_agent stream should not start without secondary_handoff event")

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {
                "kind": "result",
                "route": {"kind": "switch", "target_agent": "coordinator_2"},
            },
        ]),
    ), patch.object(
        runner,
        "_build_profile_agent",
        side_effect=[coordinator_2_agent, basic_agent],
    ) as mock_build, patch.object(
        runner,
        "_run_basic_stream",
        create=True,
        side_effect=_run_basic_stream_side_effect,
    ) as mock_basic:
        events = asyncio.run(
            _collect_stream_events(
                runner.run_stream(session.session_id, "帮我查 Jetson Thor 最新消息")
            )
        )

    payloads = [json.loads(event[6:]) for event in events]
    secondary_handoff_payloads = [
        payload for payload in payloads if payload.get("type") == "secondary_handoff"
    ]
    switched_events = [
        payload["event"]
        for payload in payloads
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "agent_switched"
    ]

    assert secondary_handoff_payloads == []
    assert switched_events == [
        {"kind": "agent_switched", "source": "coordinator_1", "target_agent": "coordinator_2"},
        {"kind": "agent_switched", "source": "coordinator_2", "target_agent": "basic_agent"},
    ]
    assert mock_build.call_count == 1
    assert mock_basic.call_count == 1


def test_run_stream_coordinator_2_secondary_handoff_buffers_dual_signal_agent_switched_until_after_dispatch():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_1",
        "coordinator_2",
        "basic_agent",
    ]
    session = AgentSession(
        session_id="coord2-secondary-handoff-buffer-switch-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    coordinator_2_agent = MagicMock()
    coordinator_2_agent.memory.steps = []
    coordinator_2_agent.tools = {}
    basic_agent = MagicMock()
    basic_agent.memory.steps = []
    basic_agent.tools = {}
    basic_agent.state = {}

    async def _coordinator_2_stream():
        yield 'data: {"type": "session_start", "session_id": "coord2-secondary-handoff-buffer-switch-session"}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_started", "source": "coordinator_2", "message_id": "assistant-msg-buffered-switch", "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_delta", "source": "coordinator_2", "message_id": "assistant-msg-buffered-switch", "text": "需要联网搜索", "partial": true, "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "agent_switched", "source": "coordinator_2", "target_agent": "basic_agent"}}\n\n'
        yield 'data: {"type": "secondary_handoff", "target_agent": "basic_agent", "task": "联网搜索 Jetson Thor 最新消息"}\n\n'
        yield 'data: {"type": "timeline_event", "event": {"kind": "message_completed", "source": "coordinator_2", "message_id": "assistant-msg-buffered-switch", "display": "chat_bubble"}}\n\n'
        yield 'data: {"type": "done", "session_id": "coord2-secondary-handoff-buffer-switch-session"}\n\n'

    async def _basic_agent_stream():
        yield 'data: {"type": "session_start", "session_id": "coord2-secondary-handoff-buffer-switch-session"}\n\n'
        yield 'data: {"type": "done", "session_id": "coord2-secondary-handoff-buffer-switch-session"}\n\n'

    def _run_basic_stream_side_effect(
        active_session, active_message, active_images, _enable_thinking=None
    ):
        if active_session.current_agent_name == "coordinator_2":
            return _coordinator_2_stream()
        return _basic_agent_stream()

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {
                "kind": "result",
                "route": {"kind": "switch", "target_agent": "coordinator_2"},
            },
        ]),
    ), patch.object(
        runner,
        "_build_profile_agent",
        side_effect=[coordinator_2_agent, basic_agent],
    ), patch.object(
        runner,
        "_run_basic_stream",
        create=True,
        side_effect=_run_basic_stream_side_effect,
    ):
        events = asyncio.run(
            _collect_stream_events(
                runner.run_stream(session.session_id, "帮我查 Jetson Thor 最新消息")
            )
        )

    payloads = [json.loads(event[6:]) for event in events]
    dispatch_index = next(
        index
        for index, payload in enumerate(payloads)
        if payload.get("type") == "agent_step"
        and payload.get("step_type") == "dispatch"
        and payload.get("dispatch_kind") == "secondary_handoff"
    )
    message_completed_index = next(
        index
        for index, payload in enumerate(payloads)
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "message_completed"
        and payload.get("event", {}).get("source") == "coordinator_2"
    )
    switched_index = next(
        index
        for index, payload in enumerate(payloads)
        if payload.get("type") == "timeline_event"
        and payload.get("event", {}).get("kind") == "agent_switched"
        and payload.get("event", {}).get("source") == "coordinator_2"
    )

    assert dispatch_index < message_completed_index < switched_index


def test_run_basic_stream_basic_agent_emits_content_for_final_answer_step_output():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    from smolagents.memory import FinalAnswerStep

    def _agent_stream():
        yield ChatMessageStreamDelta(content="我已经整理好结果。")
        yield FinalAnswerStep(output="这是 FinalAnswerStep 给用户的最终回复")

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="basic-agent-final-answer-step-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "请直接给出最后结论", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    content_payloads = [
        payload["content"] for payload in payloads if payload.get("type") == "content"
    ]

    assert content_payloads == ["这是 FinalAnswerStep 给用户的最终回复"]


def test_run_basic_stream_basic_agent_emits_content_when_only_final_action_output_exists():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    from smolagents.agents import ActionOutput

    def _agent_stream():
        yield ChatMessageStreamDelta(content="先搜索并整理信息")
        yield ActionOutput(output="这是最终给用户的回复", is_final_answer=True)

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="basic-agent-final-output-only-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "帮我查 Jetson Thor 最新消息", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    assert any(
        payload.get("type") == "content"
        and payload.get("content") == "这是最终给用户的回复"
        for payload in payloads
    )


def test_run_basic_stream_basic_agent_does_not_repeat_streamed_final_answer_as_thought_and_content():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    from smolagents.agents import ActionOutput

    final_text = "这是最终给用户的回复"

    def _agent_stream():
        yield ChatMessageStreamDelta(content=final_text)
        yield ActionOutput(output=final_text, is_final_answer=True)

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="basic-agent-streamed-final-dedup-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "请直接给出最后结论", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    thought_payloads = [
        payload for payload in payloads
        if payload.get("type") == "agent_step" and payload.get("step_type") == "thought"
    ]
    content_payloads = [
        payload.get("content") for payload in payloads if payload.get("type") == "content"
    ]

    assert thought_payloads == []
    assert content_payloads == [final_text]


def test_run_basic_stream_basic_agent_keeps_streamed_thought_when_final_answer_differs():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    from smolagents.agents import ActionOutput

    thought_text = "先搜索并整理信息"
    final_text = "这是最终给用户的回复"

    def _agent_stream():
        yield ChatMessageStreamDelta(content=thought_text)
        yield ActionOutput(output=final_text, is_final_answer=True)

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="basic-agent-streamed-thought-keep-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "请直接给出最后结论", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    thought_payloads = [
        payload for payload in payloads
        if payload.get("type") == "agent_step" and payload.get("step_type") == "thought"
    ]
    content_payloads = [
        payload.get("content") for payload in payloads if payload.get("type") == "content"
    ]

    assert thought_payloads == [
        {
            "type": "agent_step",
            "step_type": "thought",
            "content": thought_text,
            "partial": True,
        }
    ]
    assert content_payloads == [final_text]


def test_run_stream_rejects_coordinator_2_secondary_handoff_to_non_basic_agent():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = [
        "coordinator_1",
        "coordinator_2",
        "basic_agent",
    ]
    session = AgentSession(
        session_id="coord2-invalid-secondary-handoff-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    coordinator_2_agent = MagicMock()
    coordinator_2_agent.memory.steps = []
    coordinator_2_agent.tools = {}

    async def _coordinator_2_stream():
        yield 'data: {"type": "session_start", "session_id": "coord2-invalid-secondary-handoff-session"}\n\n'
        yield 'data: {"type": "secondary_handoff", "target_agent": "coordinator_1", "task": "非法转交"}\n\n'
        yield 'data: {"type": "done", "session_id": "coord2-invalid-secondary-handoff-session"}\n\n'

    def _run_basic_stream_side_effect(
        active_session, active_message, active_images, _enable_thinking=None
    ):
        assert active_session.current_agent_name == "coordinator_2"
        return _coordinator_2_stream()

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {
                "kind": "result",
                "route": {"kind": "switch", "target_agent": "coordinator_2"},
            },
        ]),
    ), patch.object(
        runner,
        "_build_profile_agent",
        side_effect=[coordinator_2_agent],
    ), patch.object(
        runner,
        "_run_basic_stream",
        create=True,
        side_effect=_run_basic_stream_side_effect,
    ):
        events = asyncio.run(
            _collect_stream_events(
                runner.run_stream(session.session_id, "请转交")
            )
        )

    payloads = [json.loads(event[6:]) for event in events]
    assert any(
        payload.get("type") == "error"
        and "basic_agent" in payload.get("error", "")
        for payload in payloads
    )


def test_run_basic_stream_basic_agent_does_not_repeat_final_answer_as_thought_and_content():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    from smolagents.agents import ActionOutput

    final_text = "这是最终给用户的回复"

    def _agent_stream():
        yield ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0, end_time=0.1),
            model_output=final_text,
            tool_calls=[],
            observations=None,
            action_output=final_text,
            is_final_answer=True,
        )
        yield ActionOutput(output=final_text, is_final_answer=True)

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="basic-agent-final-dedup-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "请直接给出最后结论", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    thought_payloads = [
        payload for payload in payloads
        if payload.get("type") == "agent_step" and payload.get("step_type") == "thought"
    ]
    content_payloads = [
        payload.get("content") for payload in payloads if payload.get("type") == "content"
    ]

    assert thought_payloads == []
    assert content_payloads == [final_text]


def test_run_basic_stream_basic_agent_emits_final_answer_when_only_final_action_step_exists():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    final_text = "这是最终给用户的回复"

    def _agent_stream():
        yield ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0, end_time=0.1),
            model_output=final_text,
            tool_calls=[],
            observations=None,
            action_output=final_text,
            is_final_answer=True,
        )

    agent.run.return_value = _agent_stream()
    session = AgentSession(
        session_id="basic-agent-final-action-step-only-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "请直接给出最后结论", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    thought_payloads = [
        payload for payload in payloads
        if payload.get("type") == "agent_step" and payload.get("step_type") == "thought"
    ]
    content_payloads = [
        payload.get("content") for payload in payloads if payload.get("type") == "content"
    ]

    assert thought_payloads == []
    assert content_payloads == [final_text]


def test_run_basic_stream_answer_now_does_not_emit_legacy_content_for_coordinator_2():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = [
        ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0, end_time=0.1),
            model_output="已经在聊天气泡里显示的内容",
            tool_calls=[],
            observations=None,
        )
    ]
    agent.tools = {}
    agent.run.return_value = iter([])

    session = AgentSession(
        session_id="coordinator-2-answer-now-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )
    session.answer_now = True

    events = asyncio.run(
        _collect_stream_events(
            runner._run_basic_stream(session, "请立即收口", None)
        )
    )

    payloads = [json.loads(event[6:]) for event in events]
    assert all(payload.get("type") != "content" for payload in payloads)
    assert payloads[-1] == {
        "type": "done",
        "session_id": "coordinator-2-answer-now-session",
    }


def test_run_stream_emits_dispatch_aborted_when_coordinator_stream_is_aborted():
    runner = _make_runner()
    coordinator_agent = MagicMock()
    coordinator_agent.memory.steps = []
    coordinator_agent.tools = {}
    session = AgentSession(
        session_id="stream-dispatch-aborted-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_agent",
    )

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {
                "kind": "timeline",
                "event": {
                    "kind": "routing_delta",
                    "source": "coordinator_1",
                    "message_id": "routing-card-aborted",
                    "text": "正在进行任务调度",
                    "partial": True,
                },
            },
            {"kind": "dispatch", "dispatch_status": "running", "dispatch_stage": "routing", "dispatch_message": "正在进行任务调度"},
            {"kind": "dispatch", "dispatch_status": "aborted", "dispatch_stage": "routing", "dispatch_message": "任务调度已中止"},
            {"kind": "result", "route": {"kind": "aborted"}},
        ]),
    ):
        events = asyncio.run(
            _collect_stream_events(runner.run_stream(session.session_id, "停止这个请求"))
        )

    payloads = [json.loads(event[6:]) for event in events]
    assert payloads == [
        {"type": "session_start", "session_id": "stream-dispatch-aborted-session"},
        {
            "type": "timeline_event",
            "event": {
                "kind": "routing_delta",
                "source": "coordinator_1",
                "message_id": "routing-card-aborted",
                "text": "正在进行任务调度",
                "partial": True,
            },
        },
        {
            "type": "agent_step",
            "step_type": "dispatch",
            "dispatch_status": "running",
            "dispatch_stage": "routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "dispatch_message": "正在进行任务调度",
        },
        {
            "type": "agent_step",
            "step_type": "dispatch",
            "dispatch_status": "aborted",
            "dispatch_stage": "routing",
            "dispatch_kind": "routing",
            "display_slot": "before_body",
            "dispatch_message": "任务调度已中止",
        },
        {"type": "aborted", "session_id": "stream-dispatch-aborted-session"},
        {"type": "done", "session_id": "stream-dispatch-aborted-session"},
    ]


def test_run_stream_passthrough_keeps_dispatch_stream_disabled():
    runner = _make_runner()
    coordinator_agent = MagicMock()
    coordinator_agent.memory.steps = []
    coordinator_agent.tools = {}
    session = AgentSession(
        session_id="stream-passthrough-session",
        agent=coordinator_agent,
        mode="tool_calling",
        current_agent_name="coordinator_agent",
    )

    async def _basic_stream():
        yield 'data: {"type": "session_start", "session_id": "stream-passthrough-session"}\n\n'
        yield 'data: {"type": "agent_step", "step_type": "thought", "content": "basic thought", "partial": true}\n\n'
        yield 'data: {"type": "done", "session_id": "stream-passthrough-session"}\n\n'

    with patch.object(runner, "get_or_create_session", return_value=session), patch.object(
        runner, "_decode_request_images", return_value=None
    ), patch.object(
        runner,
        "_run_coordinator_routing_stream",
        create=True,
        return_value=_async_events([
            {"kind": "result", "route": {"kind": "passthrough"}},
        ]),
    ), patch.object(
        runner,
        "_run_basic_stream",
        create=True,
        return_value=_basic_stream(),
    ) as mock_basic:
        events = asyncio.run(
            _collect_stream_events(runner.run_stream(session.session_id, "hello"))
        )

    payloads = [json.loads(event[6:]) for event in events]
    assert payloads == [
        {"type": "session_start", "session_id": "stream-passthrough-session"},
        {
            "type": "agent_step",
            "step_type": "thought",
            "content": "basic thought",
            "partial": True,
        },
        {"type": "done", "session_id": "stream-passthrough-session"},
    ]
    mock_basic.assert_called_once_with(session, "hello", None, None)


def _async_events(events):
    async def _stream():
        for event in events:
            yield event

    return _stream()


async def _collect_stream_events(stream):
    return [event async for event in stream]
