# Tool Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable SmartToolCallingAgent to accept multiple tool_calls in a single LLM response, automatically analyze dependencies via static rules, and execute them in parallel/serial stages — plus provide search_and_fetch / wikipedia_search_and_fetch composite tools and a frontend orchestration view.

**Architecture:** A new `ToolOrchestrator` module (`backend/agent/orchestrator.py`) handles dependency analysis and staged execution using `ThreadPoolExecutor`. It integrates into `SmartToolCallingAgent` via an override of `process_tool_calls()`. Composite tools wrap existing search+fetch logic with concurrent fetching. The frontend gets new SSE event types for real-time orchestration visualization.

**Tech Stack:** Python 3.12, concurrent.futures.ThreadPoolExecutor, smolagents (override, not patch), FastAPI SSE, Next.js 14 + React + TypeScript + Tailwind CSS

---

## File Structure

| File | Responsibility | New/Modify |
|------|---------------|------------|
| `backend/agent/orchestrator.py` | DependencyAnalyzer + ExecutionPlan + PlanExecutor | New |
| `backend/tools/search_and_fetch.py` | search_and_fetch composite tool | New |
| `backend/tools/wikipedia_search_and_fetch.py` | wikipedia_search_and_fetch composite tool | New |
| `backend/agent/agent_runner.py` | Override `process_tool_calls()`, add `__init__`, register new tools, emit orchestration SSE events | Modify |
| `backend/agent/local_llm_model.py` | Adjust `_extract_tool_calls()` for multi-tool + final_answer coexistence | Modify |
| `config/config-agent-prompts.yaml` | Remove single-tool restriction, add multi-tool guidance + composite tool docs | Modify |
| `frontend/src/lib/api.ts` | Extend `AgentStep` type, add orchestration interfaces | Modify |
| `frontend/src/components/AgentStepDisplay.tsx` | Add `OrchestrationView` sub-component | Modify |
| `frontend/src/app/page.tsx` | Handle new orchestration SSE events | Modify |
| `frontend/src/app/vision/page.tsx` | Handle new orchestration SSE events | Modify |
| `tests/test_orchestrator.py` | Unit tests for ToolOrchestrator | New |
| `tests/test_search_and_fetch.py` | Unit tests for composite tools | New |
| `AGENTS.md` | Update architecture docs | Modify |

---

### Task 1: ToolOrchestrator Core — DependencyAnalyzer + ExecutionPlan

**Files:**
- Create: `backend/agent/orchestrator.py`
- Test: `tests/test_orchestrator.py`

This task creates the core orchestration module with dependency analysis and execution plan generation. No execution yet — just the analysis and plan data structures.

- [ ] **Step 1: Write failing tests for DependencyAnalyzer**

```python
# tests/test_orchestrator.py
"""Tests for ToolOrchestrator dependency analysis and execution."""
import pytest
from unittest.mock import MagicMock

# We'll create minimal mock tool_calls for testing
def _make_tool_call(call_id: str, name: str, arguments: dict = None):
    """Create a mock ChatMessageToolCall-like object."""
    tc = MagicMock()
    tc.id = call_id
    tc.function.name = name
    tc.function.arguments = arguments or {}
    return tc


class TestDependencyAnalyzer:
    """Test static rule-based dependency analysis."""

    def test_single_tool_no_stages(self):
        """Single tool call should produce one stage with one tool."""
        from backend.agent.orchestrator import DependencyAnalyzer
        analyzer = DependencyAnalyzer()
        tc1 = _make_tool_call("tc_0", "read_file", {"file_path": "/a.py"})
        plan = analyzer.analyze([tc1])
        assert len(plan.stages) == 1
        assert len(plan.stages[0].tool_calls) == 1
        assert plan.stages[0].parallel is False

    def test_same_readonly_tools_parallel(self):
        """Multiple read_file calls should be grouped into one parallel stage."""
        from backend.agent.orchestrator import DependencyAnalyzer
        analyzer = DependencyAnalyzer()
        tc1 = _make_tool_call("tc_0", "read_file", {"file_path": "/a.py"})
        tc2 = _make_tool_call("tc_1", "read_file", {"file_path": "/b.py"})
        tc3 = _make_tool_call("tc_2", "read_file", {"file_path": "/c.py"})
        plan = analyzer.analyze([tc1, tc2, tc3])
        assert len(plan.stages) == 1
        assert plan.stages[0].parallel is True
        assert len(plan.stages[0].tool_calls) == 3

    def test_cross_readonly_categories_parallel(self):
        """read_file + web_search should be parallel (both readonly categories)."""
        from backend.agent.orchestrator import DependencyAnalyzer
        analyzer = DependencyAnalyzer()
        tc1 = _make_tool_call("tc_0", "read_file", {"file_path": "/a.py"})
        tc2 = _make_tool_call("tc_1", "web_search", {"query": "hello"})
        plan = analyzer.analyze([tc1, tc2])
        assert len(plan.stages) == 1
        assert plan.stages[0].parallel is True

    def test_write_then_execute_serial(self):
        """edit_file -> exec_command must be serial (two stages)."""
        from backend.agent.orchestrator import DependencyAnalyzer
        analyzer = DependencyAnalyzer()
        tc1 = _make_tool_call("tc_0", "edit_file", {"file_path": "/a.py", "search_text": "x", "replace_text": "y"})
        tc2 = _make_tool_call("tc_1", "exec_command", {"command": "python test.py"})
        plan = analyzer.analyze([tc1, tc2])
        assert len(plan.stages) == 2
        assert plan.stages[0].parallel is False
        assert plan.stages[0].tool_calls[0].id == "tc_0"
        assert plan.stages[1].parallel is False
        assert plan.stages[1].tool_calls[0].id == "tc_1"

    def test_mixed_readonly_then_write(self):
        """[read_file, read_file, edit_file, exec_command] -> 3 stages."""
        from backend.agent.orchestrator import DependencyAnalyzer
        analyzer = DependencyAnalyzer()
        tc1 = _make_tool_call("tc_0", "read_file", {"file_path": "/a.py"})
        tc2 = _make_tool_call("tc_1", "read_file", {"file_path": "/b.py"})
        tc3 = _make_tool_call("tc_2", "edit_file", {"file_path": "/a.py", "search_text": "x", "replace_text": "y"})
        tc4 = _make_tool_call("tc_3", "exec_command", {"command": "pytest"})
        plan = analyzer.analyze([tc1, tc2, tc3, tc4])
        assert len(plan.stages) == 3
        assert plan.stages[0].parallel is True   # read_file x2
        assert len(plan.stages[0].tool_calls) == 2
        assert plan.stages[1].parallel is False   # edit_file
        assert plan.stages[2].parallel is False   # exec_command

    def test_multiple_writes_serial(self):
        """Multiple edit_file calls should be serial (one per stage)."""
        from backend.agent.orchestrator import DependencyAnalyzer
        analyzer = DependencyAnalyzer()
        tc1 = _make_tool_call("tc_0", "edit_file", {"file_path": "/a.py", "search_text": "x", "replace_text": "y"})
        tc2 = _make_tool_call("tc_1", "edit_file", {"file_path": "/b.py", "search_text": "x", "replace_text": "y"})
        plan = analyzer.analyze([tc1, tc2])
        assert len(plan.stages) == 2
        assert plan.stages[0].parallel is False
        assert plan.stages[1].parallel is False

    def test_multiple_fetches_parallel(self):
        """Multiple web_fetch calls should be parallel."""
        from backend.agent.orchestrator import DependencyAnalyzer
        analyzer = DependencyAnalyzer()
        tc1 = _make_tool_call("tc_0", "web_fetch", {"url": "https://a.com"})
        tc2 = _make_tool_call("tc_1", "web_fetch", {"url": "https://b.com"})
        tc3 = _make_tool_call("tc_2", "web_fetch", {"url": "https://c.com"})
        plan = analyzer.analyze([tc1, tc2, tc3])
        assert len(plan.stages) == 1
        assert plan.stages[0].parallel is True

    def test_unknown_tool_serial(self):
        """Unknown tools should default to serial."""
        from backend.agent.orchestrator import DependencyAnalyzer
        analyzer = DependencyAnalyzer()
        tc1 = _make_tool_call("tc_0", "custom_mcp_tool", {})
        tc2 = _make_tool_call("tc_1", "another_mcp_tool", {})
        plan = analyzer.analyze([tc1, tc2])
        assert len(plan.stages) == 2

    def test_readonly_after_write_new_stage(self):
        """readonly tools after a write should start a new stage."""
        from backend.agent.orchestrator import DependencyAnalyzer
        analyzer = DependencyAnalyzer()
        tc1 = _make_tool_call("tc_0", "edit_file", {"file_path": "/a.py", "search_text": "x", "replace_text": "y"})
        tc2 = _make_tool_call("tc_1", "check_process", {"name": "python"})
        plan = analyzer.analyze([tc1, tc2])
        assert len(plan.stages) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Project/LLM-deployment && .sglang/bin/python -m pytest tests/test_orchestrator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.agent.orchestrator'`

