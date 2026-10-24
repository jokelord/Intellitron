# Coordinator_2 Secondary Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `coordinator_2` a real secondary-handoff coordinator that can switch to `basic_agent`, keep its reasoning in chat bubbles without leaking tool-call JSON, and attach downstream tool/observation cards to the correct assistant bubble in both text and vision pages.

**Architecture:** Tighten `coordinator_2` into a reason-or-handoff profile by removing direct tool exposure, explicitly exposing a managed `basic_agent` handoff target, and intercepting the resulting managed-agent tool call in `AgentRunner` so the backend performs a real profile switch instead of treating it as ordinary tool execution. Keep `coordinator_2` visible output on the existing `message_*` timeline path, filter tool-call transcript text before bubble emission, and reuse the existing `AgentStepDisplay` cards by routing `agent_step` updates to the currently active projected assistant bubble.

**Tech Stack:** Python, FastAPI, smolagents, pytest, Next.js 14, React, TypeScript, node:test.

---

### Task 1: Lock Down `coordinator_2` Profile Exposure

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `config/config-agent-prompts.yaml`
- Test: `tests/test_get_tools_no_load_images.py`
- Test: `tests/test_multi_agent_config.py`

- [ ] **Step 1: Write the failing tests**

Add backend/profile tests that describe the intended profile boundary:

```python
def test_build_profile_agent_hides_direct_tools_for_coordinator_2():
    config = deepcopy(get_config())
    config.agent.enabled_agent_profiles = ["coordinator_1", "coordinator_2", "basic_agent"]
    runner = _make_runner(config)

    agent = runner._build_profile_agent("coordinator_2")

    assert list(agent.tools) == []
    assert [tool.name for tool in agent.tools_and_managed_agents] == ["basic_agent"]


def test_coordinator_2_prompt_explicitly_requires_canonical_switch_output():
    cfg = ConfigLoader().load(reload=True)
    prompt = cfg.agent_prompts.coordinator_2["prompt_templates"]["system_prompt"]

    assert 'switch_agent' in prompt
    assert 'Calling tools:' not in prompt
    assert '"arguments":"basic_agent"' in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. "/home/zk/Project/LLM-deployment/.sglang/bin/python" -m pytest tests/test_get_tools_no_load_images.py tests/test_multi_agent_config.py -q`

Expected: FAIL because `coordinator_2` still sees normal tools and its prompt does not contain the stricter switch contract.

- [ ] **Step 3: Write minimal implementation**

Make these production changes:

```python
PROFILE_TOOL_ALLOWLIST = {
    "coordinator_2": set(),
}
```

and in `_build_profile_agent()` pass a single managed agent only for `coordinator_2`:

```python
if profile_name == "coordinator_2":
    kwargs["managed_agents"] = [self._build_managed_basic_agent()]
```

Update `config/config-agent-prompts.yaml` so `coordinator_2` system prompt explicitly states:

```yaml
- 当需要时效性信息、外部搜索、文件检查、代码执行或任何工具能力时，必须切换到 `basic_agent`
- 切换时只能输出 canonical JSON：[{"id":"call_0","type":"agent","function":{"name":"switch_agent","arguments":"basic_agent"}}]
- 不要输出 `Calling tools:`，也不要混入其他 prose
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. "/home/zk/Project/LLM-deployment/.sglang/bin/python" -m pytest tests/test_get_tools_no_load_images.py tests/test_multi_agent_config.py -q`

Expected: PASS

### Task 2: Implement Backend Secondary Handoff Execution

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Test: `tests/test_multi_agent_runner.py`
- Test: `tests/test_agent_session_context.py`

- [ ] **Step 1: Write the failing tests**

Add focused tests for runner-owned secondary handoff:

