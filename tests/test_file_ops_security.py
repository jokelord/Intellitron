from __future__ import annotations

import os
import stat
import sys

import pytest

from backend.security.capabilities import OperationCapability
from backend.security.path_policy import PathPolicy, PathPolicyError
from backend.tools import file_ops
from backend.tools.file_ops import use_file_operation_policy


def _policy(root, **kwargs) -> PathPolicy:
    return PathPolicy(file_operation_roots=[root], max_file_bytes=kwargs.pop("max_file_bytes", 1024), **kwargs)


def _capability(policy: PathPolicy) -> OperationCapability:
    root = str(policy.file_operation_roots[0])
    session_base = str(policy.session_base_dir)
    return OperationCapability(
        session_id="file-test-session",
        read_roots=(root,),
        search_roots=(root,),
        file_operation_roots=tuple(str(item) for item in policy.file_operation_roots),
        image_input_roots=tuple(str(item) for item in policy.image_input_roots),
        session_temp_base_dir=session_base,
        command_mode="argv",
        allowed_executables=("python",),
        max_exec_timeout_seconds=60,
        max_output_bytes=4096,
        max_file_bytes=policy.max_file_bytes,
        allow_overwrite_existing=policy.allow_overwrite_existing,
        allow_browser=False,
        exec_timeout=1,
        termination_grace_seconds=0,
        work_dir=root,
        python_executable=sys.executable,
        nvm_dir=session_base,
        environment_allowlist=(),
    )


def _write(path, content, policy) -> str:
    with use_file_operation_policy(policy, capability=_capability(policy)):
        return file_ops.write_file(str(path), content)


def _edit(path, search_text, replace_text, policy) -> str:
    with use_file_operation_policy(policy, capability=_capability(policy)):
        return file_ops.edit_file(str(path), search_text, replace_text)


