import os
import stat
import sys
from pathlib import Path

from backend.security.capabilities import OperationCapability
from backend.security.environment_builder import CommandRuntimeConfig
from backend.tools import sys_ops


def _runtime(
    tmp_path: Path,
    *,
    profile_dir: str | None = None,
    python_executable: str | None = None,
) -> CommandRuntimeConfig:
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    for name in ("playwright-cli", "npx"):
        executable = tools / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return CommandRuntimeConfig(
        command_mode="restricted_shell",
        allowed_executables=("npx", "playwright-cli", "printf", "python"),
        command_blacklist=None,
        exec_timeout=60,
        max_exec_timeout_seconds=3600,
        max_output_bytes=4096,
        termination_grace_seconds=0.1,
        allow_browser=True,
        work_dir=str(tmp_path),
        python_executable=python_executable or sys.executable,
        nvm_dir=str(tmp_path / "nvm"),
        playwright_profile_dir=profile_dir,
        environment_allowlist=(),
        command_path_entries=(str(tools),),
    )


def _capture_popen(monkeypatch, tmp_path, *, runtime=None):
    captured = {}

    def fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        raise RuntimeError("stop after spawn")

    resolved_runtime = runtime or _runtime(tmp_path)
    monkeypatch.setattr(sys_ops.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        sys_ops,
        "_get_command_runtime",
        lambda runtime_config=None, capability=None: (
            capability.command_runtime_config()
            if capability is not None
            else runtime_config or resolved_runtime
        ),
    )
    return captured


def _capability(tmp_path: Path, runtime: CommandRuntimeConfig, session_id: str):
    root = str(tmp_path.resolve())
    return OperationCapability(
        session_id=session_id,
        read_roots=(root,),
        search_roots=(root,),
        file_operation_roots=(root,),
        image_input_roots=(root,),
        session_temp_base_dir=root,
        command_mode=runtime.command_mode,
        allowed_executables=runtime.allowed_executables,
        max_exec_timeout_seconds=runtime.max_exec_timeout_seconds,
        max_output_bytes=runtime.max_output_bytes,
        max_file_bytes=1024,
        allow_overwrite_existing=True,
        allow_browser=runtime.allow_browser,
        exec_timeout=runtime.exec_timeout,
        termination_grace_seconds=runtime.termination_grace_seconds,
        work_dir=runtime.work_dir,
        python_executable=runtime.python_executable,
        nvm_dir=runtime.nvm_dir,
        playwright_profile_dir=runtime.playwright_profile_dir,
        environment_allowlist=runtime.environment_allowlist,
        command_path_entries=runtime.command_path_entries,
    )


def _browser_argv(captured):
    process_argv = list(captured["args"][0])
    process_argv[0] = Path(process_argv[0]).name
    return tuple(process_argv)


def test_exec_command_builds_minimal_agent_environment(monkeypatch, tmp_path):
    captured = _capture_popen(monkeypatch, tmp_path)
    runtime = _runtime(tmp_path)
    capability = _capability(tmp_path, runtime, "environment-test")
    monkeypatch.setenv("VIRTUAL_ENV", "/home/test/project/.sglang")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/home/test/project/.sglang")
    monkeypatch.setenv("PYTHONHOME", "/home/test/project/.sglang")
    monkeypatch.setenv("PYTHONPATH", "/home/test/project")
    monkeypatch.setenv("SECRET_TOKEN", "must-not-leak")
    sys_ops.exec_command(
        "python -c 'import sys'",
        session_id=capability.session_id,
        capability=capability,
    )
    env = captured["kwargs"]["env"]
    command = captured["args"][0]
    assert env["VIRTUAL_ENV"] == str(Path(sys.executable).absolute().parent.parent)
    assert "UV_PROJECT_ENVIRONMENT" not in env
    assert "PYTHONHOME" not in env
    assert "PYTHONPATH" not in env
    assert "SECRET_TOKEN" not in env
    assert env["PATH"].split(os.pathsep)[0] == str(
        Path(sys.executable).absolute().parent
    )
    assert command[-1] == "python -c 'import sys'"
    assert all("eval" not in value for value in command)


