# Turn-Parallel Tool Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace nested plural child dispatch with one singular tool call per child, execute every accepted tool call in a model turn concurrently, and return one turn-scoped shared child-agent blackboard.

**Architecture:** `SmartToolCallingAgent` preflights the calls from one model turn, applies the configured child-call cap, and sends one accepted call list to a simplified one-stage `ToolOrchestrator`. Each `dispatching_parallel_agent` invocation launches exactly one child and returns a structured outcome; `process_tool_calls()` formats all dispatch outcomes into one ordered shared blackboard while preserving ordinary tool observations and structured memory traces.

**Tech Stack:** Python 3.12, smolagents, `concurrent.futures.ThreadPoolExecutor`, asyncio child runners, FastAPI SSE/timeline events, pytest, Next.js/TypeScript projection tests.

## Global Constraints

- The existing sub-agent dispatching system very likely contains a code bug and must be updated according to the user's requirements.
- Start with regression coverage of the user's real `Calling tools:` payload shape and trace parser -> schema -> wrapper -> child runtime -> observation boundaries.
- The only dispatch function name is `dispatching_parallel_agent`.
- Its `arguments` object contains exactly `child_id` and `task`, both required; `additionalProperties` is false.
- Completely remove active uses of `subtask`, `subtasks`, `may_write`, and `reserved_files` from dispatch code, schema, prompt, and tests.
- Do not add compatibility aliases for the plural tool or old payload.
- Every accepted tool call from one model turn is submitted in one parallel stage; dependent work belongs in future LLM turns.
- Preserve `manager_agent_max_parallel_children` as the per-turn accepted child-call cap.
- Preserve child timeout, stop cleanup, timeline replay, structured tool history, and the Prefix-Cache Covenant.
- Do not modify vendored dependencies under `.sglang/`.
- Do not run Git commands or create commits unless the user separately grants permission.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `backend/agent/orchestrator.py` | One-stage turn planner and concurrent executor | Remove category/stage sequencing and global timeout |
| `backend/agent/agent_runner.py` | Singular schema, child runtime, turn preflight, shared blackboard | Replace plural dispatch path |
| `backend/agent/local_llm_model.py` | Canonical transcript parsing diagnostics | Remove obsolete dispatch-specific assumptions; retain strict canonical parsing |
| `config/config-agent-prompts.yaml` | Local-LLM canonical examples and future-turn dependency rule | Replace old nested protocol |
| `tests/test_multi_tool_parsing.py` | Real transcript regression fixtures | Add singular canonical fixtures |
| `tests/test_manager_agent_dispatch.py` | Schema, runtime, cap, blackboard, concurrency | Rewrite old plural tests |
| `tests/test_orchestrator.py` | One-stage all-tools-parallel contract | Replace dependency-stage tests |
| `tests/test_agent_session_context.py` | Stop/run-scope and structured-memory safety | Extend only where behavior changes |
| `frontend/src/lib/timeline-projection.test.mts` | One-stage projection/grouping contract | Update if current fixtures assume serial stages |
| `AGENTS.md` | Current architecture and traps | Update after verified behavior |

---

### Task 1: Reproduce the Real Failure Boundary and Lock the Singular Parser Contract

**Files:**
- Modify: `tests/test_multi_tool_parsing.py`
- Modify: `backend/agent/local_llm_model.py:766-819,1114-1213,1669-1839` only if the failing test identifies a parser defect

**Interfaces:**
- Consumes: `LocalLLMModel.detect_malformed_tool_transcript(content)` and `_parse_tool_calls_from_text(content)`.
- Produces: canonical `ChatMessageToolCall` objects whose outer IDs and singular function names survive parsing.

- [ ] **Step 1: Add the user's old transcript as a diagnostic regression fixture**

Add a test containing the exact structural shape (long task text may be shortened without changing nesting):

