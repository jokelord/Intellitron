from copy import deepcopy
from unittest.mock import MagicMock

from backend.agent.agent_runner import AgentRunner, AgentSession
from backend.config import get_config


def _make_runner() -> AgentRunner:
    config = deepcopy(get_config())
    config.model.model_type = "text"
    config.agent.entry_agent_name = "coordinator_1"
    config.agent.enabled_agent_profiles = ["coordinator_1", "coordinator_2", "basic_agent"]
    return AgentRunner(MagicMock(), config)


def test_compile_context_pack_for_coordinator_2_uses_route_receipt():
    """v2 契约：_compile_context_pack 仅返回 routing_receipt 作为 Tier 2 审计快照；
    不得返回 working_memory / unresolved_items / tool_ledger /
    recent_visible_history / agent_contract / user_goal。
    """
    runner = _make_runner()
    session = AgentSession(
        session_id="ctx-pack-session",
        agent=MagicMock(),
        mode="tool_calling",
    )
    session.timeline_events = [
        {
            "kind": "routing_decision",
            "source": "coordinator_1",
            "target_agent": "coordinator_2",
            "reason": "需要复杂推理",
        }
    ]
    # v2：尽管 session.behavior_records 仍可能携带历史 v1 字段（兼容旧 session），
    # _compile_context_pack 必须**忽略**它们，不允许泄漏到 LLM 输入。
    session.behavior_records = {
        "unresolved_items": ["还未完成方程推导"],
        "working_memory": ["先整理边界条件"],
        "handoff_ledger": ["coordinator_1 -> coordinator_2"],
        "tool_ledger": ["尚未调用工具"],
    }

    pack = runner._compile_context_pack(
        session,
        target_agent="coordinator_2",
        user_goal="推导方程",
    )

    assert pack["routing_receipt"]["decision"] == "coordinator_2"
    assert pack["routing_receipt"]["reason_summary"] == "需要复杂推理"
    forbidden_keys = {
        "unresolved_items",
        "working_memory",
        "handoff_ledger",
        "tool_ledger",
        "recent_visible_history",
        "agent_contract",
        "user_goal",
        "session_frame",
    }
    leaked = forbidden_keys & set(pack.keys())
    assert not leaked, f"v2 禁止以下字段出现在 context_pack 中：{leaked}"


def test_compile_context_pack_prefers_behavior_route_receipt_when_present():
    """v2 契约：_compile_context_pack 优先使用 session.behavior_records 中的
    route_receipt；即使该字段存在，也仅返回 routing_receipt 这一项。"""
    runner = _make_runner()
    session = AgentSession(
        session_id="ctx-pack-session-override",
        agent=MagicMock(),
        mode="tool_calling",
    )
    session.timeline_events = [
        {
            "kind": "routing_timeout_fallback",
            "source": "system",
            "from_agent": "coordinator_1",
            "target_agent": "basic_agent",
            "reason": "timeout_fallback",
        }
    ]
    session.behavior_records = {
        "route_receipt": {
            "decider": "coordinator_1",
            "decision": "coordinator_2",
            "reason_summary": "已有人审定为复杂推理",
            "timeout_fallback": False,
        },
        "unresolved_items": ["等待继续推导"],
    }

    pack = runner._compile_context_pack(
        session,
        target_agent="coordinator_2",
        user_goal="继续推导",
    )

    assert pack["routing_receipt"]["decision"] == "coordinator_2"
    assert pack["routing_receipt"]["timeout_fallback"] is False
    assert "unresolved_items" not in pack, (
        "v2 禁止 unresolved_items 出现在 context_pack 中。"
    )


def test_compile_context_pack_uses_timeout_fallback_event_for_route_receipt():
    runner = _make_runner()
    session = AgentSession(
        session_id="ctx-pack-timeout-session",
        agent=MagicMock(),
        mode="tool_calling",
    )
    session.timeline_events = [
        {
            "kind": "routing_timeout_fallback",
            "source": "system",
            "from_agent": "coordinator_1",
            "target_agent": "basic_agent",
            "reason": "timeout_fallback",
        }
    ]

    pack = runner._compile_context_pack(
        session,
        target_agent="basic_agent",
        user_goal="处理超时回退",
    )

    assert pack["routing_receipt"] == {
        "decider": "coordinator_1",
        "decision": "basic_agent",
        "reason_summary": "timeout_fallback",
        "timeout_fallback": True,
    }


def test_compile_context_pack_uses_latest_relevant_routing_event():
    runner = _make_runner()
    session = AgentSession(
        session_id="ctx-pack-latest-routing-session",
        agent=MagicMock(),
        mode="tool_calling",
    )
    session.timeline_events = [
        {
            "kind": "routing_decision",
            "source": "coordinator_1",
            "target_agent": "basic_agent",
            "reason": "旧路由",
        },
        {
            "kind": "routing_decision",
            "source": "coordinator_1",
            "target_agent": "coordinator_2",
            "reason": "最新路由",
        },
    ]

    pack = runner._compile_context_pack(
        session,
        target_agent="coordinator_2",
        user_goal="继续推导",
    )

    assert pack["routing_receipt"]["decision"] == "coordinator_2"
    assert pack["routing_receipt"]["reason_summary"] == "最新路由"


def test_compile_context_pack_handles_direct_answer_routing_decision():
    runner = _make_runner()
    session = AgentSession(
        session_id="ctx-pack-direct-answer-session",
        agent=MagicMock(),
        mode="tool_calling",
    )
    session.timeline_events = [
        {
            "kind": "routing_decision",
            "source": "coordinator_1",
            "decision": "direct_answer",
        }
    ]

    pack = runner._compile_context_pack(
        session,
        target_agent="coordinator_1",
        user_goal="直接回答",
    )

    assert pack["routing_receipt"] == {
        "decider": "coordinator_1",
        "decision": "direct_answer",
        "reason_summary": None,
        "timeout_fallback": False,
    }