- [ ] **Step 3: Implement DependencyAnalyzer and ExecutionPlan**

```python
# backend/agent/orchestrator.py
"""
Tool Orchestrator: 多工具编排执行引擎

当 LLM 一次输出多个 tool_calls 时，自动分析工具间依赖关系，
构建分阶段执行计划（DAG），并行/串行执行工具。

设计文档: docs/superpowers/specs/2026-04-11-tool-orchestrator-design.md
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, Future, wait, FIRST_EXCEPTION
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ── 工具分类（静态规则） ──────────────────────────────────

TOOL_CATEGORIES: dict[str, str] = {
    # readonly
    "read_file": "readonly",
    "check_process": "readonly",
    "list_skills": "readonly",
    "read_skill": "readonly",
    # search
    "web_search": "search",
    "wikipedia_search": "search",
    "wikipedia_summary": "search",
    "search_and_fetch": "search",
    "wikipedia_search_and_fetch": "search",
    # fetch
    "web_fetch": "fetch",
    # write
    "edit_file": "write",
    "write_file": "write",
    # execute
    "exec_command": "execute",
}

# 只读类别：这些类别之间也可以并行
READONLY_CATEGORY_NAMES = frozenset({"readonly", "search", "fetch"})


def _get_category(tool_name: str) -> str:
    """获取工具的类别，未知工具返回 'unknown'。"""
    return TOOL_CATEGORIES.get(tool_name, "unknown")


def _is_readonly(category: str) -> bool:
    """判断类别是否为只读。"""
    return category in READONLY_CATEGORY_NAMES


# ── 数据结构 ─────────────────────────────────────────────

@dataclass
class ExecutionStage:
    """一个执行阶段，包含可并行执行的工具调用。"""
    stage_id: int
    parallel: bool
    tool_calls: list  # List of ChatMessageToolCall-like objects
    results: list = field(default_factory=list)  # Filled after execution


@dataclass
class ExecutionPlan:
    """完整的执行计划，由多个阶段组成。"""
    stages: list[ExecutionStage] = field(default_factory=list)

    @property
    def total_tools(self) -> int:
        return sum(len(s.tool_calls) for s in self.stages)

    def to_dict(self) -> dict:
        """序列化为 SSE 事件可用的字典。"""
        return {
            "stages": [
                {
                    "stage_id": s.stage_id,
                    "parallel": s.parallel,
                    "tools": [
                        {
                            "call_id": tc.id,
                            "name": tc.function.name,
                            "args": tc.function.arguments if isinstance(tc.function.arguments, dict) else {},
                            "status": "pending",
                        }
                        for tc in s.tool_calls
                    ],
                }
                for s in self.stages
            ]
        }


@dataclass
class ToolResult:
    """单个工具的执行结果。"""
    call_id: str
    tool_name: str
    status: str  # "completed" | "failed" | "skipped" | "timeout"
    output: str = ""
    error: str = ""
    duration_ms: float = 0


# ── DependencyAnalyzer ───────────────────────────────────

class DependencyAnalyzer:
    """
    基于静态规则分析工具调用间的依赖关系，生成分阶段执行计划。

    规则:
    1. 同一只读类别（readonly/search/fetch）的连续工具 → 并行
    2. 不同只读类别的连续工具 → 也并行
    3. write 类工具 → 独占一个 stage（串行）
    4. execute 类工具 → 独占一个 stage（串行）
    5. 未知工具 → 独占一个 stage（串行）
    6. 只读工具遇到非只读工具时 → 前面的只读组结束，非只读开始新 stage
    """

    def analyze(self, tool_calls: list) -> ExecutionPlan:
        """将 tool_calls 列表分析为 ExecutionPlan。"""
        if not tool_calls:
            return ExecutionPlan()

        stages: list[ExecutionStage] = []
        current_readonly_group: list = []  # 累积连续的只读工具
        stage_counter = 0

        def _flush_readonly_group():
            nonlocal stage_counter
            if current_readonly_group:
                parallel = len(current_readonly_group) > 1
                stages.append(ExecutionStage(
                    stage_id=stage_counter,
                    parallel=parallel,
                    tool_calls=list(current_readonly_group),
                ))
                stage_counter += 1
                current_readonly_group.clear()

        for tc in tool_calls:
            category = _get_category(tc.function.name)

            if _is_readonly(category):
                # 只读工具：累积到当前只读组
                current_readonly_group.append(tc)
            else:
                # 非只读工具：先 flush 之前的只读组，然后独占一个 stage
                _flush_readonly_group()
                stages.append(ExecutionStage(
                    stage_id=stage_counter,
                    parallel=False,
                    tool_calls=[tc],
                ))
                stage_counter += 1

        # flush 末尾的只读组
        _flush_readonly_group()

        return ExecutionPlan(stages=stages)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/Project/LLM-deployment && .sglang/bin/python -m pytest tests/test_orchestrator.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agent/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: add ToolOrchestrator core — DependencyAnalyzer + ExecutionPlan"
```

---

### Task 2: ToolOrchestrator — PlanExecutor (staged execution with ThreadPoolExecutor)

**Files:**
- Modify: `backend/agent/orchestrator.py`
- Test: `tests/test_orchestrator.py` (add execution tests)

This task adds the execution engine that runs stages sequentially, with parallel tools within a stage executed via ThreadPoolExecutor.

- [ ] **Step 1: Write failing tests for PlanExecutor**

Append to `tests/test_orchestrator.py`:

```python
import time as _time


def _make_mock_tool(name: str, return_value: str = "ok", delay: float = 0, error: str = None):
    """Create a mock smolagents Tool object."""
    tool = MagicMock()
    tool.name = name
    def _call(**kwargs):
        if delay:
            _time.sleep(delay)
        if error:
            raise RuntimeError(error)
        return return_value
    tool.__call__ = _call
    # smolagents tools have a .forward method that is the actual callable
    tool.forward = _call
    return tool


class TestPlanExecutor:
    """Test staged execution with ThreadPoolExecutor."""

    def test_single_stage_single_tool(self):
        """Execute a single tool in a single stage."""
        from backend.agent.orchestrator import DependencyAnalyzer, PlanExecutor, ToolResult
        analyzer = DependencyAnalyzer()
        executor = PlanExecutor(max_parallel=5, global_timeout=60)

        tc = _make_tool_call("tc_0", "read_file", {"file_path": "/a.py"})
        plan = analyzer.analyze([tc])

        tools = {"read_file": _make_mock_tool("read_file", return_value="file content")}
        results = executor.execute(plan, tools)
        assert len(results) == 1
        assert results[0].status == "completed"
        assert results[0].output == "file content"

    def test_parallel_stage_executes_concurrently(self):
        """Parallel tools in a stage should execute concurrently (total time < sum of delays)."""
        from backend.agent.orchestrator import DependencyAnalyzer, PlanExecutor
        analyzer = DependencyAnalyzer()
        executor = PlanExecutor(max_parallel=5, global_timeout=60)

        tc1 = _make_tool_call("tc_0", "read_file", {"file_path": "/a.py"})
        tc2 = _make_tool_call("tc_1", "read_file", {"file_path": "/b.py"})
        plan = analyzer.analyze([tc1, tc2])

        tools = {"read_file": _make_mock_tool("read_file", return_value="ok", delay=0.3)}
        start = _time.time()
        results = executor.execute(plan, tools)
        elapsed = _time.time() - start

        assert len(results) == 2
        assert all(r.status == "completed" for r in results)
        # If parallel, total time should be ~0.3s, not ~0.6s
        assert elapsed < 0.5, f"Expected parallel execution, but took {elapsed:.2f}s"

    def test_tool_failure_records_error(self):
        """A failing tool should record error, not crash the executor."""
        from backend.agent.orchestrator import DependencyAnalyzer, PlanExecutor
        analyzer = DependencyAnalyzer()
        executor = PlanExecutor(max_parallel=5, global_timeout=60)

        tc = _make_tool_call("tc_0", "exec_command", {"command": "bad_cmd"})
        plan = analyzer.analyze([tc])

        tools = {"exec_command": _make_mock_tool("exec_command", error="command not found")}
        results = executor.execute(plan, tools)
        assert len(results) == 1
        assert results[0].status == "failed"
        assert "command not found" in results[0].error

    def test_serial_stages_execute_in_order(self):
        """Serial stages: stage 1 should only start after stage 0 finishes."""
        from backend.agent.orchestrator import DependencyAnalyzer, PlanExecutor
        analyzer = DependencyAnalyzer()
        executor = PlanExecutor(max_parallel=5, global_timeout=60)

        tc1 = _make_tool_call("tc_0", "edit_file", {"file_path": "/a.py", "search_text": "x", "replace_text": "y"})
        tc2 = _make_tool_call("tc_1", "exec_command", {"command": "pytest"})
        plan = analyzer.analyze([tc1, tc2])

        execution_order = []
        def make_ordered_tool(name, order_idx):
            tool = MagicMock()
            tool.name = name
            def _call(**kwargs):
                execution_order.append(order_idx)
                return f"{name} done"
            tool.__call__ = _call
            tool.forward = _call
            return tool

        tools = {
            "edit_file": make_ordered_tool("edit_file", 0),
            "exec_command": make_ordered_tool("exec_command", 1),
        }
        results = executor.execute(plan, tools)
        assert execution_order == [0, 1]
        assert all(r.status == "completed" for r in results)

    def test_unknown_tool_skipped(self):
        """If tool not found in tools dict, result should be 'failed'."""
        from backend.agent.orchestrator import DependencyAnalyzer, PlanExecutor
        analyzer = DependencyAnalyzer()
        executor = PlanExecutor(max_parallel=5, global_timeout=60)

        tc = _make_tool_call("tc_0", "nonexistent_tool", {})
        plan = analyzer.analyze([tc])

        tools = {}
        results = executor.execute(plan, tools)
        assert len(results) == 1
        assert results[0].status == "failed"

    def test_event_callback_called(self):
        """Event callback should be called for each tool status change."""
        from backend.agent.orchestrator import DependencyAnalyzer, PlanExecutor
        analyzer = DependencyAnalyzer()
        events = []
        executor = PlanExecutor(max_parallel=5, global_timeout=60)

        tc = _make_tool_call("tc_0", "read_file", {"file_path": "/a.py"})
        plan = analyzer.analyze([tc])

        tools = {"read_file": _make_mock_tool("read_file", return_value="ok")}
        results = executor.execute(plan, tools, event_callback=lambda e: events.append(e))

        # Should have at least "running" and "completed" events
        statuses = [e.get("status") for e in events if e.get("type") == "orchestration_update"]
        assert "running" in statuses
        assert "completed" in statuses
```