def test_exec_command_rejects_legacy_browser_cli_variants(monkeypatch, tmp_path):
    captured = _capture_popen(monkeypatch, tmp_path)
    runtime = _runtime(tmp_path)
    capability = _capability(tmp_path, runtime, "legacy-cli-session")
    commands = (
        "playwright-cli --version",
        "playwright-cli open https://example.com",
        "npx playwright-cli open https://example.com",
    )

    for command in commands:
        result = sys_ops.exec_command(
            command,
            session_id=capability.session_id,
            capability=capability,
        )
        assert "legacy_browser_cli_disabled" in result

    assert captured == {}


def test_browser_command_open_defaults_to_headed_and_persistent(monkeypatch, tmp_path):
    captured = _capture_popen(monkeypatch, tmp_path)
    runtime = _runtime(tmp_path)
    capability = _capability(tmp_path, runtime, "headed-test")
    sys_ops.browser_command(
        "open https://scholar.google.com",
        session_id=capability.session_id,
        capability=capability,
    )
    browser_argv = _browser_argv(captured)
    assert browser_argv[:5] == (
        "playwright-cli",
        "-s",
        "headed-test",
        "open",
        "https://scholar.google.com",
    )
    assert "--browser=chromium" in browser_argv
    assert "--headed" in browser_argv
    assert "--persistent" in browser_argv


def test_browser_command_open_uses_configured_profile_dir(monkeypatch, tmp_path):
    runtime = _runtime(tmp_path, profile_dir="/tmp/agent-playwright-profile")
    captured = _capture_popen(monkeypatch, tmp_path, runtime=runtime)
    capability = _capability(tmp_path, runtime, "profile-test")
    sys_ops.browser_command(
        "open https://scholar.google.com",
        session_id=capability.session_id,
        capability=capability,
    )
    browser_argv = _browser_argv(captured)
    assert "--profile=/tmp/agent-playwright-profile" in browser_argv
    assert "--persistent" not in browser_argv


def test_browser_command_keeps_explicit_headless_or_profile_flags(
    monkeypatch, tmp_path
):
    runtime = _runtime(tmp_path, profile_dir="/tmp/agent-playwright-profile")
    captured = _capture_popen(monkeypatch, tmp_path, runtime=runtime)
    capability = _capability(tmp_path, runtime, "profile-flags-test")
    sys_ops.browser_command(
        "open https://scholar.google.com --headless --profile=/tmp/custom-profile",
        session_id=capability.session_id,
        capability=capability,
    )
    browser_argv = _browser_argv(captured)
    assert "--headless" in browser_argv
    assert "--profile=/tmp/custom-profile" in browser_argv
    assert "--headed" not in browser_argv
    assert browser_argv.count("--profile=/tmp/custom-profile") == 1


def test_playwright_click_output_adds_tab_switch_hint():
    output = """### Ran Playwright code
```js
await page.getByText('示例新闻').click();
```
### Open tabs
- 0: (current) [新闻门户](https://news.example.com/portal)
- 1: [示例新闻](https://news.example.com/article-1)
- 2: [示例新闻](https://news.example.com/article-1)
### Page
- Page URL: https://news.example.com/portal
- Page Title: 新闻门户
### Snapshot
- [Snapshot](.playwright-cli/page.yml)
"""
    annotated = sys_ops._annotate_playwright_output("playwright-cli click e26", output)
    assert "检测到点击后可能在新标签页打开了页面" in annotated
    assert "tab-select" in annotated
    assert "不要重复点击同一链接" in annotated


def test_exec_command_uses_configured_agent_python_path(monkeypatch, tmp_path):
    custom_python = tmp_path / "custom-agent" / "bin" / "python"
    custom_python.parent.mkdir(parents=True)
    custom_python.symlink_to(sys.executable)
    runtime = _runtime(tmp_path, python_executable=str(custom_python))
    captured = _capture_popen(monkeypatch, tmp_path, runtime=runtime)
    capability = _capability(tmp_path, runtime, "python-path-test")
    sys_ops.exec_command(
        "python -V",
        session_id=capability.session_id,
        capability=capability,
    )
    env = captured["kwargs"]["env"]
    assert env["PATH"].split(os.pathsep)[0] == str(custom_python.parent)
    assert env["VIRTUAL_ENV"] == str(custom_python.parent.parent)
