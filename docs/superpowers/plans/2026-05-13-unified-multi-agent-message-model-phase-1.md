# Unified Multi-Agent Message Model Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Phase 1 of the unified multi-agent message model: add `coordinator_1` timeout fallback, introduce canonical timeline events plus projection hooks, render `coordinator_2` as chat-bubble streaming, keep dispatch truly SSE-driven, and compile agent context with deterministic compiler only.

**Architecture:** Extend the existing multi-agent backend instead of replacing it wholesale. Add a canonical timeline layer inside the coordinator/basic routing flow, keep Web UI streaming mandatory, preserve compatibility with existing `agent_step` / `content` events during migration, and compile Tier 2/3 behavior state in code rather than through per-turn LLM summaries.

**Tech Stack:** Python, FastAPI, smolagents, Next.js 14, React, TypeScript, Tailwind, pytest.

---

## File Map

### Backend files to modify
- `backend/config.py`
  - Add/parse a Phase 1 misc config entry for `coordinator_1_timeout_seconds`.
- `backend/agent/agent_runner.py`
  - Add coordinator profile split (`coordinator_1` / `coordinator_2`), timeline event creation, routing timeout fallback, `coordinator_2` visible-message path, deterministic behavior compiler hooks, and backward-compatible SSE emission.
- `backend/agent/local_llm_model.py`
  - Extend switch target parsing to accept `coordinator_2` / future agents without breaking existing shorthand and canonical array parsing.
- `config/config-agent-prompts.yaml`
  - Introduce `coordinator_1` / `coordinator_2` prompt structure and keep top-level layout stable.

### Frontend files to modify
- `frontend/src/lib/api.ts`
  - Add `timeline_event` parsing while preserving existing `agent_step` / `content` compatibility.
- `frontend/src/app/page.tsx`
  - Add a unified reducer/projection path for timeline events and render `coordinator_2` as a streaming assistant bubble.
- `frontend/src/app/vision/page.tsx`
  - Mirror the same projection logic as text page.
- `frontend/src/components/AgentStepDisplay.tsx`
  - Update dispatch card rendering to reflect true SSE-driven rolling text and keep existing agent-step rendering.

### Tests to modify/create
- `tests/test_multi_agent_runner.py`
  - Main coordinator routing, timeout fallback, profile switching, and timeline emission tests.
- `tests/test_agent_session_context.py`
  - Coordinator streaming behavior, reasoning-only handling, and behavior-state carry-forward tests.
- `tests/test_multi_tool_parsing.py`
  - Extend switch target parsing coverage for `coordinator_2`.
- Create: `tests/test_timeline_projection.py`
  - Focused frontend/backend-shape tests for `timeline_event` projection contracts if a Python-side contract test is helpful.
- Create: `frontend/src/lib/timeline-projection.test.ts` or `frontend/src/lib/timeline-projection.test.mts`
  - Frontend reducer tests for dispatch card / chat bubble / agent-step projection.

### Documentation to update during implementation
- `AGENTS.md`
  - Already updated; keep synchronized if implementation names drift.
- `UNIFIED_MESSAGE_MODEL_MEMO.md`
  - Already added; update only if payload names change during implementation.

---

### Task 1: Add Phase 1 Config And Prompt Profiles

**Files:**
- Modify: `backend/config.py`
- Modify: `config/config-agent-prompts.yaml`
- Test: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Write the failing test**

Add prompt/config tests to lock the new profile split and timeout config.

```python
def test_config_exposes_coordinator_1_timeout_seconds():
    config = get_config()
    assert hasattr(config.misc, "coordinator_1_timeout_seconds")


def test_coordinator_1_and_coordinator_2_prompts_exist():
    runner = _make_runner()
    assert hasattr(runner.config.agent_prompts, "coordinator_1")
    assert hasattr(runner.config.agent_prompts, "coordinator_2")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py::test_config_exposes_coordinator_1_timeout_seconds tests/test_multi_agent_runner.py::test_coordinator_1_and_coordinator_2_prompts_exist -q`

Expected: FAIL because config and prompt structure still only expose the old `coordinator_agent` profile.

- [ ] **Step 3: Write minimal implementation**

Update `backend/config.py` and `config/config-agent-prompts.yaml` to define:

