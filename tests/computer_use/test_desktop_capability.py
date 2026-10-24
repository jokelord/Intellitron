from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from backend.config import AgentConfig, AppConfig, ReadFileConfig
from backend.security.capabilities import (
    CapabilityError,
    build_session_capability,
    derive_child_capability,
    require_capability,
)


def _config(tmp_path: Path, **overrides: object) -> AppConfig:
    root = tmp_path / "allowed"
    root.mkdir(exist_ok=True)
    values: dict[str, object] = {
        "search_whitelist_paths": [str(root)],
        "work_dir": str(tmp_path),
        "python_executable": str(Path(__file__).parents[2] / ".sglang/bin/python"),
        "nvm_dir": str(tmp_path / "nvm"),
        "allow_browser": True,
    }
    values.update(overrides)
    return AppConfig(
        read_file=ReadFileConfig(allowed_roots=[str(root)]),
        agent=AgentConfig(**values),
    )


def test_desktop_authority_defaults_off_and_is_independent_from_browser(tmp_path: Path):
    capability = build_session_capability(
        _config(tmp_path), session_id=str(uuid4()), mode="tool_calling"
    )

    assert capability.allow_browser is True
    assert capability.allow_desktop_capture is False
    assert capability.allow_desktop_input is False
    with pytest.raises(CapabilityError, match="desktop_capture") as denied:
        require_capability(capability, "desktop_capture")
    assert denied.value.code == "desktop_disabled"


def test_missing_and_session_mismatched_authority_fail_closed(tmp_path: Path):
    capability = build_session_capability(
        _config(tmp_path), session_id=str(uuid4()), mode="tool_calling"
    )

    with pytest.raises(CapabilityError) as missing:
        require_capability(None, "desktop_input")
    assert missing.value.code == "missing_capability"
    with pytest.raises(CapabilityError) as mismatch:
        require_capability(capability, "session", "different-session")
    assert mismatch.value.code == "capability_session_mismatch"


def test_input_authority_requires_capture_authority(tmp_path: Path):
    with pytest.raises(CapabilityError, match="allow_desktop_input"):
        build_session_capability(
            _config(
                tmp_path,
                allow_desktop_capture=False,
                allow_desktop_input=True,
            ),
            session_id=str(uuid4()),
            mode="tool_calling",
        )


def test_child_derivation_cannot_widen_desktop_authority(tmp_path: Path):
    parent = build_session_capability(
        _config(
            tmp_path,
            allow_desktop_capture=True,
            allow_desktop_input=True,
        ),
        session_id=str(uuid4()),
        mode="tool_calling",
    )

    child = derive_child_capability(parent, "child-context")

    assert child.allow_desktop_capture is False
    assert child.allow_desktop_input is False
    with pytest.raises(CapabilityError):
        require_capability(child, "desktop_input")


def test_invalid_capability_cannot_be_replaced_into_input_without_capture(tmp_path: Path):
    capability = build_session_capability(
        _config(tmp_path), session_id=str(uuid4()), mode="tool_calling"
    )

    with pytest.raises(CapabilityError, match="allow_desktop_input"):
        replace(capability, allow_desktop_input=True)
