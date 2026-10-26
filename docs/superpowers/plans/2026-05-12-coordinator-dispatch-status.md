# Coordinator Dispatch Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a user-visible "任务调度" transition card for coordinator routing, stream coordinator lifecycle state to the frontend, keep coordinator reasoning visible in backend terminal logs only, and move routing prompt configuration into YAML.

**Architecture:** Keep the existing `/v1/agent/chat` SSE envelope and extend `agent_step` with a new `dispatch` step type. Make coordinator routing observable as a stream lifecycle inside `AgentRunner.run_stream()`, while rendering a dedicated dispatch card in both text and vision pages. Route prompt text is moved into `config/config-agent-prompts.yaml`, and backend logs print coordinator inference text with readable Chinese output.

**Tech Stack:** Python, FastAPI, smolagents, Next.js 14, React, TypeScript, Tailwind, pytest.

---

### Task 1: Move Coordinator Routing Prompt Into YAML

**Files:**
- Modify: `config/config-agent-prompts.yaml`
- Modify: `backend/agent/agent_runner.py`
- Test: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Write the failing tests**

Add assertions that the routing prompt is rendered from config and carries the short-reasoning constraint:

```python
def test_build_coordinator_turn_message_uses_yaml_template():
    runner = _make_runner()

    prompt = runner._build_coordinator_turn_message("使用浏览器 打开 bing.com")

    assert "Route this user turn." not in prompt
    assert "内部推理文本尽量简短" in prompt
    assert "少于 100 字" in prompt
    assert "Original user task" not in prompt
    assert "使用浏览器 打开 bing.com" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py::test_build_coordinator_turn_message_uses_yaml_template -v`

Expected: FAIL because `_build_coordinator_turn_message()` still returns hardcoded English text.

- [ ] **Step 3: Write minimal implementation**

Add a `routing_turn_prompt` template under `coordinator_agent.prompt_templates` and render it from `agent_runner.py` using the current task text as a template variable.

Core direction:

```python
template = self.config.agent_prompts.coordinator_agent["prompt_templates"]["routing_turn_prompt"]
return template.replace("{{raw_task}}", raw_task)
```

Template content must explicitly require:

```yaml
If you switch, keep internal reasoning text as short as possible, targeting fewer than 100 Chinese characters.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py::test_build_coordinator_turn_message_uses_yaml_template -v`

Expected: PASS

### Task 2: Add Dispatch Step Protocol For Coordinator Stream Lifecycle

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Test: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Write the failing tests**

Add focused stream tests:

```python
def test_run_stream_emits_dispatch_running_before_coordinator_finishes():
    runner = _make_runner()
    session = AgentSession(session_id="dispatch-session", agent=MagicMock(), mode="tool_calling", current_agent_name="coordinator_agent")

    with patch.object(runner, "get_or_create_session", return_value=session), \
         patch.object(runner, "_decode_request_images", return_value=None), \
         patch.object(runner, "_run_coordinator_routing_stream", create=True, return_value=_async_events([
             {"kind": "dispatch", "dispatch_status": "running", "dispatch_stage": "routing"},
             {"kind": "result", "route": {"kind": "final", "answer": "协调器直接回复"}},
         ])):
        events = asyncio.run(_collect_stream_events(runner.run_stream(session.session_id, "你好")))

    payloads = [json.loads(event[6:]) for event in events]
    assert payloads[0]["type"] == "session_start"
    assert payloads[1] == {
        "type": "agent_step",
        "step_type": "dispatch",
        "dispatch_status": "running",
        "dispatch_stage": "routing",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py::test_run_stream_emits_dispatch_running_before_coordinator_finishes -v`

Expected: FAIL because `run_stream()` currently blocks in `_run_coordinator_routing_pass()` and emits no dispatch step.

- [ ] **Step 3: Write minimal implementation**

Refactor coordinator routing into a stream lifecycle helper, for example `_run_coordinator_routing_stream()`, that yields internal state records and a final route result. In `run_stream()`:

