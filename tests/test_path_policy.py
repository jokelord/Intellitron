from __future__ import annotations

import os
import socket
import stat

import pytest

from backend.security.path_policy import PathPolicy, PathPolicyError


def _policy(root, **kwargs) -> PathPolicy:
    return PathPolicy(file_operation_roots=[root], session_base_dir=root / "sessions", **kwargs)


def _error_code(call, code: str) -> None:
    with pytest.raises(PathPolicyError) as caught:
        call()
    assert caught.value.code == code


def test_create_paths_are_canonically_contained_and_missing_parents_are_allowed(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    policy = _policy(root)

    resolved = policy.validate_create(str(root / "nested" / ".." / "nested" / "note.txt"), "write")

    assert resolved.root == str(root.resolve())
    assert resolved.resolved == str((root / "nested" / "note.txt").resolve(strict=False))
    _error_code(lambda: policy.validate_create(str(tmp_path / "outside.txt"), "write"), "path_not_allowed")


def test_absolute_and_home_spelled_tool_paths_resolve_to_the_same_allowed_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / "allowed"
    root.mkdir(parents=True)
    target = root / "note.txt"
    target.write_text("allowed content")
    monkeypatch.setenv("HOME", str(home))
    policy = _policy(root)

    absolute = policy.resolve_existing(target, "read")
    home_spelled = policy.resolve_existing("~/allowed/note.txt", "read")

    assert absolute.resolved == home_spelled.resolved == str(target.resolve())
    assert absolute.root == home_spelled.root == str(root.resolve())


def test_existing_paths_reject_directories_and_non_regular_files(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    policy = _policy(root)
    directory = root / "directory"
    directory.mkdir()
    fifo = root / "pipe"
    os.mkfifo(fifo)
    _error_code(lambda: policy.validate_modify(str(directory), "edit"), "invalid_file_type")
    _error_code(lambda: policy.validate_modify(str(fifo), "edit"), "invalid_file_type")


def test_existing_paths_reject_unix_sockets_when_the_platform_allows_fixture_creation(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    policy = _policy(root)
    sock = socket.socket(socket.AF_UNIX)
    socket_path = root / "socket"
    try:
        try:
            sock.bind(str(socket_path))
        except PermissionError:
            pytest.skip("sandbox does not permit Unix socket fixtures")
        _error_code(lambda: policy.validate_modify(str(socket_path), "edit"), "invalid_file_type")
    finally:
        sock.close()


def test_existing_paths_reject_devices_when_the_platform_allows_fixture_creation(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    policy = _policy(root)
    device = root / "null-device"
    try:
        os.mknod(device, stat.S_IFCHR | 0o600, os.makedev(1, 3))
    except PermissionError:
        pytest.skip("sandbox does not permit device fixtures")
    _error_code(lambda: policy.validate_modify(str(device), "edit"), "invalid_file_type")


def test_read_allows_an_in_root_symlink_but_mutation_rejects_it(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("safe")
    link = root / "in-root-link.txt"
    link.symlink_to(target)
    policy = _policy(root)

    assert policy.resolve_existing(str(link), "read").resolved == str(target.resolve())
    _error_code(lambda: policy.validate_modify(str(link), "edit"), "symlink_not_allowed")


def test_mutation_rejects_parent_and_final_symlink_escapes(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    parent_link = root / "linked-parent"
    parent_link.symlink_to(outside, target_is_directory=True)
    final_link = root / "linked-file.txt"
    outside_file = outside / "outside.txt"
    outside_file.write_text("secret")
    final_link.symlink_to(outside_file)
    policy = _policy(root)

    _error_code(lambda: policy.validate_create(str(parent_link / "new.txt"), "write"), "path_not_allowed")
    _error_code(lambda: policy.validate_modify(str(final_link), "edit"), "symlink_not_allowed")


@pytest.mark.parametrize(
    "session_id",
    ["", " ", "../escape", "a/b", r"a\\b", "a b", "a;rm", "a$(x)", ".", "..", "x" * 129],
)
def test_session_directory_rejects_unsafe_identifiers(tmp_path, session_id):
    root = tmp_path / "allowed"
    root.mkdir()
    policy = _policy(root)

    _error_code(lambda: policy.validate_session_dir(session_id), "invalid_session_id")


def test_session_directory_is_always_a_child_of_its_fixed_base(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    policy = _policy(root)

    directory = policy.validate_session_dir("session_123-ABC")

    assert os.path.commonpath([directory, str((root / "sessions").resolve())]) == str((root / "sessions").resolve())
    assert os.path.basename(directory) == "session_123-ABC"


def test_malformed_paths_are_rejected_without_accessing_the_filesystem(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    policy = _policy(root)

    _error_code(lambda: policy.validate_create("bad\x00path", "write"), "invalid_path")
