from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from backend.config import AppConfig, ConfigLoader
from backend.security.capabilities import build_session_capability
from backend.security.command_policy import CommandPolicy
from backend.tools.sys_ops import exec_command


def _load_agent_config(monkeypatch, tmp_path: Path, agent: dict):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config-overall.yaml").write_text(
        yaml.safe_dump(
            {
                "misc": "config/misc.yaml",
                "agent_operation": "config/agent-operation.yaml",
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "misc.yaml").write_text("compact: {}\n", encoding="utf-8")
    (config_dir / "agent-operation.yaml").write_text(
        yaml.safe_dump({"agent": agent}), encoding="utf-8"
    )

    import backend.config as config_module

    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    return ConfigLoader(config_path=config_dir / "config-overall.yaml").load().agent


def test_command_config_loads_validated_policy_settings(monkeypatch, tmp_path):
    config = _load_agent_config(
        monkeypatch,
        tmp_path,
        {
            "entry_agent_name": "basic_agent",
            "command_mode": "restricted_shell",
            "allowed_executables": ["ls", "rg", "playwright-cli"],
            "max_exec_timeout_seconds": 90,
            "max_output_bytes": 65536,
            "termination_grace_seconds": 0.75,
            "allow_browser": False,
            "work_dir": str(tmp_path),
            "nvm_dir": str(tmp_path / "nvm"),
            "environment_allowlist": ["LANG", "DISPLAY"],
            "command_path_entries": [str(tmp_path / "tools")],
            "command_cwd_allowlist": [str(tmp_path / "documents")],
            "command_blacklist": [
                {
                    "pattern": r"\bterraform\s+destroy\b",
                    "reason": "no infrastructure deletion",
                    "mode": "shell",
                }
            ],
        },
    )

    assert config.command_mode == "restricted_shell"
    assert config.allowed_executables == ["ls", "rg", "playwright-cli"]
    assert config.max_exec_timeout_seconds == 90.0
    assert config.max_output_bytes == 65536
    assert config.termination_grace_seconds == 0.75
    assert config.allow_browser is False
    assert config.work_dir == str(tmp_path)
    assert config.nvm_dir == str(tmp_path / "nvm")
    assert config.environment_allowlist == ["LANG", "DISPLAY"]
    assert config.command_path_entries == [str(tmp_path / "tools")]
    assert config.command_cwd_allowlist == [str(tmp_path / "documents")]


def test_default_agent_config_preserves_desktop_identity_for_system_proxy(monkeypatch):
    from backend.config import get_config

    config = get_config()

    assert "XDG_CURRENT_DESKTOP" in config.agent.environment_allowlist
    assert "DESKTOP_SESSION" in config.agent.environment_allowlist


@pytest.mark.parametrize("home_name", ["alice", "bob"])
def test_command_config_expands_trusted_home_roots_before_policy_validation(
    monkeypatch, tmp_path, home_name
):
    home = tmp_path / home_name
    documents = home / "Documents"
    documents.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    config = _load_agent_config(
        monkeypatch, tmp_path,
        {"command_cwd_allowlist": ["~/Documents", "~"]},
    )
    assert config.command_cwd_allowlist == [str(documents), str(home)]
    policy = CommandPolicy(command_cwd_allowlist=config.command_cwd_allowlist)
    assert policy.evaluate_shell(f"cd {documents}").allowed
    assert not policy.evaluate_shell("cd ~/Documents").allowed


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("command_mode", "unrestricted", "command_mode"),
        ("allowed_executables", [], "allowed_executables"),
        ("allowed_executables", "ls", "allowed_executables"),
        ("max_exec_timeout_seconds", 0, "max_exec_timeout_seconds"),
        ("max_exec_timeout_seconds", "not-a-number", "max_exec_timeout_seconds"),
        ("max_exec_timeout_seconds", 86_401, "max_exec_timeout_seconds"),
        ("max_output_bytes", -1, "max_output_bytes"),
        ("max_output_bytes", 1.5, "max_output_bytes"),
        ("max_output_bytes", 1_073_741_825, "max_output_bytes"),
        ("exec_timeout", 0, "exec_timeout"),
        ("termination_grace_seconds", -0.1, "termination_grace_seconds"),
        ("termination_grace_seconds", float("inf"), "termination_grace_seconds"),
        ("allow_browser", "yes", "allow_browser"),
        ("work_dir", "", "work_dir"),
        ("python_executable", "", "python_executable"),
        ("nvm_dir", "", "nvm_dir"),
        ("work_dir", "relative/work", "work_dir"),
        ("python_executable", "python", "python_executable"),
        ("nvm_dir", "relative/nvm", "nvm_dir"),
        ("playwright_profile_dir", "relative/profile", "playwright_profile_dir"),
        ("environment_allowlist", "LANG", "environment_allowlist"),
        ("environment_allowlist", ["OPENAI_API_KEY"], "environment_allowlist"),
        ("environment_allowlist", ["SSH_AUTH_SOCK"], "environment_allowlist"),
        ("environment_allowlist", ["LD_PRELOAD"], "environment_allowlist"),
        ("environment_allowlist", ["NODE_OPTIONS"], "environment_allowlist"),
        ("environment_allowlist", ["AWS_ACCESS_KEY_ID"], "environment_allowlist"),
        ("command_path_entries", "/opt/bin", "command_path_entries"),
        ("command_cwd_allowlist", "/tmp", "command_cwd_allowlist"),
        ("command_cwd_allowlist", ["relative"], "command_cwd_allowlist"),
        ("command_cwd_allowlist", ["/"], "command_cwd_allowlist"),
        ("command_cwd_allowlist", ["~/Documents/*"], "command_cwd_allowlist"),
        ("command_cwd_allowlist", [None], "command_cwd_allowlist"),
    ],
)
def test_command_config_rejects_invalid_policy_values(
    monkeypatch, tmp_path, field, value, match
):
    with pytest.raises(ValueError, match=match):
        _load_agent_config(
            monkeypatch,
            tmp_path,
            {"entry_agent_name": "basic_agent", field: value},
        )


