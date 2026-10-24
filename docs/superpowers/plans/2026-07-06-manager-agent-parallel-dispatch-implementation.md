# Manager Agent Parallel Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `manager_agent` as the default local assistant manager that can launch non-blocking child `basic_agent` sessions, expose child replay/live streams in new tabs, and synthesize child final reports through a shared blackboard.

**Architecture:** Add `manager_agent` as a normal agent profile with a strict read-only + dispatch tool allowlist. Implement `dispatching_parallel_agents` inside backend agent runtime so it validates child tasks, creates fresh `basic_agent` sessions, records per-child event buffers, runs children independently of UI subscribers, and returns a blackboard observation only after all children finish. Extend frontend timeline projection with a dedicated manager dispatch card and add a read-only child transcript route that replays buffered events then follows live events.

**Tech Stack:** Python/FastAPI/smolagents backend, existing `AgentRunner` runtime, Next.js 14/React/Tailwind frontend, Node `node:test` frontend unit tests, pytest backend tests.

**Authoritative Spec:** `docs/superpowers/specs/2026-07-06-manager-agent-parallel-dispatch-design.md`

**Repository Constraints:** Do not use git commands without explicit user permission. Do not create worktrees. Do not commit. Use TDD: write failing tests first, run them, implement, rerun. Use `apply_patch` for manual edits.

---

## File Structure

- Modify `config/config-misc.yaml`: set `agent.entry_agent_name: manager_agent`, add manager caps, include `manager_agent` in enabled profiles.
- Modify `config/config-agent-prompts.yaml`: add `manager_agent` prompt profile. Do not change `coordinator_1` routing choices in this implementation unless a focused test is added for that routing behavior.
- Modify `backend/config.py`: add manager config fields and `AgentPromptsConfig.manager_agent`.
- Modify `backend/agent/agent_runner.py`: profile allowlist, manager tool construction, child run metadata, child event buffers, dispatch validation, child background execution, blackboard assembly, child stream generator.
- Modify `backend/api/agent.py`: add `/v1/agent/child/stream` SSE endpoint.
- Modify `tests/test_multi_agent_config.py`: config/prompt parsing tests.
- Modify or add backend tests, preferably `tests/test_manager_agent_dispatch.py`: manager allowlist, validation, child session isolation, event replay, blackboard behavior.
- Modify `frontend/src/lib/api.ts`: manager child event types and `childAgentStream` client.
- Modify `frontend/src/lib/timeline-projection.ts`: dedicated manager dispatch projection.
- Modify `frontend/src/lib/timeline-projection.test.mts` and `frontend/src/lib/api.test.mts`: frontend contract tests.
- Add `frontend/src/components/ManagerParallelDispatchCard.tsx`: manager child card renderer.
- Modify `frontend/src/app/page.tsx` and `frontend/src/app/vision/page.tsx`: wire manager projection into both pages.
- Add `frontend/src/app/agent/child/[childRunId]/page.tsx`: child transcript viewer route.

---

### Task 1: Config And Prompt Profile

**Files:**
- Modify: `backend/config.py`
- Modify: `config/config-misc.yaml`
- Modify: `config/config-agent-prompts.yaml`
- Modify: `tests/test_multi_agent_config.py`
- Modify: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Write failing config tests**

Add tests asserting manager defaults and prompt parsing. The expected failure before implementation is that `manager_agent` is not a dataclass field/default profile and real config still uses `basic_agent`.

```python
def test_parse_agent_config_exposes_manager_agent_defaults():
    loader = TestParseAgentConfig()._make_loader_with_data({})

    cfg = loader._parse_agent_config()

    assert cfg.entry_agent_name == "manager_agent"
    assert cfg.enabled_agent_profiles == [
        "coordinator_1",
        "coordinator_2",
        "manager_agent",
        "basic_agent",
    ]
    assert cfg.manager_agent_max_child_agents_per_dispatch == 6
    assert cfg.manager_agent_default_max_parallel_children == 3
    assert cfg.manager_agent_max_parallel_children == 4
    assert cfg.manager_agent_child_event_buffer_limit == 5000


def test_parse_agent_config_preserves_manager_agent_overrides():
    loader = TestParseAgentConfig()._make_loader_with_data(
        {
            "entry_agent_name": "manager_agent",
            "enabled_agent_profiles": ["manager_agent", "basic_agent"],
            "manager_agent_max_child_agents_per_dispatch": 2,
            "manager_agent_default_max_parallel_children": 1,
            "manager_agent_max_parallel_children": 2,
            "manager_agent_child_event_buffer_limit": 123,
        }
    )

    cfg = loader._parse_agent_config()

    assert cfg.entry_agent_name == "manager_agent"
    assert cfg.enabled_agent_profiles == ["manager_agent", "basic_agent"]
    assert cfg.manager_agent_max_child_agents_per_dispatch == 2
    assert cfg.manager_agent_default_max_parallel_children == 1
    assert cfg.manager_agent_max_parallel_children == 2
    assert cfg.manager_agent_child_event_buffer_limit == 123
```