def test_policy_context_rejects_policy_that_does_not_match_capability(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    policy = _policy(root)
    broader_policy = PathPolicy(
        file_operation_roots=[tmp_path],
        max_file_bytes=policy.max_file_bytes,
        session_base_dir=policy.session_base_dir,
    )

    with pytest.raises(PathPolicyError, match="capability_policy_mismatch"):
        with use_file_operation_policy(
            broader_policy, capability=_capability(policy)
        ):
            pass


@pytest.mark.parametrize("tool", [file_ops.write_file, file_ops.edit_file])
def test_file_tools_reject_outside_roots_without_creating_or_modifying_files(tmp_path, tool):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    policy = _policy(root)

    if tool is file_ops.write_file:
        result = _write(outside, "blocked", policy)
    else:
        outside.write_text("original")
        result = _edit(outside, "original", "blocked", policy)

    assert "[path_not_allowed]" in result
    assert not outside.exists() if tool is file_ops.write_file else outside.read_text() == "original"


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_file_tools_reject_parent_and_final_symlinks_without_touching_outside_target(tmp_path, tool):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "external.txt"
    external.write_text("original")
    parent_link = root / "parent-link"
    parent_link.symlink_to(outside, target_is_directory=True)
    final_link = root / "final-link.txt"
    final_link.symlink_to(external)
    policy = _policy(root)

    if tool == "write":
        parent_result = _write(parent_link / "new.txt", "blocked", policy)
        final_result = _write(final_link, "blocked", policy)
    else:
        parent_result = _edit(parent_link / "external.txt", "original", "blocked", policy)
        final_result = _edit(final_link, "original", "blocked", policy)

    assert "[path_not_allowed]" in parent_result
    assert "[symlink_not_allowed]" in final_result
    assert external.read_text() == "original"
    assert not (outside / "new.txt").exists()


@pytest.mark.parametrize("tool", [file_ops.write_file, file_ops.edit_file])
def test_file_tools_reject_directory_destinations(tmp_path, tool):
    root = tmp_path / "allowed"
    root.mkdir()
    destination = root / "directory"
    destination.mkdir()
    policy = _policy(root)

    if tool is file_ops.write_file:
        result = _write(destination, "blocked", policy)
    else:
        result = _edit(destination, "", "blocked", policy)

    assert "[invalid_file_type]" in result


def test_write_rejects_content_larger_than_byte_limit_before_creating_file(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    target = root / "too-large.txt"

    result = _write(target, "éé", _policy(root, max_file_bytes=3))

    assert "[file_too_large]" in result
    assert not target.exists()


def test_edit_rejects_content_larger_than_byte_limit_before_modifying_file(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("a")

    result = _edit(target, "a", "éé", _policy(root, max_file_bytes=3))

    assert "[file_too_large]" in result
    assert target.read_text() == "a"


def test_write_creates_missing_parents_only_beneath_the_authorized_root(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    target = root / "nested" / "deeper" / "target.txt"

    result = _write(target, "created", _policy(root))

    assert result == f"文件已写入: {target} (7 字节)"
    assert target.read_text() == "created"


def test_default_policy_allows_authorized_regular_file_overwrite_atomically(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("before")

    result = _write(target, "after", _policy(root))

    assert result == f"文件已写入: {target} (5 字节)"
    assert target.read_text() == "after"
    assert not list(root.glob(".agent-write-*"))


@pytest.mark.parametrize("operation", ["write", "edit"])
def test_atomic_overwrite_preserves_existing_file_mode(tmp_path, operation):
    root = tmp_path / "allowed"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("before")
    target.chmod(0o640)
    policy = _policy(root)

    if operation == "write":
        result = _write(target, "after", policy)
    else:
        result = _edit(target, "before", "after", policy)

    assert "失败" not in result
    assert target.read_text() == ("after" if operation == "write" else "after\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_strict_overwrite_policy_rejects_existing_regular_file_without_modifying_it(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("before")

    result = _write(target, "after", _policy(root, allow_overwrite_existing=False))

    assert "[overwrite_not_allowed]" in result
    assert target.read_text() == "before"


def test_edit_modifies_existing_file_when_write_overwrite_policy_is_disabled(tmp_path):
    """The write create/overwrite switch does not disable explicit edit operations."""
    root = tmp_path / "allowed"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("before")

    result = _edit(target, "before", "after", _policy(root, allow_overwrite_existing=False))

    assert "失败" not in result
    assert target.read_text() == "after\n"


def test_edit_empty_search_requires_modify_policy_and_cannot_append_outside_root(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("before")

    result = _edit(outside, "", "after", _policy(root))

    assert "[path_not_allowed]" in result
    assert outside.read_text() == "before"


def test_atomic_write_rechecks_a_swapped_target_and_never_modifies_external_file(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("before")
    external = tmp_path / "external.txt"
    external.write_text("external-before")
    policy = _policy(root)
    original_check = policy.check_atomic_target
    swapped = False

    def swap_before_final_check(parent_fd, name, *, require_existing, allow_overwrite):
        nonlocal swapped
        if not swapped:
            target.unlink()
            target.symlink_to(external)
            swapped = True
        return original_check(parent_fd, name, require_existing=require_existing, allow_overwrite=allow_overwrite)

    monkeypatch.setattr(policy, "check_atomic_target", swap_before_final_check)

    result = _write(target, "blocked", policy)

    assert "[symlink_not_allowed]" in result
    assert external.read_text() == "external-before"
    assert target.is_symlink()
    assert not list(root.glob(".agent-write-*"))


def test_permission_error_retries_full_atomic_write_and_cleans_failed_tempfile(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    target = root / "target.txt"
    policy = _policy(root)
    original_replace = os.replace
    attempts = 0

    def fail_once(src, dst, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("temporary lock")
        return original_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(file_ops.os, "replace", fail_once)
    monkeypatch.setattr(file_ops.time, "sleep", lambda _: None)

    result = _write(target, "after", policy)

    assert result == f"文件已写入: {target} (5 字节)"
    assert attempts == 2
    assert target.read_text() == "after"
    assert not list(root.glob(".agent-write-*"))


def test_policy_denial_does_not_retry_or_leave_temporary_files(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    sleep_calls = []
    monkeypatch.setattr(file_ops.time, "sleep", sleep_calls.append)

    result = _write(outside, "blocked", _policy(root))

    assert "[path_not_allowed]" in result
    assert sleep_calls == []
    assert not list(root.glob(".agent-write-*"))
