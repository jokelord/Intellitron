"""Wire-only Vision automation contracts."""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_HIGH_ENTROPY_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._~:-]{15,127}\Z")


def validate_automation_lifecycle_id(value: str) -> str:
    """Validate a transport-only task/cycle/token identifier."""

    if _HIGH_ENTROPY_ID_RE.fullmatch(value) is None:
        raise ValueError("automation lifecycle IDs must use high-entropy wire grammar")
    return value


class VisionAutomationBinding(BaseModel):
    """Transport-only browser task identity; never serialized into prompts."""

    model_config = ConfigDict(extra="forbid", strict=True)

    page_generation: int = Field(ge=0, le=9_007_199_254_740_991)
    mode: Literal["chat", "agent"]
    top_level_context_id: str
    automation_task_id: str
    capture_cycle_id: str
    cleanup_token: str | None = None

    @field_validator("top_level_context_id")
    @classmethod
    def _validate_context_id(cls, value: str) -> str:
        if value != value.strip() or not 1 <= len(value.encode("utf-8")) <= 256:
            raise ValueError("top_level_context_id must be a bounded trimmed string")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise ValueError("top_level_context_id cannot contain control characters")
        return value

    @field_validator("automation_task_id", "capture_cycle_id", "cleanup_token")
    @classmethod
    def _validate_high_entropy_id(cls, value: str | None) -> str | None:
        return validate_automation_lifecycle_id(value) if value is not None else None


class VisionAutomationTransportMetadata(BaseModel):
    """Client declaration checked against the actual inbound image payload."""

    model_config = ConfigDict(extra="forbid", strict=True)

    history_image_policy: Literal["aggregate_prior_image_blocks"]
    manual_image_count: int
    source_count: int
    retained_tick_count: int
    binding: VisionAutomationBinding


@dataclass(frozen=True, slots=True)
class VisionValidationResult:
    image_count: int
    encoded_data_url_bytes: int


@dataclass(slots=True)
class AgentRequestAdmissionStage:
    """Opaque runner-owned state spanning Agent validation and registration."""

    session_id: str
    projected_historical_image_count: int
    vision_automation_metadata: VisionAutomationTransportMetadata | None
    tool_configuration_snapshot: Any = None
    tool_configuration_candidate: Any = None
    _session: Any = None
    _history_omission: Any = None
    _enable_sticky_vision_automation_omission: bool = False
    _requires_prepare: bool = False
    _created_session: bool = False
    _committed: bool = False
    _accepted: bool = False
    _rolled_back: bool = False


def fingerprint_request_payload(payload: object) -> str:
    """Hash canonical transport data without retaining prompt or image bytes."""

    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