```python
yield session_start
async for item in self._run_coordinator_routing_stream(...):
    if item["kind"] == "dispatch":
        yield dispatch_agent_step
    elif item["kind"] == "result":
        route = item["route"]
        break
```

Dispatch payload should stay inside `agent_step` and include `dispatch_status`, `dispatch_stage`, and optional message/target agent.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py -q`

Expected: PASS for new dispatch lifecycle tests and existing coordinator route tests.

### Task 3: Stream Coordinator Internals To Terminal Only

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `backend/agent/local_llm_model.py`
- Test: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Write the failing test**

Add a test that coordinator stream classification can consume aggregated stream output while not requiring frontend thought emission:

```python
def test_classify_coordinator_stream_uses_stream_content_for_switch():
    runner = _make_runner()
    model = MagicMock()
    model.generate_stream.return_value = iter([ChatMessageStreamDelta(content='[{"id":"call_0","type":"agent","function":{"name":"switch_agent","arguments":"basic_agent"}}]')])
    model._last_stream_content = '[{"id":"call_0","type":"agent","function":{"name":"switch_agent","arguments":"basic_agent"}}]'

    result = runner._classify_coordinator_output(ChatMessage(role=MessageRole.ASSISTANT, content=model._last_stream_content, tool_calls=None))
    assert result == {"kind": "switch", "target_agent": "basic_agent"}
```

- [ ] **Step 2: Run test to verify it fails or exposes missing helper boundaries**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py -q`

Expected: FAIL or require a new helper boundary for coordinator stream aggregation.

- [ ] **Step 3: Write minimal implementation**

Implement coordinator stream execution using `generate_stream()` or equivalent streamed model path, but keep emitted deltas confined to backend logs. Log streamed coordinator text directly with readable Chinese output.

Implementation rules:

```python
logger.info(f"[COORDINATOR] {delta_text}")
```

Do not send this delta as frontend thought content. After stream completes, classify the aggregated final content using `_parse_switch_agent_command()` and existing final/direct-answer logic.

- [ ] **Step 4: Run tests to verify it passes**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py tests/test_multi_tool_parsing.py -q`

Expected: PASS

### Task 4: Add Dispatch Step Type To Frontend API And Renderer

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/components/AgentStepDisplay.tsx`

- [ ] **Step 1: Extend frontend step types**

Add dispatch fields to `AgentStep`:

```ts
export interface AgentStep {
  step_type: 'thought' | 'tool_call' | 'observation' | 'orchestration' | 'dispatch';
  dispatch_status?: 'running' | 'completed' | 'error' | 'aborted';
  dispatch_stage?: 'routing' | 'handoff' | 'direct_answer';
  dispatch_target_agent?: string;
  dispatch_message?: string;
}
```

- [ ] **Step 2: Update SSE parsing**

Inside `agentChatStream()`, keep the generic `agent_step` path but preserve dispatch fields when present:

```ts
const step: AgentStep = {
  step_type: json.step_type,
  content: json.content,
  dispatch_status: json.dispatch_status,
  dispatch_stage: json.dispatch_stage,
  dispatch_target_agent: json.dispatch_target_agent,
  dispatch_message: json.dispatch_message,
}
```

- [ ] **Step 3: Add dispatch card renderer**

Add a dispatch branch in `AgentStepDisplay.tsx` instead of reusing thought styling. The card should render:

```tsx
if (step.step_type === 'dispatch') {
  return <DispatchCard step={step} />
}
```

`DispatchCard` should use the confirmed visual direction: dark card, title `任务调度`, orange pulse indicator, text-bounded sweep animation, and lifecycle-specific copy.

- [ ] **Step 4: Manually verify local type/build sanity**

Run: `npm test` if frontend tests exist; otherwise defer to the final `npm run build` or repo-standard frontend verification command during final verification.

Expected: Type errors resolved, renderer compiles.

### Task 5: Wire Dispatch Placeholder Into Text And Vision Pages