```python
def test_user_reported_plural_dispatch_transcript_reaches_old_runtime_shape(model):
    content = '''Calling tools:
[{"id":"call_1","type":"function","function":{"name":"dispatching_parallel_agents","arguments":{"subtasks":[{"name":"poet_a","task":"Compose poem A with a self-contained detailed briefing."},{"name":"poet_b","task":"Compose poem B with a self-contained detailed briefing."}]}}}]'''

    assert model.detect_malformed_tool_transcript(content) is None
    calls = model._parse_tool_calls_from_text(content)
    assert calls is not None
    assert [(call.id, call.function.name) for call in calls] == [
        ("call_1", "dispatching_parallel_agents")
    ]
```

This test documents that the supplied transcript is canonical JSON and identifies whether failure occurs before or after text parsing. Do not keep the plural protocol as a supported runtime contract after the migration; retain the fixture only as a historical diagnostic test or replace its final assertion with explicit unknown-tool rejection once Task 2 lands.

- [ ] **Step 2: Add the new multi-call singular transcript test**

```python
def test_singular_dispatch_transcript_preserves_each_call_and_child_id(model):
    content = '''Calling tools:
[{"id":"call_1","type":"function","function":{"name":"dispatching_parallel_agent","arguments":{"child_id":"poet_a","task":"Compose poem A with a self-contained detailed briefing."}}},{"id":"call_2","type":"function","function":{"name":"dispatching_parallel_agent","arguments":{"child_id":"poet_b","task":"Compose poem B with a self-contained detailed briefing."}}}]'''

    assert model.detect_malformed_tool_transcript(content) is None
    calls = model._parse_tool_calls_from_text(content)
    assert calls is not None
    assert [call.id for call in calls] == ["call_1", "call_2"]
    assert [call.function.name for call in calls] == [
        "dispatching_parallel_agent",
        "dispatching_parallel_agent",
    ]
    assert [call.function.arguments for call in calls] == [
        {"child_id": "poet_a", "task": "Compose poem A with a self-contained detailed briefing."},
        {"child_id": "poet_b", "task": "Compose poem B with a self-contained detailed briefing."},
    ]
```

- [ ] **Step 3: Run both tests and record the boundary**

Run:

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_multi_tool_parsing.py::test_user_reported_plural_dispatch_transcript_reaches_old_runtime_shape \
  tests/test_multi_tool_parsing.py::test_singular_dispatch_transcript_preserves_each_call_and_child_id
```

Expected before runtime migration: both parsing tests pass. If either fails, make the smallest parser correction and state the exact failure in a code comment or test name. If both pass, record in the implementation summary that the historical production failure was downstream of canonical text parsing; do not invent a more specific root cause without logs.

- [ ] **Step 4: Keep strict malformed-transcript behavior**

Ensure `detect_malformed_tool_transcript()` still rejects unexpected outer wrapper keys and malformed arrays, but remove error copy that teaches obsolete dispatch fields. The canonical wrapper remains exactly `id`, `type`, and `function`.

- [ ] **Step 5: Run parser regression tests**

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_multi_tool_parsing.py \
  tests/test_local_llm_model_streaming.py \
  tests/test_tool_call_normalization.py
```

Expected: all selected tests pass.

---

### Task 2: Replace the Nested Dispatch Tool with the Singular Contract

**Files:**
- Modify: `backend/agent/agent_runner.py:740-756,1910-1940,2243-2410,2562-2939,2997-3054`
- Modify: `tests/test_manager_agent_dispatch.py`

**Interfaces:**
- Consumes: manager session, `child_id: str`, and `task: str`.
- Produces: `ManagerChildDispatchOutcome` and `_dispatching_parallel_agent_for_session(manager_session, child_id, task, *, turn_dispatch_id, call_id)`.

- [ ] **Step 1: Replace old schema tests with strict singular schema tests**

```python
def test_dispatching_parallel_agent_schema_is_singular_and_minimal():
    runner = _make_manager_runner()
    manager_session, _ = _make_manager_session(runner)
    agent = runner._build_profile_agent("manager_agent", session=manager_session)

    assert "dispatching_parallel_agent" in agent.tools
    assert "dispatching_parallel_agents" not in agent.tools
    tool = agent.tools["dispatching_parallel_agent"]
    assert set(tool.inputs) == {"child_id", "task"}
    assert tool.inputs["child_id"]["type"] == "string"
    assert tool.inputs["task"]["type"] == "string"
```

