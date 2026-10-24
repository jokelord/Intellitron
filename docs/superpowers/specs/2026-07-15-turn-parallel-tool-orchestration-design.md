# Turn-Parallel Tool Orchestration and Single-Child Dispatch Design

> Date: 2026-07-15
> Status: Approved for implementation
> Supersedes: the dispatch payload and staged execution rules in
> `2026-07-06-manager-agent-parallel-dispatch-design.md` and
> `2026-04-11-tool-orchestrator-design.md`

## 1. Summary

This design simplifies local-LLM tool use in two related ways:

1. One `dispatching_parallel_agent` tool call launches exactly one child agent.
2. Every tool call emitted in the same LLM turn is independent and starts in the
   same parallel execution stage.

The LLM no longer constructs a nested `subtasks` array. It emits ordinary
canonical tool calls with distinct call IDs. Multiple child tasks are expressed
as multiple calls to the same singular tool. Reports from those calls are
assembled into one turn-scoped shared blackboard before the next LLM turn.

The existing sub-agent dispatching system very likely contains a code bug. The
implementation must first preserve the user's failing payloads as regression
fixtures and trace failures across parsing, schema validation, tool execution,
child startup, and observation assembly. Passing mocked unit tests alone is not
evidence that the production path works.

## 2. Motivation

The existing contract asks a local LLM to produce nested data:

```json
{
  "subtasks": [
    {"name": "poet_a", "task": "..."},
    {"name": "poet_b", "task": "..."}
  ]
}
```

This duplicates concepts already present in the OpenAI tool-call envelope:

- the outer array already represents multiple calls;
- each call already has an `id`;
- the tool call already has one `arguments` object;
- the orchestrator already sees all calls from one model turn.

It also led to parser ambiguity around nested `name` fields, additional schema
instructions, call-local blackboards, and special timeout handling. These layers
are difficult for a local LLM and difficult to reason about in the backend.

The old Orchestrator also imposes backend-authored dependency semantics. It
classifies tools as read-only, write, execute, or unknown and then creates serial
stages. The new product rule moves turn planning back to the LLM:

- calls emitted together mean "safe and useful to run concurrently";
- dependent work belongs in a future LLM turn after observing current results;
- the backend does not infer hidden dependencies or reorder work into serial
  stages.

## 3. Goals

- Make child dispatch easy for local LLMs to generate correctly.
- Use one singular dispatch tool call per child task.
- Execute all calls from one LLM turn concurrently.
- Aggregate all child reports from that turn into one shared blackboard.
- Keep normal tool results available in the same observation.
- Let the LLM express dependencies by waiting for a future turn.
- Preserve child context reuse through an explicit `child_id`.
- Preserve stop, timeout, timeline, replay, and prefix-cache safety.
- Add an end-to-end regression test for the real canonical transcript shape.

## 4. Non-Goals

- No nested managers or child-created managers.
- No backend dependency inference or DAG construction.
- No `subtask` or `subtasks` compatibility alias.
- No plural `dispatching_parallel_agents` compatibility alias.
- No `may_write` or `reserved_files` fields.
- No automatic repair of malformed dispatch payloads.
- No injection of timeline events, UI state, session IDs, or raw child tool
  traces into manager prompts.
- No persistence of child sessions beyond the existing in-memory lifecycle.

The old protocol has not shipped as a stable external API and is actively
failing in local-LLM use. A clean break is preferable to compatibility code.

## 5. Canonical Tool Contract

### 5.1 Tool name

The only child-dispatch tool name is:

```text
dispatching_parallel_agent
```

The plural `dispatching_parallel_agents` name is removed from the prompt,
schema, profile allowlist, parser-specific handling, tests, and runtime.

### 5.2 Input schema

`arguments` contains exactly two fields:

```json
{
  "child_id": "poet_a",
  "task": "Write one self-contained child-agent task in English."
}
```

Schema:

```json
{
  "type": "object",
  "properties": {
    "child_id": {"type": "string"},
    "task": {"type": "string"}
  },
  "required": ["child_id", "task"],
  "additionalProperties": false
}
```

Rules:

- `child_id` is a stable manager-scoped child identity, not a tool name.
- Reusing the exact `child_id` in a later turn reuses that child's context.
- A new `child_id` creates a fresh child context.
- `task` is a precise, self-contained English briefing.
- `task` carries any file scope, read-only instruction, output format, and
  constraints needed by the child.
- `arguments` must not contain `subtask`, `subtasks`, `name`, `may_write`,
  `reserved_files`, `max_parallel`, or arbitrary extra fields.

### 5.3 Multiple children

The manager emits one canonical array containing one call per child:

```json
[
  {
    "id": "call_1",
    "type": "function",
    "function": {
      "name": "dispatching_parallel_agent",
      "arguments": {
        "child_id": "poet_a",
        "task": "Compose the first poem and return only the requested format."
      }
    }
  },
  {
    "id": "call_2",
    "type": "function",
    "function": {
      "name": "dispatching_parallel_agent",
      "arguments": {
        "child_id": "poet_b",
        "task": "Compose the second poem and return only the requested format."
      }
    }
  }
]
```

`call_1` and `call_2` identify tool calls. `poet_a` and `poet_b` identify child
contexts. The two namespaces must not be conflated.

## 6. Turn-Parallel Execution Model

### 6.1 One turn, one stage

For `N` real tool calls in one assistant response, the Orchestrator produces one
execution stage containing all `N` calls. It submits every accepted call before
waiting for any result.

There are no static categories and no backend-created serial stages. The
following are deleted or reduced to a single-stage planner:

- `TOOL_CATEGORIES`;
- `READONLY_CATEGORY_NAMES`;
- category lookup helpers;
- serial-stage dependency rules;
- `_execute_serial()` as a multi-call execution strategy.

A single tool call may continue through the parent smolagents path if doing so
preserves identical behavior. Multiple calls always use the turn-parallel path.

### 6.2 Dependency ownership

The manager/basic agent prompt states:

- emit multiple calls in one turn only when they can run concurrently;
- do not emit a call that depends on another call's result in the same turn;
- after observing results, issue dependent calls in a future turn;
- write or execute calls that might conflict must be separated into future
  turns by the LLM.

The backend does not attempt to guess file conflicts or data dependencies.

### 6.3 Concurrency limits

`agent.manager_agent_max_parallel_children` remains the maximum number of
accepted `dispatching_parallel_agent` calls in one manager turn.

Selection is deterministic:

- retain the first configured number of dispatch calls in model output order;
- do not launch excess child calls;
- represent each excess call as `skipped` with a user-configured-limit reason;
- include ordered dropped `call_id` and `child_id` values in the shared
  blackboard;
- do not suppress unrelated non-dispatch tool calls.

All retained calls are submitted concurrently. The generic orchestrator must
not impose a hard-coded five-worker cap that silently serializes a larger turn.
Child dispatch remains bounded by the manager child limit. Normal tools rely on
their own configured/internal resource limits.

### 6.4 Timeout and stop

The old hard-coded 60-second orchestration timeout is removed. It is unsuitable
for child runs and can label queued work as timed out without stopping the
underlying thread.

- Individual tools retain their own timeout policies.
- Child agents retain `manager_agent_child_timeout_seconds`.
- One tool failure does not cancel sibling calls.
- Stop signals all active calls/children through existing abort paths.
- The run scope is not released until executing workers finish cleanup.

## 7. Shared Blackboard

### 7.1 Scope

There is one shared child-agent blackboard per manager LLM turn, not one
blackboard per tool call. It is assembled after all retained
`dispatching_parallel_agent` calls reach terminal state.

The blackboard is an observation projection, not mutable cross-agent memory.
Children do not read one another's reports while running. The manager receives
the completed shared projection in its next model input as the normal tool
observation tail.

### 7.2 Format

