"""
Prefix Cache Stability v2 — 回归与不变量测试

本文件覆盖 v2 SPEC 提出的核心不变量：
  - 同一 session 内 `_build_runtime_context_for_profile` 必须 session-pinned，
    不能覆盖已冻结的 runtime_context。
  - 同一 session 同一 profile 两次构造 agent，其 system_prompt 字节级稳定。
  - `_activate_profile_agent` 不再向 `state["context_pack"]` 写入。
  - `_activate_secondary_handoff` 不再维护 `working_memory`。
  - `_compile_context_pack`（v2 已删除）整方法不应再存在。
  - `_decode_request_images` 在图片不超 max_image_dimension 时保留
    `_pre_encoded_b64` / `_pre_encoded_mime`，保证 prefix cache 字节级稳定。
  - `route_receipt` 与 `handoff_ledger` 仍保留为 Tier 2 审计字段。
"""

from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from backend.agent.agent_runner import (
    AgentRunner,
    AgentSession,
    SecondaryHandoffRequest,
)
from backend.config import get_config


# ──────────────────────────────────────────────────────────────
# 工具 / 共享 fixture
# ──────────────────────────────────────────────────────────────


def _make_runner(llm_service: MagicMock | None = None) -> AgentRunner:
    config = deepcopy(get_config())
    config.model.model_type = "text"
    config.agent.entry_agent_name = "coordinator_1"
    config.agent.enabled_agent_profiles = [
        "coordinator_1",
        "coordinator_2",
        "basic_agent",
    ]
    return AgentRunner(llm_service or MagicMock(), config)


# ──────────────────────────────────────────────────────────────
# T-6: runtime_context session-pinned
# ──────────────────────────────────────────────────────────────


def test_runtime_context_is_session_pinned_and_not_overwritten():
    """同 session 第二次调用 _build_runtime_context_for_profile 必须返回同一字符串。"""
    runner = _make_runner()

    session = AgentSession(
        session_id="pinned-runtime-context-session",
        agent=MagicMock(),
        mode="tool_calling",
    )
    assert session.runtime_context is None

    first_value = runner._build_runtime_context_for_profile(
        "basic_agent", session=session
    )
    assert first_value is not None
    assert session.runtime_context == first_value

    second_value = runner._build_runtime_context_for_profile(
        "basic_agent", session=session
    )
    assert second_value == first_value
    assert session.runtime_context == first_value


def test_runtime_context_never_overrides_existing_value():
    """即使用户已写入自定义 runtime_context，也绝不被覆盖。"""
    runner = _make_runner()

    session = AgentSession(
        session_id="pinned-runtime-override-session",
        agent=MagicMock(),
        mode="tool_calling",
    )
    session.runtime_context = "Current local time: pinned-value"
    result = runner._build_runtime_context_for_profile(
        "basic_agent", session=session
    )
    assert result == "Current local time: pinned-value"
    assert session.runtime_context == "Current local time: pinned-value"


def test_coordinator_1_never_receives_runtime_context():
    """coordinator_1 是纯路由 profile，按 SPEC §5.2 不应注入 runtime time。"""
    runner = _make_runner()

    session = AgentSession(
        session_id="coordinator-1-no-runtime-session",
        agent=MagicMock(),
        mode="tool_calling",
    )
    result = runner._build_runtime_context_for_profile(
        "coordinator_1", session=session
    )
    assert result is None
    assert session.runtime_context is None


# ──────────────────────────────────────────────────────────────
# T-2: _activate_profile_agent 不再写 state["context_pack"]
# ──────────────────────────────────────────────────────────────


def test_activate_profile_agent_does_not_write_context_pack_into_state():
    """v2 严格要求：state["context_pack"] 永远不被 LLM 消费，必须不再写入。"""
    runner = _make_runner()
    current_agent = MagicMock()
    current_agent.memory.steps = []
    current_agent.state = {"shared": True}
    session = AgentSession(
        session_id="no-context-pack-state-session",
        agent=current_agent,
        mode="tool_calling",
    )
    next_agent = MagicMock()
    next_agent.memory.steps = []
    next_agent.state = {}

    with patch.object(runner, "_build_profile_agent", return_value=next_agent):
        runner._activate_profile_agent(session, "coordinator_2")

    assert "context_pack" not in (next_agent.state or {}), (
        "v2 禁止向 agent.state 写入 context_pack。"
        "该字段从未被 LLM 消费，写入会误导未来维护者。"
    )


def test_activate_profile_agent_does_not_invoke_compile_context_pack():
    """v2 整体移除 _compile_context_pack；新代码路径不应再调用它。"""
    runner = _make_runner()
    current_agent = MagicMock()
    current_agent.memory.steps = []
    current_agent.state = {"shared": True}
    session = AgentSession(
        session_id="no-compile-call-session",
        agent=current_agent,
        mode="tool_calling",
    )
    next_agent = MagicMock()
    next_agent.memory.steps = []
    next_agent.state = {}

    with patch.object(runner, "_build_profile_agent", return_value=next_agent), \
         patch.object(
             runner, "_compile_context_pack", create=True
         ) as mock_compile:
        runner._activate_profile_agent(session, "coordinator_2")

    mock_compile.assert_not_called()


# ──────────────────────────────────────────────────────────────
# T-3: _activate_secondary_handoff 不再写 working_memory
# ──────────────────────────────────────────────────────────────


