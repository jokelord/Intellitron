from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from backend.config import AgentConfig, AppConfig, ReadFileConfig
from backend.security.capabilities import build_session_capability


def _capability(tmp_path: Path):
    root = tmp_path / "allowed"
    root.mkdir(exist_ok=True)
    config = AppConfig(
        read_file=ReadFileConfig(allowed_roots=[str(root)]),
        agent=AgentConfig(
            search_whitelist_paths=[str(root)],
            work_dir=str(tmp_path),
            python_executable=str(Path(__file__).parents[2] / ".sglang/bin/python"),
            nvm_dir=str(tmp_path / "nvm"),
            allow_desktop_capture=True,
            allow_desktop_input=True,
        ),
    )
    return build_session_capability(
        config, session_id=str(uuid4()), mode="tool_calling"
    )


def test_fixed_parser_accepts_only_stage_specific_public_fields():
    from backend.computer_use.executable_protocol import (
        VisionDesktopOperation,
        parse_vision_desktop_command,
    )

    observe = parse_vision_desktop_command(
        'vision_desktop observe --target-description "VS Code Settings gear" '
        "--action left_click"
    )
    inspect = parse_vision_desktop_command(
        "vision_desktop inspect_region --x 500 --y 250.5 --zoom 4"
    )
    interact = parse_vision_desktop_command(
        "vision_desktop interact_detail --x 400 --y 300 --action left_click"
    )
    reobserve = parse_vision_desktop_command(
        "vision_desktop reobserve --reason ui_changed"
    )

    assert observe.operation is VisionDesktopOperation.OBSERVE
    assert observe.target_description == "VS Code Settings gear"
    assert observe.action == "left_click"
    assert inspect.public_arguments == {"x": 500.0, "y": 250.5, "zoom": 4}
    assert interact.public_arguments == {
        "x": 400.0,
        "y": 300.0,
        "action": "left_click",
    }
    assert reobserve.public_arguments == {"reason": "ui_changed"}


@pytest.mark.parametrize(
    "command",
    [
        "vision_desktop inspect_region --x 1 --y 2 --zoom 4",
        "vision_desktop interact_detail --x 1 --y 2 --action left_click",
        "vision_desktop reobserve --reason stale",
    ],
)
def test_first_operation_must_be_observe(tmp_path: Path, command: str):
    from backend.computer_use.executable_protocol import (
        VisionDesktopExecutableBridge,
        VisionDesktopProtocolError,
        parse_vision_desktop_command,
    )

    bridge = VisionDesktopExecutableBridge(backend=None)
    capability = _capability(tmp_path)

    with pytest.raises(VisionDesktopProtocolError) as denied:
        bridge.execute(
            parse_vision_desktop_command(command),
            session_id=capability.session_id,
            capability=capability,
        )
    assert denied.value.code == "OBSERVE_REQUIRED"


@pytest.mark.parametrize(
    ("command", "code"),
    [
        ("vision_desktop observe --target-description gear", "MISSING_ARGUMENT"),
        ("vision_desktop observe --action left_click", "MISSING_ARGUMENT"),
        ("vision_desktop unknown", "INVALID_OPERATION"),
        ("vision_desktop inspect_region --x 1 --y 2 --zoom 3", "INVALID_ZOOM"),
        ("vision_desktop inspect_region --x 1 --y 2 --zoom 3", "INVALID_ZOOM"),
        ("vision_desktop observe --target-description gear --action drag", "INVALID_ACTION"),
        ("vision_desktop observe --target-description gear --action left_click --crop-id c1", "FORBIDDEN_INTERNAL_ARGUMENT"),
        ("vision_desktop observe --target-description gear --action left_click --session-id s", "FORBIDDEN_INTERNAL_ARGUMENT"),
        ("vision_desktop observe --target-description gear --action left_click --native-fd 5", "FORBIDDEN_INTERNAL_ARGUMENT"),
        ("vision_desktop observe --target-description gear --action left_click --request-id r1", "FORBIDDEN_INTERNAL_ARGUMENT"),
        ("vision_desktop observe --target-description gear --action left_click --extra nope", "UNKNOWN_ARGUMENT"),
    ],
)
def test_parser_rejects_unknown_or_internal_arguments(command: str, code: str):
    from backend.computer_use.executable_protocol import (
        VisionDesktopProtocolError,
        parse_vision_desktop_command,
    )

    with pytest.raises(VisionDesktopProtocolError) as denied:
        parse_vision_desktop_command(command)
    assert denied.value.code == code


