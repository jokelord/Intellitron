from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from smolagents.memory import ActionStep
from smolagents.models import ChatMessageToolCall, ChatMessageToolCallFunction
from smolagents.monitoring import Timing
from smolagents.utils import AgentToolCallError

from backend.agent.agent_runner import (
    AgentRunner,
    AgentSession,
    _create_tool_wrapper,
)
from backend.config import (
    AgentConfig,
    AppConfig,
    ReadFileConfig,
    get_config,
)
from backend.security.capabilities import OperationCapability, build_session_capability
from backend.skills.load_local_images.load_local_images import load_images_to_ipc
from backend.tools import sys_ops


def _runner_and_agent(tmp_path: Path):
    allowed = tmp_path / "allowed"
    images = tmp_path / "images"
    session_temp = tmp_path / "session-temp"
    for directory in (allowed, images, session_temp):
        directory.mkdir()
    config = AppConfig(
        read_file=ReadFileConfig(allowed_roots=[str(allowed)], max_source_bytes=4096),
        agent=AgentConfig(
            entry_agent_name="basic_agent",
            enabled_agent_profiles=["basic_agent"],
            search_whitelist_paths=[str(allowed)],
            file_operation_roots=[str(allowed)],
            image_input_roots=[str(images)],
            session_temp_base_dir=str(session_temp),
            max_file_bytes=4096,
            work_dir=str(tmp_path),
            python_executable=str(Path(__file__).parents[1] / ".sglang/bin/python"),
            nvm_dir=str(tmp_path / "nvm"),
            command_mode="argv",
            allowed_executables=["printf"],
            exec_timeout=2,
            max_exec_timeout_seconds=3,
            max_output_bytes=1024,
            allow_browser=False,
        ),
        agent_prompts=deepcopy(get_config().agent_prompts),
    )
    runner = AgentRunner(MagicMock(), config)
    session_id = str(uuid4())
    capability = build_session_capability(
        config, session_id=session_id, mode="tool_calling"
    )
    session = AgentSession(
        session_id=session_id,
        agent=SimpleNamespace(memory=SimpleNamespace(steps=[]), state={}),
        mode="tool_calling",
        capability=capability,
        tool_configuration_snapshot=runner.default_tool_configuration_snapshot,
        current_agent_name="basic_agent",
    )
    agent = runner._build_profile_agent("basic_agent", session=session)
    session.agent = agent
    session.active_agent = agent
    runner.sessions.put(session)
    return runner, session, agent


def _step() -> ActionStep:
    return ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
        model_output="canonical tool call",
    )


def _parse_call(runner: AgentRunner, agent, name: str, arguments: dict):
    content = (
        "<<TOOL_CALL>>\n"
        + json.dumps(
            {"name": name, "arguments": arguments},
            ensure_ascii=False,
            indent=2,
        )
        + "\n<<END_TOOL_CALL>>"
    )
    resolution = runner.model._resolve_assistant_control(
        content,
        None,
        agent.tools_and_managed_agents,
    )
    assert resolution.source == "text"
    assert len(resolution.tool_calls) == 1
    return resolution.tool_calls[0]


def _process(agent, call):
    step = _step()
    events = list(agent.process_tool_calls(SimpleNamespace(tool_calls=[call]), step))
    return step, events


@pytest.mark.parametrize(
    ("tool_name", "arguments", "denial_code"),
    [
        (
            "write_file",
            {"file_path": "{outside}", "content": "must not be written"},
            "path_not_allowed",
        ),
        ("exec_command", {"command": "python -V"}, "executable_not_allowed"),
    ],
)
def test_canonical_parser_to_wrapper_denies_unauthorized_side_effects(
    tmp_path, monkeypatch, tool_name, arguments, denial_code
):
    runner, _session, agent = _runner_and_agent(tmp_path)
    outside = tmp_path / "outside.txt"
    arguments = {
        key: value.format(outside=str(outside)) if isinstance(value, str) else value
        for key, value in arguments.items()
    }
    launched = False

    def fail_launch(*_args, **_kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("a denied command must not launch")

    monkeypatch.setattr(sys_ops, "_run_process", fail_launch)
    call = _parse_call(runner, agent, tool_name, arguments)
    step, _events = _process(agent, call)

    assert f"[{denial_code}]" in step.observations
    assert outside.exists() is False
    assert launched is False


def test_canonical_parser_to_real_image_wrapper_denies_outside_input(tmp_path):
    runner, session, agent = _runner_and_agent(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"not-an-authorized-image")

    def load_local_images(
        raw_paths: list[str],
        max_dimension: int = 128,
        *,
        session_id: str | None = None,
        capability: OperationCapability | None = None,
    ) -> str:
        """Load local images through the session-bound IPC loader.

        Args:
            raw_paths: Local image paths to load.
            max_dimension: Maximum image dimension.
            session_id: Internally bound session identity.
            capability: Internally bound operation capability.

        Returns:
            Human-readable image loading result.
        """
        result = load_images_to_ipc(
            session_id or "",
            raw_paths,
            max_dimension=max_dimension,
            capability=capability,
        )
        return result.summary

    tool = _create_tool_wrapper(load_local_images, "load_images_to_ipc")
    tool._injected_session_id = session.session_id
    tool._injected_capability = session.capability
    agent.tools[tool.name] = tool
    call = _parse_call(
        runner,
        agent,
        tool.name,
        {"raw_paths": [str(outside)], "max_dimension": 128},
    )
    step, _events = _process(agent, call)

    assert "[path_not_allowed]" in step.observations
    assert Path(session.capability.session_temp_base_dir, session.session_id).exists() is False


def test_native_tool_call_cannot_override_wrapper_bound_session_id(tmp_path):
    _runner, session, agent = _runner_and_agent(tmp_path)
    call = ChatMessageToolCall(
        id="override-call",
        type="function",
        function=ChatMessageToolCallFunction(
            name="write_file",
            arguments={
                "file_path": str(tmp_path / "outside.txt"),
                "content": "must not be written",
                "session_id": "attacker-session",
            },
        ),
    )

    with pytest.raises(
        AgentToolCallError,
        match="session_id is not in the tool's input schema",
    ):
        _process(agent, call)

    assert session.capability.session_id != "attacker-session"
    assert (tmp_path / "outside.txt").exists() is False


def test_canonical_tool_call_fails_closed_when_bound_capability_is_missing(tmp_path):
    runner, _session, agent = _runner_and_agent(tmp_path)
    target = tmp_path / "allowed" / "never-created.txt"
    agent.tools["write_file"]._injected_capability = None
    call = _parse_call(
        runner,
        agent,
        "write_file",
        {"file_path": str(target), "content": "must not be written"},
    )

    step, _events = _process(agent, call)

    assert "[missing_capability]" in step.observations
    assert target.exists() is False


def test_accepted_canonical_write_preserves_normal_tool_observation(tmp_path):
    runner, _session, agent = _runner_and_agent(tmp_path)
    target = tmp_path / "allowed" / "accepted.txt"
    call = _parse_call(
        runner,
        agent,
        "write_file",
        {"file_path": str(target), "content": "accepted output"},
    )

    step, events = _process(agent, call)

    assert target.read_text(encoding="utf-8") == "accepted output"
    assert "accepted.txt" in step.observations
    assert "安全策略阻断" not in step.observations
    assert events