```text
Shared child-agent blackboard for this turn.

Call: call_1
Child: poet_a
Status: completed
Final report:
...

---

Call: call_2
Child: poet_b
Status: failed
Error:
...
```

If calls were dropped:

```text
Dispatch limit applied.
Configured maximum: 2
Requested child calls: 4
Executed child calls: 2
Not executed: call_3/poet_c, call_4/poet_d
```

Rules:

- preserve model call order, regardless of completion order;
- include each call ID, child ID, terminal status, and final report/error;
- do not include assigned task text, internal session ID, timeline payloads,
  frontend URLs, or raw child tool traces;
- do not merge normal tool outputs into child reports;
- normal tool outputs appear in a separate observation section;
- each tool call remains represented in `memory_step.tool_calls` so historical
  tool traces remain structured and prefix-cache safe.

### 7.3 Runtime result boundary

Each singular dispatch tool returns a structured internal result to the
Orchestrator containing at least:

- `call_id` supplied by the Orchestrator;
- `child_id`;
- status;
- final report or error;
- child runtime metadata used only for timeline/UI projection.

The user-facing/string blackboard is formatted once at the turn aggregation
boundary. It must not be created independently inside each dispatch invocation.

## 8. Child Identity and Runtime

Child identity remains manager-scoped:

```text
(manager_session.session_id, child_id) -> child_session_id
```

One singular dispatch call performs:

1. validate `child_id` and `task`;
2. resolve or create the child session;
3. create/update the child run record;
4. emit timeline status events;
5. run the child to a terminal state;
6. return an internal child result for turn aggregation.

Different child IDs from the same turn run concurrently. Duplicate child IDs in
one turn are invalid because they would race on one context. Both calls are not
allowed to mutate the same `ChildAgentRun`.

The old per-manager dispatch lock must not serialize distinct child IDs. If a
lock is still needed for identity-map mutation, its critical section is limited
to lookup/create bookkeeping and is never held while a child LLM run executes.

## 9. Parser and Error Handling

The parser accepts the canonical OpenAI tool-call array and preserves all outer
call IDs. Nested-field name conflict workarounds are no longer needed for this
tool because `arguments` contains `child_id`, not `name`.

Validation failures become concise tool observations that tell the manager the
exact canonical shape. The backend does not silently rename fields or convert an
old plural payload.

Required failures include:

- plural tool name;
- missing or blank `child_id`;
- missing or too-short `task`;
- any extra argument field;
- duplicate `child_id` in one turn;
- malformed/non-object `arguments`;
- malformed canonical outer array.

The implementation begins with diagnostic tests covering the user's two real
transcripts. Tests must prove:

1. text parsing yields one dispatch call with the old payload before migration;
2. the old runtime failure boundary is identified and documented in the test or
   implementation notes;
3. equivalent new singular calls parse, execute child stubs, and produce one
   shared blackboard;
4. the same behavior works through `SmartToolCallingAgent.process_tool_calls`,
   not only by calling a private dispatch coroutine directly.

If the exact production failure cannot be reproduced without runtime logs, add
boundary-specific error logging and retain a test that exercises every boundary
with the canonical transcript. Do not claim a unique historical root cause
without evidence.

## 10. Prompt Changes

Manager prompt examples use only the singular schema. They explicitly say:

- one child per `dispatching_parallel_agent` call;
- multiple independent children are multiple calls in one JSON array;
- use `child_id`, never `name`;
- no nested `subtask`/`subtasks` array;
- same-turn calls run concurrently;
- dependent work must wait for a future turn;
- write conflicts must be avoided by separating work into future turns;
- backend config controls the child-call cap;
- wait for the shared blackboard before synthesis.

The general agent prompt must also explain the turn-parallel rule for ordinary
tools. It must not promise backend dependency analysis or serial reordering.

## 11. Events and Frontend Compatibility

Existing orchestration SSE event types remain usable:

- `orchestration_plan` contains exactly one parallel stage for a multi-call turn;
- `orchestration_update` reports each call independently;
- `orchestration_done` reports aggregate counts.

Manager child timeline events continue to use `dispatch_id` for UI grouping, but
all singular dispatch calls in one manager turn share one turn dispatch/group
ID. Each child retains a distinct `child_run_id` and call ID.

Text and vision pages must remain synchronized. Existing projection/UI tests are
updated only where the removal of multiple serial stages changes expected data.
No new frontend protocol is introduced.

## 12. Prefix-Cache Covenant

- Preserve all historical structured assistant tool calls and user-role tool
  observations.
- Append the current turn's shared blackboard as the normal observation tail.
- Do not rewrite earlier tool traces into the new schema.
- Do not inject dispatch IDs, session IDs, timeline state, or UI state into
  system messages or historical prompt sections.
- Child prompts remain child system/runtime/tool prefix plus child canonical
  memory and the new task tail.
- Provider adapters may change transport representation, not logical messages.

## 13. Testing Strategy

### Backend unit and integration tests

- singular tool schema exposes exactly `child_id` and `task`;
- plural tool is absent from prompt and schema;
- removed fields are rejected;
- user's transcript shapes are retained as regression fixtures;
- multiple singular dispatch calls parse with stable call IDs;
- all multi-tool calls start before any sibling is allowed to complete;
- mixed normal and dispatch calls execute concurrently;
- no dependency categories or serial stages remain;
- duplicate same-turn child IDs fail before child launch;
- distinct child IDs run concurrently without a manager-wide execution lock;
- dispatch cap skips only excess dispatch calls and reports them in order;
- child reports are aggregated into one ordered shared blackboard;
- normal tool observations remain separate;
- one failure does not cancel successful siblings;
- child timeout and stop cleanup remain correct;
- memory keeps all structured tool calls;
- no hard-coded orchestrator global timeout remains.

### Frontend tests

- a multi-call plan renders as one parallel stage;
- manager child cards group singular calls from one turn correctly;
- status updates remain keyed by child run/call identity;
- text and vision behavior stays consistent.

### Manual acceptance test

Ask the local manager LLM to create two independent poems. Verify that it emits
two singular canonical calls, both child runs enter `running` before either
finishes, the UI shows both, and the manager receives one shared blackboard with
both reports before synthesizing its answer.

## 14. Acceptance Criteria

- No prompt, schema, runtime, or active test uses `dispatching_parallel_agents`.
- No active dispatch contract contains `subtask`, `subtasks`, `may_write`, or
  `reserved_files`.
- One singular call launches one child.
- Multiple singular calls in one turn launch distinct children concurrently.
- Every multi-tool turn has exactly one parallel execution stage.
- The backend does not infer or impose serial dependencies.
- Multiple child reports from one turn appear in one ordered shared blackboard.
- Dependent sequencing is documented as an LLM future-turn responsibility.
- The user's canonical failure scenario has an integration-level regression
  test through parser and `process_tool_calls`.
- Stop, child timeout, timeline replay, structured memory, and prefix-cache tests
  pass.

## 15. Expected Implementation Areas

- `backend/agent/orchestrator.py`
- `backend/agent/agent_runner.py`
- `backend/agent/local_llm_model.py`
- `config/config-agent-prompts.yaml`
- `backend/config.py` and `config/config-misc.yaml` only if semantics need text
  clarification; the existing child limit remains
- `tests/test_orchestrator.py`
- `tests/test_manager_agent_dispatch.py`
- `tests/test_multi_tool_parsing.py`
- relevant session/stop/prefix-cache tests
- frontend projection tests if the one-stage plan changes fixtures
- `AGENTS.md` after implementation behavior is verified

## 16. Multi-Tool Manager Card Projection Amendment

The generic orchestration stage is not the manager-child UI. In a multi-tool
turn, `dispatching_parallel_agent` calls are nested under
`orchestration_plan.stages[].tools[]`, so the frontend must not assume that each
dispatch also appears as a top-level `agent_step` with `step_type=tool_call`.

