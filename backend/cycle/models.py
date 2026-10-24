"""
Cycle API 请求/响应模型
"""

from __future__ import annotations

import re
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, StrictBool, field_validator, model_validator

from backend.agent.tool_configuration import AgentToolConfigurationRequest


class CycleStepRequest(BaseModel):
    """单个步骤的请求模型"""

    type: Literal["wait", "message", "cycle"]
    wait: Optional[float] = None
    message: Optional[str] = None
    photo: Optional[list[int]] = None
    cycle_count: Optional[int] = None
    cycle_steps: Optional[list[CycleStepRequest]] = None


class ScheduledMessageRequest(BaseModel):
    """A top-level fixed-wall-clock message definition for one Cycle run.

    This model intentionally does not inherit from :class:`CycleStepRequest`:
    scheduled messages belong to the independent scheduled track and can never
    be nested inside an ordinary ``cycle`` step.
    """

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    time: str
    recurrence: Literal["once", "daily"]
    day_count: int = 1
    message: str = ""
    photo: Optional[list[int]] = None

    @field_validator("time")
    @classmethod
    def _validate_time(cls, value: str) -> str:
        if not isinstance(value, str) or re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d", value
        ) is None:
            raise ValueError("time must use exact HH:mm:ss format")
        return value

    @model_validator(mode="after")
    def _validate_recurrence_and_payload(self) -> "ScheduledMessageRequest":
        if isinstance(self.day_count, bool) or self.day_count <= 0:
            raise ValueError("day_count must be a positive integer")
        if self.recurrence == "once" and self.day_count != 1:
            raise ValueError("once recurrence requires day_count to be 1")
        if not self.message and not self.photo:
            raise ValueError("scheduled message requires text or a camera photo")
        return self


class CycleStartRequest(BaseModel):
    """启动 Cycle 执行的请求"""

    session_id: Optional[str] = None
    cycle_run_id: Optional[str] = None
    steps: list[CycleStepRequest]
    scheduled_messages: list[ScheduledMessageRequest] = Field(default_factory=list)
    timezone: Optional[str] = None
    agent_mode: bool = False
    enable_thinking: bool = False
    enable_local_memory: StrictBool = False
    show_agent_steps: bool = False
    share_context: bool = True
    tool_configuration: Optional[AgentToolConfigurationRequest] = None

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (TypeError, ZoneInfoNotFoundError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def _validate_cycle_identity_and_scheduled_track(self) -> "CycleStartRequest":
        # The API maps an identity conflict to 409 before producer admission.
        if self.scheduled_messages and self.timezone is None:
            raise ValueError("timezone is required when scheduled_messages are provided")
        schedule_ids = [item.id for item in self.scheduled_messages]
        if len(schedule_ids) != len(set(schedule_ids)):
            raise ValueError("scheduled_messages ids must be unique per Cycle run")
        return self


class CycleStopRequest(BaseModel):
    """停止 Cycle 执行的请求"""

    session_id: Optional[str] = None
    cycle_run_id: Optional[str] = None

    @model_validator(mode="after")
    def _require_identity(self):
        if not self.session_id and not self.cycle_run_id:
            raise ValueError("cycle_run_id or session_id is required")
        return self
