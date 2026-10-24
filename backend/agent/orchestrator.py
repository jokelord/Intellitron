"""Turn-parallel execution for tool calls emitted by one model response."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.utils.logger import get_logger


logger = get_logger(__name__)


@dataclass
class ExecutionStage:
    stage_id: int
    parallel: bool
    tool_calls: list
    results: list = field(default_factory=list)


@dataclass
class ExecutionPlan:
    stages: list[ExecutionStage] = field(default_factory=list)

    @property
    def total_tools(self) -> int:
        return sum(len(stage.tool_calls) for stage in self.stages)

    def to_dict(self) -> dict:
        return {
            "stages": [
                {
                    "stage_id": stage.stage_id,
                    "parallel": stage.parallel,
                    "tools": [
                        {
                            "call_id": call.id,
                            "name": call.function.name,
                            "args": call.function.arguments
                            if isinstance(call.function.arguments, dict)
                            else {},
                            "status": "pending",
                        }
                        for call in stage.tool_calls
                    ],
                }
                for stage in self.stages
            ]
        }


@dataclass
class ToolResult:
    call_id: str
    tool_name: str
    status: str
    output: Any = None
    error: str = ""
    duration_ms: float = 0


class DependencyAnalyzer:
    """Compatibility facade whose only rule is one model turn, one stage."""

    def analyze(self, tool_calls: list) -> ExecutionPlan:
        if not tool_calls:
            return ExecutionPlan()
        return ExecutionPlan(
            stages=[
                ExecutionStage(
                    stage_id=0,
                    parallel=len(tool_calls) > 1,
                    tool_calls=list(tool_calls),
                )
            ]
        )


class PlanExecutor:
    def execute(
        self,
        plan: ExecutionPlan,
        tools: dict,
        event_callback: Callable[[dict], None] | None = None,
        *,
        turn_dispatch_id: str | None = None,
    ) -> list[ToolResult]:
        results: list[ToolResult] = []
        for stage in plan.stages:
            if len(stage.tool_calls) == 1:
                stage_results = [
                    self._execute_single_tool(
                        stage.tool_calls[0], tools, event_callback, turn_dispatch_id
                    )
                ]
            else:
                with ThreadPoolExecutor(max_workers=len(stage.tool_calls)) as pool:
                    futures = [
                        pool.submit(
                            self._execute_single_tool,
                            call,
                            tools,
                            event_callback,
                            turn_dispatch_id,
                        )
                        for call in stage.tool_calls
                    ]
                    stage_results = [future.result() for future in futures]
            stage.results = stage_results
            results.extend(stage_results)
        return results

    def _execute_single_tool(
        self,
        call,
        tools: dict,
        event_callback: Callable[[dict], None] | None,
        turn_dispatch_id: str | None,
    ) -> ToolResult:
        tool_name = call.function.name
        if event_callback:
            event_callback(
                {"type": "orchestration_update", "call_id": call.id, "status": "running"}
            )
        tool = tools.get(tool_name)
        if tool is None:
            result = ToolResult(call.id, tool_name, "failed", error=f"Tool '{tool_name}' not found")
            if event_callback:
                event_callback(
                    {
                        "type": "orchestration_update",
                        "call_id": call.id,
                        "status": "failed",
                        "error": result.error,
                    }
                )
            return result

        started = time.time()
        try:
            from backend.agent.agent_runner import (
                current_session_context,
                current_tool_call_context,
            )

            current_session_context.set(
                getattr(tool, "_injected_session_id", None),
                getattr(tool, "_injected_abort_scope_id", None),
            )
            current_tool_call_context.set(call.id, turn_dispatch_id)
            arguments = call.function.arguments
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be an object")
            output = tool.forward(**arguments) if callable(getattr(tool, "forward", None)) else tool(**arguments)
            duration_ms = (time.time() - started) * 1000
            result = ToolResult(call.id, tool_name, "completed", output=output, duration_ms=duration_ms)
            if event_callback:
                preview = str(output or "")[:200]
                event_callback(
                    {
                        "type": "orchestration_update",
                        "call_id": call.id,
                        "status": "completed",
                        "result_preview": preview,
                        "duration_ms": duration_ms,
                    }
                )
            return result
        except Exception as exc:
            duration_ms = (time.time() - started) * 1000
            result = ToolResult(call.id, tool_name, "failed", error=str(exc), duration_ms=duration_ms)
            if event_callback:
                event_callback(
                    {
                        "type": "orchestration_update",
                        "call_id": call.id,
                        "status": "failed",
                        "error": str(exc),
                        "duration_ms": duration_ms,
                    }
                )
            return result
        finally:
            try:
                current_tool_call_context.clear()
                current_session_context.clear()
            except Exception:
                pass


class ToolOrchestrator:
    def __init__(self):
        self.analyzer = DependencyAnalyzer()
        self.executor = PlanExecutor()

    def analyze(self, tool_calls: list) -> ExecutionPlan:
        return self.analyzer.analyze(tool_calls)

    def execute(
        self,
        plan: ExecutionPlan,
        tools: dict,
        event_callback: Callable[[dict], None] | None = None,
        *,
        turn_dispatch_id: str | None = None,
    ) -> list[ToolResult]:
        return self.executor.execute(
            plan, tools, event_callback, turn_dispatch_id=turn_dispatch_id
        )