Add parameterized validation tests asserting missing/blank `child_id`, short `task`, and extra old fields fail. Exercise schema validation through the Tool when practical rather than only calling private helpers.

- [ ] **Step 2: Run schema tests and verify failure**

```bash
./.sglang/bin/python -m pytest -q tests/test_manager_agent_dispatch.py -k "singular_and_minimal or dispatch_validation"
```

Expected: fail because only the plural nested tool exists.

- [ ] **Step 3: Introduce a structured child outcome**

Replace `ManagerDispatchSubtask` with:

```python
@dataclass
class ManagerChildDispatchOutcome:
    call_id: str
    child_id: str
    status: str
    final_report: str = ""
    error: str = ""
```

Remove `may_write` and `reserved_files` from active dispatch request/run data. If `ChildAgentRun` persists those fields only for this feature, delete them and remove them from timeline payloads. Do not leave dead defaults.

- [ ] **Step 4: Implement strict scalar validation**

```python
@staticmethod
def _validate_manager_child_dispatch(child_id: str, task: str) -> tuple[str, str]:
    if not isinstance(child_id, str) or child_id != child_id.strip():
        raise ValueError("child_id must be a trimmed string")
    if not 3 <= len(child_id) <= 128:
        raise ValueError("child_id must be 3..128 characters")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", child_id):
        raise ValueError(
            "child_id may only contain letters, digits, underscore, dot, colon, and hyphen"
        )
    if not isinstance(task, str) or len(re.sub(r"\s+", "", task)) < 10:
        raise ValueError("task must contain at least 10 non-space characters")
    return child_id, task
```

- [ ] **Step 5: Build the singular Tool**

Create `_build_dispatching_parallel_agent_tool(session)` with:

```python
name = "dispatching_parallel_agent"
inputs = {
    "child_id": {
        "type": "string",
        "description": "Stable manager-scoped child identity; reuse it only to continue that child context.",
    },
    "task": {
        "type": "string",
        "description": "Self-contained English task briefing for exactly one child.",
    },
}
output_type = "any"
```

Its `forward(child_id, task)` resolves the current turn execution context (Task 4 supplies `call_id` and `turn_dispatch_id`) and calls `_dispatching_parallel_agent_for_session`. Do not accept `**kwargs`; smolagents must reject extra fields.

- [ ] **Step 6: Refactor runtime from list to one child**

Move the body that resolves/creates one child session and runs it to terminal state into:

```python
async def _dispatching_parallel_agent_for_session(
    self,
    manager_session: AgentSession,
    child_id: str,
    task: str,
    *,
    turn_dispatch_id: str,
    call_id: str,
) -> ManagerChildDispatchOutcome:
    ...
```

Requirements:

- use `(manager_session.session_id, child_id)` for context reuse;
- use `turn_dispatch_id` for all same-turn manager timeline events;
- do not acquire `_manager_dispatch_locks` around child execution;
- keep map/session mutation under the smallest existing lock needed;
- retain child timeout and abort handling;
- return outcome data instead of formatting a blackboard;
- emit terminal child timeline events before returning.

- [ ] **Step 7: Add one-child runtime tests**

```python
@pytest.mark.asyncio
async def test_singular_dispatch_runs_exactly_one_child():
    runner = _make_manager_runner()
    manager_session, _ = _make_manager_session(runner)
    runner._run_child_agent_to_final_report = AsyncMock(return_value="poem report")

    outcome = await runner._dispatching_parallel_agent_for_session(
        manager_session,
        "poet_a",
        "Compose one classical poem and return the requested format.",
        turn_dispatch_id="turn-dispatch-1",
        call_id="call_1",
    )

    assert outcome.call_id == "call_1"
    assert outcome.child_id == "poet_a"
    assert outcome.status == "completed"
    assert outcome.final_report == "poem report"
    assert len(runner.child_agent_runs) == 1
```

Also retain tests for same-ID later-turn reuse and different-manager isolation, renamed to `child_id` terminology.

- [ ] **Step 8: Run manager dispatch tests**