```python
def test_run_stream_coordinator_2_switches_to_basic_agent_via_secondary_handoff():
    runner = _make_runner()
    runner.config.agent.enabled_agent_profiles = ["coordinator_1", "coordinator_2", "basic_agent"]
    session = AgentSession(
        session_id="coord2-secondary-handoff-session",
        agent=MagicMock(),
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )
    coordinator_2 = MagicMock()
    coordinator_2.memory.steps = []
    coordinator_2.tools = {}
    coordinator_2.managed_agents = {
        "basic_agent": MagicMock(name="basic_agent", description="delegate to basic agent")
    }

    async def _basic_stream():
        yield 'data: {"type": "session_start", "session_id": "coord2-secondary-handoff-session"}\n\n'
        yield 'data: {"type": "agent_step", "step_type": "tool_call", "name": "search_and_fetch", "input": {"query": "Jetson Thor"}}\n\n'
        yield 'data: {"type": "done", "session_id": "coord2-secondary-handoff-session"}\n\n'

    with ...:
        events = asyncio.run(_collect_stream_events(runner.run_stream(session.session_id, "帮我查最新消息")))

    payloads = [json.loads(event[6:]) for event in events]
    assert any(
        payload.get("type") == "timeline_event"
        and payload["event"].get("kind") == "agent_switched"
        and payload["event"].get("source") == "coordinator_2"
        and payload["event"].get("target_agent") == "basic_agent"
        for payload in payloads
    )
```

Also add a context-pack/handoff-ledger test asserting that after a `coordinator_2 -> basic_agent` switch, the new active agent sees `routing_receipt.decision == "basic_agent"` and a handoff entry sourced from `coordinator_2`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. "/home/zk/Project/LLM-deployment/.sglang/bin/python" -m pytest tests/test_multi_agent_runner.py tests/test_agent_session_context.py -q`

Expected: FAIL because `coordinator_2` has no runner-owned secondary handoff path yet.

- [ ] **Step 3: Write minimal implementation**

Add a backend-controlled path that converts `coordinator_2`'s managed-agent-style `basic_agent` call into a real profile switch. The implementation should:

```python
def _build_managed_basic_agent_proxy(self):
    proxy = SimpleNamespace(name="basic_agent", description="...")
    ...

def _extract_secondary_handoff(tool_call):
    if tool_call.name == "basic_agent":
        return {"target_agent": "basic_agent", "task": ..., "additional_args": ...}

def _activate_profile_agent(...):
    session.behavior_records["handoff_ledger"] = [...]
```

Within `_run_basic_stream()` / `_run_basic_sync()` when `session.current_agent_name == "coordinator_2"` and a managed-agent call targets `basic_agent`:

1. stop treating it as a normal tool call
2. append `agent_switched` timeline with `source="coordinator_2"`
3. activate the real `basic_agent`
4. continue the same turn through `_run_basic_stream()` or `_run_basic_sync()` for `basic_agent`

Do not leave the handoff as plain tool output text.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. "/home/zk/Project/LLM-deployment/.sglang/bin/python" -m pytest tests/test_multi_agent_runner.py tests/test_agent_session_context.py -q`

Expected: PASS

### Task 3: Filter Tool Transcript Leakage From `coordinator_2` Chat Bubbles

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Test: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Write the failing tests**

Add a stream regression test for leaked canonical JSON:

```python
def test_run_basic_stream_coordinator_2_filters_tool_call_json_from_chat_bubble():
    runner = _make_runner()
    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}

    def _agent_stream():
        yield ChatMessageStreamDelta(content="<think>需要联网搜索</think>")
        yield ChatMessageStreamDelta(content='Calling tools:\n[{"id":"call_0","type":"function","function":{"name":"search_and_fetch","arguments":{"query":"Jetson Thor"}}}]')
        from smolagents.memory import ToolCall
        yield ToolCall(name="search_and_fetch", arguments={"query": "Jetson Thor"}, id="call_0")

    agent.run.return_value = _agent_stream()
    session = AgentSession(... current_agent_name="coordinator_2")

    events = asyncio.run(_collect_stream_events(runner._run_basic_stream(session, "帮我查最新消息", None)))
    payloads = [json.loads(event[6:]) for event in events]
    bubble_texts = [
        payload["event"]["text"]
        for payload in payloads
        if payload.get("type") == "timeline_event" and payload["event"].get("kind") == "message_delta"
    ]
    assert all("Calling tools:" not in text for text in bubble_texts)
    assert all('"search_and_fetch"' not in text for text in bubble_texts)
    assert any(payload.get("step_type") == "tool_call" for payload in payloads)
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `PYTHONPATH=. "/home/zk/Project/LLM-deployment/.sglang/bin/python" -m pytest tests/test_multi_agent_runner.py::test_run_basic_stream_coordinator_2_filters_tool_call_json_from_chat_bubble -q`

Expected: FAIL because raw tool-call transcript is still emitted as `message_delta`.

- [ ] **Step 3: Write minimal implementation**

Before `_emit_chat_bubble_delta(...)`, strip or buffer recognized tool-call transcript markers for `coordinator_2`:

```python
clean_text, pending_tool_text = self._strip_tool_transcript_prefix(raw_text, pending_tool_text)
if clean_text:
    _emit_chat_bubble_delta(clean_text, partial=True, reasoning=...)
