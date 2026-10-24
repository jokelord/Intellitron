from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from backend.security.capabilities import OperationCapability
from backend.security.path_policy import PathPolicy
from backend.skills.load_local_images.load_local_images import load_images_to_ipc


def _policy(image_root: Path, session_base: Path) -> PathPolicy:
    return PathPolicy(
        file_operation_roots=[image_root],
        image_input_roots=[image_root],
        session_base_dir=session_base,
        max_file_bytes=20 * 1024 * 1024,
    )


def _capability(policy: PathPolicy, session_id: str) -> OperationCapability:
    roots = tuple(str(item) for item in policy.file_operation_roots)
    return OperationCapability(
        session_id=session_id,
        read_roots=roots,
        search_roots=roots,
        file_operation_roots=roots,
        image_input_roots=tuple(str(item) for item in policy.image_input_roots),
        session_temp_base_dir=str(policy.session_base_dir),
        command_mode="argv",
        allowed_executables=("python",),
        max_exec_timeout_seconds=60,
        max_output_bytes=4096,
        max_file_bytes=policy.max_file_bytes,
        allow_overwrite_existing=policy.allow_overwrite_existing,
        allow_browser=False,
        exec_timeout=1,
        termination_grace_seconds=0,
        work_dir=str(policy.session_base_dir),
        python_executable="/usr/bin/python3",
        nvm_dir=str(policy.session_base_dir),
        environment_allowlist=(),
    )


def _jpeg(path: Path) -> None:
    Image.new("RGB", (4, 4), color=(255, 0, 0)).save(path, format="JPEG")


def test_image_loader_rejects_supported_file_outside_image_roots_without_creating_session_dir(tmp_path):
    """Removing image-root validation would load an out-of-capability JPEG."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.jpg"
    _jpeg(outside)
    sessions = tmp_path / "sessions"
    session_id = str(uuid4())
    policy = _policy(allowed, sessions)

    result = load_images_to_ipc(
        session_id,
        [str(outside)],
        max_dimension=1280,
        capability=_capability(policy, session_id),
    )

    assert result.exit_code == 1
    assert "[path_not_allowed]" in result.summary
    assert not (sessions / session_id).exists()


def test_image_loader_rejects_final_symlink_before_reading_external_image(tmp_path):
    """Dropping no-follow image validation would read the symlink target."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.jpg"
    _jpeg(outside)
    image_link = allowed / "linked.jpg"
    image_link.symlink_to(outside)
    sessions = tmp_path / "sessions"
    session_id = str(uuid4())
    policy = _policy(allowed, sessions)

    result = load_images_to_ipc(
        session_id,
        [str(image_link)],
        max_dimension=1280,
        capability=_capability(policy, session_id),
    )

    assert result.exit_code == 1
    assert "[symlink_not_allowed]" in result.summary
    assert not any(sessions.glob("*")) if sessions.exists() else True


def test_image_loader_writes_only_to_policy_allocated_session_directory(tmp_path):
    """Replacing policy allocation with string interpolation would escape this base."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    image = allowed / "image.jpg"
    _jpeg(image)
    sessions = tmp_path / "sessions"
    session_id = str(uuid4())
    policy = _policy(allowed, sessions)

    result = load_images_to_ipc(
        session_id,
        [str(image)],
        max_dimension=1280,
        capability=_capability(policy, session_id),
    )

    assert result.exit_code == 0
    outputs = list((sessions / session_id).glob("*.json"))
    assert len(outputs) == 1


def test_image_loader_validates_policy_before_following_path_metadata(tmp_path, monkeypatch):
    """A rejected symlink must not reach pre-validation filesystem probes."""
    from backend.skills.load_local_images import load_local_images

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.jpg"
    _jpeg(outside)
    image_link = allowed / "linked.jpg"
    image_link.symlink_to(outside)
    sessions = tmp_path / "sessions"
    session_id = str(uuid4())
    policy = _policy(allowed, sessions)

    monkeypatch.setattr(
        load_local_images.os.path,
        "isfile",
        lambda _: pytest.fail("isfile must not inspect a rejected symlink"),
    )
    monkeypatch.setattr(
        load_local_images.os.path,
        "getsize",
        lambda _: pytest.fail("getsize must not inspect a rejected symlink"),
    )

    result = load_images_to_ipc(
        session_id,
        [str(image_link)],
        max_dimension=1280,
        capability=_capability(policy, session_id),
    )

    assert result.exit_code == 1
    assert "[symlink_not_allowed]" in result.summary


def test_image_ipc_consumption_does_not_follow_json_symlink(tmp_path):
    """IPC consumption must not read a JSON payload through an external symlink."""
    from backend.agent.agent_runner import _read_and_consume_tool_images

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    sessions = tmp_path / "sessions"
    session_id = str(uuid4())
    policy = _policy(allowed, sessions)
    capability = _capability(policy, session_id)
    session_dir = Path(policy.allocate_session_dir(session_id))
    external = tmp_path / "external.json"
    external.write_text('{"images": []}', encoding="utf-8")
    link = session_dir / "payload.json"
    link.symlink_to(external)

    assert _read_and_consume_tool_images(session_id, capability=capability) == []
    assert external.exists()
    assert link.is_symlink()