```python
@dataclass
class MiscConfig:
    coordinator_1_timeout_seconds: int = 10
```

```yaml
coordinator_1:
  instructions: |-
    你是 `coordinator_1`，只负责在有限时间内选择目标 agent。
  prompt_templates:
    routing_turn_prompt: |-
      你必须从以下 agent 中选择一个并输出切换动作：
      - coordinator_2
      - basic_agent
      原始用户任务：
      {{raw_task}}

coordinator_2:
  instructions: |-
    你是 `coordinator_2`，负责复杂推理；必要时可以切换到其他 agent。
  prompt_templates:
    system_prompt: |-
      你可以进行复杂推理；当发现需要工具时，切换到对应 agent。
```

Keep top-level keys required by the user stable:
- `coordinator_agent` may remain only if still needed for backward compatibility during migration
- but the new plan should drive implementation toward explicit `coordinator_1` / `coordinator_2`
- preserve `basic_agent`, future profiles, and `vlm_instructions`

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py::test_config_exposes_coordinator_1_timeout_seconds tests/test_multi_agent_runner.py::test_coordinator_1_and_coordinator_2_prompts_exist -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/config.py config/config-agent-prompts.yaml tests/test_multi_agent_runner.py
git commit -m "feat: add phase-1 coordinator profiles and timeout config"
```

### Task 2: Extend Switch Parsing And Routing Classification For Multiple Targets

**Files:**
- Modify: `backend/agent/local_llm_model.py`
- Modify: `backend/agent/agent_runner.py`
- Test: `tests/test_multi_tool_parsing.py`
- Test: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Write the failing tests**

Add parsing/classification tests for `coordinator_2`.

```python
def test_parse_switch_agent_command_from_canonical_array_to_coordinator_2():
    model = _make_model()
    content = '[{"id":"call_0","type":"agent","function":{"name":"switch_agent","arguments":"coordinator_2"}}]'
    command = model._parse_switch_agent_command(content)
    assert command is not None
    assert command.target_agent == "coordinator_2"


