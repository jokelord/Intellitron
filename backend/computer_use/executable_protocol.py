"""Restricted ``vision_desktop`` command parser and trusted execution seam.

This module never launches a process or a shell.  It accepts the small public
argv vocabulary, injects context identity and monotonic protocol state, and
promotes an image to trusted provenance only after a registered backend has
verified its result.
"""

from __future__ import annotations

import math
import shlex
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from backend.security.capabilities import OperationCapability, require_capability


PROTOCOL_VERSION = 1
_ACTIONS = frozenset(
    {
        "left_click",
        "right_click",
        "left_double_click",
        "right_double_click",
        "scroll_up",
        "scroll_down",
        "scroll_up_1",
        "scroll_down_1",
    }
)
_FORBIDDEN_INTERNAL_FLAGS = frozenset(
    {
        "--binding-id",
        "--capability",
        "--context-nonce",
        "--crop-id",
        "--device-id",
        "--frame-id",
        "--geometry-version",
        "--native-fd",
        "--observation-id",
        "--protocol-version",
        "--region-id",
        "--request-id",
        "--sequence",
        "--session-id",
    }
)
_SHELL_PUNCTUATION = frozenset({"|", "||", "&", "&&", ";", "<", "<<", ">", ">>"})


class VisionDesktopProtocolError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class VisionDesktopOperation(str, Enum):
    OBSERVE = "observe"
    INSPECT_REGION = "inspect_region"
    INTERACT_DETAIL = "interact_detail"


@dataclass(frozen=True)
class ParsedVisionDesktopCommand:
    operation: VisionDesktopOperation
    target_description: str | None = None
    action: str | None = None
    x: float | None = None
    y: float | None = None
    zoom: int | None = None
    steps: int | None = None

    @property
    def public_arguments(self) -> dict[str, object]:
        values = {
            "target_description": self.target_description,
            "action": self.action,
            "x": self.x,
            "y": self.y,
            "zoom": self.zoom,
            "steps": self.steps,
        }
        return {name: value for name, value in values.items() if value is not None}


@dataclass(frozen=True)
class ExecutableEnvelope:
    protocol_version: int
    context_nonce: str
    sequence: int
    session_id: str
    operation: VisionDesktopOperation
    public_arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "public_arguments", MappingProxyType(dict(self.public_arguments))
        )


@dataclass(frozen=True)
class BackendExecutableResult:
    state: str
    text: str
    verified: bool = False
    image: object | None = None
    observation_id: str | None = None
    frame_id: str | None = None
    geometry_version: int | None = None
    dispatch_terminal: bool = False


_ATTACHMENT_ISSUER = object()


@dataclass(frozen=True)
class TrustedObservationAttachment:
    image: object
    observation_id: str
    frame_id: str
    geometry_version: int
    _issuer: object = field(repr=False, compare=False)
    provenance: str = "computer_use_observation"


def make_trusted_observation_attachment(
    *, image: object, observation_id: str, frame_id: str, geometry_version: int
) -> TrustedObservationAttachment:
    return TrustedObservationAttachment(
        image=image,
        observation_id=observation_id,
        frame_id=frame_id,
        geometry_version=geometry_version,
        _issuer=_ATTACHMENT_ISSUER,
    )


def is_trusted_observation_attachment(value: object) -> bool:
    return (
        isinstance(value, TrustedObservationAttachment)
        and getattr(value, "_issuer", None) is _ATTACHMENT_ISSUER
        and value.provenance == "computer_use_observation"
    )


@dataclass(frozen=True)
class VisionDesktopCommandResult:
    state: str
    text: str
    attachment: TrustedObservationAttachment | None = None

    def __str__(self) -> str:
        return self.text


class VisionDesktopBackend(Protocol):
    def execute(
        self,
        envelope: ExecutableEnvelope,
        capability: OperationCapability,
        check_abort: Callable[[], bool] | None,
    ) -> BackendExecutableResult: ...