Extend prompt parsing test:

```python
def test_parse_multi_agent_prompt_profiles_includes_manager_agent(self):
    loader = self._make_loader_with_prompt_data(
        {
            "manager_agent": {"instructions": "manage", "prompt_templates": {}},
            "basic_agent": {"instructions": "basic", "prompt_templates": {}},
        }
    )

    cfg = loader._parse_agent_prompts_config()

    assert cfg.manager_agent["instructions"] == "manage"
```

Update `tests/test_multi_agent_runner.py::test_config_uses_supported_enabled_entry_profile` to allow/assert `manager_agent`:

```python
assert config.agent.entry_agent_name == "manager_agent"
assert config.agent.entry_agent_name in config.agent.enabled_agent_profiles
assert "manager_agent" in config.agent.enabled_agent_profiles
assert "basic_agent" in config.agent.enabled_agent_profiles
```

- [ ] **Step 2: Run failing tests**

Run:

`./.sglang/bin/python -m pytest tests/test_multi_agent_config.py tests/test_multi_agent_runner.py::test_config_uses_supported_enabled_entry_profile -q`

Expected: FAIL because manager config fields/profile defaults do not exist or config still defaults to `basic_agent`.

- [ ] **Step 3: Implement config changes**

In `backend/config.py`:

- Change `AgentConfig.entry_agent_name` default to `"manager_agent"`.
- Change `enabled_agent_profiles` default factory to `["coordinator_1", "coordinator_2", "manager_agent", "basic_agent"]`.
- Add fields:

```python
manager_agent_max_child_agents_per_dispatch: int = 6
manager_agent_default_max_parallel_children: int = 3
manager_agent_max_parallel_children: int = 4
manager_agent_child_event_buffer_limit: int = 5000
```

- Add `manager_agent: dict = field(default_factory=dict)` to `AgentPromptsConfig`.
- Update `_parse_agent_config()` defaults and constructor args.
- Update `_parse_agent_prompts_config()` to treat `manager_agent` as known profile.

In `config/config-misc.yaml`:

```yaml
  entry_agent_name: manager_agent
  enabled_agent_profiles: [coordinator_1, coordinator_2, manager_agent, basic_agent]
  manager_agent_max_child_agents_per_dispatch: 6
  manager_agent_default_max_parallel_children: 3
  manager_agent_max_parallel_children: 4
  manager_agent_child_event_buffer_limit: 5000
```

In `config/config-agent-prompts.yaml`, add `manager_agent` before `basic_agent`. The prompt must be English and include the spec constraints:

```yaml
manager_agent:
  instructions: |-
    Your internal profile name is `manager_agent`. Do not reveal this internal name to the user.
    You are a manager for large parallel local-assistant tasks. You may use read-only tools yourself, and you may dispatch one or more child `basic_agent` workers with `dispatching_parallel_agents`.
  prompt_templates:
    system_prompt: |-
      {%- if custom_instructions %}
      {{custom_instructions}}

      {%- endif %}

      You are a manager, not a router and not a direct executor.

      Responsibilities:
      - Understand the user's goal.
      - Optionally gather broad read-only context with your read-only tools.
      - Decide whether the task benefits from one or more fresh child context windows.
      - Dispatch only `basic_agent` children using `dispatching_parallel_agents`.
      - Write every child task briefing in precise, detailed, self-contained English.
      - Remember that each child has a brand-new context window and has not seen this conversation.
      - For any child that may create, edit, or overwrite files, set `may_write=true` and provide exclusive `reserved_files`.
      - Never assign the same writable file to multiple children in the same dispatch.
      - For read-only children, set `may_write=false` and explicitly tell them not to write files.
      - Wait for the blackboard observation. Do not predict child results.
      - After child reports arrive, read and synthesize them yourself before deciding the next step.

      You do not directly write files, execute shell commands, or operate the browser. Use child `basic_agent` workers for those actions.
      Never dispatch another manager. Nested management is forbidden.
```