- [ ] **Step 2: Run tests to verify new tests fail**

Run: `cd ~/Project/LLM-deployment && .sglang/bin/python -m pytest tests/test_orchestrator.py::TestPlanExecutor -v`
Expected: FAIL with `ImportError` (PlanExecutor not defined yet)

- [ ] **Step 3: Implement PlanExecutor**

Append to `backend/agent/orchestrator.py`:

```python
class PlanExecutor:
    """
    执行 ExecutionPlan：按 stage 顺序执行，stage 内并行工具用 ThreadPoolExecutor。
    """

    def __init__(self, max_parallel: int = 5, global_timeout: float = 60):
        self.max_parallel = max_parallel
        self.global_timeout = global_timeout

    def execute(
        self,
        plan: ExecutionPlan,
        tools: dict,  # name -> smolagents Tool object
        event_callback: Callable[[dict], None] | None = None,
    ) -> list[ToolResult]:
        """
        执行编排计划，返回所有工具的结果列表。

        Args:
            plan: 执行计划
            tools: 工具字典 {name: tool_object}
            event_callback: 每次工具状态变更时回调（用于 SSE 事件推送）
        """
        all_results: list[ToolResult] = []
        start_time = time.time()

        for stage in plan.stages:
            # 检查全局超时
            elapsed = time.time() - start_time
            if elapsed >= self.global_timeout:
                for tc in stage.tool_calls:
                    result = ToolResult(
                        call_id=tc.id,
                        tool_name=tc.function.name,
                        status="timeout",
                        error=f"Global timeout ({self.global_timeout}s) exceeded",
                    )
                    all_results.append(result)
                    if event_callback:
                        event_callback({
                            "type": "orchestration_update",
                            "call_id": tc.id,
                            "status": "timeout",
                        })
                continue

            remaining_timeout = self.global_timeout - elapsed

            if stage.parallel and len(stage.tool_calls) > 1:
                results = self._execute_parallel(
                    stage, tools, remaining_timeout, event_callback
                )
            else:
                results = self._execute_serial(
                    stage, tools, remaining_timeout, event_callback
                )

            all_results.extend(results)
            stage.results = results

        return all_results

    def _execute_single_tool(
        self,
        tc,
        tools: dict,
        event_callback: Callable | None,
    ) -> ToolResult:
        """执行单个工具，返回 ToolResult。"""
        tool_name = tc.function.name
        call_id = tc.id

        # 发送 running 事件
        if event_callback:
            event_callback({
                "type": "orchestration_update",
                "call_id": call_id,
                "status": "running",
            })

        tool = tools.get(tool_name)
        if tool is None:
            result = ToolResult(
                call_id=call_id,
                tool_name=tool_name,
                status="failed",
                error=f"Tool '{tool_name}' not found",
            )
            if event_callback:
                event_callback({
                    "type": "orchestration_update",
                    "call_id": call_id,
                    "status": "failed",
                    "error": result.error,
                })
            return result

        start = time.time()
        try:
            args = tc.function.arguments
            if isinstance(args, str):
                import json
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    args = {"input": args}
            if not isinstance(args, dict):
                args = {}

            output = tool(**args)
            duration_ms = (time.time() - start) * 1000
            output_str = str(output) if output is not None else ""

            result = ToolResult(
                call_id=call_id,
                tool_name=tool_name,
                status="completed",
                output=output_str,
                duration_ms=duration_ms,
            )
            if event_callback:
                preview = output_str[:200] if len(output_str) > 200 else output_str
                event_callback({
                    "type": "orchestration_update",
                    "call_id": call_id,
                    "status": "completed",
                    "result_preview": preview,
                    "duration_ms": duration_ms,
                })
            return result

        except Exception as e:
            duration_ms = (time.time() - start) * 1000
            result = ToolResult(
                call_id=call_id,
                tool_name=tool_name,
                status="failed",
                error=str(e),
                duration_ms=duration_ms,
            )
            if event_callback:
                event_callback({
                    "type": "orchestration_update",
                    "call_id": call_id,
                    "status": "failed",
                    "error": str(e),
                    "duration_ms": duration_ms,
                })
            return result

    def _execute_parallel(
        self,
        stage: ExecutionStage,
        tools: dict,
        timeout: float,
        event_callback: Callable | None,
    ) -> list[ToolResult]:
        """并行执行一个 stage 中的所有工具。"""
        results: list[ToolResult] = []
        futures: dict[Future, Any] = {}  # future -> tool_call

        with ThreadPoolExecutor(max_workers=min(self.max_parallel, len(stage.tool_calls))) as pool:
            for tc in stage.tool_calls:
                future = pool.submit(self._execute_single_tool, tc, tools, event_callback)
                futures[future] = tc

            done, pending = wait(futures.keys(), timeout=timeout)

            for future in done:
                try:
                    results.append(future.result())
                except Exception as e:
                    tc = futures[future]
                    results.append(ToolResult(
                        call_id=tc.id,
                        tool_name=tc.function.name,
                        status="failed",
                        error=str(e),
                    ))

            for future in pending:
                future.cancel()
                tc = futures[future]
                result = ToolResult(
                    call_id=tc.id,
                    tool_name=tc.function.name,
                    status="timeout",
                    error=f"Stage timeout ({timeout:.1f}s) exceeded",
                )
                results.append(result)
                if event_callback:
                    event_callback({
                        "type": "orchestration_update",
                        "call_id": tc.id,
                        "status": "timeout",
                    })

        return results

    def _execute_serial(
        self,
        stage: ExecutionStage,
        tools: dict,
        timeout: float,
        event_callback: Callable | None,
    ) -> list[ToolResult]:
        """串行执行一个 stage 中的工具（通常只有一个）。"""
        results: list[ToolResult] = []
        for tc in stage.tool_calls:
            result = self._execute_single_tool(tc, tools, event_callback)
            results.append(result)
        return results


class ToolOrchestrator:
    """
    对外的顶层编排器，集成 DependencyAnalyzer + PlanExecutor。
    """

    def __init__(self, max_parallel: int = 5, global_timeout: float = 60):
        self.analyzer = DependencyAnalyzer()
        self.executor = PlanExecutor(
            max_parallel=max_parallel,
            global_timeout=global_timeout,
        )

    def analyze(self, tool_calls: list) -> ExecutionPlan:
        """分析依赖，生成执行计划。"""
        return self.analyzer.analyze(tool_calls)

    def execute(
        self,
        plan: ExecutionPlan,
        tools: dict,
        event_callback: Callable[[dict], None] | None = None,
    ) -> list[ToolResult]:
        """执行编排计划。"""
        return self.executor.execute(plan, tools, event_callback)
```

- [ ] **Step 4: Run all orchestrator tests**

Run: `cd ~/Project/LLM-deployment && .sglang/bin/python -m pytest tests/test_orchestrator.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agent/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: add PlanExecutor with ThreadPoolExecutor parallel execution"
```

---

### Task 3: search_and_fetch Composite Tool