def test_secondary_handoff_does_not_write_working_memory():
    """v2 删除 working_memory sidecar；handoff 不应再写入该字段。"""
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
        session_id="no-working-memory-handoff-session",
        agent=current_agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )
    next_agent = MagicMock()
    next_agent.memory.steps = []
    next_agent.state = {}

    with patch.object(runner, "_build_profile_agent", return_value=next_agent):
        runner._activate_secondary_handoff(
            session,
            SecondaryHandoffRequest(
                target_agent="basic_agent",
                task="搜索最新新闻",
                additional_args={"keyword": "Jetson Thor"},
            ),
            source_agent="coordinator_2",
            fallback_user_goal="原始用户问题",
        )

    assert "working_memory" not in (session.behavior_records or {}), (
        "v2 禁止在 session.behavior_records 中维护 working_memory；"
        "该字段一旦恢复写入，未来注入 LLM 时会破坏 prefix cache 稳定性。"
    )


# ──────────────────────────────────────────────────────────────
# T-5: route_receipt / handoff_ledger 仍保留为 Tier 2 审计
# ──────────────────────────────────────────────────────────────


def test_secondary_handoff_preserves_route_receipt_and_handoff_ledger():
    """v2 保留 route_receipt / handoff_ledger 作为 Tier 2 审计字段。"""
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
        session_id="route-receipt-preserved-session",
        agent=current_agent,
        mode="tool_calling",
        current_agent_name="coordinator_2",
    )
    next_agent = MagicMock()
    next_agent.memory.steps = []
    next_agent.state = {}

    with patch.object(runner, "_build_profile_agent", return_value=next_agent):
        runner._activate_secondary_handoff(
            session,
            SecondaryHandoffRequest(
                target_agent="basic_agent",
                task="搜索最新新闻",
            ),
            source_agent="coordinator_2",
            fallback_user_goal="原始用户问题",
        )

    assert session.behavior_records["route_receipt"] == {
        "decider": "coordinator_2",
        "decision": "basic_agent",
        "reason_summary": "secondary_handoff",
        "timeout_fallback": False,
    }
    assert session.behavior_records["handoff_ledger"] == [
        "coordinator_2 -> basic_agent"
    ]


# ──────────────────────────────────────────────────────────────
# T-1/T-4: _compile_context_pack 不再返回 v1 字段
# ──────────────────────────────────────────────────────────────


def test_compile_context_pack_no_longer_returns_v1_sidecar_fields():
    """v2 严格契约：_compile_context_pack 不得返回 working_memory / unresolved_items
    / tool_ledger / recent_visible_history / agent_contract / user_goal。
    """
    runner = _make_runner()
    session = AgentSession(
        session_id="compile-pack-v2-contract-session",
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
    session.behavior_records = {
        "unresolved_items": ["还未完成"],
        "working_memory": ["先整理边界"],
        "handoff_ledger": ["coordinator_1 -> coordinator_2"],
        "tool_ledger": ["尚未调用工具"],
    }

    pack = runner._compile_context_pack(
        session, target_agent="coordinator_2", user_goal="推导方程"
    )

    forbidden_fields = {
        "working_memory",
        "unresolved_items",
        "tool_ledger",
        "recent_visible_history",
        "agent_contract",
        "user_goal",
    }
    leaked = forbidden_fields & set(pack.keys())
    assert not leaked, f"v2 禁止以下字段出现在 context_pack 中：{leaked}"


def test_compile_context_pack_route_receipt_preserved():
    """v2 仍保留 routing_receipt 作为审计字段。"""
    runner = _make_runner()
    session = AgentSession(
        session_id="compile-pack-routing-receipt-session",
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

    pack = runner._compile_context_pack(
        session, target_agent="coordinator_2", user_goal="推导方程"
    )

    assert "routing_receipt" in pack
    assert pack["routing_receipt"]["decision"] == "coordinator_2"
    assert pack["routing_receipt"]["reason_summary"] == "需要复杂推理"


# ──────────────────────────────────────────────────────────────
# T-7: 同 session 同 profile 两次 build agent，system_prompt 字节级稳定
# ──────────────────────────────────────────────────────────────


def test_build_profile_agent_yields_byte_stable_system_prompt():
    """v2 核心不变量：同 session 同 profile 两次 _build_profile_agent 产出
    的 agent.system_prompt 字符串字节级相同，否则 prefix cache 无法复用。"""
    runner = _make_runner()
    session = AgentSession(
        session_id="stable-system-prompt-session",
        agent=MagicMock(),
        mode="tool_calling",
    )

    agent_a = runner._build_profile_agent("basic_agent", session=session)
    agent_b = runner._build_profile_agent("basic_agent", session=session)

    assert agent_a.system_prompt == agent_b.system_prompt


# ──────────────────────────────────────────────────────────────
# T-8: _decode_request_images 在不超限时保留 _pre_encoded_b64
# ──────────────────────────────────────────────────────────────


def test_decode_request_images_preserves_pre_encoded_b64_when_within_limits():
    """v2 核心不变量：图片未触发缩放时，_pre_encoded_b64 必须与原 base64 字节相同。
    否则同一图片在 prefix 中重新 JPEG 编码会造成 base64 抖动，破坏 prefix cache。
    """
    runner = _make_runner()
    runner.config.model.max_image_size = 10 * 1024 * 1024
    runner.config.model.max_image_dimension = 1280
    runner.config.model.supported_formats = "png,jpg,jpeg,gif,webp"

    img = Image.new("RGB", (16, 16), (255, 0, 0))
    import base64
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    jpeg_bytes = buf.getvalue()
    original_b64 = base64.b64encode(jpeg_bytes).decode()
    data_url = f"data:image/jpeg;base64,{original_b64}"

    decoded = runner._decode_request_images([data_url])

    assert decoded is not None and len(decoded) == 1
    pil_img = decoded[0]
    assert getattr(pil_img, "_pre_encoded_b64", None) == original_b64
    assert getattr(pil_img, "_pre_encoded_mime", None) == "image/jpeg"