The stable join key is the canonical outer `call_id`:

1. Backend `manager_child_agent_update` events include the child call's
   `call_id` (already available from the canonical tool call).
2. `TimelineEvent`, `TimelineProjection`, and `ManagerParallelChildView`
   preserve `call_id` as `callId`.
3. Rendering examines each orchestration plan step's
   `stages[].tools[].call_id` values and attaches the matching manager dispatch
   card immediately after that orchestration step.
4. All child calls sharing one backend `dispatch_id` render in one
   `ManagerParallelDispatchCard`; each row keeps its own `child_run_id`, `href`,
   status, and `Open child` action.
5. `Open child` continues to use `window.open(href, '_blank',
   'noopener,noreferrer')`, opening `/agent/child/[childRunId]`, whose existing
   replay-then-live stream shows the child conversation in real time.

The old ordinal join (the Nth dispatch card paired with the Nth top-level tool
call step) is removed. It is not robust when orchestration changes event order,
when normal and dispatch tools are mixed, or when a turn contains only a single
orchestration step.

The generic `OrchestrationView` filters `dispatching_parallel_agent` calls:

- a dispatch-only stage does not display `Stage 0 (parallel)`;
- a mixed stage continues to display ordinary tools under the generic stage;
- the dedicated manager card represents child dispatch calls without duplicate
  rendering.

Required regressions cover both text and vision pages, event-before-plan and
plan-before-event arrival, multiple child calls sharing one dispatch card,
mixed normal/dispatch stages, the new-tab link, and preservation of child
replay/live behavior.

## 17. Child-Scoped Abort and Context Disposal Amendment

The standalone child page provides an `Abort child` action while the current
child run is pending or running. The action requires explicit confirmation and
targets only that `child_run_id`; the manager and sibling children continue.

Abort lifecycle:

1. Capture the child session's active run scopes and current upstream request
   ID before changing abort state.
2. Mark only the child session/run aborted and perform provider-neutral targeted
   request cancellation. SGLang may additionally receive targeted
   `/abort_request`; no child abort may fall back to abort-all.
3. Wait until the captured run scopes finish rollback, request cleanup, native
   worker cleanup, and `finish_run()`.
4. Let the original dispatch worker produce the terminal `aborted` outcome. Do
   not synthesize a competing completion path in the API endpoint.
5. Return this tool Observation in the manager's shared blackboard:

   ```text
   Status: aborted
   Error:
   The user has aborted this child agent run.
   ```

6. After the barrier, delete the child's `AgentSession` and canonical memory and
   remove the `(manager_session_id, child_id) -> child_session_id` mapping.
   This is an explicit user-requested disposal boundary: dispatching the same
   child ID later creates a fresh context.

The terminal `ChildAgentRun` metadata and bounded replay buffer remain available
for the already-open child transcript page. They are audit/UI records, not LLM
context. Normal TTL cleanup later removes orphaned child metadata and buffers.

### Replay Buffer Anchors

The event cap remains a required host-memory safety boundary. When truncating,
the buffer preserves the latest turn's `manager_assigned_task` anchor, a
truncation marker, and the newest events fitting the configured limit. A runaway
stream therefore cannot erase the task that explains the visible activity.
Terminal events must remain visible.

### Context and Memory Diagnostics

`manager_assigned_task` may expose runtime-only diagnostics to the child page:
whether the context was fresh/reused and prior memory step count. These fields
must not enter any model prompt or manager blackboard. They distinguish model
generation degeneration from intentionally reused historical context.

SGLang/vLLM KV prefix cache is not treated as semantic shared memory. Cache
pressure may affect latency, eviction, or OOM behavior, but there is no evidence
that it caused coherent rhyme-list generation. Confirmed repository memory
leaks must still be fixed: child metadata, event buffers, identity mappings, and
subscribers are cleaned when their backing sessions expire and no run is active.