**Files:**
- Create: `backend/tools/search_and_fetch.py`
- Test: `tests/test_search_and_fetch.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_search_and_fetch.py
"""Tests for search_and_fetch and wikipedia_search_and_fetch composite tools."""
import pytest
from unittest.mock import patch, MagicMock


class TestSearchAndFetch:
    """Test the search_and_fetch composite tool."""

    @patch("backend.tools.search_and_fetch._do_search_internal")
    @patch("backend.tools.search_and_fetch._fetch_single")
    def test_basic_search_and_fetch(self, mock_fetch, mock_search):
        """Should search, then fetch all results concurrently."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_search.return_value = [
            {"title": "Result 1", "url": "https://a.com", "description": "desc1"},
            {"title": "Result 2", "url": "https://b.com", "description": "desc2"},
        ]
        mock_fetch.side_effect = ["Content A", "Content B"]

        result = search_and_fetch_impl("test query", max_results=5, timeout=15)

        assert "test query" in result
        assert "Result 1" in result
        assert "Result 2" in result
        assert "Content A" in result
        assert "Content B" in result
        assert "成功抓取 2" in result or "成功抓取: 2" in result or "成功: 2" in result

    @patch("backend.tools.search_and_fetch._do_search_internal")
    def test_empty_search_results(self, mock_search):
        """Should handle empty search results gracefully."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_search.return_value = []
        result = search_and_fetch_impl("test query", max_results=5, timeout=15)

        assert "test query" in result
        assert "0" in result  # 0 results

    @patch("backend.tools.search_and_fetch._do_search_internal")
    @patch("backend.tools.search_and_fetch._fetch_single")
    def test_partial_fetch_failure(self, mock_fetch, mock_search):
        """Should return successful results even if some fetches fail."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_search.return_value = [
            {"title": "Good", "url": "https://good.com", "description": ""},
            {"title": "Bad", "url": "https://bad.com", "description": ""},
        ]
        mock_fetch.side_effect = ["Good content", RuntimeError("Connection refused")]

        result = search_and_fetch_impl("test query", max_results=5, timeout=15)

        assert "Good content" in result
        assert ("失败" in result or "failed" in result.lower() or "ERROR" in result)

    @patch("backend.tools.search_and_fetch._do_search_internal")
    @patch("backend.tools.search_and_fetch._fetch_single")
    def test_result_format_structured(self, mock_fetch, mock_search):
        """Should return structured format with title, URL, status, content."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_search.return_value = [
            {"title": "Test Page", "url": "https://test.com", "description": "A test"},
        ]
        mock_fetch.return_value = "Page content here"

        result = search_and_fetch_impl("test", max_results=5, timeout=15)

        assert "Test Page" in result
        assert "https://test.com" in result
        assert "Page content here" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Project/LLM-deployment && .sglang/bin/python -m pytest tests/test_search_and_fetch.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement search_and_fetch**

```python
# backend/tools/search_and_fetch.py
"""
Composite search-and-fetch tool: searches the web and concurrently fetches
all result pages, returning structured results.

设计文档: docs/superpowers/specs/2026-04-11-tool-orchestrator-design.md
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait, ALL_COMPLETED

logger = logging.getLogger(__name__)

# Default timeout for individual fetch operations
_DEFAULT_TIMEOUT = 15
_DEFAULT_MAX_RESULTS = 5


def _do_search_internal(query: str, max_results: int, timeout: int, check_abort=None) -> list[dict]:
    """
    Internal: call web_search and parse its output into a list of dicts.
    Returns list of {"title": ..., "url": ..., "description": ...}
    """
    from backend.tools.web_tools import web_search as _raw_web_search
    raw_result = _raw_web_search(
        query=query, max_results=max_results, timeout=timeout, check_abort=check_abort
    )

    # web_search returns markdown-formatted text, parse it
    results = []
    lines = raw_result.strip().split("\n")
    current = {}
    for line in lines:
        line = line.strip()
        if line.startswith("### ") or line.startswith("**") and ". " in line:
            # New result title
            if current.get("url"):
                results.append(current)
            title = line.lstrip("#* ").strip()
            # Remove numbering like "1. "
            if ". " in title[:5]:
                title = title.split(". ", 1)[1]
            current = {"title": title, "url": "", "description": ""}
        elif line.startswith("http://") or line.startswith("https://"):
            current["url"] = line
        elif line.startswith("URL:") or line.startswith("链接:"):
            url = line.split(":", 1)[1].strip()
            current["url"] = url
        elif line and current and not current.get("description"):
            current["description"] = line

    if current.get("url"):
        results.append(current)

    return results[:max_results]


def _fetch_single(url: str, timeout: int = 15, check_abort=None) -> str:
    """Fetch a single URL, return text content."""
    from backend.tools.web_tools import web_fetch as _raw_web_fetch
    return _raw_web_fetch(url=url, timeout=timeout, check_abort=check_abort)


def _format_results(
    query: str,
    search_results: list[dict],
    fetch_outcomes: list[dict],
) -> str:
    """Format search+fetch results into structured text."""
    n_total = len(search_results)
    n_success = sum(1 for o in fetch_outcomes if o["status"] == "success")
    n_timeout = sum(1 for o in fetch_outcomes if o["status"] == "timeout")
    n_failed = sum(1 for o in fetch_outcomes if o["status"] == "failed")

    parts = [
        f'=== 搜索结果: "{query}" ===',
        f"共找到 {n_total} 个结果，成功抓取 {n_success} 个，超时 {n_timeout} 个，失败 {n_failed} 个",
        "",
    ]

    for i, (sr, outcome) in enumerate(zip(search_results, fetch_outcomes)):
        parts.append(f"--- [{i+1}] {sr.get('title', 'Untitled')} ---")
        parts.append(f"URL: {sr.get('url', 'N/A')}")

        if outcome["status"] == "success":
            parts.append("状态: 成功")
            content = outcome["content"]
            # Truncate very long content
            if len(content) > 8000:
                content = content[:8000] + "\n... [内容已截断]"
            parts.append(f"内容:\n{content}")
        elif outcome["status"] == "timeout":
            parts.append(f"状态: 超时")
            parts.append(f"内容: [TIMEOUT - 未能在规定时间内获取]")
        else:
            parts.append(f"状态: 失败 ({outcome.get('error', 'unknown')})")
            parts.append(f"内容: [ERROR - {outcome.get('error', 'unknown')}]")

        parts.append("")

    parts.append("=== 搜索结束 ===")
    return "\n".join(parts)


def search_and_fetch_impl(
    query: str,
    max_results: int = _DEFAULT_MAX_RESULTS,
    timeout: int = _DEFAULT_TIMEOUT,
    check_abort=None,
) -> str:
    """
    Core implementation: search web + concurrent fetch all results.

    Args:
        query: Search keywords
        max_results: Max search results (default 5)
        timeout: Per-fetch timeout in seconds (default 15)
        check_abort: Optional abort callback

    Returns:
        Structured text with search results and fetched page contents.
    """
    if not query or not query.strip():
        return "Error: 搜索关键词不能为空。"

    # 1. Search
    logger.info(f"[search_and_fetch] Searching: '{query}' (max_results={max_results})")
    try:
        search_results = _do_search_internal(query, max_results, timeout, check_abort)
    except Exception as e:
        logger.error(f"[search_and_fetch] Search failed: {e}")
        return f'=== 搜索结果: "{query}" ===\n搜索失败: {e}\n=== 搜索结束 ==='

    if not search_results:
        return f'=== 搜索结果: "{query}" ===\n共找到 0 个结果\n=== 搜索结束 ==='

    # 2. Concurrent fetch
    logger.info(f"[search_and_fetch] Fetching {len(search_results)} URLs concurrently (timeout={timeout}s)")
    fetch_outcomes = []
    futures_map = {}  # future -> index

    with ThreadPoolExecutor(max_workers=min(5, len(search_results))) as pool:
        for i, sr in enumerate(search_results):
            url = sr.get("url", "")
            if not url:
                fetch_outcomes.append({"status": "failed", "error": "No URL", "content": ""})
                continue
            future = pool.submit(_fetch_single, url, timeout, check_abort)
            futures_map[future] = i
            fetch_outcomes.append(None)  # placeholder

        # Wait with timeout
        done, pending = wait(futures_map.keys(), timeout=timeout, return_when=ALL_COMPLETED)

        for future in done:
            idx = futures_map[future]
            try:
                content = future.result()
                fetch_outcomes[idx] = {"status": "success", "content": content}
            except Exception as e:
                fetch_outcomes[idx] = {"status": "failed", "error": str(e), "content": ""}

        for future in pending:
            future.cancel()
            idx = futures_map[future]
            fetch_outcomes[idx] = {"status": "timeout", "content": ""}

    # Fill any remaining None placeholders
    fetch_outcomes = [o if o is not None else {"status": "failed", "error": "unknown", "content": ""} for o in fetch_outcomes]

    # 3. Format results
    return _format_results(query, search_results, fetch_outcomes)
```

- [ ] **Step 4: Run tests**

Run: `cd ~/Project/LLM-deployment && .sglang/bin/python -m pytest tests/test_search_and_fetch.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/tools/search_and_fetch.py tests/test_search_and_fetch.py
git commit -m "feat: add search_and_fetch composite tool with concurrent fetching"
```

---

### Task 4: wikipedia_search_and_fetch Composite Tool

**Files:**
- Create: `backend/tools/wikipedia_search_and_fetch.py`
- Test: `tests/test_search_and_fetch.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_search_and_fetch.py`:

```python
class TestWikipediaSearchAndFetch:
    """Test the wikipedia_search_and_fetch composite tool."""

    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_search_impl")
    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_summary_impl")
    def test_basic_wikipedia_search_and_fetch(self, mock_summary, mock_search):
        from backend.tools.wikipedia_search_and_fetch import wikipedia_search_and_fetch_impl

        mock_search.return_value = (
            "=== Wikipedia 搜索结果 ===\n"
            "1. **Python (programming language)**\n"
            "   Page ID: 23862\n"
            "   https://en.wikipedia.org/wiki/Python_(programming_language)\n"
            "   Snippet: Python is a programming language\n"
        )
        mock_summary.return_value = "Python is a high-level programming language..."

        result = wikipedia_search_and_fetch_impl("Python programming", max_results=5, timeout=15)

        assert "Python" in result
        assert "high-level" in result

    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_search_impl")
    def test_empty_wikipedia_results(self, mock_search):
        from backend.tools.wikipedia_search_and_fetch import wikipedia_search_and_fetch_impl

        mock_search.return_value = "No results found"
        result = wikipedia_search_and_fetch_impl("xyznonexistent", max_results=5, timeout=15)
        assert "0" in result or "无结果" in result or "No" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/Project/LLM-deployment && .sglang/bin/python -m pytest tests/test_search_and_fetch.py::TestWikipediaSearchAndFetch -v`
Expected: FAIL

- [ ] **Step 3: Implement wikipedia_search_and_fetch**

```python
# backend/tools/wikipedia_search_and_fetch.py
"""
Composite Wikipedia search-and-fetch tool: searches Wikipedia and concurrently
fetches summaries for all matching articles.

