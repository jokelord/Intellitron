from pathlib import Path
from uuid import uuid4

from backend.agent.agent_runner import _create_tool_wrapper
from backend.config import AgentConfig, AppConfig, ReadFileConfig
from backend.security.capabilities import build_session_capability
from backend.skills.load_local_images.load_local_images import load_images_to_ipc
from backend.tools import file_ops, search_ops, sys_ops


def _capability(tmp_path: Path, *, allow_browser: bool = False):
    root = tmp_path / "allowed"
    root.mkdir(exist_ok=True)
    config = AppConfig(
        read_file=ReadFileConfig(allowed_roots=[str(root)], max_source_bytes=4096),
        agent=AgentConfig(
            search_whitelist_paths=[str(root)],
            work_dir=str(tmp_path),
            python_executable=str(Path(__file__).parents[1] / ".sglang/bin/python"),
            nvm_dir=str(tmp_path / "nvm"),
            command_mode="argv",
            allowed_executables=["printf", "playwright-cli"],
            exec_timeout=2,
            max_exec_timeout_seconds=3,
            max_output_bytes=1024,
            allow_browser=allow_browser,
        ),
    )
    return build_session_capability(
        config, session_id=str(uuid4()), mode="tool_calling"
    )


def _bound(tool, capability):
    tool._injected_session_id = capability.session_id
    tool._injected_capability = capability
    return tool


def test_wrapped_write_and_edit_are_denied_outside_capability(tmp_path):
    capability = _capability(tmp_path)
    outside = tmp_path / "outside.txt"
    write = _bound(_create_tool_wrapper(file_ops.write_file, "write_file"), capability)
    edit = _bound(_create_tool_wrapper(file_ops.edit_file, "edit_file"), capability)

    write_result = write.forward(file_path=str(outside), content="secret")
    outside.write_text("before", encoding="utf-8")
    edit_result = edit.forward(
        file_path=str(outside), search_text="before", replace_text="after"
    )

    assert "[path_not_allowed]" in write_result
    assert "[path_not_allowed]" in edit_result
    assert outside.read_text(encoding="utf-8") == "before"


def test_side_effecting_base_boundaries_fail_without_capability(tmp_path, monkeypatch):
    target = tmp_path / "never-created.txt"
    assert "[missing_capability]" in file_ops.write_file(str(target), "no")
    assert not target.exists()

    launched = False

    def fail_launch(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("process must not launch")

    monkeypatch.setattr(sys_ops, "_run_process", fail_launch)
    result = sys_ops.exec_command("printf no")
    assert "[missing_capability]" in result
    assert launched is False


def test_read_search_and_process_wrappers_fail_closed_without_capability():
    def read_impl(file_path: str, capability=None) -> str:
        """Read implementation used to verify wrapper authority.

        Args:
            file_path: File path.
            capability: Internal operation capability.

        Returns:
            A marker that must never be reached without capability.
        """
        return "read executed"

    def glob_impl(pattern: str, path: str | None = None, capability=None) -> str:
        """Glob implementation used to verify wrapper authority.

        Args:
            pattern: Search pattern.
            path: Optional search path.
            capability: Internal operation capability.

        Returns:
            A marker that must never be reached without capability.
        """
        return "glob executed"

    def grep_impl(pattern: str, path: str | None = None, capability=None) -> str:
        """Grep implementation used to verify wrapper authority.

        Args:
            pattern: Search pattern.
            path: Optional search path.
            capability: Internal operation capability.

        Returns:
            A marker that must never be reached without capability.
        """
        return "grep executed"

    def process_impl(name: str, capability=None) -> str:
        """Process implementation used to verify wrapper authority.

        Args:
            name: Process name.
            capability: Internal operation capability.

        Returns:
            A marker that must never be reached without capability.
        """
        return "process executed"

    calls = [
        (_create_tool_wrapper(read_impl, "read_file"), {"file_path": "x"}),
        (_create_tool_wrapper(glob_impl, "glob"), {"pattern": "*.py"}),
        (_create_tool_wrapper(grep_impl, "grep"), {"pattern": "secret"}),
        (_create_tool_wrapper(process_impl, "check_process"), {"name": "python"}),
    ]

    for tool, kwargs in calls:
        result = tool.forward(**kwargs)
        assert result == "操作被安全策略阻断 [missing_capability]: operation capability is required"


def test_wrapped_read_and_search_use_injected_operation_roots(tmp_path):
    capability = _capability(tmp_path)
    root = tmp_path / "allowed"
    readable = root / "visible.txt"
    readable.write_text("visible", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    read_tool = _bound(_create_tool_wrapper(file_ops.read_file, "read_file"), capability)
    read_result = read_tool.forward(file_path=str(outside))
    assert "[path_not_allowed]" in read_result

    glob_tool = _bound(_create_tool_wrapper(search_ops.glob_files, "glob"), capability)
    glob_result = glob_tool.forward(pattern="*.txt", path=str(root))
    assert str(readable) in glob_result
    assert str(outside) not in glob_result


def test_wrapped_exec_and_browser_use_bound_capability(tmp_path, monkeypatch):
    capability = _capability(tmp_path, allow_browser=False)
    exec_tool = _bound(
        _create_tool_wrapper(sys_ops.exec_command, "exec_command"), capability
    )
    browser_tool = _bound(
        _create_tool_wrapper(sys_ops.browser_command, "browser_command"), capability
    )
    launched = False

    def fail_launch(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("process must not launch")

    monkeypatch.setattr(sys_ops, "_run_process", fail_launch)
    assert "[executable_not_allowed]" in exec_tool.forward(command="python -V")
    assert "[browser_disabled]" in browser_tool.forward(command="snapshot")
    assert launched is False


def test_model_supplied_session_id_is_rejected_and_not_in_schema(tmp_path):
    capability = _capability(tmp_path)
    tool = _bound(_create_tool_wrapper(sys_ops.exec_command, "exec_command"), capability)

    assert "session_id" not in tool.inputs
    result = tool.forward(command="printf ok", session_id="attacker-session")
    assert "[session_identity_override]" in result


def test_image_loader_uses_capability_image_roots_and_session_identity(tmp_path):
    capability = _capability(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"not-an-image")

    result = load_images_to_ipc(
        capability.session_id,
        [str(outside)],
        max_dimension=128,
        capability=capability,
    )

    assert "[path_not_allowed]" in result.summary
    assert result.exit_code == 1
    assert not Path(capability.session_temp_base_dir, capability.session_id).exists()


def test_wrapped_denial_is_stable_and_does_not_retry_unvalidated_call(tmp_path):
    capability = _capability(tmp_path)
    calls = 0

    def unsafe_side_effect(file_path: str, content: str) -> str:
        """Attempt an unsafe write.

        Args:
            file_path: Destination path.
            content: File content.

        Returns:
            Result text.
        """
        nonlocal calls
        calls += 1
        return file_ops.write_file(file_path, content)

    tool = _create_tool_wrapper(unsafe_side_effect, "write_file")
    result = tool.forward(file_path=str(tmp_path / "outside"), content="no")

    assert result == "操作被安全策略阻断 [missing_capability]: operation capability is required"
    assert calls == 0