```bash
./.sglang/bin/python -m pytest -q tests/test_manager_agent_dispatch.py -k "schema or singular or child_id or reuses"
```

Expected: selected tests pass.

---

### Task 3: Make the Orchestrator One-Stage and Fully Parallel

**Files:**
- Modify: `backend/agent/orchestrator.py`
- Modify: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: ordered `list[ChatMessageToolCall]` and tool dictionary.
- Produces: one `ExecutionPlan` stage and ordered-addressable `list[ToolResult]`; `ToolResult.output` may hold a structured dispatch outcome.

- [ ] **Step 1: Replace dependency tests with one-stage tests**

```python
def test_every_multi_tool_turn_has_one_parallel_stage():
    calls = [
        _make_tool_call("call_1", "read_file", {"file_path": "/a"}),
        _make_tool_call("call_2", "write_file", {"file_path": "/b", "content": "x"}),
        _make_tool_call("call_3", "exec_command", {"command": "true"}),
        _make_tool_call(
            "call_4",
            "dispatching_parallel_agent",
            {"child_id": "worker_a", "task": "Perform one independent detailed task."},
        ),
    ]

    plan = ToolOrchestrator().analyze(calls)
    assert len(plan.stages) == 1
    assert plan.stages[0].parallel is True
    assert plan.stages[0].tool_calls == calls
```

Add a barrier-based execution test. Each fake tool appends its call ID to `started`, waits on a `threading.Barrier(N + 1)`, and only then completes. The main test waits until all starts are observed before releasing the barrier. This proves submission concurrency without relying on timing thresholds.

- [ ] **Step 2: Run tests and verify old staged behavior fails**

```bash
./.sglang/bin/python -m pytest -q tests/test_orchestrator.py -k "one_parallel_stage or starts_all_calls"
```

Expected: fail because write/execute/dispatch calls currently become serial stages.

- [ ] **Step 3: Remove category analysis and build one stage**

Implement analysis as:

```python
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
```

Delete `TOOL_CATEGORIES`, `READONLY_CATEGORY_NAMES`, `_get_category()`, `_is_readonly()`, and their tests.

- [ ] **Step 4: Remove the orchestrator global timeout and worker cap**

Change `PlanExecutor` so one stage uses exactly `len(stage.tool_calls)` workers for a multi-call turn and waits for all workers. Remove `global_timeout`, `remaining_timeout`, manager-dispatch timeout exemptions, and stage timeout result synthesis. Keep exception isolation in `_execute_single_tool`.

Conceptual execution:

```python
with ThreadPoolExecutor(max_workers=len(stage.tool_calls)) as pool:
    futures = [pool.submit(self._execute_single_tool, call, tools, event_callback)
               for call in stage.tool_calls]
    results = [future.result() for future in futures]
```

Submitting all futures happens before reading any result. Individual tools and child runners remain responsible for their own timeouts.

- [ ] **Step 5: Preserve structured outputs**

Change:

```python
@dataclass
class ToolResult:
    ...
    output: Any = None
```

Use `str(output)` only for SSE preview. Do not coerce a `ManagerChildDispatchOutcome` into text inside `_execute_single_tool`.

- [ ] **Step 6: Run all orchestrator tests**

```bash
./.sglang/bin/python -m pytest -q tests/test_orchestrator.py
```

Expected: all tests pass and no test expects serial stages.

---

### Task 4: Add Turn Preflight, Dispatch Cap, and One Shared Blackboard

**Files:**
- Modify: `backend/agent/agent_runner.py:1430-1581`
- Modify: `tests/test_manager_agent_dispatch.py`

**Interfaces:**
- Consumes: all tool calls in one `chat_message` and `ManagerChildDispatchOutcome` values from Task 2.
- Produces: one parallel plan, complete `memory_step.tool_calls`, separate normal observation section, and one ordered shared blackboard.

- [ ] **Step 1: Add failing end-to-end process tests**

Construct a `chat_message` with two singular dispatch calls and inject a fake child runner guarded by an asyncio/thread barrier. Assert:

```python
assert "Shared child-agent blackboard for this turn." in memory_step.observations
assert memory_step.observations.index("Call: call_1") < memory_step.observations.index("Call: call_2")
assert "Child: poet_a" in memory_step.observations
assert "Child: poet_b" in memory_step.observations
assert len(memory_step.tool_calls) == 2
```

Add a mixed turn containing `read_file` plus two dispatch calls and assert the normal tool section and shared blackboard are both present.

- [ ] **Step 2: Add cap and duplicate-ID tests**

With child limit 2 and calls `call_1/a`, `call_2/b`, `call_3/c`, assert only `a` and `b` launch and blackboard contains:

```text
Configured maximum: 2
Requested child calls: 3
Executed child calls: 2
Not executed: call_3/c
```

For duplicate same-turn `child_id`, assert no child with that duplicate ID starts and the observation gives a concise duplicate-ID error. Normal non-dispatch calls in the same turn must still run.

- [ ] **Step 3: Create turn execution context for Tool.forward**

Add a thread/context-local immutable mapping from tool `call_id` to the shared `turn_dispatch_id`, set by `_execute_single_tool` before calling `forward()` and cleared in `finally`. Do not store the current call ID by mutating the shared Tool object because multiple calls use the same Tool concurrently.

The dispatch Tool reads:

```python
call_id = current_tool_call_context.get_call_id()
turn_dispatch_id = current_tool_call_context.get_turn_dispatch_id()
```

and rejects missing context with a clear internal error. Extend the existing `current_session_context` pattern rather than creating mutable globals.

- [ ] **Step 4: Preflight one turn before execution**

In `process_tool_calls()`:

1. preserve the special `basic_agent` handoff rule;
2. identify calls named `dispatching_parallel_agent`;
3. validate each dispatch `arguments` object has exactly `child_id` and `task`;
4. detect duplicate child IDs before launching children;
5. retain only the first `manager_agent_max_parallel_children` valid dispatch calls;
6. create skipped `ToolResult` values for excess calls;
7. retain every normal tool call;
8. assign one `turn_dispatch_id` shared by retained dispatch calls;
9. execute retained dispatch plus normal calls in one plan.

Do not route a single dispatch call through `super().process_tool_calls`; it needs the same turn context and blackboard formatting as multi-dispatch turns. A single ordinary tool may continue through the parent path.

- [ ] **Step 5: Format the shared blackboard once**

Implement a pure formatter with an explicit signature:

```python
@staticmethod
def _format_manager_turn_blackboard(
    ordered_dispatch_calls: list,
    results_by_call_id: dict[str, ToolResult],
    *,
    configured_maximum: int,
) -> str:
    ...
```

It iterates model call order, reads `child_id` from each original call, and emits completed/failed/skipped sections. It never includes task text or runtime IDs.

Assemble observations in this order:

```text
[Tool normal_tool] (call_id=...):
...

Shared child-agent blackboard for this turn.
...
```

Do not emit one `[Tool dispatching_parallel_agent]` report per child in addition to the shared blackboard.

- [ ] **Step 6: Preserve memory and SSE events**

Set `memory_step.tool_calls` to every original call, including skipped dispatch calls, so the assistant tool-call structure remains canonical. Emit `orchestration_update(status="skipped")` for capped calls and aggregate accurate completed/failed/skipped counts in `orchestration_done`.

- [ ] **Step 7: Prove distinct children are not serialized by a manager lock**

Replace the old same-manager serialization test with a barrier test showing two different child IDs enter their child runner concurrently. Keep a separate later-turn same-ID reuse test. Delete `_manager_dispatch_locks` if it has no remaining use.

- [ ] **Step 8: Run dispatch integration tests**

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_manager_agent_dispatch.py \
  tests/test_agent_session_context.py -k "dispatch or child or tool_calls or run_scope"
