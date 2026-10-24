from __future__ import annotations

import os
import pwd
import shlex
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "backend" / "skills" / "running-linux-gui-commands"
RUNNER = SKILL_DIR / "scripts" / "agent-gui-run"
DIAGNOSE = SKILL_DIR / "scripts" / "agent-gui-diagnose"


def test_skill_loader_expands_launcher_path() -> None:
    raw = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "{{SKILL_DIR}}" in raw

    from backend.tools.skill_ops import read_skill

    loaded = read_skill("running-linux-gui-commands")
    assert "{{SKILL_DIR}}" not in loaded
    assert str(SKILL_DIR / "scripts" / "agent-gui-run") in loaded


@pytest.mark.parametrize("profile", ["manager_agent", "basic_agent"])
def test_agent_prompt_delegates_gui_resolution_to_skill(profile: str) -> None:
    from backend.agent.agent_runner import AgentRunner
    from backend.agent.tool_configuration import AgentToolConfigurationRequest
    from backend.config import get_config

    runner = AgentRunner(MagicMock(), get_config())
    snapshot = runner.tool_catalog.resolve_candidate(
        AgentToolConfigurationRequest(
            schema_version=1,
            catalog_revision=runner.tool_catalog.catalog_revision,
            manager_agent_tools=["exec_command"],
            basic_agent_tools=["exec_command"],
        )
    )
    agent = runner._build_profile_agent(profile, tool_snapshot=snapshot)
    prompt = agent.initialize_system_prompt()
    assert {tool.name for tool in agent.tools_and_managed_agents} == (
        {"exec_command", "dispatching_parallel_agent"}
        if profile == "manager_agent" else {"exec_command"}
    )
    # The configured prompt intentionally keeps the GUI skill name generic;
    # only verify that visual-task guidance is injected for exec_command.
    assert "可视化任务" in prompt
    assert "某 skill" in prompt


def test_default_command_policy_allows_only_exact_gui_skill_entrypoints() -> None:
    from backend.config import ConfigLoader
    from backend.security.command_policy import CommandPolicy

    agent = ConfigLoader().load().agent
    policy = CommandPolicy(
        command_mode=agent.command_mode,
        allowed_executables=agent.allowed_executables,
        custom_blacklist=agent.command_blacklist,
        max_exec_timeout_seconds=agent.max_exec_timeout_seconds,
        max_output_bytes=agent.max_output_bytes,
    )

    for entrypoint in (RUNNER, DIAGNOSE):
        decision = policy.evaluate_shell(f"{shlex.quote(str(entrypoint))} --version")
        assert decision.allowed is True, decision

    lookalike = policy.evaluate_shell("/tmp/agent-gui-run -- /bin/true")
    assert lookalike.allowed is False
    assert lookalike.reason_code == "executable_path_not_allowed"


def test_root_custom_log_path_is_rejected_before_file_creation(tmp_path: Path) -> None:
    """The root branch must not truncate or chown an arbitrary caller path."""

    actual_uid = os.getuid()
    actual_user = pwd.getpwuid(actual_uid).pw_name
    runtime = Path("/run/user") / str(actual_uid)
    if not runtime.is_dir() or not any(runtime.glob("wayland-*")):
        pytest.skip("requires an active local Wayland socket")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_id = fake_bin / "id"
    fake_id.write_text(
        "#!/usr/bin/env bash\n"
        "case \"${1:-}\" in\n"
        "  -u) printf '0\\n' ;;\n"
        "  -un) printf 'root\\n' ;;\n"
        "  *) exec /usr/bin/id \"$@\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_id.chmod(0o755)

    fake_loginctl = tmp_path / "loginctl"
    fake_loginctl.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == show-user ]]; then\n"
        f"  printf '%s\\n' '{runtime}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_loginctl.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '/usr/bin:/bin')}",
            "AGENT_GUI_LOGINCTL": str(fake_loginctl),
            "AGENT_GUI_SYSTEMCTL": "/bin/false",
            "AGENT_GUI_RUNTIME_ROOT": str(tmp_path / "run-user"),
            "AGENT_GUI_X11_SOCKET_DIR": str(tmp_path / "x11"),
        }
    )
    for key in (
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "WAYLAND_DISPLAY",
        "DISPLAY",
        "XAUTHORITY",
        "XDG_SESSION_TYPE",
        "XDG_SESSION_ID",
        "XDG_SESSION_CLASS",
        "XDG_SEAT",
    ):
        env.pop(key, None)

    log_path = tmp_path / "must-not-be-created.log"
    result = subprocess.run(
        [
            str(RUNNER),
            "--user",
            actual_user,
            "--wait",
            "--log",
            str(log_path),
            "--",
            "/bin/true",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 64, result.stderr
    assert "custom log path" in result.stderr
    assert not log_path.exists()