- [ ] **Step 4: Run tests to pass**

Run:

`./.sglang/bin/python -m pytest tests/test_multi_agent_config.py tests/test_multi_agent_runner.py::test_config_uses_supported_enabled_entry_profile -q`

Expected: PASS.

---

### Task 2: Backend Manager Dispatch Runtime Foundations

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Add: `tests/test_manager_agent_dispatch.py`

- [ ] **Step 1: Write failing validation and allowlist tests**

Create `tests/test_manager_agent_dispatch.py` with tests that do not call the real LLM. Use `MagicMock` runner construction.

Required tests:

```python
def test_manager_agent_tool_allowlist_is_read_only_plus_dispatch():
    from backend.agent.agent_runner import PROFILE_TOOL_ALLOWLIST

    assert PROFILE_TOOL_ALLOWLIST["manager_agent"] == {
        "read_file",
        "search_and_fetch",
        "web_fetch",
        "grep",
        "glob",
        "list_skills",
        "read_skill",
        "dispatching_parallel_agents",
    }


def test_manager_agent_uses_runtime_time_context():
    from backend.agent.agent_runner import _profile_uses_runtime_time_context

    assert _profile_uses_runtime_time_context("manager_agent") is True


def test_dispatch_validation_rejects_duplicate_child_names():
    runner = _make_manager_runner()
    with pytest.raises(ValueError, match="duplicate child name"):
        runner._validate_manager_dispatch_subtasks(
            [
                {"name": "news-agent", "task": "Read source A and report findings.", "may_write": False},
                {"name": "news agent", "task": "Read source B and report findings.", "may_write": False},
            ],
            max_parallel=None,
        )


def test_dispatch_validation_rejects_write_task_without_reserved_files():
    runner = _make_manager_runner()
    with pytest.raises(ValueError, match="reserved_files"):
        runner._validate_manager_dispatch_subtasks(
            [{"name": "writer", "task": "Write a report to the assigned file.", "may_write": True}],
            max_parallel=None,
        )


def test_dispatch_validation_rejects_duplicate_reserved_files():
    runner = _make_manager_runner()
    with pytest.raises(ValueError, match="reserved file"):
        runner._validate_manager_dispatch_subtasks(
            [
                {"name": "writer-a", "task": "Write section A.", "may_write": True, "reserved_files": ["/tmp/report.md"]},
                {"name": "writer-b", "task": "Write section B.", "may_write": True, "reserved_files": ["/tmp/../tmp/report.md"]},
            ],
            max_parallel=2,
        )


def test_dispatch_validation_caps_parallelism():
    runner = _make_manager_runner()
    validated, parallel = runner._validate_manager_dispatch_subtasks(
        [
            {"name": "a", "task": "Read source A and report findings.", "may_write": False},
            {"name": "b", "task": "Read source B and report findings.", "may_write": False},
            {"name": "c", "task": "Read source C and report findings.", "may_write": False},
        ],
        max_parallel=99,
    )
    assert len(validated) == 3
    assert parallel == runner.config.agent.manager_agent_max_parallel_children
```

Helper:

```python
def _make_manager_runner() -> AgentRunner:
    config = deepcopy(get_config())
    config.model.model_type = "text"
    config.agent.entry_agent_name = "manager_agent"
    config.agent.enabled_agent_profiles = ["manager_agent", "basic_agent"]
    config.agent.manager_agent_max_child_agents_per_dispatch = 6
    config.agent.manager_agent_default_max_parallel_children = 3
    config.agent.manager_agent_max_parallel_children = 4
    config.agent.manager_agent_child_event_buffer_limit = 5000
    return AgentRunner(MagicMock(), config)
```

- [ ] **Step 2: Run failing tests**

Run:

`./.sglang/bin/python -m pytest tests/test_manager_agent_dispatch.py -q`

Expected: FAIL because allowlist/profile helpers and validation methods do not exist.

- [ ] **Step 3: Implement foundations in `agent_runner.py`**