```

Expected: all selected tests pass.

---

### Task 5: Rewrite Prompts for the Local LLM

**Files:**
- Modify: `config/config-agent-prompts.yaml:90-160` and all general multi-tool guidance
- Modify: `tests/test_manager_agent_dispatch.py`
- Modify: `tests/test_multi_agent_config.py`

**Interfaces:**
- Consumes: `manager_agent_max_parallel_children` Jinja variable.
- Produces: a rendered manager prompt with one-child examples and explicit future-turn dependency semantics.

- [ ] **Step 1: Write prompt assertions first**

```python
assert '"name":"dispatching_parallel_agent"' in prompt
assert '"child_id":"researcher-a"' in prompt
assert '"child_id":"researcher-b"' in prompt
assert '"subtasks"' not in prompt
assert "dispatching_parallel_agents" not in prompt
assert "may_write" not in prompt
assert "reserved_files" not in prompt
assert "future turn" in prompt.lower()
assert "run concurrently" in prompt.lower()
```

Also assert the rendered prompt includes the configured exact child-call maximum.

- [ ] **Step 2: Run prompt tests and verify failure**

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_manager_agent_dispatch.py -k "prompt" \
  tests/test_multi_agent_config.py -k "prompt"
```

Expected: fail on old plural examples and instructions.

- [ ] **Step 3: Replace manager protocol text**

Include one canonical single-child example and one two-call array example exactly matching the spec. State that each call has one child, same-turn calls run concurrently, and dependent/conflicting work must be emitted only after observing the current turn in a future turn.

Keep the child briefing self-contained and English. Put file-write boundaries into `task`; do not reintroduce dedicated ownership fields.

- [ ] **Step 4: Replace general Orchestrator guidance**

Remove claims that the backend analyzes dependencies or optimizes serial order. Replace them with:

```text
All tool calls you emit in the same turn run concurrently. Emit together only
calls that are independent. If one call needs another call's result, wait for
the observation and issue the dependent call in a future turn.
```

- [ ] **Step 5: Run prompt/config tests**

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_manager_agent_dispatch.py -k "prompt or schema" \
  tests/test_multi_agent_config.py -k "not real_config_files_use_multi_agent_prompt_layout"
```

Expected: selected tests pass. Keep the known unrelated planning-layout assertion separate if it still reflects stale repository expectations.

---

### Task 6: Timeline, Frontend Projection, Stop, and Prefix-Cache Regression

**Files:**
- Modify: `tests/test_manager_agent_dispatch.py`
- Modify: `tests/test_agent_session_context.py`
- Modify: `frontend/src/lib/timeline-projection.test.mts` only if fixtures fail
- Modify: frontend production projection only if tests reveal it assumes multiple stages/dispatch IDs

**Interfaces:**
- Consumes: one shared `turn_dispatch_id`, per-call IDs, child run IDs, and one parallel orchestration stage.
- Produces: stable existing SSE protocol and structured append-only memory.

- [ ] **Step 1: Add timeline grouping test**

Assert two same-turn dispatch calls emit child updates with one shared `dispatch_id`, distinct child/call identities, and one `manager_dispatch_completed` summary after both terminal outcomes. If timeline events do not currently include call ID, add `call_id` as runtime/UI metadata only; never inject it into system prompt or historical middle sections.

- [ ] **Step 2: Add stop/cleanup regression**

Start two blocked child runners, abort the manager, release workers, and assert both children reach aborted/terminal cleanup before the manager run scope becomes reusable. Preserve the existing rule that SSE generator close alone does not prematurely finish workers.

- [ ] **Step 3: Add structured-memory regression**

After a two-dispatch turn, call `write_memory_to_messages()` and assert the assistant message retains both structured tool calls followed by the user-role shared-blackboard observation. Assert no tool call is stripped and no raw timeline event appears in model messages.

- [ ] **Step 4: Run backend safety suites**

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_manager_agent_dispatch.py \
  tests/test_agent_session_context.py \
  tests/test_write_memory_tool_calls.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Run frontend projection tests**

Use the repository's existing frontend test command for `timeline-projection.test.mts`. If no production frontend change is needed, do not modify text/vision pages merely for churn. If a production change is needed, update both pages consistently and run the frontend build.

---

### Task 7: Remove Dead Protocol and Update Architecture Documentation

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `backend/agent/orchestrator.py`
- Modify: `config/config-agent-prompts.yaml`
- Modify: active tests under `tests/`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: completed implementation from Tasks 1-6.
- Produces: no active old protocol references and accurate contributor guidance.

- [ ] **Step 1: Search active code and tests for obsolete names**

Search for:

```text
dispatching_parallel_agents
ManagerDispatchSubtask
_validate_manager_dispatch_subtasks
_dispatching_parallel_agents_for_session
subtasks[].
may_write
reserved_files
TOOL_CATEGORIES
READONLY_CATEGORY_NAMES
_manager_dispatch_locks
global_timeout
```

Historical superseded design documents may retain old text. Active code, prompt, config, and tests must not.

- [ ] **Step 2: Delete dead helpers and stale tests**

Remove code rather than leaving unused compatibility branches. Ensure `manager_agent_max_parallel_children` remains the only manager child-call limit and still validates as positive configuration.

- [ ] **Step 3: Update `AGENTS.md`**

Document:

- singular canonical schema;
- one child per call;
- one same-turn shared blackboard;
- all same-turn tools run concurrently;
- dependent/conflicting operations belong in future turns;
- no file ownership metadata fields;
- no backend static dependency stages;
- cap and dropped-call reporting;
- regression warning that mocked private coroutine tests are insufficient.

- [ ] **Step 4: Run syntax and focused verification**

```bash
./.sglang/bin/python -m py_compile \
  backend/agent/agent_runner.py \
  backend/agent/local_llm_model.py \
  backend/agent/orchestrator.py