def test_classify_coordinator_output_accepts_coordinator_2_target():
    runner = _make_runner()
    chat_message = MagicMock()
    chat_message.content = '[{"id":"call_0","type":"agent","function":{"name":"switch_agent","arguments":"coordinator_2"}}]'
    chat_message.tool_calls = None
    assert runner._classify_coordinator_output(chat_message) == {
        "kind": "switch",
        "target_agent": "coordinator_2",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_tool_parsing.py::test_parse_switch_agent_command_from_canonical_array_to_coordinator_2 tests/test_multi_agent_runner.py::test_classify_coordinator_output_accepts_coordinator_2_target -q`

Expected: FAIL because routing still only accepts `basic_agent`.

- [ ] **Step 3: Write minimal implementation**

Relax target validation so Phase 1 can route to `coordinator_2` and future enabled profiles.

```python
if switch_command.target_agent not in self.config.agent.enabled_agent_profiles:
    return {"kind": "invalid", "error": f"Unsupported coordinator target: {switch_command.target_agent}"}
return {"kind": "switch", "target_agent": switch_command.target_agent}
```

Keep `LocalLLMModel._parse_switch_agent_command()` generic; do not hardcode only `basic_agent`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_tool_parsing.py tests/test_multi_agent_runner.py -q`

Expected: PASS for new target parsing and existing switch parsing coverage.

- [ ] **Step 5: Commit**

```bash
git add backend/agent/local_llm_model.py backend/agent/agent_runner.py tests/test_multi_tool_parsing.py tests/test_multi_agent_runner.py
git commit -m "feat: allow coordinator routing to additional agent targets"
```

### Task 3: Refactor Session And Routing Entry To Use `coordinator_1`

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Test: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Write the failing tests**

Update session-entry tests to expect `coordinator_1` as the routing entry profile.

```python
def test_get_or_create_session_initializes_coordinator_1_entry_state():
    runner = _make_runner()
    coordinator = MagicMock()
    with patch.object(runner, "_build_profile_agent", return_value=coordinator) as mock_build:
        session = runner.get_or_create_session(None)
    mock_build.assert_called_once_with("coordinator_1")
    assert session.current_agent_name == "coordinator_1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py::test_get_or_create_session_initializes_coordinator_1_entry_state -q`

Expected: FAIL because entry profile is still `coordinator_agent`.

- [ ] **Step 3: Write minimal implementation**

Change the plan helper/test factory and runtime defaults to use `coordinator_1` as the entry profile.

```python
config.agent.entry_agent_name = "coordinator_1"
config.agent.enabled_agent_profiles = ["coordinator_1", "coordinator_2", "basic_agent"]
```

Update `AgentSession.current_agent_name` defaults and session preparation logic to preserve shared history while rebuilding the `coordinator_1` entry agent.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py -q`

Expected: PASS with updated entry-profile expectations.

- [ ] **Step 5: Commit**

```bash
git add backend/agent/agent_runner.py tests/test_multi_agent_runner.py
git commit -m "refactor: use coordinator_1 as routing entry agent"
```

### Task 4: Add Coordinator 1 Timeout Fallback In Streaming Routing Flow

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Test: `tests/test_agent_session_context.py`
- Test: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Write the failing tests**

Add timeout fallback tests:

```python
def test_coordinator_routing_stream_times_out_to_basic_agent(agent_runner):
    coordinator = agent_runner._build_profile_agent("coordinator_1")
    coordinator.memory.steps = []
    session = AgentSession(
        session_id="coordinator-timeout-session",
        agent=coordinator,
        mode="tool_calling",
        current_agent_name="coordinator_1",
    )

    async def _collect():
        return [item async for item in agent_runner._run_coordinator_routing_stream(session, "复杂问题", None)]

    with patch.object(coordinator, "write_memory_to_messages", return_value=[]), \
         patch.object(coordinator.model, "generate_stream", side_effect=lambda *a, **k: iter(())), \
         patch("backend.agent.agent_runner.time.monotonic", side_effect=[0.0, 10.1]):
        outputs = asyncio.run(_collect())

    assert outputs[-1] == {
        "kind": "result",
        "route": {
            "kind": "switch",
            "target_agent": "basic_agent",
            "reason": "timeout_fallback",
        },
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_agent_session_context.py::test_coordinator_routing_stream_times_out_to_basic_agent -q`

Expected: FAIL because timeout fallback is not implemented.

- [ ] **Step 3: Write minimal implementation**

Use `self.config.misc.coordinator_1_timeout_seconds` inside `_run_coordinator_routing_stream()`.

```python
deadline = time.monotonic() + self.config.misc.coordinator_1_timeout_seconds
for delta in stream:
    if time.monotonic() >= deadline:
        yield {
            "kind": "dispatch",
            "dispatch_status": "running",
            "dispatch_stage": "routing",
            "dispatch_message": "路由超时，正在切换到 basic_agent",
        }
        yield {
            "kind": "result",
            "route": {
                "kind": "switch",
                "target_agent": "basic_agent",
                "reason": "timeout_fallback",
            },
        }
        return
```

Do not apply timeout to `coordinator_2`; this timeout is only for `coordinator_1` routing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_agent_session_context.py tests/test_multi_agent_runner.py -q`

Expected: PASS including timeout fallback coverage.

- [ ] **Step 5: Commit**

```bash
git add backend/agent/agent_runner.py tests/test_agent_session_context.py tests/test_multi_agent_runner.py
git commit -m "feat: add coordinator_1 timeout fallback to basic_agent"
```

### Task 5: Introduce Canonical Timeline Event Envelope In Backend Stream

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Test: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Write the failing tests**

Add stream tests for `timeline_event` emission:

```python
def test_run_stream_emits_timeline_event_for_routing_delta():
    runner = _make_runner()
    session = AgentSession(session_id="timeline-routing-session", agent=MagicMock(), mode="tool_calling", current_agent_name="coordinator_1")

    with patch.object(runner, "get_or_create_session", return_value=session), \
         patch.object(runner, "_decode_request_images", return_value=None), \
         patch.object(runner, "_run_coordinator_routing_stream", create=True, return_value=_async_events([
             {"kind": "timeline", "event": {"kind": "routing_delta", "source": "coordinator_1", "message_id": "routing-card-1", "text": "正在分析", "partial": True}},
             {"kind": "result", "route": {"kind": "switch", "target_agent": "basic_agent"}},
         ])):
        events = asyncio.run(_collect_stream_events(runner.run_stream(session.session_id, "你好")))

    payloads = [json.loads(event[6:]) for event in events]
    assert payloads[1]["type"] == "timeline_event"
    assert payloads[1]["event"]["kind"] == "routing_delta"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py::test_run_stream_emits_timeline_event_for_routing_delta -q`

Expected: FAIL because `timeline_event` is not yet emitted.

- [ ] **Step 3: Write minimal implementation**

Inside `run_stream()`, add a new branch:

```python
if coordinator_event.get("kind") == "timeline":
    yield f"data: {json.dumps({'type': 'timeline_event', 'event': coordinator_event['event']}, ensure_ascii=False)}\n\n"
    continue
```

Teach `_run_coordinator_routing_stream()` to emit timeline items for routing deltas and timeout fallback in parallel with compatibility dispatch steps.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py -q`

Expected: PASS for new timeline-event path and existing stream tests.

- [ ] **Step 5: Commit**

```bash
git add backend/agent/agent_runner.py tests/test_multi_agent_runner.py
git commit -m "feat: emit canonical timeline events in agent stream"
```

### Task 6: Add Behavior Compiler Skeleton And Tier 2/3 State

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Create: `tests/test_timeline_projection.py`

- [ ] **Step 1: Write the failing test**

Create a focused test for deterministic behavior compilation:

```python
def test_compile_context_pack_for_coordinator_2_uses_route_receipt_and_unresolved_items():
    runner = _make_runner()
    session = AgentSession(session_id="ctx-pack-session", agent=MagicMock(), mode="tool_calling")
    session.timeline_events = [
        {"kind": "routing_decision", "source": "coordinator_1", "target_agent": "coordinator_2", "reason_summary": "需要复杂推理"},
    ]
    session.behavior_records = {
        "unresolved_items": ["还未完成方程推导"],
    }

    pack = runner._compile_context_pack(session, target_agent="coordinator_2", user_goal="推导方程")

    assert pack["routing_receipt"]["decision"] == "coordinator_2"
    assert pack["unresolved_items"] == ["还未完成方程推导"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_timeline_projection.py::test_compile_context_pack_for_coordinator_2_uses_route_receipt_and_unresolved_items -q`

Expected: FAIL because timeline/behavior compiler state does not yet exist.

- [ ] **Step 3: Write minimal implementation**

Add minimal session fields and compiler helpers.

```python
@dataclass
class AgentSession:
    ...
    timeline_events: list[dict[str, Any]] = field(default_factory=list)
    behavior_records: dict[str, Any] = field(default_factory=dict)


def _append_timeline_event(self, session: AgentSession, event: dict[str, Any]) -> None:
    session.timeline_events.append(event)


def _compile_context_pack(self, session: AgentSession, target_agent: str, user_goal: str) -> dict[str, Any]:
    route_receipt = session.behavior_records.get("route_receipt") or {}
    unresolved_items = session.behavior_records.get("unresolved_items") or []
    return {
        "session_frame": {"session_id": session.session_id, "active_agent": target_agent},
        "user_goal": user_goal,
        "routing_receipt": route_receipt,
        "agent_contract": self._build_agent_contract(target_agent),
        "recent_visible_history": [],
        "working_memory": session.behavior_records.get("working_memory") or [],
        "handoff_ledger": session.behavior_records.get("handoff_ledger") or [],
        "tool_ledger": session.behavior_records.get("tool_ledger") or [],
        "unresolved_items": unresolved_items,
    }
```

For Phase 1, keep this deterministic and minimal; no LLM summarizer.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_timeline_projection.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agent/agent_runner.py tests/test_timeline_projection.py
git commit -m "feat: add deterministic behavior compiler skeleton"
```

### Task 7: Compile And Inject Context Pack When Switching Agents

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Test: `tests/test_multi_agent_runner.py`

- [ ] **Step 1: Write the failing test**

Add a handoff context-pack test:

```python
def test_activate_profile_agent_compiles_context_pack_for_target_agent():
    runner = _make_runner()
    session = AgentSession(session_id="handoff-context-session", agent=MagicMock(), mode="tool_calling")
    next_agent = MagicMock()
    next_agent.memory.steps = []
    next_agent.state = {}

    with patch.object(runner, "_build_profile_agent", return_value=next_agent), \
         patch.object(runner, "_compile_context_pack", return_value={"user_goal": "推导方程"}) as mock_compile:
        runner._activate_profile_agent(session, "coordinator_2")

    mock_compile.assert_called_once()
    assert session.current_agent_name == "coordinator_2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py::test_activate_profile_agent_compiles_context_pack_for_target_agent -q`

Expected: FAIL because `_activate_profile_agent()` does not compile or store context packs.

- [ ] **Step 3: Write minimal implementation**

Update `_activate_profile_agent()` to compile context pack and attach it to target agent state.

```python
context_pack = self._compile_context_pack(session, profile_name, user_goal=self._derive_current_user_goal(session))
next_agent.state = {
    **(getattr(next_agent, "state", {}) or {}),
    "context_pack": context_pack,
}
```

Preserve shared memory/state semantics already covered by existing tests.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agent/agent_runner.py tests/test_multi_agent_runner.py
git commit -m "feat: compile context packs during agent handoff"
```

### Task 8: Stream `coordinator_2` As Chat Bubble Instead Of Thought Step

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Create: `frontend/src/lib/timeline-projection.ts`
- Create: `frontend/src/lib/timeline-projection.test.mts`

- [ ] **Step 1: Write the failing frontend projection test**

Add a reducer/projection test:

```ts
import test from 'node:test'
import assert from 'node:assert/strict'
import { projectTimelineEvent } from './timeline-projection'

test('coordinator_2 message_delta becomes chat bubble chunk', () => {
  const result = projectTimelineEvent({
    kind: 'message_delta',
    source: 'coordinator_2',
    message_id: 'assistant-msg-42',
    text: '我们先从边界条件开始',
    partial: true,
    display: 'chat_bubble',
  })
  assert.equal(result.kind, 'chat_chunk')
  assert.equal(result.messageId, 'assistant-msg-42')
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test --experimental-strip-types "frontend/src/lib/timeline-projection.test.mts"`

Expected: FAIL because projection helper does not exist.

- [ ] **Step 3: Write minimal implementation**

Create a small projection helper and use it from both text and vision pages.

```ts
export function projectTimelineEvent(event: TimelineEvent) {
  if (event.kind === 'message_delta' && event.source === 'coordinator_2' && event.display === 'chat_bubble') {
    return { kind: 'chat_chunk', messageId: event.message_id, text: event.text, partial: event.partial }
  }
  if (event.kind === 'routing_delta') {
    return { kind: 'dispatch_update', text: event.text }
  }
  return { kind: 'noop' }
}
```

Wire `timeline_event` handling into `agentChatStream()` and both page reducers before legacy `agent_step` fallback logic.

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test --experimental-strip-types "frontend/src/lib/timeline-projection.test.mts"`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/app/page.tsx frontend/src/app/vision/page.tsx frontend/src/lib/timeline-projection.ts frontend/src/lib/timeline-projection.test.mts backend/agent/agent_runner.py
git commit -m "feat: project coordinator_2 stream into chat bubbles"
```

### Task 9: Make Dispatch Card Truly SSE-Driven And Continuously Rolling

**Files:**
- Modify: `frontend/src/components/AgentStepDisplay.tsx`
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Modify: `frontend/src/app/globals.css`
- Test: `frontend/src/lib/timeline-projection.test.mts`

- [ ] **Step 1: Write the failing projection/UI state test**

Extend projection test to cover rolling dispatch updates:

```ts
test('routing_delta updates dispatch text repeatedly', () => {
  const first = projectTimelineEvent({ kind: 'routing_delta', source: 'coordinator_1', message_id: 'routing-card-1', text: '正在分析', partial: true })
  const second = projectTimelineEvent({ kind: 'routing_delta', source: 'coordinator_1', message_id: 'routing-card-1', text: '正在分析当前任务', partial: true })
  assert.equal(first.kind, 'dispatch_update')
  assert.equal(second.kind, 'dispatch_update')
  assert.equal(second.text, '正在分析当前任务')
})
```

- [ ] **Step 2: Run test to verify it fails or exposes missing reducer behavior**

Run: `node --test --experimental-strip-types "frontend/src/lib/timeline-projection.test.mts"`

Expected: FAIL or expose missing repeated dispatch-update behavior.

- [ ] **Step 3: Write minimal implementation**

Update `DispatchCard` to use actual event-driven text plus continuous motion.

```tsx
<div className="dispatch-marquee-window">
  <div className="dispatch-marquee-track">{liveText}</div>
</div>
```

```css
@keyframes dispatch-marquee {
  0% { transform: translateX(0%); }
  100% { transform: translateX(-50%); }
}

.dispatch-marquee-track {
  display: inline-flex;
  min-width: max-content;
  animation: dispatch-marquee 8s linear infinite;
}
```

Keep the text source bound to live SSE updates; the animation is only presentation.

- [ ] **Step 4: Run tests and frontend sanity checks**

Run: `node --test --experimental-strip-types "frontend/src/lib/timeline-projection.test.mts" && npm run type-check`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/AgentStepDisplay.tsx frontend/src/app/page.tsx frontend/src/app/vision/page.tsx frontend/src/app/globals.css frontend/src/lib/timeline-projection.test.mts
git commit -m "feat: make dispatch card truly stream-driven and continuously rolling"
```

### Task 10: Keep CLI Readable While Preserving Web UI Streaming

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Test: `tests/test_agent_session_context.py`

- [ ] **Step 1: Write the failing test**

Add a small test for CLI/log throttling helper behavior if extracted, or at least lock that coordinator logs can still be emitted without turning SSE into token-per-line CLI output.

```python
def test_coordinator_terminal_logging_can_be_disabled_or_throttled_separately(agent_runner):
    assert hasattr(agent_runner, "_should_emit_cli_stream_chunk")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_agent_session_context.py::test_coordinator_terminal_logging_can_be_disabled_or_throttled_separately -q`

Expected: FAIL because no readable CLI-stream strategy boundary exists.

- [ ] **Step 3: Write minimal implementation**

Introduce a minimal boundary so Web UI streaming and terminal printing can diverge.

```python
def _should_emit_cli_stream_chunk(self, text: str, *, source: str) -> bool:
    return False if source == "coordinator_1" else len(text.strip()) >= 8
```

Use this only for terminal/live printing decisions, not for Web UI SSE. Web UI must keep true streaming.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_agent_session_context.py -q`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agent/agent_runner.py tests/test_agent_session_context.py
git commit -m "refactor: separate CLI readability from web streaming"
```

### Task 11: Full Phase 1 Verification And Documentation Sync

**Files:**
- Modify if needed: `AGENTS.md`
- Modify if needed: `UNIFIED_MESSAGE_MODEL_MEMO.md`

- [ ] **Step 1: Run backend tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py tests/test_agent_session_context.py tests/test_multi_tool_parsing.py tests/test_timeline_projection.py -q`

Expected: PASS

- [ ] **Step 2: Run frontend tests and typing checks**

Run: `node --test --experimental-strip-types "frontend/src/lib/timeline-projection.test.mts" && npm run type-check`

Expected: PASS

- [ ] **Step 3: Run production-facing frontend build**

Run: `npm run build`

Expected: PASS

- [ ] **Step 4: Manual live verification**

Verify all of the following against a running backend/frontend:

```text
1. 用户发消息后，任务调度卡片立即出现。
2. coordinator_1 文本持续变化，卡片文字持续滚动，扫光持续循环。
3. 若 10 秒内未选路，卡片显示 timeout fallback，然后 basic_agent 接管。
4. 若选中 coordinator_2，其推理以普通聊天气泡方式流式展示。
5. 若 coordinator_2 再切到 basic_agent，agent steps UI 正常出现。
6. text 页面与 vision 页面行为一致。
7. CLI 输出可读，不是一 token 一行，但 Web UI 仍保持真实流式。
```

- [ ] **Step 5: Sync docs if implementation names drift**

If any final event names, config names, or context-pack keys changed during implementation, update these files before finishing:

```bash
git add AGENTS.md UNIFIED_MESSAGE_MODEL_MEMO.md docs/superpowers/specs/2026-05-13-unified-multi-agent-message-model-design.md
```

- [ ] **Step 6: Commit**

```bash
git add backend frontend tests AGENTS.md UNIFIED_MESSAGE_MODEL_MEMO.md docs/superpowers/specs/2026-05-13-unified-multi-agent-message-model-design.md docs/superpowers/plans/2026-05-13-unified-multi-agent-message-model-phase-1.md
git commit -m "feat: implement phase-1 unified multi-agent message model"
```
