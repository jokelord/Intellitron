from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from smolagents import Tool

from backend.agent.agent_runner import AgentRunner, AgentSession
from backend.agent.tool_configuration import (
    AgentToolConfigurationError,
    AgentToolConfigurationRequest,
)
from backend.config import get_config
from backend.security.capabilities import build_session_capability


class _NamedTool(Tool):
    description = "Test-only externally discovered tool."
    inputs = {}
    output_type = "string"

    def __init__(self, name: str):
        self.name = name
        super().__init__()

    def forward(self) -> str:
        return self.name


def _snapshot(runner: AgentRunner, manager: list[str], basic: list[str]):
    return runner.tool_catalog.resolve_candidate(
        AgentToolConfigurationRequest(
            schema_version=1,
            catalog_revision=runner.tool_catalog.catalog_revision,
            manager_agent_tools=manager,
            basic_agent_tools=basic,
        )
    )


def _manager_session(runner: AgentRunner, snapshot) -> AgentSession:
    session_id = str(uuid4())
    agent = MagicMock()
    agent.memory.steps = []
    session = AgentSession(
        session_id=session_id,
        agent=agent,
        mode="tool_calling",
        capability=build_session_capability(
            runner.config,
            session_id=session_id,
            mode="tool_calling",
        ),
        tool_configuration_snapshot=snapshot,
        current_agent_name="manager_agent",
    )
    assert runner.sessions.put(session) is None
    return session


def _session_without_snapshot(runner: AgentRunner) -> AgentSession:
    session_id = str(uuid4())
    agent = MagicMock()
    agent.memory.steps = []
    session = AgentSession(
        session_id=session_id,
        agent=agent,
        mode="tool_calling",
        capability=build_session_capability(
            runner.config,
            session_id=session_id,
            mode="tool_calling",
        ),
        current_agent_name="manager_agent",
    )
    runner.sessions.put(session)
    return session


def test_basic_business_selection_preserves_non_business_discovered_tools():
    """Removing the non-business preservation branch must drop mcp_probe."""

    runner = AgentRunner(MagicMock(), get_config())
    snapshot = _snapshot(runner, [], ["read_file"])
    discovered = [_NamedTool("mcp_probe"), _NamedTool("read_file")]

    with patch(
        "backend.agent.agent_runner.get_tools_from_config",
        return_value=discovered,
    ):
        agent = runner._build_profile_agent("basic_agent", tool_snapshot=snapshot)

    assert [name for name in agent.tools if name != "final_answer"] == [
        "read_file",
        "mcp_probe",
    ]


def test_runtime_business_tools_follow_snapshot_catalog_order():
    """Iterating discovery order instead of the frozen plan must fail this test."""

    runner = AgentRunner(MagicMock(), get_config())
    snapshot = _snapshot(runner, [], ["grep", "read_file"])
    discovered = [
        _NamedTool("grep"),
        _NamedTool("mcp_probe"),
        _NamedTool("read_file"),
    ]

    with patch(
        "backend.agent.agent_runner.get_tools_from_config",
        return_value=discovered,
    ):
        agent = runner._build_profile_agent("basic_agent", tool_snapshot=snapshot)

    assert [name for name in agent.tools if name != "final_answer"] == [
        "read_file",
        "grep",
        "mcp_probe",
    ]


def test_manager_dispatch_tool_is_constructed_once():
    """Reintroducing the former pre-filter construction must increment the count."""

    runner = AgentRunner(MagicMock(), get_config())
    snapshot = _snapshot(runner, [], [])
    original = runner._build_dispatching_parallel_agent_tool

    with (
        patch(
            "backend.agent.agent_runner.get_tools_from_config",
            # Manager keeps its pre-feature policy: dynamically discovered
            # non-business tools stay basic-only, while dispatch is mandatory.
            return_value=[_NamedTool("mcp_probe")],
        ),
        patch.object(
            runner,
            "_build_dispatching_parallel_agent_tool",
            side_effect=original,
        ) as build_dispatch,
    ):
        agent = runner._build_profile_agent("manager_agent", tool_snapshot=snapshot)

    assert build_dispatch.call_count == 1
    assert [name for name in agent.tools if name != "final_answer"] == [
        "dispatching_parallel_agent"
    ]


@pytest.mark.asyncio
async def test_child_creation_copies_exact_parent_tool_snapshot():
    """Building a child from defaults instead of the parent snapshot must fail."""

    runner = AgentRunner(MagicMock(), get_config())
    snapshot = _snapshot(runner, ["exec_command"], ["read_file"])
    manager = _manager_session(runner, snapshot)
    runner._run_child_agent_to_final_report = AsyncMock(return_value="done")

    outcome = await runner._dispatching_parallel_agent_for_session(
        manager,
        "snapshot-child",
        "Inspect the requested files independently and report the result.",
        turn_dispatch_id="dispatch-1",
        call_id="call-1",
    )

    child_session_id = runner.child_agent_sessions_by_key[
        (manager.session_id, outcome.child_id)
    ]
    child = runner.sessions.get(child_session_id)
    assert child is not None
    assert child.tool_configuration_snapshot is snapshot