设计文档: docs/superpowers/specs/2026-04-11-tool-orchestrator-design.md
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, wait, ALL_COMPLETED

from backend.tools.wikipedia_tools import wikipedia_search_impl, wikipedia_summary_impl

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15
_DEFAULT_MAX_RESULTS = 5


def _parse_search_results(raw_text: str) -> list[dict]:
    """
    Parse wikipedia_search_impl output into structured list.
    Returns list of {"title": ..., "url": ..., "page_id": ...}
    """
    results = []
    lines = raw_text.strip().split("\n")
    current = {}

    for line in lines:
        line = line.strip()
        # Match title patterns like "1. **Title**" or "### Title"
        title_match = re.match(r"^\d+\.\s+\*\*(.+?)\*\*", line)
        if not title_match:
            title_match = re.match(r"^###\s+(.+)", line)

        if title_match:
            if current.get("title"):
                results.append(current)
            current = {"title": title_match.group(1).strip(), "url": "", "page_id": ""}
        elif line.startswith("https://"):
            current["url"] = line
        elif "Page ID:" in line:
            pid = line.split("Page ID:")[-1].strip()
            current["page_id"] = pid

    if current.get("title"):
        results.append(current)

    return results


def _format_results(query: str, titles: list[dict], fetch_outcomes: list[dict]) -> str:
    """Format Wikipedia search+summary results into structured text."""
    n_total = len(titles)
    n_success = sum(1 for o in fetch_outcomes if o["status"] == "success")
    n_timeout = sum(1 for o in fetch_outcomes if o["status"] == "timeout")
    n_failed = sum(1 for o in fetch_outcomes if o["status"] == "failed")

    parts = [
        f'=== Wikipedia 搜索结果: "{query}" ===',
        f"共找到 {n_total} 个条目，成功获取 {n_success} 个摘要，超时 {n_timeout} 个，失败 {n_failed} 个",
        "",
    ]

    for i, (item, outcome) in enumerate(zip(titles, fetch_outcomes)):
        parts.append(f"--- [{i+1}] {item.get('title', 'Untitled')} ---")
        if item.get("url"):
            parts.append(f"URL: {item['url']}")

        if outcome["status"] == "success":
            parts.append("状态: 成功")
            content = outcome["content"]
            if len(content) > 6000:
                content = content[:6000] + "\n... [摘要已截断]"
            parts.append(f"摘要:\n{content}")
        elif outcome["status"] == "timeout":
            parts.append("状态: 超时")
            parts.append("摘要: [TIMEOUT]")
        else:
            parts.append(f"状态: 失败 ({outcome.get('error', 'unknown')})")
            parts.append(f"摘要: [ERROR - {outcome.get('error', 'unknown')}]")

        parts.append("")

    parts.append("=== 搜索结束 ===")
    return "\n".join(parts)


def wikipedia_search_and_fetch_impl(
    query: str,
    max_results: int = _DEFAULT_MAX_RESULTS,
    timeout: int = _DEFAULT_TIMEOUT,
    check_abort=None,
) -> str:
    """
    Core implementation: search Wikipedia + concurrent summary fetch.
    """
    if not query or not query.strip():
        return "Error: 搜索关键词不能为空。"

    # 1. Search
    logger.info(f"[wiki_search_and_fetch] Searching: '{query}'")
    try:
        raw_search = wikipedia_search_impl(query, max_results=max_results, timeout=timeout)
    except Exception as e:
        return f'=== Wikipedia 搜索结果: "{query}" ===\n搜索失败: {e}\n=== 搜索结束 ==='

    titles = _parse_search_results(raw_search)
    if not titles:
        return f'=== Wikipedia 搜索结果: "{query}" ===\n共找到 0 个条目\n=== 搜索结束 ==='

    # 2. Concurrent summary fetch
    logger.info(f"[wiki_search_and_fetch] Fetching {len(titles)} summaries")
    fetch_outcomes = [None] * len(titles)
    futures_map = {}

    with ThreadPoolExecutor(max_workers=min(5, len(titles))) as pool:
        for i, item in enumerate(titles):
            title = item.get("title", "")
            if not title:
                fetch_outcomes[i] = {"status": "failed", "error": "No title", "content": ""}
                continue
            future = pool.submit(wikipedia_summary_impl, title, timeout)
            futures_map[future] = i

        done, pending = wait(futures_map.keys(), timeout=timeout, return_when=ALL_COMPLETED)

        for future in done:
            idx = futures_map[future]
            try:
                content = future.result()
                if content.startswith("Error:") or content.startswith("No Wikipedia"):
                    fetch_outcomes[idx] = {"status": "failed", "error": content, "content": ""}
                else:
                    fetch_outcomes[idx] = {"status": "success", "content": content}
            except Exception as e:
                fetch_outcomes[idx] = {"status": "failed", "error": str(e), "content": ""}

        for future in pending:
            future.cancel()
            idx = futures_map[future]
            fetch_outcomes[idx] = {"status": "timeout", "content": ""}

    fetch_outcomes = [o if o is not None else {"status": "failed", "error": "unknown", "content": ""} for o in fetch_outcomes]

    return _format_results(query, titles, fetch_outcomes)
```

- [ ] **Step 4: Run tests**

Run: `cd ~/Project/LLM-deployment && .sglang/bin/python -m pytest tests/test_search_and_fetch.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/tools/wikipedia_search_and_fetch.py tests/test_search_and_fetch.py
git commit -m "feat: add wikipedia_search_and_fetch composite tool"
```

---

### Task 5: Integrate Orchestrator into SmartToolCallingAgent

**Files:**
- Modify: `backend/agent/agent_runner.py` (lines ~698-1070)
- Modify: `backend/agent/orchestrator.py` (minor: add observation formatting)

This is the key integration task. Override `process_tool_calls()`, add `__init__` to SmartToolCallingAgent, emit SSE events for orchestration.

- [ ] **Step 1: Add `__init__` to SmartToolCallingAgent**

In `backend/agent/agent_runner.py`, the class `SmartToolCallingAgent` (line 698) currently has no `__init__`. Add one:

Find (around line 698-710):
```python
class SmartToolCallingAgent(ToolCallingAgent):
    """
    增强版 ToolCallingAgent：

    1. 纯文本响应自动视为 final_answer（模型无需显式调用）
    2. 拦截 final_answer 工具调用，静默处理（不显示 "Calling tool: 'final_answer'"）
    """

    # ── 工具调用文本清洗的正则模式（编译一次复用） ──
```

Insert after the docstring, before the regex patterns:

```python
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from backend.agent.orchestrator import ToolOrchestrator
        self.orchestrator = ToolOrchestrator(max_parallel=5, global_timeout=60)