./.sglang/bin/python -m pytest -q \
  tests/test_orchestrator.py \
  tests/test_manager_agent_dispatch.py \
  tests/test_multi_tool_parsing.py \
  tests/test_local_llm_model_streaming.py \
  tests/test_tool_call_normalization.py \
  tests/test_agent_session_context.py \
  tests/test_write_memory_tool_calls.py \
  tests/test_multi_agent_config.py
```

Expected: all relevant tests pass. If the previously known unrelated `test_real_config_files_use_multi_agent_prompt_layout` remains stale, report it explicitly with its unchanged failure rather than hiding it.

- [ ] **Step 5: Run manual local-LLM acceptance test when services are available**

Prompt the manager for two independent poems and verify two singular calls, concurrent child `running` events, one shared blackboard, and a synthesized manager answer. If services/models are not running, report this verification gap explicitly; unit tests are not a substitute for the user-observed production path.

---

## Implementation Review Gates

After implementation, use two independent review passes:

1. Spec compliance review: verify every acceptance criterion in `docs/superpowers/specs/2026-07-15-turn-parallel-tool-orchestration-design.md` against code and tests.
2. Code quality/concurrency review: inspect thread/context-local use, lock scope, stop cleanup, result ordering, memory serialization, and absence of silent compatibility behavior.

Fix confirmed findings with failing tests before final verification.

---

### Task 8: Restore the Dedicated Manager Dispatch Card in Multi-Tool Turns

**Files:**
- Modify: `frontend/src/lib/timeline-projection.ts`
- Modify: `frontend/src/lib/message-types.ts`
- Modify: `frontend/src/components/AgentStepDisplay.tsx`
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Modify: `frontend/src/lib/timeline-projection.test.mts`
- Test: relevant backend manager timeline tests

**Interfaces:**
- Consumes: `manager_child_agent_update.call_id`, orchestration
  `stages[].tools[].call_id`, and existing child `href`/`child_run_id`.
- Produces: one dedicated `ManagerParallelDispatchCard` per same-turn
  `dispatch_id`, anchored to its orchestration plan step by call ID.

- [ ] **Step 1: Add failing call-ID projection tests**

Assert timeline projection preserves `call_id` as `callId`, merge/update retains
it, and two child rows with different call IDs remain grouped by one dispatch ID.

- [ ] **Step 2: Add failing page-source/render contracts**

Replace the old ordinal/top-level-tool-call assertions. Both pages must derive
the dispatch call-ID set from `step.stages[].tools[]`, intersect it with each
dispatch card's child `callId` values, and pass the card through
`AgentStepDisplay.renderAfterStep` after the matching orchestration step.

- [ ] **Step 3: Preserve call ID through projection and message state**

Add optional `call_id` to `TimelineEvent`, required `callId` to manager-child
projection/view where valid, and preserve prior value on partial updates.

- [ ] **Step 4: Replace ordinal pairing with call-ID pairing in both pages**

Use a pure/shared helper where practical. Event order must not matter: React
recomputes the join from current `agentSteps` and `managerDispatches` on each
render. Do not pair by array index.

- [ ] **Step 5: Suppress duplicate generic dispatch rendering**

Filter `dispatching_parallel_agent` tools inside `OrchestrationView`. Return no
generic orchestration UI when no ordinary tools remain; retain normal tools and
their statuses for mixed stages.

- [ ] **Step 6: Verify Open child and replay/live behavior**

Keep `window.open(href, '_blank', 'noopener,noreferrer')` and the existing
`/agent/child/[childRunId]` stream route. Add regression assertions that each
rendered child row retains its backend `href`.

- [ ] **Step 7: Run verification**

```bash
node --experimental-strip-types --test src/lib/timeline-projection.test.mts
npm run build:verify
./.sglang/bin/python -m pytest -q tests/test_manager_agent_dispatch.py -k "timeline or call_id or child"
```

Expected: all selected tests and the frontend production build pass.

---

### Task 9: Add Child-Scoped Abort, Safe Context Disposal, and Bounded Replay Anchors

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `backend/api/agent.py`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/child-agent-transcript.ts`
- Modify: `frontend/src/app/agent/child/[childRunId]/page.tsx`
- Modify: `tests/test_manager_agent_dispatch.py`
- Modify: `tests/test_agent_stop_api.py`
- Modify: `tests/test_session_store_cleanup.py`
- Modify: `frontend/src/lib/api.test.mts`