Add only imports used by the implementation, such as `re` for name normalization and any typing/dataclass imports that are referenced by new code.

Add dataclasses near `AgentSession`:

```python
@dataclass
class ManagerDispatchSubtask:
    name: str
    task: str
    may_write: bool = False
    reserved_files: list[str] = field(default_factory=list)


@dataclass
class ChildAgentRun:
    child_run_id: str
    manager_session_id: str
    dispatch_id: str
    child_session_id: str
    child_name: str
    profile_name: str
    assigned_task: str
    may_write: bool
    reserved_files: list[str]
    status: str = "pending"
    final_report: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
```

Update `PROFILE_TOOL_ALLOWLIST`:

```python
"manager_agent": {
    "read_file",
    "search_and_fetch",
    "web_fetch",
    "grep",
    "glob",
    "list_skills",
    "read_skill",
    "dispatching_parallel_agents",
},
```

Update `_profile_uses_runtime_time_context()` to return true for `manager_agent`.

Add `AgentRunner.__init__` state:

```python
self.child_agent_runs: dict[str, ChildAgentRun] = {}
self.child_agent_event_buffers: dict[str, list[dict[str, Any]]] = {}
self.child_agent_subscribers: dict[str, list[asyncio.Queue[dict[str, Any] | None]]] = {}
self._child_agent_lock = threading.Lock()
```

Add helper methods:

```python
def _normalize_child_agent_name(self, name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return normalized

def _normalize_reserved_file_path(self, path_value: str) -> str:
    return str(Path(path_value).expanduser().resolve())

def _validate_manager_dispatch_subtasks(self, subtasks: list[dict[str, Any]], max_parallel: int | None) -> tuple[list[ManagerDispatchSubtask], int]:
    ...
```

Validation rules must match the spec: non-empty subtasks, cap count, unique normalized names, task length at least 10 non-space chars, `may_write=true` requires non-empty `reserved_files`, duplicate normalized reserved paths rejected, max_parallel defaults and caps.

- [ ] **Step 4: Run tests to pass**

Run:

`./.sglang/bin/python -m pytest tests/test_manager_agent_dispatch.py -q`

Expected: PASS.

---

### Task 3: Manager Dispatch Tool And Child Execution

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `tests/test_manager_agent_dispatch.py`

- [ ] **Step 1: Write failing child execution tests**

Add tests with a fake child stream method so no real LLM is called:

```python
@pytest.mark.asyncio
async def test_dispatch_parallel_agents_creates_fresh_basic_child_sessions_and_blackboard():
    runner = _make_manager_runner()
    manager_agent = MagicMock()
    manager_agent.memory.steps = []
    manager_session = AgentSession(
        session_id="manager-session",
        agent=manager_agent,
        mode="tool_calling",
        current_agent_name="manager_agent",
    )
    runner.sessions.put(manager_session)

    async def fake_run_child(child_run, session):
        session.agent.memory.steps.append(f"child-step-{child_run.child_name}")
        return f"final report from {child_run.child_name}"

    runner._run_child_agent_to_final_report = fake_run_child

    result = await runner._dispatching_parallel_agents_for_session(
        manager_session,
        [
            {"name": "news-a", "task": "Read source A and report findings.", "may_write": False},
            {"name": "news-b", "task": "Read source B and report findings.", "may_write": False},
        ],
        max_parallel=2,
    )

    assert "Parallel child agents completed." in result
    assert "Child: news-a" in result
    assert "final report from news-a" in result
    assert "Child: news-b" in result
    assert "final report from news-b" in result
    assert "session_id" not in result.lower()
    child_runs = list(runner.child_agent_runs.values())
    assert {run.child_name for run in child_runs} == {"news-a", "news-b"}
    assert all(run.profile_name == "basic_agent" for run in child_runs)
    assert all(run.child_session_id != manager_session.session_id for run in child_runs)
    assert manager_agent.memory.steps == []


@pytest.mark.asyncio
async def test_dispatch_parallel_agents_buffers_child_events_for_replay():
    runner = _make_manager_runner()
    manager_session = AgentSession(
        session_id="manager-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="manager_agent",
    )
    runner.sessions.put(manager_session)

    async def fake_run_child(child_run, session):
        runner._record_child_agent_event(child_run.child_run_id, {"type": "content", "content": "hello"})
        return "child done"

    runner._run_child_agent_to_final_report = fake_run_child
    await runner._dispatching_parallel_agents_for_session(
        manager_session,
        [{"name": "reader", "task": "Read a source and report findings.", "may_write": False}],
        max_parallel=1,
    )

    child_run_id = next(iter(runner.child_agent_runs))
    events = runner.child_agent_event_buffers[child_run_id]
    assert any(event.get("type") == "manager_assigned_task" for event in events)
    assert any(event.get("type") == "content" and event.get("content") == "hello" for event in events)
    assert events[-1]["type"] == "done"
```