@pytest.mark.asyncio
async def test_child_reuse_fails_closed_on_tool_snapshot_mismatch():
    """Removing the reuse fingerprint guard must allow a tampered child to run."""

    runner = AgentRunner(MagicMock(), get_config())
    parent_snapshot = _snapshot(runner, [], ["read_file"])
    manager = _manager_session(runner, parent_snapshot)
    runner._run_child_agent_to_final_report = AsyncMock(return_value="done")
    first = await runner._dispatching_parallel_agent_for_session(
        manager,
        "stable-child",
        "Inspect the first independent source and report the result.",
        turn_dispatch_id="dispatch-1",
        call_id="call-1",
    )
    child_session_id = runner.child_agent_sessions_by_key[
        (manager.session_id, first.child_id)
    ]
    child = runner.sessions.get(child_session_id)
    assert child is not None
    child.tool_configuration_snapshot = _snapshot(runner, [], ["grep"])

    with pytest.raises(AgentToolConfigurationError) as caught:
        await runner._dispatching_parallel_agent_for_session(
            manager,
            "stable-child",
            "Continue with the same child context and report the result.",
            turn_dispatch_id="dispatch-2",
            call_id="call-2",
        )

    assert caught.value.code == "tool_configuration_locked"
    assert manager.behavior_records["tool_configuration_audit"][-1]["event"] == (
        "child_snapshot_mismatch"
    )


@pytest.mark.asyncio
async def test_child_reset_fails_closed_on_tool_snapshot_mismatch():
    """Reset must not rebuild a tampered shell from ambient defaults."""

    runner = AgentRunner(MagicMock(), get_config())
    parent_snapshot = _snapshot(runner, [], ["read_file"])
    manager = _manager_session(runner, parent_snapshot)
    runner._run_child_agent_to_final_report = AsyncMock(return_value="done")
    first = await runner._dispatching_parallel_agent_for_session(
        manager,
        "reset-child",
        "Inspect the first independent source and report the result.",
        turn_dispatch_id="dispatch-1",
        call_id="call-1",
    )
    child_session_id = runner.child_agent_sessions_by_key[
        (manager.session_id, first.child_id)
    ]
    child = runner.sessions.get(child_session_id)
    assert child is not None
    child.tool_configuration_snapshot = _snapshot(runner, [], ["grep"])
    child_run = runner.child_agent_runs[child_session_id]

    with pytest.raises(AgentToolConfigurationError) as caught:
        runner._reset_child_model_context(child, child_run)

    assert caught.value.code == "tool_configuration_locked"


@pytest.mark.asyncio
async def test_child_creation_rejects_manager_without_frozen_snapshot():
    """Ambient defaults must not become a missing manager session snapshot."""

    runner = AgentRunner(MagicMock(), get_config())
    manager = _session_without_snapshot(runner)

    with pytest.raises(AgentToolConfigurationError) as caught:
        await runner._dispatching_parallel_agent_for_session(
            manager,
            "missing-snapshot-child",
            "Inspect the requested source independently and report the result.",
            turn_dispatch_id="dispatch-1",
            call_id="call-1",
        )

    assert caught.value.code == "tool_configuration_locked"
    assert manager.behavior_records["tool_configuration_audit"][-1]["event"] == (
        "manager_snapshot_missing"
    )


def test_existing_session_build_rejects_missing_frozen_snapshot():
    """Profile reconstruction must not pull mutable runner defaults."""

    runner = AgentRunner(MagicMock(), get_config())
    session = _session_without_snapshot(runner)

    with pytest.raises(AgentToolConfigurationError) as caught:
        runner._build_profile_agent("manager_agent", session=session)

    assert caught.value.code == "tool_configuration_locked"


def test_existing_session_admission_rejects_missing_frozen_snapshot():
    """Existing context admission must not synthesize an ambient snapshot."""

    runner = AgentRunner(MagicMock(), get_config())
    session = _session_without_snapshot(runner)

    with pytest.raises(AgentToolConfigurationError) as caught:
        runner.stage_agent_request_admission(
            session.session_id,
            "tool_calling",
            None,
            0,
            None,
        )

    assert caught.value.code == "tool_configuration_locked"


def test_profile_filter_cannot_read_ambient_default_snapshot():
    """The legacy helper must reject manager/basic calls without a snapshot."""

    runner = AgentRunner(MagicMock(), get_config())

    with pytest.raises(RuntimeError, match="explicit frozen tool snapshot"):
        runner._filter_tools_for_profile("basic_agent", [_NamedTool("read_file")])


def test_profile_build_rejects_missing_selected_runtime_wrapper():
    """Silently skipping a selected-but-unregistered wrapper must fail this test."""

    runner = AgentRunner(MagicMock(), get_config())
    snapshot = _snapshot(runner, [], ["read_file"])

    with (
        patch(
            "backend.agent.agent_runner.get_tools_from_config",
            return_value=[_NamedTool("mcp_probe")],
        ),
        pytest.raises(AgentToolConfigurationError) as caught,
    ):
        runner._build_profile_agent("basic_agent", tool_snapshot=snapshot)

    assert caught.value.code == "tool_prompt_render_failed"
