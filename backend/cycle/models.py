"""
Cycle API 请求/响应模型
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel


class CycleStepRequest(BaseModel):
    """单个步骤的请求模型"""

    type: str  # "wait" | "message" | "cycle"
    wait: Optional[float] = None
    message: Optional[str] = None
    photo: Optional[list[int]] = None
    cycle_count: Optional[int] = None
    cycle_steps: Optional[list[CycleStepRequest]] = None


class CycleStartRequest(BaseModel):
    """启动 Cycle 执行的请求"""

    session_id: Optional[str] = None
    steps: list[CycleStepRequest]
    agent_mode: bool = False
    share_context: bool = True


class CycleStopRequest(BaseModel):
    """停止 Cycle 执行的请求"""

    session_id: str