**Files:**
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`

- [ ] **Step 1: Insert optimistic dispatch placeholder on send**

When creating the assistant placeholder message, initialize `agentSteps` with one dispatch step:

```ts
agentSteps: [{
  step_type: 'dispatch',
  dispatch_status: 'running',
  dispatch_stage: 'routing',
}]
```

- [ ] **Step 2: Merge real dispatch events instead of duplicating cards**

Update the `onStep` reducer logic in both pages so that if the last step is `dispatch` and the incoming step is also `dispatch`, the last step is replaced or merged instead of appended.

```ts
if (step.step_type === 'dispatch') {
  if (lastStep?.step_type === 'dispatch') {
    lastMessage.agentSteps = [...steps.slice(0, -1), { ...lastStep, ...step }]
  } else {
    lastMessage.agentSteps = [...steps, step]
  }
  return updated
}
```

- [ ] **Step 3: Finalize dispatch when execution or content begins**

When the first non-dispatch step or final answer content arrives, make sure the dispatch step is no longer in `running` state.

```ts
if (lastStep?.step_type === 'dispatch' && lastStep.dispatch_status === 'running') {
  lastStep = { ...lastStep, dispatch_status: 'completed' }
}
```

- [ ] **Step 4: Verify text/vision parity by reading both diffs side by side**

Check that both pages implement the same dispatch placeholder, merge, and finalize logic.

### Task 6: Add Regression Tests For Dispatch Lifecycle

**Files:**
- Modify: `tests/test_multi_agent_runner.py`
- Modify: `tests/test_agent_api_images.py`

- [ ] **Step 1: Add coordinator direct-answer dispatch test**

Expected SSE order:

```python
[
  {"type": "session_start", ...},
  {"type": "agent_step", "step_type": "dispatch", "dispatch_status": "running", "dispatch_stage": "routing"},
  {"type": "agent_step", "step_type": "dispatch", "dispatch_status": "completed", "dispatch_stage": "direct_answer"},
  {"type": "content", "content": "..."},
  {"type": "done", ...},
]
```

- [ ] **Step 2: Add coordinator switch dispatch test**

Expected SSE order begins with session start and dispatch running/completed before entering basic stream thought/tool output.

- [ ] **Step 3: Add error or aborted dispatch test**

Expected SSE contains a dispatch step with `dispatch_status` equal to `error` or `aborted` before terminal error/done or aborted/done events.

- [ ] **Step 4: Add vision-path coverage if stream API image test assumptions change**

Update `tests/test_agent_api_images.py` only if request/stream lifecycle assertions need to account for dispatch-first behavior.

### Task 7: Verify Logging And Run Final Regression Suite

**Files:**
- Modify if needed: `backend/agent/agent_runner.py`
- Modify if needed: `backend/api/agent.py`

- [ ] **Step 1: Audit new JSON SSE/log output for readable Chinese**

Any new `json.dumps()` used in stream responses or structured logging must include `ensure_ascii=False`.

- [ ] **Step 2: Run targeted regression suite**

Run:

```bash
PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest \
  tests/test_multi_agent_runner.py \
  tests/test_multi_tool_parsing.py \
  tests/test_agent_api_images.py \
  tests/test_agent_session_context.py \
  tests/test_get_tools_no_load_images.py -q
```

Expected: all pass.

- [ ] **Step 3: Run broader multi-agent regression suite**

Run:

```bash
PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest \
  tests/test_multi_agent_config.py \
  tests/test_multi_agent_runner.py \
  tests/test_multi_tool_parsing.py \
  tests/test_agent_session_context.py \
  tests/test_get_tools_no_load_images.py \
  tests/test_agent_api_images.py \
  tests/test_agent_stop_api.py \
  tests/test_write_memory_tool_calls.py \
  tests/test_stream_tools_cycle.py \
  tests/test_orchestrator.py -q
```

Expected: all pass.

## Self-Review

- Spec coverage: prompt relocation, dispatch lifecycle, frontend task dispatch card, terminal-only coordinator reasoning, readable Chinese logs, and text/vision parity are all covered by Tasks 1-7.
- Placeholder scan: no TODO/TBD placeholders remain; each task names exact files and explicit verification commands.
- Type consistency: `dispatch` is the single new step type across backend SSE, frontend types, and renderer logic.
