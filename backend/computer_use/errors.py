from __future__ import annotations


class ComputerUseError(RuntimeError):
    """Stable fail-closed error returned by the computer-use subsystem."""

    def __init__(self, code: str, detail: str, *, field: str | None = None):
        self.code = code
        self.detail = detail
        self.field = field
        suffix = f" at {field}" if field else ""
        super().__init__(f"{code}{suffix}: {detail}")


# Fixed helper capture/transport statuses retained in private receipts. Unknown
# payload strings never become receipt reasons or model-facing guidance.
NATIVE_DISPATCH_REASON_CODES = frozenset({
    "NATIVE_TIMEOUT", "PIPEWIRE_NO_PROCESS_CALLBACK", "PIPEWIRE_PROCESS_CALLBACK_NO_BUFFER",
    "PIPEWIRE_STREAM_PAUSED", "PIPEWIRE_STREAM_CONNECTING", "PIPEWIRE_STREAM_NOT_STREAMING",
    "PIPEWIRE_STREAM_ERROR", "PIPEWIRE_UNAVAILABLE", "PIPEWIRE_BUFFER_REJECTED",
    "PIPEWIRE_ONLY_EMPTY_BUFFERS", "PIPEWIRE_TIMESTAMP_REJECTED", "PIPEWIRE_PTS_NONADVANCING",
    "PIPEWIRE_DISCONNECTED_BEFORE_STREAMING", "PIPEWIRE_DISCONNECTED_AFTER_STREAMING",
    "PIPEWIRE_NEW_FRAME_COPY_FAILED", "PIPEWIRE_GEOMETRY_CHANGED", "EIS_DISCONNECTED",
    "INPUT_REVOKED", "DISPATCH_DEADLINE_EXCEEDED",
    "X11_GEOMETRY_CHANGED", "X11_CAPTURE_FAILED", "X11_XWAYLAND_NOT_SUPPORTED",
    "FRAME_ALLOCATION_FAILED", "FRAME_TIMEOUT", "NATIVE_BACKEND_UNAVAILABLE",
})


ERROR_CODES = frozenset(
    {
        "INVALID_GLOBAL_COORDINATE",
        "INVALID_ZOOM",
        "INVALID_LOCAL_COORDINATE",
        "INVALID_ACTION",
        "INVALID_STEPS",
        "TARGET_NOT_VISIBLE",
        "TARGET_AMBIGUOUS",
        "STALE_FRAME",
        "STALE_CHECK_INDETERMINATE",
        "CAPTURE_STALLED",
        "GEOMETRY_CHANGED",
        "INVALID_CROP_ID",
        "INVALID_CONTEXT_BINDING",
        "UNSUPPORTED_CAPTURE_GEOMETRY",
        "UNPROVEN_MONITOR_COVERAGE",
        "INPUT_REGION_NOT_FOUND",
        "AMBIGUOUS_INPUT_REGION",
        "INPUT_REGION_MISMATCH",
        "INPUT_CAPABILITY_MISSING",
        "INPUT_DEVICE_NOT_READY",
        "CALIBRATION_REQUIRED",
        "PORTAL_CANCELLED",
        "SESSION_CLOSED",
        "PREDISPATCH_BUDGET_EXHAUSTED",
        "CANCELLED",
        "HELPER_PROTOCOL_ERROR",
        "HELPER_LIFECYCLE_ERROR",
        "ACTION_DELIVERY_UNKNOWN",
        "INVALID_MODEL_OUTPUT",
        "INVALID_CAPTURE_RESULT",
        "INVALID_PROTOCOL_STATE",
        "OBSERVE_REQUIRED",
        "DETAIL_REQUIRED",
        "UNAVAILABLE",
        "INVALID_OPERATOR_SESSION_CONFIG",
        "COMPUTER_USE_INITIALIZATION_FAILED",
        "LEGACY_EXECUTABLE_REQUIRED",
    }
)