```

Reuse existing parsing helpers so these patterns are filtered:

- `Calling tools:`
- canonical JSON arrays
- `<tool_call>...</tool_call>`
- partial prefixes that are still accumulating toward a tool call

Keep ordinary reasoning/final text flowing to the bubble unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. "/home/zk/Project/LLM-deployment/.sglang/bin/python" -m pytest tests/test_multi_agent_runner.py -q`

Expected: PASS

### Task 4: Route Tool/Observation Steps To The Active Projected Assistant Bubble

**Files:**
- Modify: `frontend/src/lib/dispatch-message.ts`
- Modify: `frontend/src/lib/dispatch-message.test.mts`
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`

- [ ] **Step 1: Write the failing frontend tests**

Add helper-level tests for choosing the active assistant message id:

```ts
test('getActiveTrackedAssistantMessageId prefers the newest projected coordinator bubble', () => {
  assert.equal(
    getActiveTrackedAssistantMessageId('assistant-original', new Set(['assistant-projected-1', 'assistant-projected-2'])),
    'assistant-projected-2',
  );
});

test('getActiveTrackedAssistantMessageId falls back to the original assistant id when no projected bubble exists', () => {
  assert.equal(getActiveTrackedAssistantMessageId('assistant-original', new Set()), 'assistant-original');
});
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `node --test --experimental-strip-types "frontend/src/lib/dispatch-message.test.mts"`

Expected: FAIL because the helper does not exist and pages still always write steps to `assistantMessageId`.

- [ ] **Step 3: Write minimal implementation**

Add a helper such as:

```ts
export function getActiveTrackedAssistantMessageId(
  assistantMessageId: string | null | undefined,
  projectedAssistantMessageIds?: Iterable<string>,
): string | null | undefined {
  const projectedIds = projectedAssistantMessageIds ? Array.from(projectedAssistantMessageIds) : [];
  return projectedIds.length > 0 ? projectedIds[projectedIds.length - 1] : assistantMessageId;
}
```

Then update both `page.tsx` and `vision/page.tsx` so `onStep`, `onContent`, `onComplete`, and `onError` route updates through that active id whenever a projected `coordinator_2` bubble exists.

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test --experimental-strip-types "frontend/src/lib/dispatch-message.test.mts"`

Expected: PASS

### Task 5: Sync Page-Level Behavior And Run End-to-End Verification

**Files:**
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Modify: `frontend/src/lib/timeline-projection.test.mts`
- Test: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Add parity assertions if needed**

Extend `frontend/src/lib/timeline-projection.test.mts` source-level parity coverage to assert both pages use the same active tracked message helper when applying `agent_step` updates.

- [ ] **Step 2: Run frontend tests and type-check**

Run: `node --test --experimental-strip-types "frontend/src/lib/timeline-projection.test.mts"`

Expected: PASS

Run: `npm run type-check`

Expected: PASS

- [ ] **Step 3: Run backend phase-1 baseline relevant to this feature**

Run: `PYTHONPATH=. "/home/zk/Project/LLM-deployment/.sglang/bin/python" -m pytest tests/test_multi_agent_runner.py tests/test_agent_session_context.py tests/test_multi_tool_parsing.py tests/test_timeline_projection.py -q`

Expected: PASS

- [ ] **Step 4: Run frontend build**

Run: `npm run build`

Expected: PASS