**Interfaces:**
- Produces: `POST /v1/agent/child/abort` with `child_run_id` and an idempotent
  terminal response.
- Produces: `AgentRunner.abort_child_agent_run(child_run_id)` and a barrier-aware
  context disposal operation.
- Preserves: terminal child run metadata and replay events for UI/audit only.

- [ ] **Step 1: Write failing backend lifecycle tests**

Cover one active child plus one sibling. Assert the target request ID is captured,
only the target is aborted, disposal waits for its captured run scope, the
sibling/manager remain un-aborted, the outcome contains `The user has aborted
this child agent run.`, and the target session/mapping disappear only after the
worker exits.

- [ ] **Step 2: Write failing API and idempotency tests**

Assert 404 for unknown child, success for active child, and a stable already-
terminal response for repeated clicks. Assert request abort uses
`fallback_to_abort_all=False`.

- [ ] **Step 3: Implement child-scoped abort and barrier**

Capture request/run-scope identity before abort, signal only the child session,
perform targeted upstream cancellation, await worker cleanup without blocking
the event loop, and then remove the session and identity mapping. The endpoint
must not directly format or inject the manager blackboard.

- [ ] **Step 4: Preserve user-abort reason through the original worker**

Store an explicit user-abort reason on `ChildAgentRun`; do not let generic
`Child agent run was aborted` handling overwrite it. The existing dispatch
outcome/blackboard formatter then reports the exact hint naturally.

- [ ] **Step 5: Fix replay truncation anchors and TTL cleanup**

Preserve the latest `manager_assigned_task`, truncation marker, and newest events
within the configured cap. Extend runner cleanup so expired inactive child
sessions remove identity mappings, run records, event buffers, and subscribers;
never clean active runs.

- [ ] **Step 6: Add runtime-only context diagnostics**

Emit `reused_context` and `prior_step_count` on `manager_assigned_task`, render
them on the child page, and prove they do not enter child/manager model messages.

- [ ] **Step 7: Implement child-page confirmation UI**

Show `Abort child` only for pending/running status. Require confirmation, disable
while the request is in flight, call the child abort endpoint, and rely on replay-
live `aborted` event for terminal UI state. Preserve `Open child` behavior.

- [ ] **Step 8: Verify**

Run focused backend dispatch/stop/cleanup suites, frontend child API/transcript
tests, timeline projection tests, `py_compile`, and `npm run build:verify`.