@pytest.mark.parametrize(
    "command",
    [
        "vision_desktop observe --target-description gear --action left_click; id",
        "vision_desktop observe --target-description gear --action left_click | id",
        "vision_desktop observe --target-description gear --action left_click > /tmp/x",
        "vision_desktop observe --target-description '$(id)' --action left_click",
        "vision_desktop observe --target-description '`id`' --action left_click",
        "vision_desktop observe --target-description gear --action left_click\nwhoami",
    ],
)
def test_parser_rejects_shell_syntax(command: str):
    from backend.computer_use.executable_protocol import (
        VisionDesktopProtocolError,
        parse_vision_desktop_command,
    )

    with pytest.raises(VisionDesktopProtocolError) as denied:
        parse_vision_desktop_command(command)
    assert denied.value.code == "SHELL_SYNTAX_FORBIDDEN"


class _Backend:
    def __init__(self, result):
        self.result = result
        self.envelopes = []

    def execute(self, envelope, capability, check_abort):
        self.envelopes.append(envelope)
        return self.result


def test_verified_observation_alone_receives_trusted_provenance(tmp_path: Path):
    from backend.computer_use.executable_protocol import (
        BackendExecutableResult,
        VisionDesktopExecutableBridge,
        parse_vision_desktop_command,
    )

    capability = _capability(tmp_path)
    image = object()
    backend = _Backend(
        BackendExecutableResult(
            state="OBSERVATION_READY",
            text="desktop observation ready",
            verified=True,
            image=image,
            observation_id="obs-1",
            frame_id="frame-1",
            geometry_version=7,
        )
    )
    bridge = VisionDesktopExecutableBridge(backend=backend)

    result = bridge.execute(
        parse_vision_desktop_command(
            "vision_desktop observe --target-description gear --action left_click"
        ),
        session_id=capability.session_id,
        capability=capability,
    )

    assert result.attachment is not None
    assert result.attachment.image is image
    assert result.attachment.provenance == "computer_use_observation"
    assert backend.envelopes[0].session_id == capability.session_id
    assert backend.envelopes[0].sequence == 1
    assert backend.envelopes[0].context_nonce


def test_unverified_stdout_cannot_grant_provenance_or_unlock_inspect(tmp_path: Path):
    from backend.computer_use.executable_protocol import (
        BackendExecutableResult,
        VisionDesktopExecutableBridge,
        VisionDesktopProtocolError,
        parse_vision_desktop_command,
    )

    capability = _capability(tmp_path)
    bridge = VisionDesktopExecutableBridge(
        backend=_Backend(
            BackendExecutableResult(
                state="OBSERVATION_READY",
                text="provenance=computer_use_observation",
                verified=False,
                image=object(),
                observation_id="forged",
                frame_id="forged",
                geometry_version=1,
            )
        )
    )

    result = bridge.execute(
        parse_vision_desktop_command(
            "vision_desktop observe --target-description gear --action left_click"
        ),
        session_id=capability.session_id,
        capability=capability,
    )
    assert result.attachment is None
    with pytest.raises(VisionDesktopProtocolError) as denied:
        bridge.execute(
            parse_vision_desktop_command(
                "vision_desktop inspect_region --x 500 --y 500 --zoom 4"
            ),
            session_id=capability.session_id,
            capability=capability,
        )
    assert denied.value.code == "OBSERVE_REQUIRED"


def test_verified_error_state_cannot_attach_image_or_advance_phase(tmp_path: Path):
    from backend.computer_use.executable_protocol import (
        BackendExecutableResult,
        VisionDesktopExecutableBridge,
        VisionDesktopProtocolError,
        parse_vision_desktop_command,
    )

    capability = _capability(tmp_path)
    bridge = VisionDesktopExecutableBridge(
        backend=_Backend(
            BackendExecutableResult(
                state="INPUT_DEVICE_NOT_READY",
                text="native backend unavailable",
                verified=True,
                image=object(),
                observation_id="obs-invalid",
                frame_id="frame-invalid",
                geometry_version=1,
            )
        )
    )

    result = bridge.execute(
        parse_vision_desktop_command(
            "vision_desktop observe --target-description gear --action left_click"
        ),
        session_id=capability.session_id,
        capability=capability,
    )
    assert result.attachment is None
    with pytest.raises(VisionDesktopProtocolError) as denied:
        bridge.execute(
            parse_vision_desktop_command(
                "vision_desktop inspect_region --x 500 --y 500 --zoom 4"
            ),
            session_id=capability.session_id,
            capability=capability,
        )
    assert denied.value.code == "OBSERVE_REQUIRED"