def _tokens(command: str) -> tuple[str, ...]:
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise VisionDesktopProtocolError("INVALID_COMMAND", "command is empty or invalid")
    if any(marker in command for marker in ("$(`", "$(", "`", "\n", "\r")):
        raise VisionDesktopProtocolError(
            "SHELL_SYNTAX_FORBIDDEN", "shell syntax is not allowed"
        )
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        values = tuple(lexer)
    except ValueError as exc:
        raise VisionDesktopProtocolError("INVALID_COMMAND", "invalid argv quoting") from exc
    if any(value in _SHELL_PUNCTUATION for value in values):
        raise VisionDesktopProtocolError(
            "SHELL_SYNTAX_FORBIDDEN", "shell syntax is not allowed"
        )
    return values


def _parse_flags(tokens: tuple[str, ...], allowed: frozenset[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        flag = tokens[index]
        if flag in _FORBIDDEN_INTERNAL_FLAGS:
            raise VisionDesktopProtocolError(
                "FORBIDDEN_INTERNAL_ARGUMENT", f"{flag} is runtime-owned"
            )
        if flag not in allowed:
            raise VisionDesktopProtocolError("UNKNOWN_ARGUMENT", f"unknown argument: {flag}")
        if flag in parsed:
            raise VisionDesktopProtocolError("DUPLICATE_ARGUMENT", f"duplicate argument: {flag}")
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
            raise VisionDesktopProtocolError("MISSING_ARGUMENT", f"{flag} requires a value")
        parsed[flag] = tokens[index + 1]
        index += 2
    return parsed


def _required(values: Mapping[str, str], *names: str) -> None:
    missing = [name for name in names if name not in values]
    if missing:
        raise VisionDesktopProtocolError(
            "MISSING_ARGUMENT", f"missing required arguments: {', '.join(missing)}"
        )


def _finite(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise VisionDesktopProtocolError(
            "INVALID_COORDINATE", f"{name} must be finite"
        ) from exc
    if not math.isfinite(parsed):
        raise VisionDesktopProtocolError("INVALID_COORDINATE", f"{name} must be finite")
    return parsed


def _action(value: str) -> str:
    if value not in _ACTIONS:
        raise VisionDesktopProtocolError("INVALID_ACTION", "unsupported mouse action")
    return value


def parse_vision_desktop_command(command: str) -> ParsedVisionDesktopCommand:
    """Parse exactly one fixed ``vision_desktop`` argv command."""
    tokens = _tokens(command)
    if not tokens or tokens[0] != "vision_desktop":
        raise VisionDesktopProtocolError(
            "INVALID_EXECUTABLE", "the executable must be exactly vision_desktop"
        )
    if len(tokens) < 2:
        raise VisionDesktopProtocolError("INVALID_OPERATION", "operation is required")
    try:
        operation = VisionDesktopOperation(tokens[1])
    except ValueError as exc:
        raise VisionDesktopProtocolError("INVALID_OPERATION", "unsupported operation") from exc
    tail = tokens[2:]

    if operation is VisionDesktopOperation.OBSERVE:
        values = _parse_flags(tail, frozenset({"--target-description", "--action"}))
        target = values["--target-description"].strip() if "--target-description" in values else None
        if target is not None and (not target or len(target) > 4096):
            raise VisionDesktopProtocolError(
                "INVALID_TARGET_DESCRIPTION", "target description is empty or too long"
            )
        return ParsedVisionDesktopCommand(
            operation=operation,
            target_description=target,
            action=_action(values["--action"]) if "--action" in values else None,
        )

    if operation is VisionDesktopOperation.INSPECT_REGION:
        values = _parse_flags(tail, frozenset({"--x", "--y", "--zoom"}))
        _required(values, "--x", "--y")
        try:
            zoom_text = values.get("--zoom", "4")
            zoom = int(zoom_text)
        except ValueError as exc:
            raise VisionDesktopProtocolError("INVALID_ZOOM", "zoom must be 2, 4, 6 or 8") from exc
        if str(zoom) != zoom_text or zoom not in (2, 4, 6, 8):
            raise VisionDesktopProtocolError("INVALID_ZOOM", "zoom must be 2, 4, 6 or 8")
        return ParsedVisionDesktopCommand(
            operation=operation,
            x=_finite(values["--x"], "x"),
            y=_finite(values["--y"], "y"),
            zoom=zoom,
        )

    if operation is VisionDesktopOperation.INTERACT_DETAIL:
        values = _parse_flags(tail, frozenset({"--x", "--y", "--action", "--steps"}))
        _required(values, "--x", "--y", "--action")
        action = _action(values["--action"])
        steps = None
        if "--steps" in values:
            if action not in {"scroll_up", "scroll_down", "scroll_up_1", "scroll_down_1"}:
                raise VisionDesktopProtocolError("INVALID_STEPS", "steps apply only to scrolling")
            try:
                steps = int(values["--steps"])
            except ValueError as exc:
                raise VisionDesktopProtocolError("INVALID_STEPS", "steps must be an integer from 1 through 5") from exc
            if str(steps) != values["--steps"] or not 1 <= steps <= 5:
                raise VisionDesktopProtocolError("INVALID_STEPS", "steps must be an integer from 1 through 5")
            if action.endswith("_1") and steps != 1:
                raise VisionDesktopProtocolError("INVALID_STEPS", "legacy scrolling action sends one step")
        return ParsedVisionDesktopCommand(
            operation=operation,
            x=_finite(values["--x"], "x"),
            y=_finite(values["--y"], "y"),
            action=action,
            steps=steps,
        )

    raise VisionDesktopProtocolError("INVALID_OPERATION", "unsupported operation")

@dataclass
class _ContextState:
    context_nonce: str
    sequence: int = 0
    phase: str = "new"
    requested_action: str | None = None
    terminal_error: str | None = None


class VisionDesktopExecutableBridge:
    """Serialize stateful operations and inject trusted protocol identity."""

    def __init__(self, backend: VisionDesktopBackend | None):
        self._backend = backend
        self._states: dict[str, _ContextState] = {}
        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.RLock] = {}

    def log_rejection(self, *, session_id, code, command=None, elapsed_ms=0):
        try:
            log = getattr(self._backend, "log_rejection", None)
            if log is not None:
                log(session_id=session_id, code=code, command=command,
                    operation=command.operation.value if command else None,
                    elapsed_ms=elapsed_ms)
        except Exception:
            pass

    def execute(self, command, *, session_id, capability, check_abort=None):
        started = time.monotonic()
        try:
            return self._execute(command, session_id=session_id,
                                 capability=capability, check_abort=check_abort)
        except Exception as exc:
            self.log_rejection(session_id=session_id, code=getattr(exc, "code", "UNAVAILABLE"),
                               command=command, elapsed_ms=round((time.monotonic() - started) * 1000, 3))
            raise

    def _execute(
        self,
        command: ParsedVisionDesktopCommand,
        *,
        session_id: str,
        capability: OperationCapability | None,
        check_abort: Callable[[], bool] | None = None,
    ) -> VisionDesktopCommandResult:
        bound = require_capability(capability, "session", session_id)
        require_capability(bound, "desktop_capture")
        if command.operation is VisionDesktopOperation.INTERACT_DETAIL:
            require_capability(bound, "desktop_input")
        if check_abort is not None and check_abort():
            return VisionDesktopCommandResult("CANCELLED", "vision_desktop cancelled")

        with self._lock:
            operation_lock = self._session_locks.setdefault(session_id, threading.RLock())
        with operation_lock:
            state = self._states.get(session_id)
            if state is None:
                state = _ContextState(context_nonce=str(uuid.uuid4()))
                self._states[session_id] = state
            if state.phase == "new" and command.operation is not VisionDesktopOperation.OBSERVE:
                raise VisionDesktopProtocolError(
                    "OBSERVE_REQUIRED", "observe must be the first operation"
                )
            if state.phase == "terminal" and command.operation not in {
                VisionDesktopOperation.OBSERVE,
            }:
                raise VisionDesktopProtocolError(
                    state.terminal_error or "OBSERVE_REQUIRED",
                    "start a new intent with observe before another physical action",
                )
            if command.operation is VisionDesktopOperation.INSPECT_REGION and state.phase not in {"observed", "detail"}:
                raise VisionDesktopProtocolError(
                    "OBSERVE_REQUIRED", "inspect_region requires an accepted observation"
                )
            if command.operation is VisionDesktopOperation.INTERACT_DETAIL:
                if state.phase != "detail":
                    raise VisionDesktopProtocolError(
                        "DETAIL_REQUIRED", "call inspect_region, view the returned zoomed image, then interact_detail"
                    )
                if state.requested_action is not None and command.action != state.requested_action:
                    raise VisionDesktopProtocolError(
                        "INVALID_ACTION", "action differs from the trusted requested action"
                    )

            state.sequence += 1
            if command.operation is VisionDesktopOperation.OBSERVE and state.phase == "detail":
                # A refresh attempt cannot leave the previous detail eligible,
                # even if native prepare/capture later fails. Terminal stays terminal.
                state.phase = "observed"
            if command.operation is VisionDesktopOperation.INSPECT_REGION:
                # A failed replacement inspection must not leave the old
                # detail eligible for an interaction.
                state.phase = "observed"
            envelope = ExecutableEnvelope(
                protocol_version=PROTOCOL_VERSION,
                context_nonce=state.context_nonce,
                sequence=state.sequence,
                session_id=session_id,
                operation=command.operation,
                public_arguments=command.public_arguments,
            )
            if self._backend is None:
                return VisionDesktopCommandResult(
                    "UNAVAILABLE",
                    "vision_desktop unavailable [NATIVE_PLATFORM_INTEGRATION_UNIMPLEMENTED]",
                )
            raw = self._backend.execute(envelope, bound, check_abort)
            if not isinstance(raw, BackendExecutableResult):
                raise VisionDesktopProtocolError(
                    "INVALID_BACKEND_RESULT", "backend result is not trusted protocol data"
                )

            attachment = self._trusted_attachment(raw, command.operation)
            if attachment is not None:
                if command.operation is VisionDesktopOperation.OBSERVE:
                    state.phase = "observed"
                    state.terminal_error = None
                    state.requested_action = command.action
                elif command.operation is VisionDesktopOperation.INSPECT_REGION:
                    state.phase = "detail"
            elif command.operation is VisionDesktopOperation.INTERACT_DETAIL:
                if raw.verified:
                    state.phase = "terminal"
                    state.terminal_error = "OBSERVE_REQUIRED"
                elif raw.dispatch_terminal or raw.state in {"ACTION_DELIVERY_UNKNOWN", "INDETERMINATE", "CANCELLED"}:
                    state.phase = "terminal"
                    state.terminal_error = raw.state
            return VisionDesktopCommandResult(raw.state, raw.text, attachment)

    def dispose_session(self, session_id: str) -> None:
        with self._lock:
            self._states.pop(session_id, None)
            dispose = getattr(self._backend, "dispose_session", None)
            if dispose is not None:
                dispose(session_id)

    @staticmethod
    def _trusted_attachment(
        result: BackendExecutableResult,
        operation: VisionDesktopOperation,
    ) -> TrustedObservationAttachment | None:
        expected_state = {
            VisionDesktopOperation.OBSERVE: "OBSERVATION_READY",
            VisionDesktopOperation.INSPECT_REGION: "DETAIL_READY",
        }.get(operation)
        complete = (
            expected_state is not None
            and result.state == expected_state
            and result.verified
            and result.image is not None
            and isinstance(result.observation_id, str)
            and bool(result.observation_id)
            and isinstance(result.frame_id, str)
            and bool(result.frame_id)
            and isinstance(result.geometry_version, int)
            and not isinstance(result.geometry_version, bool)
            and result.geometry_version >= 0
        )
        if not complete:
            return None
        return make_trusted_observation_attachment(
            image=result.image,
            observation_id=result.observation_id,
            frame_id=result.frame_id,
            geometry_version=result.geometry_version,
        )


_default_bridge = VisionDesktopExecutableBridge(backend=None)


def install_vision_desktop_backend(backend: VisionDesktopBackend) -> None:
    global _default_bridge
    _default_bridge = VisionDesktopExecutableBridge(backend=backend)


def clear_vision_desktop_backend() -> None:
    global _default_bridge
    _default_bridge = VisionDesktopExecutableBridge(backend=None)


def dispose_vision_desktop_session(session_id: str) -> None:
    _default_bridge.dispose_session(session_id)


def log_vision_desktop_rejection(*, session_id, code):
    """Record command-tool early rejection without persisting raw argv."""
    _default_bridge.log_rejection(session_id=session_id, code=code)


def public_protocol_error_detail(exc):
    """Safe fixed guidance, including no echo of unknown argv or axis values."""
    return {
        "INVALID_COORDINATE": "coordinate argument must be finite",
        "INVALID_ZOOM": "zoom must be a supported value",
        "UNKNOWN_ARGUMENT": "unsupported argument; check the executable contract",
        "DUPLICATE_ARGUMENT": "duplicate argument; supply each flag once",
        "MISSING_ARGUMENT": "missing required argument; check the executable contract",
        "INVALID_ACTION": "unsupported action or action differs from the trusted request",
        "INVALID_STEPS": "scroll steps must be a supported integer; omit steps for click actions",
        "OBSERVE_REQUIRED": "observe must be accepted first",
        "DETAIL_REQUIRED": "call inspect_region, view the returned zoomed image, then interact_detail",
        "FORBIDDEN_INTERNAL_ARGUMENT": "runtime identity is backend-owned",
        "ACTION_DELIVERY_UNKNOWN": "delivery is uncertain; never replay; use observe for a new intent",
        "CAPTURE_STALLED": "capture is stale; observe for a new intent",
        "OBSERVATION_TOO_OLD": "observation is stale; observe for a new intent",
        "FRAME_CHANGED": "screen changed; observe for a new intent",
        "DETAIL_FRAME_CHANGED": "detail screen region changed; observe for a new intent",
        "GEOMETRY_CHANGED": "desktop geometry changed; observe for a new intent",
        "STALE_FRAME": "frame is stale; observe for a new intent",
        "STALE_CHECK_INDETERMINATE": "freshness could not be verified; observe for a new intent",
        "NATIVE_TIMEOUT": "native transport timed out; observe for a new intent",
    }.get(exc.code, "command rejected; check the executable contract and observation state")


def execute_vision_desktop_command(
    command: str,
    *,
    session_id: str,
    capability: OperationCapability | None,
    check_abort: Callable[[], bool] | None = None,
) -> VisionDesktopCommandResult:
    try:
        parsed = parse_vision_desktop_command(command)
    except VisionDesktopProtocolError as exc:
        _default_bridge.log_rejection(session_id=session_id, code=exc.code)
        raise
    return _default_bridge.execute(
        parsed,
        session_id=session_id,
        capability=capability,
        check_abort=check_abort,
    )


__all__ = [
    "BackendExecutableResult",
    "ExecutableEnvelope",
    "ParsedVisionDesktopCommand",
    "TrustedObservationAttachment",
    "VisionDesktopCommandResult",
    "VisionDesktopExecutableBridge",
    "VisionDesktopOperation",
    "VisionDesktopProtocolError",
    "clear_vision_desktop_backend",
    "execute_vision_desktop_command",
    "install_vision_desktop_backend",
    "is_trusted_observation_attachment",
    "make_trusted_observation_attachment",
    "parse_vision_desktop_command",
]
