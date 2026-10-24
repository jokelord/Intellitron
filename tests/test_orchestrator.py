import threading
from unittest.mock import MagicMock

from backend.agent.orchestrator import DependencyAnalyzer, PlanExecutor, ToolOrchestrator


def _call(call_id: str, name: str, arguments: dict | None = None):
    call = MagicMock()
    call.id = call_id
    call.function.name = name
    call.function.arguments = arguments or {}
    return call


def _tool(forward):
    tool = MagicMock()
    tool.forward = forward
    return tool


def test_empty_turn_has_no_stage():
    assert DependencyAnalyzer().analyze([]).stages == []


def test_single_call_has_one_nonparallel_stage():
    call = _call("call_1", "read_file")
    plan = ToolOrchestrator().analyze([call])
    assert len(plan.stages) == 1
    assert plan.stages[0].parallel is False
    assert plan.stages[0].tool_calls == [call]


def test_every_multi_tool_turn_has_one_parallel_stage():
    calls = [
        _call("call_1", "read_file", {"file_path": "/a"}),
        _call("call_2", "write_file", {"file_path": "/b", "content": "x"}),
        _call("call_3", "exec_command", {"command": "true"}),
        _call("call_4", "dispatching_parallel_agent", {
            "child_id": "worker-a", "task": "Perform one independent detailed task."
        }),
    ]
    plan = ToolOrchestrator().analyze(calls)
    assert len(plan.stages) == 1
    assert plan.stages[0].parallel is True
    assert plan.stages[0].tool_calls == calls
    assert plan.total_tools == 4


def test_plan_projection_contains_one_parallel_stage_in_model_order():
    calls = [_call("call_1", "first"), _call("call_2", "second")]
    payload = ToolOrchestrator().analyze(calls).to_dict()
    assert len(payload["stages"]) == 1
    assert payload["stages"][0]["parallel"] is True
    assert [tool["call_id"] for tool in payload["stages"][0]["tools"]] == [
        "call_1", "call_2"
    ]


def test_executor_starts_more_than_five_calls_before_any_can_complete():
    count = 6
    barrier = threading.Barrier(count)
    started = []
    lock = threading.Lock()

    def forward(index):
        with lock:
            started.append(index)
        barrier.wait(timeout=2)
        return index

    calls = [_call(f"call_{index}", "work", {"index": index}) for index in range(count)]
    plan = DependencyAnalyzer().analyze(calls)
    results = PlanExecutor().execute(plan, {"work": _tool(forward)})

    assert sorted(started) == list(range(count))
    assert [result.call_id for result in results] == [call.id for call in calls]
    assert all(result.status == "completed" for result in results)


def test_executor_preserves_structured_output():
    marker = object()
    call = _call("call_1", "work")
    result = PlanExecutor().execute(
        DependencyAnalyzer().analyze([call]), {"work": _tool(lambda: marker)}
    )[0]
    assert result.output is marker


def test_one_tool_failure_does_not_cancel_sibling():
    def fail():
        raise RuntimeError("boom")

    calls = [_call("bad", "bad"), _call("good", "good")]
    results = PlanExecutor().execute(
        DependencyAnalyzer().analyze(calls),
        {"bad": _tool(fail), "good": _tool(lambda: "ok")},
    )
    assert [(result.call_id, result.status) for result in results] == [
        ("bad", "failed"), ("good", "completed")
    ]


def test_unknown_tool_is_reported_without_crashing_turn():
    call = _call("missing", "missing")
    result = PlanExecutor().execute(DependencyAnalyzer().analyze([call]), {})[0]
    assert result.status == "failed"
    assert "not found" in result.error
