"""Privacy-preserving structured audit records for computer-use attempts."""
from __future__ import annotations

from datetime import datetime, timezone


SCHEMA_VERSION = "computer_use.audit.v1"


def audit_attempt(request, frames, crop, receipt, diagnostics=None) -> dict:
    state = getattr(receipt, "state", None)
    state = state.value if hasattr(state, "value") else state
    possible = getattr(receipt, "last_possible_event", None)
    record = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request.request_id,
        "session_id": request.session_id,
        "binding_id": request.binding_id,
        "action": request.action.value if hasattr(request.action, "value") else request.action,
        "state": state,
        "action_sent": True if state == "ACTION_SENT" else None if state == "ACTION_DELIVERY_UNKNOWN" else False,
        "last_possible_event": possible,
        "crop_id": getattr(crop, "crop_id", None),
        "observation_id": getattr(crop, "observation_id", None),
        "frame_id": getattr(crop, "frame_id", None),
        "calibration_fingerprint": request.calibration_fingerprint,
        "diagnostics": dict(diagnostics or {}),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    # Do not persist screenshot bytes, model prompts, target descriptions or
    # raw tool payloads in the audit record.
    return record


__all__ = ["SCHEMA_VERSION", "audit_attempt"]