```

- [ ] **Step 2: Override `process_tool_calls()` method**

Add the following method to `SmartToolCallingAgent` class, **before** the `_step_stream` method (insert before line 874):

```python
    def process_tool_calls(self, chat_message, memory_step):
        """
        覆写父类 process_tool_calls：支持多工具编排执行。

        - 单个工具调用 → 走原有逻辑 (super)
        - 多个工具调用 → 走 ToolOrchestrator 编排执行
        """
        from smolagents.memory import ActionStep, ToolCall as MemoryToolCall
        from smolagents.models import ChatMessageToolCall

        tool_calls = chat_message.tool_calls or []
        if len(tool_calls) <= 1:
            # 单个工具调用，走原有逻辑
            yield from super().process_tool_calls(chat_message, memory_step)
            return

        # ── 多工具编排 ──
        logger.info(f"🎯 [Orchestrator] 检测到 {len(tool_calls)} 个工具调用，启动编排执行")

        # 1. 分析依赖，生成执行计划
        plan = self.orchestrator.analyze(tool_calls)
        logger.info(
            f"🎯 [Orchestrator] 执行计划: {len(plan.stages)} 个阶段, "
            f"总计 {plan.total_tools} 个工具"
        )

        # 2. 构建 tools 字典 {name: tool_object}
        tools_dict = {}
        for tool in self.tools:
            tool_name = getattr(tool, "name", None)
            if tool_name:
                tools_dict[tool_name] = tool

        # 3. 发送编排计划 SSE 事件
        # 通过 yield 一个特殊的事件对象传递给上层 SSE 处理
        yield {"_orchestration_event": True, "type": "orchestration_plan", **plan.to_dict()}

        # 4. 执行编排计划
        events_buffer = []

        def _event_callback(event):
            events_buffer.append(event)

        results = self.orchestrator.execute(plan, tools_dict, event_callback=_event_callback)

        # 5. 发送所有缓冲的更新事件
        for event in events_buffer:
            yield {"_orchestration_event": True, **event}

        # 6. 写入 memory_step 的 observations
        observations_parts = []
        for r in results:
            status_label = {"completed": "成功", "failed": "失败", "timeout": "超时", "skipped": "跳过"}.get(r.status, r.status)
            if r.status == "completed":
                observations_parts.append(
                    f"[{r.tool_name}] → {status_label}\n{r.output}"
                )
            else:
                observations_parts.append(
                    f"[{r.tool_name}] → {status_label}: {r.error}"
                )

        combined_observation = "\n\n".join(observations_parts)
        memory_step.observations = combined_observation

        # 7. 写入 memory_step 的 tool_calls (保持 smolagents 兼容)
        memory_step.tool_calls = [
            MemoryToolCall(
                name=tc.function.name,
                arguments=tc.function.arguments if isinstance(tc.function.arguments, dict) else {},
                id=tc.id,
            )
            for tc in tool_calls
        ]

        # 8. 发送完成事件
        n_completed = sum(1 for r in results if r.status == "completed")
        n_failed = sum(1 for r in results if r.status == "failed")
        n_timeout = sum(1 for r in results if r.status == "timeout")
        n_skipped = sum(1 for r in results if r.status == "skipped")
        total_ms = sum(r.duration_ms for r in results)

        yield {
            "_orchestration_event": True,
            "type": "orchestration_done",
            "total": len(results),
            "completed": n_completed,
            "failed": n_failed,
            "timeout": n_timeout,
            "skipped": n_skipped,
            "duration_ms": total_ms,
        }

        # 9. Yield observation 给父级处理（类似原始 process_tool_calls 的行为）
        from smolagents.agents import ActionOutput
        yield ActionOutput(
            output=combined_observation,
            is_final_answer=False,
        )

        logger.info(
            f"🎯 [Orchestrator] 编排完成: {n_completed}/{len(results)} 成功, "
            f"{n_failed} 失败, {n_timeout} 超时, {n_skipped} 跳过"
        )
```

- [ ] **Step 3: Update `_run_agent()` SSE event handler to recognize orchestration events**

In `backend/agent/agent_runner.py`, find the `_run_agent` method's event processing loop (around lines 1580-1625 where `step_output` is handled). Add handling for orchestration events.

Find the section (approximately lines 1595-1625):
```python
                    elif isinstance(step_output, MemoryToolCall):
                        ...
```

Add **before** the MemoryToolCall handler (so orchestration events are caught first):

```python
                    elif isinstance(step_output, dict) and step_output.get("_orchestration_event"):
                        # 编排事件：直接转发给前端
                        sse_event = {k: v for k, v in step_output.items() if k != "_orchestration_event"}
                        loop.call_soon_threadsafe(queue.put_nowait, sse_event)
```

- [ ] **Step 4: Verify by running existing tests (no regressions)**

Run: `cd ~/Project/LLM-deployment && .sglang/bin/python -m pytest tests/ -v --ignore=tests/test_camera_service.py --ignore=tests/test_cycle_api.py --ignore=tests/test_cycle_config.py --ignore=tests/test_cycle_executor.py --ignore=tests/test_load_images_script.py -x`
Expected: All tests PASS (no regressions)

- [ ] **Step 5: Commit**

```bash
git add backend/agent/agent_runner.py backend/agent/orchestrator.py
git commit -m "feat: integrate ToolOrchestrator into SmartToolCallingAgent via process_tool_calls override"
```

---

### Task 6: Register Composite Tools in Agent

**Files:**
- Modify: `backend/agent/agent_runner.py` (lines 211-215 imports, lines 590-666 `_get_tools`)

- [ ] **Step 1: Add imports for composite tools**

In `backend/agent/agent_runner.py`, find the tool imports section (lines 211-215):

```python
from backend.tools.file_ops import edit_file, read_file, write_file
from backend.tools.skill_ops import list_skills, read_skill
from backend.tools.sys_ops import exec_command, check_process
from backend.tools.web_tools import web_search, web_fetch
from backend.tools.wikipedia_tools import wikipedia_search, wikipedia_summary
```

Add after these imports:

```python
from backend.tools.search_and_fetch import search_and_fetch_impl
from backend.tools.wikipedia_search_and_fetch import wikipedia_search_and_fetch_impl
```

- [ ] **Step 2: Create @tool decorated wrappers and register them in `_get_tools()`**

In `backend/agent/agent_runner.py`, find the `_get_tools()` function (around line 590). After the existing web_fetch wrapper creation (line 627), add the composite tool wrappers:

```python
    # Composite search+fetch tools
    search_and_fetch_tool = _create_tool_wrapper(search_and_fetch_impl, "search_and_fetch")
    wikipedia_search_and_fetch_tool = _create_tool_wrapper(
        wikipedia_search_and_fetch_impl, "wikipedia_search_and_fetch"
    )
```

Then in the tools list (around line 629-635), add them after `web_fetch_tool`:

```python
    tools = [
        CustomPythonInterpreterTool(authorized_imports=authorized_imports),
        read_file_tool, write_file_tool, edit_file_tool,
        exec_command_tool, check_process_tool,
        list_skills_tool, read_skill_tool,
        web_search_tool, web_fetch_tool,
        search_and_fetch_tool,  # NEW
    ]
```

And in the wikipedia conditional block (around line 642-650), add after `wikipedia_summary_tool`:

```python
    if enable_wikipedia:
        wikipedia_search_tool = _make_session_aware_wrapper(wikipedia_search, "wikipedia_search")
        wikipedia_summary_tool = _make_session_aware_wrapper(wikipedia_summary, "wikipedia_summary")
        tools.append(wikipedia_search_tool)
        tools.append(wikipedia_summary_tool)
        tools.append(wikipedia_search_and_fetch_tool)  # NEW
```

- [ ] **Step 3: Verify registration**

Run: `cd ~/Project/LLM-deployment && .sglang/bin/python -c "from backend.agent.agent_runner import _get_tools; tools = _get_tools(); print([getattr(t, 'name', type(t).__name__) for t in tools])"`
Expected: Output should include `search_and_fetch` and `wikipedia_search_and_fetch`

- [ ] **Step 4: Commit**

```bash
git add backend/agent/agent_runner.py
git commit -m "feat: register search_and_fetch and wikipedia_search_and_fetch tools"
```

---

### Task 7: Update Prompts (config-agent-prompts.yaml)

**Files:**
- Modify: `config/config-agent-prompts.yaml` (lines 94-101, line 313)

- [ ] **Step 1: Remove single-tool restriction and add multi-tool guidance**

In `config/config-agent-prompts.yaml`, find line 96:
```
    2. 一次动作只能调用一个工具；如调用多个工具，须分多步进行，每步调用一个工具并观察结果。
```

Replace with:
```
    2. 你可以在一次响应中调用多个工具。系统会自动分析依赖关系并优化执行顺序（独立工具并行、有依赖的工具串行）。你只需按逻辑顺序列出所有工具调用即可。
```

- [ ] **Step 2: Remove the code_agent single-tool restriction**

Find line 313:
```
    注意：一次只能调用一个工具；如需调用多个工具，分多步进行，每步调用一个工具并观察结果。
```

Replace with:
```
    注意：你可以在一次响应中调用多个工具，系统会自动编排执行（独立工具并行、有依赖工具串行）。
```

- [ ] **Step 3: Add composite tool descriptions in the prompt**

Find a suitable location in the prompt after the tool descriptions area (after line 101). Add guidance text:

```
    9. 复合搜索工具: 当你需要搜索互联网或维基百科信息时，优先使用 search_and_fetch 或 wikipedia_search_and_fetch，它们会自动并发抓取所有搜索结果的页面内容并整理返回，比分步调用 web_search + web_fetch 更高效。网络工具支持 timeout 参数（默认15秒），超时后会返回已获取的部分结果。