- [ ] **Step 2: Run failing tests**

Run:

`./.sglang/bin/python -m pytest tests/test_manager_agent_dispatch.py -q`

Expected: FAIL because dispatch execution methods do not exist.

- [ ] **Step 3: Implement dispatch tool and child execution**

In `_build_profile_agent()`, when `profile_name == "manager_agent"`, include a tool built by `_build_dispatching_parallel_agents_tool(session)`.

Implementation pattern:

```python
def _build_dispatching_parallel_agents_tool(self, session: AgentSession | None):
    runner = self
    class DispatchingParallelAgentsTool(Tool):
        name = "dispatching_parallel_agents"
        description = "Dispatch one or more fresh basic_agent child sessions for parallel work."
        inputs = {...}
        output_type = "string"
        def forward(self, subtasks: list[dict[str, Any]], max_parallel: int | None = None) -> str:
            if session is None:
                raise ValueError("manager session is required")
            return runner._run_async_blocking(
                runner._dispatching_parallel_agents_for_session(session, subtasks, max_parallel)
            )
    return DispatchingParallelAgentsTool()
```

If the codebase lacks `_run_async_blocking`, add a safe helper that runs a coroutine from the worker thread used by smolagents. Do not call `asyncio.run()` inside an already-running event loop.

Add `_dispatching_parallel_agents_for_session()`:

- Validate subtasks.
- Create one `ChildAgentRun` per subtask.
- Create child session with `basic_agent` profile and new `session_id`.
- Put child sessions in `self.sessions`.
- Record `manager_assigned_task` into child event buffer.
- Emit manager timeline events through an event sink if available; at minimum append to `manager_session.timeline_events`.
- Run children concurrently using `asyncio.Semaphore(parallel)` and `asyncio.gather(..., return_exceptions=True)`.
- Return `_format_manager_blackboard(child_runs)`.

Add `_run_child_agent_to_final_report()`:

- Use existing `_run_basic_stream(child_session, assigned_task, pil_images=None, enable_thinking=None)`.
- Parse streamed SSE strings.
- Record each child event in `_record_child_agent_event(child_run_id, payload)`.
- Accumulate `content` chunks as final report.
- Mark failed on `error`, aborted on `aborted`, completed on `done`.

Add event helpers:

```python
def _record_child_agent_event(self, child_run_id: str, event: dict[str, Any]) -> None:
    with self._child_agent_lock:
        buffer = self.child_agent_event_buffers.setdefault(child_run_id, [])
        buffer.append({"seq": len(buffer), **event})
        limit = self.config.agent.manager_agent_child_event_buffer_limit
        if len(buffer) > limit:
            del buffer[: len(buffer) - limit]
        subscribers = list(self.child_agent_subscribers.get(child_run_id, []))
    for subscriber in subscribers:
        subscriber.put_nowait(buffer[-1])
```

Ensure the blackboard format excludes `session_id`, URLs and raw child tool traces.

- [ ] **Step 4: Run backend tests**

Run:

`./.sglang/bin/python -m pytest tests/test_manager_agent_dispatch.py tests/test_multi_agent_config.py -q`

Expected: PASS.

---

### Task 4: Child Replay/Live Stream API

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `backend/api/agent.py`
- Add/modify: `tests/test_agent_stop_api.py` or `tests/test_manager_agent_dispatch.py`

- [ ] **Step 1: Write failing child stream tests**

Add tests for runner-level replay/live generator:

```python
@pytest.mark.asyncio
async def test_stream_child_agent_events_replays_completed_buffer():
    runner = _make_manager_runner()
    child_run_id = "child-reader-1"
    runner.child_agent_runs[child_run_id] = ChildAgentRun(
        child_run_id=child_run_id,
        manager_session_id="manager-session",
        dispatch_id="dispatch-1",
        child_session_id="child-session",
        child_name="reader",
        profile_name="basic_agent",
        assigned_task="Read a source and report findings.",
        may_write=False,
        reserved_files=[],
        status="completed",
    )
    runner.child_agent_event_buffers[child_run_id] = [
        {"seq": 0, "type": "manager_assigned_task", "task": "Read a source and report findings."},
        {"seq": 1, "type": "content", "content": "done"},
        {"seq": 2, "type": "done", "session_id": "child-session"},
    ]

    events = []
    async for event in runner.stream_child_agent_events(child_run_id):
        events.append(event)

    assert any('"type": "manager_assigned_task"' in event for event in events)
    assert any('"content": "done"' in event for event in events)
    assert events[-1].startswith("data: ")


@pytest.mark.asyncio
async def test_stream_child_agent_events_rejects_unknown_child():
    runner = _make_manager_runner()
    with pytest.raises(KeyError):
        events = runner.stream_child_agent_events("missing-child")
        await events.__anext__()
```

- [ ] **Step 2: Run failing tests**

Run:

`./.sglang/bin/python -m pytest tests/test_manager_agent_dispatch.py -q`

Expected: FAIL because `stream_child_agent_events` does not exist.

- [ ] **Step 3: Implement stream endpoint**

In `AgentRunner` add:

```python
async def stream_child_agent_events(self, child_run_id: str) -> AsyncGenerator[str, None]:
    with self._child_agent_lock:
        child_run = self.child_agent_runs.get(child_run_id)
        if child_run is None:
            raise KeyError(child_run_id)
        replay = list(self.child_agent_event_buffers.get(child_run_id, []))
        is_terminal = child_run.status in {"completed", "failed", "aborted"}
        subscriber: asyncio.Queue[dict[str, Any] | None] | None = None
        if not is_terminal:
            subscriber = asyncio.Queue()
            self.child_agent_subscribers.setdefault(child_run_id, []).append(subscriber)
    try:
        for payload in replay:
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        if is_terminal:
            return
        assert subscriber is not None
        while True:
            payload = await subscriber.get()
            if payload is None:
                return
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if payload.get("type") in {"done", "error", "aborted"}:
                return
    finally:
        if subscriber is not None:
            with self._child_agent_lock:
                subscribers = self.child_agent_subscribers.get(child_run_id, [])
                if subscriber in subscribers:
                    subscribers.remove(subscriber)
```

In `backend/api/agent.py` add:

```python
@router.get("/child/stream")
async def child_agent_stream(child_run_id: str, raw_request: Request):
    agent_runner = raw_request.app.state.agent_runner
    if agent_runner is None:
        raise HTTPException(status_code=503, detail="Agent 服务未启用")
    try:
        stream = agent_runner.stream_child_agent_events(child_run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Child agent run not found") from exc
    return StreamingResponse(stream, media_type="text/event-stream", headers={...})
```

If `KeyError` occurs only when the generator starts, wrap the generator in an inner async generator that catches and emits/raises before response construction.

- [ ] **Step 4: Run backend tests**

Run:

`./.sglang/bin/python -m pytest tests/test_manager_agent_dispatch.py tests/test_agent_stop_api.py -q`

Expected: PASS.

---

### Task 5: Frontend Types, Client, And Projection

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/timeline-projection.ts`
- Modify: `frontend/src/lib/api.test.mts`
- Modify: `frontend/src/lib/timeline-projection.test.mts`

- [ ] **Step 1: Write failing frontend tests**

In `timeline-projection.test.mts` add:

```ts
test('manager child update becomes dedicated manager dispatch projection', () => {
  const result = projectTimelineEvent({
    kind: 'manager_child_agent_update',
    source: 'manager_agent',
    dispatch_id: 'dispatch-1',
    child_run_id: 'child-1',
    child_name: 'news-reading-agent',
    status: 'running',
    assigned_task: 'Read source A and report findings.',
    href: '/agent/child/child-1',
  });

  assert.deepEqual(result, {
    kind: 'manager_child_agent_update',
    dispatchId: 'dispatch-1',
    childRunId: 'child-1',
    childName: 'news-reading-agent',
    status: 'running',
    assignedTask: 'Read source A and report findings.',
    href: '/agent/child/child-1',
    finalPreview: undefined,
    error: undefined,
  });
});
```

In `api.test.mts` add a test for `childAgentStream` replay parsing:

```ts
test('childAgentStream reads child replay and live SSE events', async (t) => {
  const client = new LLMClient('/api/backend');
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const seenUrls: string[] = [];
  globalThis.fetch = async (url) => {
    seenUrls.push(String(url));
    const encoder = new TextEncoder();
    return new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: 'manager_assigned_task', task: 'Read source A.' })}\n\n`));
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: 'content', content: 'done' })}\n\n`));
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: 'done', session_id: 'child-session' })}\n\n`));
        controller.close();
      },
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  };

  const events: any[] = [];
  await client.childAgentStream('child-1', (event) => events.push(event));

  assert.equal(seenUrls[0], '/api/backend/v1/agent/child/stream?child_run_id=child-1');
  assert.deepEqual(events.map((event) => event.type), ['manager_assigned_task', 'content', 'done']);
});
```