@pytest.mark.parametrize(
    "blacklist",
    [
        [{"pattern": "[", "reason": "bad", "mode": "shell"}],
        [{"pattern": "", "reason": "bad", "mode": "shell"}],
        [{"pattern": "x", "mode": "shell"}],
        [{"pattern": "x", "reason": "bad", "mode": "invalid"}],
    ],
)
def test_command_config_rejects_invalid_blacklist(monkeypatch, tmp_path, blacklist):
    with pytest.raises(ValueError):
        _load_agent_config(
            monkeypatch,
            tmp_path,
            {
                "entry_agent_name": "basic_agent",
                "command_blacklist": blacklist,
            },
        )


def _make_skill(root, directory="original-browser-folder", *, name="playwright-browser-skill"):
    skill = root / "backend" / "skills" / directory
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test browser skill.\n---\n",
        encoding="utf-8",
    )
    launcher = skill / "scripts" / "playwright-browser"
    launcher.parent.mkdir()
    launcher.write_text("#!/bin/sh\nprintf 'browser launched\\n'\n", encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


def _skill_ref(**overrides):
    return {"skill": "playwright-browser-skill", "path": "scripts/playwright-browser", **overrides}


@pytest.mark.parametrize("move", ["skill_directory", "project_root"])
def test_skill_executable_reference_tracks_relocation_without_granting_other_scripts(
    monkeypatch, tmp_path, move
):
    root = tmp_path / "repository"
    launcher = _make_skill(root)
    other = launcher.with_name("unlisted-script")
    other.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    other.chmod(0o755)
    settings = {"allowed_executables": ["printf", "/opt/custom/keep", _skill_ref()]}
    original = _load_agent_config(monkeypatch, root, settings)
    assert original.allowed_executables == ["printf", "/opt/custom/keep", str(launcher)]
    capability = build_session_capability(
        AppConfig(agent=original), session_id=str(uuid4()), mode="tool_calling"
    )

    old_launcher = str(launcher)
    if move == "skill_directory":
        renamed = launcher.parents[1].with_name("renamed-readable-browser")
        launcher.parents[1].rename(renamed)
        launcher = renamed / "scripts" / "playwright-browser"
    else:
        relocated = tmp_path / "relocated-repository"
        root.rename(relocated)
        launcher = relocated / launcher.relative_to(root)
        root = relocated
    reloaded = _load_agent_config(monkeypatch, root, settings)

    assert reloaded.allowed_executables == ["printf", "/opt/custom/keep", str(launcher)]
    assert capability.allowed_executables == ("printf", "/opt/custom/keep", old_launcher)
    policy = CommandPolicy(allowed_executables=reloaded.allowed_executables)
    assert policy.evaluate_shell(f"{launcher} --probe-runtime").allowed
    assert not policy.evaluate_shell(f"{launcher.with_name('unlisted-script')}").allowed
    assert not policy.evaluate_shell(old_launcher).allowed


def test_plain_executable_entries_do_not_scan_skills(monkeypatch, tmp_path):
    skills = tmp_path / "backend" / "skills"
    skills.parent.mkdir()
    skills.write_text("not a directory", encoding="utf-8")

    config = _load_agent_config(
        monkeypatch, tmp_path, {"allowed_executables": ["printf", "/opt/custom/keep"]}
    )

    assert config.allowed_executables == ["printf", "/opt/custom/keep"]


@pytest.mark.parametrize(
    "reference",
    [
        {"skill": "playwright-browser-skill"},
        _skill_ref(extra="not-supported"),
        _skill_ref(skill="missing-skill"),
        _skill_ref(skill="original-browser-folder"),
        _skill_ref(skill="../playwright-browser-skill"),
        _skill_ref(skill=""),
        _skill_ref(skill=123),
        _skill_ref(path="/bin/sh"),
        _skill_ref(path="../scripts/playwright-browser"),
        _skill_ref(path="scripts/../scripts/playwright-browser"),
        _skill_ref(path="scripts/missing"),
        _skill_ref(path="scripts"),
        _skill_ref(path=""),
        _skill_ref(path=123),
    ],
)
def test_skill_executable_reference_rejects_invalid_grants(monkeypatch, tmp_path, reference):
    _make_skill(tmp_path)

    with pytest.raises(ValueError, match="skill executable"):
        _load_agent_config(monkeypatch, tmp_path, {"allowed_executables": [reference]})


@pytest.mark.parametrize("invalid", ["duplicate", "non_executable", "script_escape", "folder_escape", "metadata_escape"])
def test_skill_executable_reference_fails_closed_for_unsafe_targets(monkeypatch, tmp_path, invalid):
    root = tmp_path / "repository"
    launcher = _make_skill(root)
    if invalid == "duplicate":
        _make_skill(root, "duplicate-browser-folder")
    elif invalid == "non_executable":
        launcher.chmod(0o644)
    else:
        outside = tmp_path / "outside"
        if invalid == "script_escape":
            outside.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            outside.chmod(0o755)
            launcher.unlink()
            launcher.symlink_to(outside)
        elif invalid == "folder_escape":
            launcher.parents[1].rename(outside)
            launcher.parents[1].symlink_to(outside, target_is_directory=True)
        else:
            metadata = launcher.parents[1] / "SKILL.md"
            metadata.rename(outside)
            metadata.symlink_to(outside)

    with pytest.raises(ValueError, match="skill executable"):
        _load_agent_config(monkeypatch, root, {"allowed_executables": [_skill_ref()]})


def test_dynamic_browser_launcher_keeps_public_session_binding(monkeypatch, tmp_path):
    launcher = _make_skill(tmp_path, "new-browser-directory")
    marker = tmp_path / "launched"
    launcher.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf 'browser launched\\n'\n", encoding="utf-8")
    agent = _load_agent_config(
        monkeypatch,
        tmp_path,
        {
            "allowed_executables": [_skill_ref()],
            "python_executable": str(Path(__file__).parents[1] / ".sglang/bin/python"),
            "work_dir": str(tmp_path),
        },
    )
    capability = build_session_capability(
        AppConfig(agent=agent), session_id=str(uuid4()), mode="tool_calling"
    )
    # Changing mutable application settings must not change admitted authority.
    agent.allowed_executables.clear()
    for arguments, expected in (
        ("help", "invalid_browser_skill_session"),
        (f"--session-id {uuid4()} help", "capability_session_mismatch"),
    ):
        result = exec_command(
            f"{launcher} {arguments}", session_id=capability.session_id, capability=capability
        )
        assert expected in result
        assert not marker.exists()

    result = exec_command(
        f"{launcher} --session-id {capability.session_id} help",
        session_id=capability.session_id,
        capability=capability,
    )
    assert "browser launched" in result
    assert marker.is_file()


def test_public_browser_binding_rejects_multiple_allowlisted_launcher_invocations(monkeypatch, tmp_path):
    first = _make_skill(tmp_path)
    second = _make_skill(tmp_path, "secondary-browser-folder", name="secondary-browser")
    marker = tmp_path / "launched"
    for launcher in (first, second):
        launcher.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    agent = _load_agent_config(
        monkeypatch,
        tmp_path,
        {
            "allowed_executables": [_skill_ref(), _skill_ref(skill="secondary-browser")],
            "python_executable": str(Path(__file__).parents[1] / ".sglang/bin/python"),
            "work_dir": str(tmp_path),
        },
    )
    capability = build_session_capability(
        AppConfig(agent=agent), session_id=str(uuid4()), mode="tool_calling"
    )

    result = exec_command(
        f"{first} --session-id {capability.session_id} help && "
        f"{second} --session-id {capability.session_id} help",
        session_id=capability.session_id,
        capability=capability,
    )

    assert "invalid_browser_skill_invocation" in result
    assert not marker.exists()