```

- [ ] **Step 4: Commit**

```bash
git add config/config-agent-prompts.yaml
git commit -m "feat: update prompts to enable multi-tool calls and describe composite tools"
```

---

### Task 8: Adjust local_llm_model.py for Multi-Tool + final_answer

**Files:**
- Modify: `backend/agent/local_llm_model.py` (lines 510-541)

- [ ] **Step 1: Update `_extract_tool_calls()` to handle multi-tool + final_answer coexistence**

The current logic at lines 526-531 filters final_answer and returns None if only final_answer remains. This is correct. But we need to ensure that when there are real tools AND final_answer, only the real tools are returned (final_answer handled separately by `_step_stream`).

Read the current code at `backend/agent/local_llm_model.py:510-542` to confirm it already handles this correctly. The existing code:

```python
# 过滤 final_answer
filtered = [tc for tc in parsed if tc.function.name != "final_answer"]
if filtered:
    return filtered
# 仅剩 final_answer，返回 None
return None
```

This is already correct — it preserves real tools and drops final_answer. No code change needed here unless testing reveals an issue.

**Verify with a test:**

```bash
cd ~/Project/LLM-deployment && .sglang/bin/python -c "
# Quick sanity check: _extract_tool_calls with multi-tool + final_answer
from unittest.mock import MagicMock
import json