- [ ] **Step 2: Run failing frontend tests**

Run:

`cd frontend && npm test -- --runTestsByPath src/lib/timeline-projection.test.mts src/lib/api.test.mts`

If the project uses direct Node tests instead of `npm test`, use the existing package script shown in `frontend/package.json`.

Expected: FAIL because projection/client additions do not exist.

- [ ] **Step 3: Implement types and client**

In `timeline-projection.ts`, add:

```ts
export type ManagerChildStatus = 'pending' | 'running' | 'completed' | 'failed' | 'aborted';
```

Extend `TimelineProjection` union with:

```ts
| {
    kind: 'manager_child_agent_update';
    dispatchId: string;
    childRunId: string;
    childName: string;
    status: ManagerChildStatus;
    assignedTask?: string;
    href?: string;
    finalPreview?: string;
    error?: string;
  }
```

Add projection branch for `event.kind === 'manager_child_agent_update'`.

In `api.ts`, add event interface and method:

```ts
export interface ChildAgentStreamEvent {
  type: string;
  [key: string]: unknown;
}

async childAgentStream(
  childRunId: string,
  onEvent: (event: ChildAgentStreamEvent) => void,
  onError?: (error: Error) => void,
  onComplete?: () => void,
): Promise<void> { ... }
```

The method should use `fetch(`${this.baseUrl}/v1/agent/child/stream?child_run_id=${encodeURIComponent(childRunId)}`)` and parse SSE lines like `agentChatStream`.

- [ ] **Step 4: Run frontend tests**

Run:

`cd frontend && npm test -- --runTestsByPath src/lib/timeline-projection.test.mts src/lib/api.test.mts`

Expected: PASS.

---

### Task 6: Manager Dispatch Card And Page Integration

**Files:**
- Add: `frontend/src/components/ManagerParallelDispatchCard.tsx`
- Modify: `frontend/src/lib/message-types.ts` if a message field type is needed
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Modify: `frontend/src/lib/timeline-projection.test.mts`

- [ ] **Step 1: Write failing source-level tests**

Add tests to `timeline-projection.test.mts` that pin text/vision integration without requiring browser rendering:

```ts
test('text and vision pages render manager parallel dispatch card from projection', async () => {
  const [textPageSource, visionPageSource, componentSource] = await Promise.all([
    readFile(path.join(testDir, '../app/page.tsx'), 'utf8'),
    readFile(path.join(testDir, '../app/vision/page.tsx'), 'utf8'),
    readFile(path.join(testDir, '../components/ManagerParallelDispatchCard.tsx'), 'utf8'),
  ]);

  assert.match(textPageSource, /manager_child_agent_update/);
  assert.match(visionPageSource, /manager_child_agent_update/);
  assert.match(textPageSource, /ManagerParallelDispatchCard/);
  assert.match(visionPageSource, /ManagerParallelDispatchCard/);
  assert.match(componentSource, /window\.open\(href, '_blank', 'noopener,noreferrer'\)/);
  assert.match(componentSource, /childRunId/);
});
```

- [ ] **Step 2: Run failing test**

Run:

`cd frontend && npm test -- --runTestsByPath src/lib/timeline-projection.test.mts`

Expected: FAIL because component/page integration does not exist.

- [ ] **Step 3: Implement card component**

Create `ManagerParallelDispatchCard.tsx`:

- Props: `children: ManagerChildAgentProjection[]` or a grouped dispatch object.
- Display title `Parallel agents`.
- Render child name, status, assigned task preview, final preview/error.
- Button opens `href` in a new tab via `window.open(href, '_blank', 'noopener,noreferrer')`.
- No nested bullets in user-facing Markdown; UI JSX can use normal layout.

Suggested exported types:

```ts
export interface ManagerParallelChildView {
  dispatchId: string;
  childRunId: string;
  childName: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'aborted';
  assignedTask?: string;
  href?: string;
  finalPreview?: string;
  error?: string;
}
```

- [ ] **Step 4: Integrate text and vision pages**

In both pages:

- Import `ManagerParallelDispatchCard`.
- On `projection.kind === 'manager_child_agent_update'`, merge the child update into a stable assistant message/card keyed by `dispatchId`.
- Do not append one bubble per child update.
- Keep existing routing/secondary handoff logic unchanged.

Minimal acceptable state model:

- Store manager dispatch children in the assistant message as a local field if `Message` type allows extension, or encode as an `agentSteps` dispatch-like step only if the component can render it.
- Prefer explicit local field in message type: `managerDispatches?: Record<string, ManagerParallelChildView[]>`.

- [ ] **Step 5: Run frontend tests**

Run:

`cd frontend && npm test -- --runTestsByPath src/lib/timeline-projection.test.mts`

Expected: PASS.

---

### Task 7: Child Transcript Viewer Route

**Files:**
- Add: `frontend/src/app/agent/child/[childRunId]/page.tsx`
- Modify: `frontend/src/lib/api.test.mts` or add route source test

- [ ] **Step 1: Write failing route source test**

Add a source test that verifies route uses child stream client and renders manager assigned task/content/tool steps:

```ts
test('child agent route subscribes to replay-live child stream', async () => {
  const source = await readFile(path.join(testDir, '../app/agent/child/[childRunId]/page.tsx'), 'utf8');

  assert.match(source, /childAgentStream/);
  assert.match(source, /params\.childRunId/);
  assert.match(source, /manager_assigned_task/);
  assert.match(source, /AgentStepDisplay/);
});
```

- [ ] **Step 2: Run failing test**

Run:

`cd frontend && npm test -- --runTestsByPath src/lib/api.test.mts`

Expected: FAIL because route file does not exist.

- [ ] **Step 3: Implement route**

The page should:

- Be client-side (`'use client'`).
- Read `params.childRunId`.
- Call `llmClient.childAgentStream(childRunId, onEvent, onError, onComplete)` in `useEffect`.
- Render manager task block when event type is `manager_assigned_task`.
- Convert `agent_step` events to `AgentStep[]` and render `AgentStepDisplay`.
- Append `content` event chunks to final answer area.
- Show terminal status on `done`, `error`, `aborted`.
- Abort the fetch on unmount if the client supports it; disconnect must not stop backend child run.

- [ ] **Step 4: Run route test**

Run:

`cd frontend && npm test -- --runTestsByPath src/lib/api.test.mts`

Expected: PASS.

---

### Task 8: End-To-End Verification Pass

**Files:**
- Any files touched above

- [ ] **Step 1: Run focused backend tests**

Run:

`./.sglang/bin/python -m pytest tests/test_multi_agent_config.py tests/test_multi_agent_runner.py::test_config_uses_supported_enabled_entry_profile tests/test_manager_agent_dispatch.py tests/test_agent_stop_api.py -q`

Expected: PASS.

- [ ] **Step 2: Run focused frontend tests**

Run:

`cd frontend && npm test -- --runTestsByPath src/lib/timeline-projection.test.mts src/lib/api.test.mts`

Expected: PASS.

- [ ] **Step 3: Run type/build check if available**

Inspect `frontend/package.json` and run the existing type/build command if it is lightweight enough for this repo. If not available, report that no typecheck script exists.

- [ ] **Step 4: Manual code review checklist**

Confirm:

- No `.worktrees` creation.
- No git commands were used.
- `manager_agent` cannot see write/execute/browser tools.
- Child session creation uses fresh `basic_agent` context and does not copy manager memory.
- `session_id` is not included in blackboard observation text.
- Child tab is replay/live and non-blocking.
- Text and vision pages have matching manager projection glue.