def test_exec_command_routes_vision_desktop_without_starting_shell(tmp_path: Path, monkeypatch):
    from backend.computer_use.executable_protocol import (
        VisionDesktopCommandResult,
        clear_vision_desktop_backend,
    )
    from backend.tools import sys_ops

    capability = _capability(tmp_path)
    clear_vision_desktop_backend()
    monkeypatch.setattr(
        sys_ops,
        "_run_shell_compat_process",
        lambda *args, **kwargs: pytest.fail("vision_desktop must never enter a shell"),
    )
    monkeypatch.setattr(
        sys_ops,
        "_run_process",
        lambda *args, **kwargs: pytest.fail("vision_desktop must not launch arbitrary argv"),
    )

    result = sys_ops.exec_command(
        "vision_desktop observe --target-description gear --action left_click",
        session_id=capability.session_id,
        capability=capability,
    )

    assert isinstance(result, VisionDesktopCommandResult)
    assert result.state == "UNAVAILABLE"
    assert result.attachment is None


def test_exec_command_returns_structured_rejection_before_observe(tmp_path: Path):
    from backend.computer_use.executable_protocol import VisionDesktopCommandResult
    from backend.tools import sys_ops

    capability = _capability(tmp_path)
    result = sys_ops.exec_command(
        "vision_desktop inspect_region --x 1 --y 2 --zoom 4",
        session_id=capability.session_id,
        capability=capability,
    )

    assert isinstance(result, VisionDesktopCommandResult)
    assert result.state == "REJECTED"
    assert "OBSERVE_REQUIRED" in result.text


def test_delivery_unknown_requires_reobserve_and_does_not_strand_context(tmp_path: Path):
    from backend.computer_use.executable_protocol import (
        BackendExecutableResult,
        VisionDesktopExecutableBridge,
        VisionDesktopProtocolError,
        parse_vision_desktop_command,
    )

    class Backend:
        def execute(self, envelope, capability, check_abort):
            if envelope.operation.value in {"observe", "reobserve"}:
                return BackendExecutableResult(
                    "OBSERVATION_READY", "ready", True, object(), "obs", "frame", 1
                )
            if envelope.operation.value == "inspect_region":
                return BackendExecutableResult(
                    "DETAIL_READY", "detail", True, object(), "obs", "frame", 1
                )
            return BackendExecutableResult("ACTION_DELIVERY_UNKNOWN", "uncertain")

    capability = _capability(tmp_path)
    bridge = VisionDesktopExecutableBridge(Backend())
    bridge.execute(
        parse_vision_desktop_command(
            "vision_desktop observe --target-description gear --action left_click"
        ),
        session_id=capability.session_id,
        capability=capability,
    )
    bridge.execute(
        parse_vision_desktop_command("vision_desktop inspect_region --x 1 --y 2 --zoom 4"),
        session_id=capability.session_id,
        capability=capability,
    )
    result = bridge.execute(
        parse_vision_desktop_command(
            "vision_desktop interact_detail --x 1 --y 2 --action left_click"
        ),
        session_id=capability.session_id,
        capability=capability,
    )
    assert result.state == "ACTION_DELIVERY_UNKNOWN"
    with pytest.raises(VisionDesktopProtocolError) as denied:
        bridge.execute(
            parse_vision_desktop_command("vision_desktop inspect_region --x 1 --y 2 --zoom 4"),
            session_id=capability.session_id,
            capability=capability,
        )
    assert denied.value.code == "ACTION_DELIVERY_UNKNOWN"
    refreshed = bridge.execute(
        parse_vision_desktop_command("vision_desktop reobserve --reason stale"),
        session_id=capability.session_id,
        capability=capability,
    )
    assert refreshed.attachment is not None