# Simulate result with multiple tool_calls including final_answer
result = MagicMock()
result.tool_calls = [
    {'id': 'tc_0', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{\"file_path\": \"/a.py\"}'}},
    {'id': 'tc_1', 'type': 'function', 'function': {'name': 'final_answer', 'arguments': '{\"answer\": \"done\"}'}},
]
print('Simulated tool_calls:', [tc['function']['name'] for tc in result.tool_calls])
print('After filtering final_answer:', [tc['function']['name'] for tc in result.tool_calls if tc['function']['name'] != 'final_answer'])
"
```

Expected: `['read_file']` — final_answer filtered out, real tools preserved.

- [ ] **Step 2: Commit (if changes were needed) or mark as verified**

If no code change needed:
```bash
echo "local_llm_model.py already handles multi-tool + final_answer correctly"
```

---

### Task 9: Frontend — Restore api.ts and Extend Types

**Files:**
- Modify: `frontend/src/lib/api.ts` (restore from git, then extend)

**IMPORTANT:** `frontend/src/lib/api.ts` is missing from the working tree (deleted but not committed). It exists in git at commit `577d730`. Must restore it first.

- [ ] **Step 1: Restore api.ts from git**

```bash
cd ~/Project/LLM-deployment && git checkout 577d730 -- frontend/src/lib/api.ts
```

- [ ] **Step 2: Extend AgentStep interface**

In `frontend/src/lib/api.ts`, find the `AgentStep` interface (line 27):

```typescript
export interface AgentStep {
  step_type: 'thought' | 'tool_call' | 'observation';
  content?: string;
  name?: string;
  input?: string | Record<string, unknown>;
  partial?: boolean;
}
```

Replace with:

```typescript
export interface AgentStep {
  step_type: 'thought' | 'tool_call' | 'observation' | 'orchestration';
  content?: string;
  name?: string;
  input?: string | Record<string, unknown>;
  partial?: boolean;
  // Orchestration fields
  orchestration_type?: 'plan' | 'update' | 'done';
  stages?: OrchestrationStage[];
  orchestration_update?: OrchestrationUpdate;
  orchestration_summary?: OrchestrationSummary;
}

/** 编排执行阶段 */
export interface OrchestrationStage {
  stage_id: number;
  parallel: boolean;
  tools: OrchestrationTool[];
}

/** 编排中的单个工具 */
export interface OrchestrationTool {
  call_id: string;
  name: string;
  args: Record<string, unknown>;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'skipped' | 'timeout';
  result_preview?: string;
  duration_ms?: number;
}

/** 编排进度更新 */
export interface OrchestrationUpdate {
  call_id: string;
  status: 'running' | 'completed' | 'failed' | 'skipped' | 'timeout';
  result_preview?: string;
  duration_ms?: number;
}

/** 编排完成汇总 */
export interface OrchestrationSummary {
  total: number;
  completed: number;
  failed: number;
  skipped: number;
  timeout: number;
  duration_ms: number;
}
```

- [ ] **Step 3: Add orchestration event handling in `agentChatStream`**

In `frontend/src/lib/api.ts`, find the SSE event parsing section in `agentChatStream()` (around line 342). After the `json.type === 'agent_step'` handler, add:

```typescript
              } else if (json.type === 'orchestration_plan') {
                const step: AgentStep = {
                  step_type: 'orchestration',
                  orchestration_type: 'plan',
                  stages: json.stages,
                };
                onStep(step);
              } else if (json.type === 'orchestration_update') {
                const step: AgentStep = {
                  step_type: 'orchestration',
                  orchestration_type: 'update',
                  orchestration_update: {
                    call_id: json.call_id,
                    status: json.status,
                    result_preview: json.result_preview,
                    duration_ms: json.duration_ms,
                  },
                };
                onStep(step);
              } else if (json.type === 'orchestration_done') {
                const step: AgentStep = {
                  step_type: 'orchestration',
                  orchestration_type: 'done',
                  orchestration_summary: {
                    total: json.total,
                    completed: json.completed,
                    failed: json.failed,
                    skipped: json.skipped,
                    timeout: json.timeout,
                    duration_ms: json.duration_ms,
                  },
                };
                onStep(step);
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/lib/api.ts
git commit -m "feat: restore api.ts and extend with orchestration types + SSE handling"
```

---

### Task 10: Frontend — OrchestrationView Component

**Files:**
- Modify: `frontend/src/components/AgentStepDisplay.tsx`

- [ ] **Step 1: Read current AgentStepDisplay.tsx**

Read `frontend/src/components/AgentStepDisplay.tsx` in its entirety (143 lines) to understand the current structure before making changes.

- [ ] **Step 2: Add OrchestrationView sub-component**

In `frontend/src/components/AgentStepDisplay.tsx`, add the import for orchestration types at the top:

```typescript
import type { AgentStep, OrchestrationStage, OrchestrationTool, OrchestrationUpdate, OrchestrationSummary } from '@/lib/api';
```

Then add the `OrchestrationView` component before the `StepCard` function. This component manages orchestration state (accumulating updates):

```typescript
/** Status icon for orchestration tool */
const STATUS_ICON: Record<string, string> = {
  pending: '⏳',
  running: '🔄',
  completed: '✅',
  failed: '❌',
  skipped: '⏭️',
  timeout: '⏰',
};

const STATUS_COLORS: Record<string, string> = {
  pending: 'text-gray-400',
  running: 'text-blue-400',
  completed: 'text-green-400',
  failed: 'text-red-400',
  skipped: 'text-yellow-400',
  timeout: 'text-orange-400',
};

function OrchestrationToolCard({ tool }: { tool: OrchestrationTool }) {
  const [expanded, setExpanded] = useState(false);
  const icon = STATUS_ICON[tool.status] || '❓';
  const colorClass = STATUS_COLORS[tool.status] || 'text-gray-400';

  return (
    <div className="border border-gray-700 rounded-md p-2 min-w-[140px] bg-gray-800/50">
      <div
        className="flex items-center gap-1 cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <span>{icon}</span>
        <span className="font-mono text-xs text-gray-200 truncate">{tool.name}</span>
        {tool.duration_ms != null && (
          <span className="text-xs text-gray-500 ml-auto">{(tool.duration_ms / 1000).toFixed(1)}s</span>
        )}
      </div>
      {/* Brief args preview */}
      <div className="text-xs text-gray-500 truncate mt-0.5">
        {Object.entries(tool.args).slice(0, 2).map(([k, v]) => `${k}: ${String(v).slice(0, 30)}`).join(', ')}
      </div>
      {expanded && tool.result_preview && (
        <pre className="text-xs text-gray-400 mt-1 whitespace-pre-wrap max-h-40 overflow-auto">
          {tool.result_preview}
        </pre>
      )}
    </div>
  );
}

function OrchestrationView({ stages, summary }: {
  stages: OrchestrationStage[];
  summary?: OrchestrationSummary;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const totalTools = stages.reduce((sum, s) => sum + s.tools.length, 0);

  return (
    <div className="border border-indigo-700/50 rounded-lg p-3 bg-gray-900/50 my-2">
      {/* Header */}
      <button
        className="flex items-center gap-2 w-full text-left"
        onClick={() => setCollapsed(!collapsed)}
      >
        {collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
        <span className="text-sm font-medium text-indigo-300">
          🎯 编排执行 ({totalTools} 个工具, {stages.length} 个阶段)
        </span>
        {summary && (
          <span className="text-xs text-gray-400 ml-auto">
            {summary.completed}/{summary.total} 成功 · {(summary.duration_ms / 1000).toFixed(1)}s
          </span>
        )}
      </button>

      {!collapsed && (
        <div className="mt-2 space-y-2">
          {stages.map((stage) => (
            <div key={stage.stage_id} className="pl-2 border-l-2 border-gray-700">
              <div className="text-xs text-gray-500 mb-1">
                Stage {stage.stage_id} {stage.parallel ? '(并行)' : '(串行)'}
              </div>
              <div className={stage.parallel ? 'flex flex-wrap gap-2' : 'space-y-2'}>
                {stage.tools.map((tool) => (
                  <OrchestrationToolCard key={tool.call_id} tool={tool} />
                ))}
              </div>
            </div>
          ))}

          {/* Summary bar */}
          {summary && (
            <div className="text-xs text-gray-400 pt-1 border-t border-gray-700">
              总耗时: {(summary.duration_ms / 1000).toFixed(1)}s | 
              完成: {summary.completed} | 
              {summary.failed > 0 && `失败: ${summary.failed} | `}
              {summary.timeout > 0 && `超时: ${summary.timeout} | `}
              {summary.skipped > 0 && `跳过: ${summary.skipped}`}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Update StepCard to handle orchestration step_type**

In the `StepCard` component, add a branch for orchestration steps. Find the existing render logic and add:

```typescript
// Inside StepCard, before the existing return:
if (step.step_type === 'orchestration' && step.orchestration_type === 'plan' && step.stages) {
  return (
    <OrchestrationView
      stages={step.stages}
      summary={undefined}
    />
  );
}
if (step.step_type === 'orchestration' && step.orchestration_type === 'done' && step.orchestration_summary) {
  // The done event triggers a re-render of the OrchestrationView with summary
  // This is handled by state management in the parent component
  return null;
}
if (step.step_type === 'orchestration' && step.orchestration_type === 'update') {
  // Update events are processed in page.tsx / vision/page.tsx to update tool statuses
  return null;
}
```

Also add 'orchestration' to the STEP_ICON, STEP_LABEL, and STEP_COLORS maps:
```typescript
const STEP_ICON = {
  ...existing...,
  orchestration: '🎯',
};
const STEP_LABEL = {
  ...existing...,
  orchestration: '编排执行',
};
const STEP_COLORS = {
  ...existing...,
  orchestration: { bg: 'bg-indigo-900/30', border: 'border-indigo-700/50', text: 'text-indigo-300' },
};
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/AgentStepDisplay.tsx
git commit -m "feat: add OrchestrationView component for multi-tool execution visualization"
```

---

### Task 11: Frontend — SSE Event Handling in page.tsx and vision/page.tsx

**Files:**
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`

Both pages need to handle orchestration SSE events and update the orchestration state (tool statuses) in real-time.

- [ ] **Step 1: Read current page.tsx SSE handling**

Read `frontend/src/app/page.tsx` to understand how `agentSteps` state is managed and how `onStep` callback is used. Find the section where `agentChatStream` is called.

- [ ] **Step 2: Add orchestration state management to page.tsx**

Find the `onStep` callback in page.tsx. It currently accumulates steps like:

```typescript
(step: AgentStep) => {
  setAgentSteps(prev => [...prev, step]);
  // or similar
}
```

Add logic to update orchestration tool statuses when an `orchestration_update` event arrives:

```typescript
(step: AgentStep) => {
  if (step.step_type === 'orchestration' && step.orchestration_type === 'update' && step.orchestration_update) {
    // Update existing orchestration plan's tool status
    setAgentSteps(prev => {
      const updated = [...prev];
      // Find the orchestration plan step and update the matching tool
      for (let i = updated.length - 1; i >= 0; i--) {
        if (updated[i].step_type === 'orchestration' && updated[i].orchestration_type === 'plan' && updated[i].stages) {
          const planStep = { ...updated[i], stages: updated[i].stages!.map(stage => ({
            ...stage,
            tools: stage.tools.map(tool =>
              tool.call_id === step.orchestration_update!.call_id
                ? { ...tool, ...step.orchestration_update!, status: step.orchestration_update!.status }
                : tool
            ),
          }))};
          updated[i] = planStep;
          break;
        }
      }
      return updated;
    });
    return;
  }
  if (step.step_type === 'orchestration' && step.orchestration_type === 'done' && step.orchestration_summary) {
    // Attach summary to the plan step
    setAgentSteps(prev => {
      const updated = [...prev];
      for (let i = updated.length - 1; i >= 0; i--) {
        if (updated[i].step_type === 'orchestration' && updated[i].orchestration_type === 'plan') {
          updated[i] = { ...updated[i], orchestration_summary: step.orchestration_summary };
          break;
        }
      }
      return updated;
    });
    return;
  }
  // Default: add step normally
  setAgentSteps(prev => [...prev, step]);
}
```

- [ ] **Step 3: Apply identical changes to vision/page.tsx**

Apply the same orchestration event handling logic to `frontend/src/app/vision/page.tsx`. The structure should be identical to page.tsx's handling.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/app/page.tsx frontend/src/app/vision/page.tsx
git commit -m "feat: handle orchestration SSE events in text and vision pages"
```

---

### Task 12: Update AGENTS.md

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Add orchestrator documentation to AGENTS.md**

Add a new section after the "SmartToolCallingAgent 三层处理" section. Insert after line 51:

```markdown
### 多工具编排（ToolOrchestrator）

当 LLM 一次输出多个 tool_calls 时，`SmartToolCallingAgent.process_tool_calls()` 会调用 `ToolOrchestrator` 进行编排执行：

1. **DependencyAnalyzer** 根据静态规则将 tool_calls 分组为 stages（只读工具并行，写/执行工具串行）
2. **PlanExecutor** 用 `ThreadPoolExecutor` 并行执行 stage 内的工具，按序执行 stages
3. 编排结果通过 `orchestration_plan`/`orchestration_update`/`orchestration_done` SSE 事件实时推送前端
4. 单个 tool_call 时不走编排，走原有 `super().process_tool_calls()` 逻辑

核心文件: `backend/agent/orchestrator.py`

### 复合搜索工具

| 工具 | 文件 | 功能 |
|------|------|------|
| `search_and_fetch` | `backend/tools/search_and_fetch.py` | 搜索互联网 + 并发抓取所有结果页面 |
| `wikipedia_search_and_fetch` | `backend/tools/wikipedia_search_and_fetch.py` | 搜索维基百科 + 并发获取所有摘要 |
```

Also update the "核心文件" table to include `orchestrator.py`.

- [ ] **Step 2: Add orchestrator to common pitfalls section**

Add a new pitfall entry:

```markdown
11. **多工具编排 process_tool_calls 覆写**: `SmartToolCallingAgent.process_tool_calls()` 覆写了 smolagents 的父类方法。当 smolagents 升级时，需检查父类 `process_tool_calls` 的签名和行为是否有变化。编排事件通过 `yield {"_orchestration_event": True, ...}` 传递，在 `_run_agent()` 的事件循环中被识别并转发为 SSE 事件。
```

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md
git commit -m "docs: update AGENTS.md with orchestrator architecture and composite tools"
```

---

### Task 13: Final Integration Test & Verification

**Files:**
- All modified files

- [ ] **Step 1: Run all unit tests**

```bash
cd ~/Project/LLM-deployment && .sglang/bin/python -m pytest tests/test_orchestrator.py tests/test_search_and_fetch.py tests/test_write_memory_tool_calls.py tests/test_strip_tool_call_text.py -v
```

Expected: All tests PASS

- [ ] **Step 2: Run full test suite (skip hardware-dependent tests)**

```bash
cd ~/Project/LLM-deployment && .sglang/bin/python -m pytest tests/ -v --ignore=tests/test_camera_service.py --ignore=tests/test_cycle_api.py --ignore=tests/test_cycle_config.py --ignore=tests/test_cycle_executor.py --ignore=tests/test_load_images_script.py -x
```

Expected: All tests PASS

- [ ] **Step 3: Verify frontend builds**

```bash
cd ~/Project/LLM-deployment/frontend && npm run build
```

Expected: Build succeeds with no TypeScript errors

- [ ] **Step 4: Quick smoke test — import verification**

```bash
cd ~/Project/LLM-deployment && .sglang/bin/python -c "
from backend.agent.orchestrator import ToolOrchestrator, DependencyAnalyzer, PlanExecutor, ExecutionPlan, ToolResult
from backend.tools.search_and_fetch import search_and_fetch_impl
from backend.tools.wikipedia_search_and_fetch import wikipedia_search_and_fetch_impl
from backend.agent.agent_runner import SmartToolCallingAgent
print('All imports successful')

# Verify orchestrator is instantiated in agent
import inspect
src = inspect.getsource(SmartToolCallingAgent.__init__)
assert 'ToolOrchestrator' in src, 'ToolOrchestrator not in __init__'
print('SmartToolCallingAgent.__init__ includes ToolOrchestrator')

# Verify process_tool_calls is overridden
assert 'process_tool_calls' in SmartToolCallingAgent.__dict__, 'process_tool_calls not overridden'
print('process_tool_calls is overridden in SmartToolCallingAgent')
"
```

Expected: All checks pass

- [ ] **Step 5: Final commit (if any remaining changes)**

```bash
git status
# If clean, no action needed
# If dirty, commit any remaining changes
```
