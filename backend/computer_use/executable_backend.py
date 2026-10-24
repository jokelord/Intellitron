"""Adapter from the fixed ``vision_desktop`` protocol to typed grounding.

The executable protocol is deliberately synchronous because it is called by
the existing command-tool worker.  The grounding engine remains asynchronous;
this adapter is the only boundary that drives its coroutine transitions and
keeps the immutable request/observation/detail records per Agent session.

No model call, coordinate inference, shell, or native fallback lives here.
The deployment supplies an ``engine_factory`` whose engine has already been
constructed with a readiness-checked native capture/input backend.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import threading
import time
import uuid
from dataclasses import dataclass, replace
from io import BytesIO
from typing import Any, Callable

from PIL import Image

from backend.security.capabilities import OperationCapability

from .contracts import (
    Action,
    EncodedStage,
    GroundingRequest,
    ImageArtifact,
    ProtocolDescriptor,
)
from .errors import ComputerUseError, ERROR_CODES, NATIVE_DISPATCH_REASON_CODES
from .coordinates import local_to_monitor
from .model_adapter import decode_stage, describe_stage
from .executable_protocol import (
    BackendExecutableResult,
    ExecutableEnvelope,
    VisionDesktopBackend,
    VisionDesktopOperation,
)


# Receipt reasons are backend facts, never application/target judgements.
_DISPATCH_REASON_CODES = {
    "CAPTURE_STALLED": "CAPTURE_STALLED", "OBSERVATION_TOO_OLD": "OBSERVATION_TOO_OLD",
    "FRAME_CHANGED": "FRAME_CHANGED", "TARGET_CHANGED": "DETAIL_FRAME_CHANGED",
    "GEOMETRY_CHANGED": "GEOMETRY_CHANGED", "geometry_changed": "GEOMETRY_CHANGED",
    "BINDING_CHANGED": "GEOMETRY_CHANGED", "binding_changed": "GEOMETRY_CHANGED",
    "STREAM_GENERATION_CHANGED": "STALE_FRAME", "SEQUENCE_REGRESSED": "STALE_FRAME",
    "STALE_FRAME": "STALE_FRAME", "NATIVE_TIMEOUT": "NATIVE_TIMEOUT",
    "INVALID_CLOCK": "STALE_CHECK_INDETERMINATE", "INVALID_TIMESTAMP": "STALE_CHECK_INDETERMINATE",
    "MISSING_OBSERVATION": "STALE_CHECK_INDETERMINATE", "INVALID_TARGET_REGION": "STALE_CHECK_INDETERMINATE",
    "freshness_unavailable": "STALE_CHECK_INDETERMINATE", "freshness": "STALE_CHECK_INDETERMINATE",
    "unsupported_backend": "UNSUPPORTED_CAPTURE_GEOMETRY",
    "desktop_input_disabled": "INPUT_CAPABILITY_MISSING", "capability_mismatch": "INVALID_CONTEXT_BINDING",
    "INVALID_CONTEXT_BINDING": "INVALID_CONTEXT_BINDING",
    "user_cancelled": "CANCELLED", "cancelled": "CANCELLED", "CANCELLED": "CANCELLED",
    "session_closed": "SESSION_CLOSED", "SESSION_CLOSED": "SESSION_CLOSED",
}
_DISPATCH_REASON_CODES.update({code: "CAPTURE_STALLED" for code in NATIVE_DISPATCH_REASON_CODES
                               if code.startswith("PIPEWIRE_")})
_DISPATCH_REASON_CODES.update(PIPEWIRE_GEOMETRY_CHANGED="GEOMETRY_CHANGED",
    EIS_DISCONNECTED="INPUT_DEVICE_NOT_READY", INPUT_REVOKED="INPUT_DEVICE_NOT_READY",
    DISPATCH_DEADLINE_EXCEEDED="STALE_FRAME",
    X11_GEOMETRY_CHANGED="GEOMETRY_CHANGED", X11_CAPTURE_FAILED="CAPTURE_STALLED",
    FRAME_ALLOCATION_FAILED="CAPTURE_STALLED", FRAME_TIMEOUT="CAPTURE_STALLED",
    X11_XWAYLAND_NOT_SUPPORTED="INPUT_DEVICE_NOT_READY",
    NATIVE_BACKEND_UNAVAILABLE="INPUT_DEVICE_NOT_READY")
_DISPATCH_LOG_REASONS = frozenset(_DISPATCH_REASON_CODES) | {"complete", "release_uncertain"}


class InitializationUnavailableBackend:
    """Pinned startup rejection, without capture, input, or configuration reload."""

    runtime_mode = "unavailable"

    def __init__(self, error: Exception):
        code = error.code if isinstance(error, ComputerUseError) else None
        self.initialization_error_code = (
            code if code in ERROR_CODES else "COMPUTER_USE_INITIALIZATION_FAILED"
        )

    def execute(self, envelope, capability, check_abort):
        code = self.initialization_error_code
        return BackendExecutableResult(
            code, f"vision_desktop unavailable [{code}]: ask the operator to fix startup configuration and restart the backend",
            verified=False,
        )


@dataclass
class _SessionState:
    engine: Any
    request: GroundingRequest
    observation: Any
    overview_artifact: ImageArtifact
    detail: Any = None
    detail_artifact: ImageArtifact | None = None
    terminal: bool = False
    inspections: int = 0
    terminal_result: str | None = None
    bound_action: str | None = None


def _run_awaitable(value: Any) -> Any:
    """Run one engine coroutine from either sync code or an event-loop thread."""

    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)

    result: list[Any] = []
    failure: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(value))
        except BaseException as exc:  # propagate the original typed failure
            failure.append(exc)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    if not result:
        raise RuntimeError("grounding coroutine returned no result")
    return result[0]


def _artifact(value: Any) -> ImageArtifact:
    if isinstance(value, ImageArtifact):
        return value
    if isinstance(value, Image.Image):
        output = BytesIO()
        value.save(output, format="PNG", optimize=False)
        raw = output.getvalue()
        return ImageArtifact(
            png=raw,
            width=value.width,
            height=value.height,
            content_hash=hashlib.sha256(raw).hexdigest(),
            renderer_fingerprint="runtime-pil-artifact",
        )
    png = getattr(value, "png", None)
    width = getattr(value, "width", None)
    height = getattr(value, "height", None)
    if (
        not isinstance(png, (bytes, bytearray, memoryview))
        or isinstance(width, bool)
        or not isinstance(width, int)
        or width <= 0
        or isinstance(height, bool)
        or not isinstance(height, int)
        or height <= 0
    ):
        raise ComputerUseError("INVALID_CAPTURE_RESULT", "renderer did not return an image artifact")
    raw = bytes(png)
    return ImageArtifact(
        png=raw,
        width=width,
        height=height,
        content_hash=getattr(value, "content_hash", None) or hashlib.sha256(raw).hexdigest(),
        renderer_fingerprint=getattr(value, "renderer_fingerprint", None) or "runtime-artifact",
    )


def _image(value: Any) -> Image.Image:
    if isinstance(value, Image.Image):
        return value.copy()
    artifact = _artifact(value)
    try:
        with Image.open(BytesIO(artifact.png)) as decoded:
            return decoded.convert("RGB").copy()
    except Exception as exc:
        raise ComputerUseError("INVALID_CAPTURE_RESULT", "renderer image artifact is invalid") from exc


class VisionDesktopGroundingBackend(VisionDesktopBackend):
    """Execute protocol operations against a session-bound GroundingEngine."""

    def __init__(
        self,
        engine_factory: Callable[[str, OperationCapability], Any],
        *,
        diagnostics=None,
    ):
        if not callable(engine_factory):
            raise TypeError("engine_factory must be callable")
        self._diagnostics = diagnostics
        self._diagnostic_local = threading.local()
        self._engine_factory = engine_factory
        self._states: dict[str, _SessionState] = {}
        self._protocol_state: dict[str, tuple[str, int]] = {}
        self._capabilities: dict[str, OperationCapability] = {}
        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.RLock] = {}

    def _diagnostic_update(self, **values):
        record = getattr(self._diagnostic_local, "record", None)
        if record is not None:
            record.update(values)

    def log_rejection(self, *, session_id, code, operation=None, command=None, elapsed_ms=0):
        if self._diagnostics is None:
            return
        values = dict(session_id=session_id, operation=operation, result="REJECTED",
                      error_code=code, phase="executable_preflight", elapsed_ms=elapsed_ms)
        if command is not None:
            values.update(self._argument_diagnostics(command.operation, command.public_arguments))
        self._write_diagnostics(values)

    def _write_diagnostics(self, record):
        try:
            if self._diagnostics is not None:
                self._diagnostics.write(record)
        except Exception:
            pass

    @staticmethod
    def _argument_diagnostics(operation, args):
        values = {}
        action = args.get("action")
        if action in {"left_click", "right_click", "left_double_click", "right_double_click", "scroll_up", "scroll_down", "scroll_up_1", "scroll_down_1"}:
            values["action"] = action
            steps = args.get("steps", 1)
            if action.startswith("scroll_") and isinstance(steps, int) and not isinstance(steps, bool) and 1 <= steps <= 5:
                values["steps"] = steps
        if operation is VisionDesktopOperation.INSPECT_REGION:
            values.update(stage1_point=[args.get("x"), args.get("y")], selected_zoom=args.get("zoom", 4))
        elif operation is VisionDesktopOperation.INTERACT_DETAIL:
            values["stage2_point"] = [args.get("x"), args.get("y")]
        return values

    def _snapshot_diagnostics(self, envelope):
        state = self._states.get(envelope.session_id)
        if state is None:
            return
        frame = state.observation.frame
        self._diagnostic_update(request_id=state.request.request_id,
            binding_id=state.request.binding_id, frame_id=frame.frame_id,
            calibration_fingerprint=state.request.calibration_fingerprint,
            observation_id=frame.observation_id, geometry_version=frame.geometry_version,
            stage1_zoom=getattr(state.engine.config, "stage1_zoom", 4),
            pre_action_validation=getattr(state.engine.config, "pre_action_validation", None),
            stream_generation=getattr(frame, "stream_generation", None),
            frame_sequence=getattr(frame, "sequence", None),
            captured_monotonic=getattr(frame, "captured_monotonic", None),
            arrived_monotonic=getattr(frame, "arrived_monotonic", None),
            source_size=[frame.width, frame.height],
            logical_origin=getattr(getattr(frame, "binding", None), "origin", None),
            logical_size=getattr(getattr(frame, "binding", None), "size", None))
        if state.detail is not None:
            crop = state.detail.crop
            self._diagnostic_update(crop_id=crop.crop_id,
                source_rect=[crop.x0, crop.y0, crop.x1, crop.y1],
                source_size=[crop.source_width, crop.source_height])

    def execute(self, envelope, capability, check_abort=None):
        started = time.monotonic()
        record = dict(session_id=envelope.session_id, context_nonce=envelope.context_nonce,
                      sequence=envelope.sequence, operation=envelope.operation.value, phase="backend")
        record.update(self._argument_diagnostics(envelope.operation, envelope.public_arguments))
        self._diagnostic_local.record = record
        with self._lock:
            operation_lock = self._session_locks.setdefault(envelope.session_id, threading.RLock())
        try:
            with operation_lock:
                # Snapshot before interaction clears the accepted detail.
                try:
                    self._snapshot_diagnostics(envelope)
                except Exception:
                    pass
                result = self._execute(envelope, capability, check_abort)
                if result.state == "OBSERVATION_READY" and envelope.operation in {
                    VisionDesktopOperation.OBSERVE,
                }:
                    # A newly accepted overview has no current detail crop.
                    # Interaction records retain their pre-cleanup mapping.
                    record.pop("crop_id", None)
                    record.pop("source_rect", None)
                try:
                    self._snapshot_diagnostics(envelope)
                except Exception:
                    pass
                record.update(result=result.state)
                if result.state in {"OBSERVATION_READY", "DETAIL_READY"}:
                    try:
                        state = self._states[envelope.session_id]
                        artifact = state.detail_artifact if result.state == "DETAIL_READY" else state.overview_artifact
                        record.update(image_size=[artifact.width, artifact.height],
                                      png_sha256=hashlib.sha256(artifact.png).hexdigest())
                        from .debug_images import save_debug_image
                        saved = save_debug_image(state.engine.config, envelope,
                            "stage2" if result.state == "DETAIL_READY" else "stage1", artifact)
                        if saved is not None:
                            record["debug_image_path"] = saved
                    except Exception:
                        pass
                if result.state not in {"OBSERVATION_READY", "DETAIL_READY", "ACTION_SENT"}:
                    record["error_code"] = result.state
                return result
        finally:
            record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            self._write_diagnostics(record)
            self._diagnostic_local.record = None

    def _execute(
        self,
        envelope: ExecutableEnvelope,
        capability: OperationCapability,
        check_abort: Callable[[], bool] | None,
    ) -> BackendExecutableResult:
        if capability.session_id != envelope.session_id:
            return BackendExecutableResult("INVALID_CONTEXT_BINDING", "session capability mismatch")
        with self._lock:
            operation_lock = self._session_locks.setdefault(envelope.session_id, threading.RLock())
        try:
            with operation_lock:
                pinned = self._capabilities.get(envelope.session_id)
                if pinned is not None and pinned is not capability:
                    return BackendExecutableResult("INVALID_CONTEXT_BINDING", "session capability changed")
                if pinned is None:
                    if envelope.operation is not VisionDesktopOperation.OBSERVE:
                        return BackendExecutableResult("OBSERVE_REQUIRED", "observe must be accepted first")
                    self._capabilities[envelope.session_id] = capability
                if envelope.protocol_version != 1:
                    return BackendExecutableResult("INVALID_PROTOCOL_STATE", "unsupported protocol version")
                previous = self._protocol_state.get(envelope.session_id)
                if previous is None:
                    if envelope.operation is not VisionDesktopOperation.OBSERVE:
                        return BackendExecutableResult("OBSERVE_REQUIRED", "observe must be accepted first")
                else:
                    nonce, sequence = previous
                    if envelope.context_nonce != nonce or envelope.sequence != sequence + 1:
                        return BackendExecutableResult(
                            "INVALID_PROTOCOL_STATE", "context nonce or sequence is stale"
                        )
                self._protocol_state[envelope.session_id] = (
                    envelope.context_nonce,
                    envelope.sequence,
                )
                if envelope.operation is VisionDesktopOperation.OBSERVE:
                    result = self._observe(envelope, capability, check_abort)
                elif envelope.operation is VisionDesktopOperation.INSPECT_REGION:
                    result = self._inspect(envelope, capability, check_abort)
                elif envelope.operation is VisionDesktopOperation.INTERACT_DETAIL:
                    result = self._interact(envelope, capability, check_abort)
                else:
                    raise ComputerUseError("INVALID_PROTOCOL_STATE", "unsupported desktop operation")
                return result
        except ComputerUseError as exc:
            self._diagnostic_update(error_type=type(exc).__name__, error_field=exc.field)
            guidance = {
                "INVALID_MODEL_OUTPUT": "invalid tool arguments; check the named field and current image",
                "INVALID_ZOOM": "unsupported zoom",
                "INVALID_STEPS": "invalid scroll steps",
                "INVALID_GLOBAL_COORDINATE": "invalid overview-relative point",
                "INVALID_LOCAL_COORDINATE": "invalid detail-relative point",
                "STALE_FRAME": "observation is stale; observe",
                "OBSERVE_REQUIRED": "a new observe intent is required",
                "DETAIL_REQUIRED": "call inspect_region, view the returned zoomed image, then interact_detail",
                "CANCELLED": "desktop operation cancelled",
            }.get(exc.code, "desktop operation rejected; check state and observe")
            field = f" at {exc.field}" if exc.field in {"arguments.x", "arguments.y", "arguments.zoom", "arguments.action", "arguments.steps", "axis", "name", "arguments"} else ""
            return BackendExecutableResult(exc.code, f"{exc.code}{field}: {guidance}", verified=False)
        except (TypeError, ValueError, KeyError) as exc:
            self._diagnostic_update(error_type=type(exc).__name__)
            return BackendExecutableResult("INVALID_PROTOCOL_STATE", "invalid desktop protocol state or arguments", verified=False)
        except Exception as exc:
            # Native/helper failures are intentionally opaque to the model and
            # never create a trusted attachment.
            self._diagnostic_update(error_type=type(exc).__name__)
            code = getattr(exc, "code", "UNAVAILABLE")
            if code in {"HELPER_NOT_BUILT", "HELPER_NOT_EXECUTABLE"}:
                self._diagnostic_update(phase="helper_preflight", error_code=code)
                guidance = ("configured desktop helper is missing; ask the operator to build it"
                            if code == "HELPER_NOT_BUILT" else
                            "configured desktop helper cannot be executed; ask the operator to check permissions")
                return BackendExecutableResult(code, f"{code}: {guidance}", verified=False)
            return BackendExecutableResult(code, f"vision_desktop unavailable [{getattr(exc, 'code', type(exc).__name__)}]", verified=False)

    def _engine(self, session_id: str, capability: OperationCapability, envelope=None, check_abort=None) -> Any:
        engine = self._states.get(session_id).engine if session_id in self._states else None
        if engine is None:
            if hasattr(self._engine_factory, "dispose_session"):
                engine = self._engine_factory(session_id, capability,
                    context_nonce=envelope.context_nonce, check_abort=check_abort)
            else:
                engine = self._engine_factory(session_id, capability)
        if engine is None:
            raise ComputerUseError("INPUT_DEVICE_NOT_READY", "grounding engine is not configured")
        binding = getattr(engine, "binding", None)
        if getattr(binding, "session_id", None) != session_id:
            raise ComputerUseError("INVALID_CONTEXT_BINDING", "grounding engine binding mismatch")
        expected_binding = getattr(capability, "desktop_binding_id", None)
        if expected_binding is not None and expected_binding != getattr(binding, "binding_id", None):
            raise ComputerUseError("INVALID_CONTEXT_BINDING", "grounding engine binding identity mismatch")
        prepare = getattr(engine, "prepare", None)
        if prepare is not None:
            prepare(check_abort)
        return engine

    @staticmethod
    def _request(envelope: ExecutableEnvelope, engine: Any, target: str, action: str) -> GroundingRequest:
        config = getattr(engine, "config", None)
        protocol_name = getattr(config, "protocol", "relative_1000")
        pixel_semantics = getattr(config, "pixel_semantics", None)
        fingerprint = getattr(config, "calibration_fingerprint", None)
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ComputerUseError("CALIBRATION_REQUIRED", "calibration fingerprint is missing")
        binding = engine.binding
        return GroundingRequest(
            request_id=str(uuid.uuid4()),
            session_id=envelope.session_id,
            binding_id=binding.binding_id,
            target_description=target,
            action=Action(action),
            protocol=ProtocolDescriptor(name=protocol_name, pixel_semantics=pixel_semantics),
            calibration_fingerprint=fingerprint,
        )

    def _observe(self, envelope, capability, check_abort):
        args = envelope.public_arguments
        target = args.get("target_description", "current desktop")
        action = args.get("action")
        if not isinstance(target, str) or not target or (action is not None and not isinstance(action, str)):
            raise ComputerUseError("INVALID_MODEL_OUTPUT", "observe arguments are incomplete")
        previous = self._states.get(envelope.session_id)
        if previous is not None:
            previous.detail = None
            previous.detail_artifact = None
        engine = self._engine(envelope.session_id, capability, envelope, check_abort)
        # The capture-only placeholder does not prebind a later input action.
        request = self._request(envelope, engine, target, action if action is not None else Action.LEFT_CLICK.value)
        observation = _run_awaitable(engine.observe(request, capability, check_abort))
        frame = observation.frame
        artifact = _artifact(observation.overview)
        image = _image(artifact)
        self._states[envelope.session_id] = _SessionState(
            engine, request, observation, artifact, bound_action=action
        )
        return BackendExecutableResult(
            "OBSERVATION_READY",
            "desktop observation ready",
            verified=True,
            image=image,
            observation_id=frame.observation_id,
            frame_id=frame.frame_id,
            geometry_version=frame.geometry_version,
        )

    def _state(self, envelope: ExecutableEnvelope) -> _SessionState:
        state = self._states.get(envelope.session_id)
        if state is None:
            raise ComputerUseError("OBSERVE_REQUIRED", "observe must be accepted first")
        return state

    def _inspect(self, envelope, capability, check_abort):
        state = self._state(envelope)
        if state.terminal:
            raise ComputerUseError("OBSERVE_REQUIRED", "form a new intent with observe after recovery")
        # Every replacement attempt invalidates the old crop, including a
        # budget or native prepare rejection; it must never remain clickable.
        state.detail = None
        state.detail_artifact = None
        if state.inspections >= getattr(state.engine.config, "max_pre_dispatch_attempts", 3):
            raise ComputerUseError("INSPECT_LIMIT_REACHED", "no further detail crops are allowed for this overview")
        state.inspections += 1
        prepare = getattr(state.engine, "prepare", None)
        if prepare is not None:
            prepare(check_abort)
        args = envelope.public_arguments
        grid_label_placement = getattr(
            getattr(state.engine, "config", None), "grid_label_placement", "edge"
        )
        instance = describe_stage(
            "stage1", state.overview_artifact, None, state.request,
            grid_label_placement=grid_label_placement,
            stage1_zoom=getattr(state.engine.config, "stage1_zoom", 4),
        )
        decision = decode_stage(
            EncodedStage(
                instance=instance,
                messages=(),
                tools=(),
                image_metadata={},
            ),
            {
                "name": "inspect_region",
                "arguments": {
                    "x": args.get("x"),
                    "y": args.get("y"),
                    "zoom": args.get("zoom", 4),
                },
            },
        )
        detail = _run_awaitable(
            state.engine.inspect_region(state.request, state.observation, decision, check_abort)
        )
        state.detail = detail
        state.detail_artifact = _artifact(detail.detail)
        image = _image(state.detail_artifact)
        crop = detail.crop
        return BackendExecutableResult(
            "DETAIL_READY",
            "detail observation ready; use only the latest image",
            verified=True,
            image=image,
            observation_id=detail.frame.observation_id,
            frame_id=detail.frame.frame_id,
            geometry_version=detail.frame.geometry_version,
        )

    def _interact(self, envelope, capability, check_abort):
        state = self._state(envelope)
        if state.terminal:
            raise ComputerUseError("OBSERVE_REQUIRED", "form a new intent with observe after recovery")
        prepare = getattr(state.engine, "prepare", None)
        if prepare is not None:
            prepare(check_abort)
        if state.detail is None:
            raise ComputerUseError("DETAIL_REQUIRED", "inspect_region must be accepted first")
        args = envelope.public_arguments
        if state.detail_artifact is None:
            raise ComputerUseError("DETAIL_REQUIRED", "detail image is unavailable")
        try:
            action = Action(args.get("action"))
        except (TypeError, ValueError) as exc:
            raise ComputerUseError("INVALID_ACTION", "invalid action", field="arguments.action") from exc
        if state.bound_action is not None and action.value != state.bound_action:
            raise ComputerUseError("INVALID_ACTION", "action differs from explicitly bound action", field="arguments.action")
        request = replace(state.request, action=action)
        grid_label_placement = getattr(
            getattr(state.engine, "config", None), "grid_label_placement", "edge"
        )
        instance = describe_stage(
            "stage2", state.detail_artifact, state.detail.crop, request,
            grid_label_placement=grid_label_placement,
            stage1_zoom=getattr(state.engine.config, "stage1_zoom", 4),
        )
        decision = decode_stage(
            EncodedStage(
                instance=instance,
                messages=(),
                tools=(),
                image_metadata={},
            ),
            {
                "name": "interact_detail",
                "arguments": {
                    "x": args.get("x"),
                    "y": args.get("y"),
                    "action": args.get("action"),
                    **({"steps": args["steps"]} if "steps" in args else {}),
                },
            },
        )
        state.request = request
        nx, ny = local_to_monitor(state.detail.crop, decision.x, decision.y)
        self._diagnostic_update(
            normalized_source_point=[float(nx), float(ny)],
            source_point=[float(nx * state.detail.crop.source_width), float(ny * state.detail.crop.source_height)])
        receipt = _run_awaitable(
            state.engine.interact_detail(
                state.request, state.detail, decision, capability, check_abort
            )
        )
        raw_state = getattr(receipt, "state", receipt)
        state_name = getattr(raw_state, "value", raw_state)
        cleanup = getattr(receipt, "cleanup_status", None)
        cleanup = cleanup if isinstance(cleanup, str) and cleanup in _DISPATCH_LOG_REASONS else "unclassified"
        last_event = getattr(receipt, "last_possible_event", None)
        freshness = getattr(receipt, "freshness", {})
        evidence = {key: freshness.get(key) for key in
                    ("age_seconds", "observed_sequence", "current_sequence", "global_score", "target_score")
                    if hasattr(freshness, "get") and isinstance(freshness.get(key), (int, float))}
        self._diagnostic_update(dispatch_state=state_name, cleanup_status=cleanup,
            last_possible_event=last_event,
            dispatch_stage="validation" if last_event is None else "input", freshness=evidence,
            pre_action_validation=getattr(receipt, "pre_action_validation", None)
                or getattr(state.engine.config, "pre_action_validation", None))
        if state_name != "ACTION_SENT" and last_event is not None:
            # Possible physical delivery always dominates a more specific
            # rejection reason. Never turn it into a safely retryable error.
            state_name = "ACTION_DELIVERY_UNKNOWN"
        elif state_name in {"CANCELLED", "INDETERMINATE"}:
            state_name = _DISPATCH_REASON_CODES.get(cleanup, state_name)
        verified = state_name == "ACTION_SENT"
        state.terminal = True
        state.terminal_result = str(state_name)
        state.detail = None
        state.detail_artifact = None
        return BackendExecutableResult(
            str(state_name),
            ("desktop action result: ACTION_SENT" if verified else
             "ACTION_DELIVERY_UNKNOWN: input delivery is uncertain; never replay this action" if state_name == "ACTION_DELIVERY_UNKNOWN" else
             f"{state_name}: no input sent; observe for a new intent"),
            verified=verified,
            dispatch_terminal=True,
        )

    def dispose_session(self, session_id: str) -> None:
        with self._lock:
            state = self._states.pop(session_id, None)
            self._protocol_state.pop(session_id, None)
            self._capabilities.pop(session_id, None)
            dispose = getattr(self._engine_factory, "dispose_session", None)
            if dispose is not None:
                dispose(session_id)
            elif state is not None:
                close = getattr(state.engine, "close", None)
                if close is not None:
                    close()

    def shutdown(self) -> None:
        with self._lock:
            for session_id in tuple(self._states):
                self.dispose_session(session_id)
            self._protocol_state.clear()
            self._capabilities.clear()
            shutdown = getattr(self._engine_factory, "shutdown", None)
            if shutdown is not None:
                shutdown()


__all__ = ["VisionDesktopGroundingBackend"]
