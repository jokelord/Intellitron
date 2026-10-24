# Custom Compact Focus Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement one shared custom-focus compact behavior for both auto-compact next-turn focus and manual `/compact <custom_instruction>`.

**Architecture:** Reuse the existing `CompactRequest.custom_instructions` pipe. Frontend parses manual `/compact <...>` into `customInstructions`; backend generates the same kind of custom instruction for auto-compact from the real next-turn user message, and passes it through existing compact prompt builders. Cache-first source messages remain unchanged; the focus text appears only in the tail compact-control instruction or reliable compact instruction.

**Tech Stack:** Python FastAPI backend, compact orchestrator, smolagents AgentRunner, Next.js/React frontend, Node built-in `node:test`, pytest.

---

## File Structure

- Modify: `frontend/src/lib/slash-commands.ts` — add pure parser for `/compact <...>` arguments.
- Modify: `frontend/src/app/page.tsx` — pass current command args into parser and `buildCompactRequestBody`.
- Modify: `frontend/src/app/vision/page.tsx` — mirror text page changes.
- Modify: `frontend/src/lib/slash-commands-state.test.mts` or `frontend/src/lib/slash-commands.test.mts` — add parser and integration regression tests.
- Create: `backend/compact/custom_focus.py` — one small helper for generated auto-compact focus instructions.
- Modify: `backend/api/compact.py` — forward manual agent-mode `custom_instructions`.
- Modify: `backend/api/chat.py` — pass generated next-turn focus into chat auto-compact.
- Modify: `backend/agent/agent_runner.py` — pass manual and auto custom focus into agent compact requests.
- Modify: backend compact tests under `tests/test_compact_*.py` — add focused coverage for forwarding and prompt-shape invariants.

## Task 1: Frontend Manual `/compact <...>` Parsing and Transmission

**Files:**
- Modify: `frontend/src/lib/slash-commands.ts`
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Modify: `frontend/src/lib/slash-commands-state.test.mts`
- Modify: `frontend/src/lib/compact-body.test.mts`

- [ ] **Step 1: Add failing parser tests**

Add tests to `frontend/src/lib/slash-commands-state.test.mts`:

```ts
import { parseCompactCustomInstruction } from "./slash-commands.ts";

test("/compact parser returns null custom instructions for empty args", () => {
  assert.deepEqual(parseCompactCustomInstruction(""), {
    ok: true,
    customInstructions: null,
  });
  assert.deepEqual(parseCompactCustomInstruction("   "), {
    ok: true,
    customInstructions: null,
  });
});

test("/compact parser extracts angle-bracket custom instruction", () => {
  assert.deepEqual(parseCompactCustomInstruction(" <focus on code> "), {
    ok: true,
    customInstructions: "focus on code",
  });
});

test("/compact parser preserves inner literal angle brackets", () => {
  assert.deepEqual(parseCompactCustomInstruction("<focus with <literal> brackets>"), {
    ok: true,
    customInstructions: "focus with <literal> brackets",
  });
});

test("/compact parser rejects non-envelope args", () => {
  assert.deepEqual(parseCompactCustomInstruction("focus on code"), {
    ok: false,
    reason: "invalid_custom_instruction",
  });
});

test("/compact parser rejects empty envelope", () => {
  assert.deepEqual(parseCompactCustomInstruction("<>") , {
    ok: false,
    reason: "invalid_custom_instruction",
  });
});
```

- [ ] **Step 2: Run frontend parser tests and verify RED**

Run: `node --loader ts-node/esm frontend/src/lib/slash-commands-state.test.mts`

Expected: FAIL because `parseCompactCustomInstruction` is not exported.

If this repo's local runner does not have `ts-node`, use the existing repo pattern: `node --experimental-strip-types frontend/src/lib/slash-commands-state.test.mts`.

- [ ] **Step 3: Implement parser**

Add to `frontend/src/lib/slash-commands.ts`:

```ts
export type CompactCustomInstructionParseResult =
  | { ok: true; customInstructions: string | null }
  | { ok: false; reason: "invalid_custom_instruction" };

export function parseCompactCustomInstruction(
  args: string
): CompactCustomInstructionParseResult {
  const trimmed = args.trim();
  if (!trimmed) return { ok: true, customInstructions: null };
  if (!trimmed.startsWith("<") || !trimmed.endsWith(">")) {
    return { ok: false, reason: "invalid_custom_instruction" };
  }
  const inner = trimmed.slice(1, -1).trim();
  if (!inner) return { ok: false, reason: "invalid_custom_instruction" };
  return { ok: true, customInstructions: inner };
}
```

- [ ] **Step 4: Run parser tests and verify GREEN**

Run: `node --experimental-strip-types frontend/src/lib/slash-commands-state.test.mts`

Expected: PASS for parser tests and existing slash-command tests.

- [ ] **Step 5: Add failing page integration tests by source inspection**

Extend `frontend/src/lib/slash-commands-state.test.mts` with source assertions:

```ts
test("manual /compact pages parse args and forward customInstructions", async () => {
  for (const pagePath of [
    path.join(testDir, "../app/page.tsx"),
    path.join(testDir, "../app/vision/page.tsx"),
  ]) {
    const source = await readFile(pagePath, "utf8");
    assert.match(
      source,
      /parseCompactCustomInstruction\(args\)/,
      `${pagePath} must parse /compact args before building compact body`
    );
    assert.match(
      source,
      /customInstructions:\s*parsedCustom\.customInstructions/,
      `${pagePath} must forward parsed custom instructions to buildCompactRequestBody`
    );
    assert.match(
      source,
      /invalid_custom_instruction/,
      `${pagePath} must surface invalid custom instruction syntax as compact_failed`
    );
  }
});
```

- [ ] **Step 6: Run page integration test and verify RED**

Run: `node --experimental-strip-types frontend/src/lib/slash-commands-state.test.mts`

Expected: FAIL because pages do not parse args and still pass `customInstructions: null`.

- [ ] **Step 7: Update page handlers**

In both `frontend/src/app/page.tsx` and `frontend/src/app/vision/page.tsx`:

Import parser:

```ts
import { parseCompactCustomInstruction } from '@/lib/slash-commands';
```

Change `handleSelectSlashCommand` to accept args:

```ts
const handleSelectSlashCommand = useCallback(
  async (cmd: SlashCommand, args = '') => {
```

Before building the body:

```ts
const parsedCustom = parseCompactCustomInstruction(args);
if (!parsedCustom.ok) {
  setInput('');
  setSlashMenu({ visible: false, query: '', selectedIndex: 0 });
  const failedId = `compact-failed-${Date.now()}`;
  applyCompactProjection(
    {
      kind: 'compact_failed',
      summaryMessageId: failedId,
      reason: parsedCustom.reason,
    },
    setMessages,
    newlyCompactMarkedRef
  );
  return;
}
```

Change body construction:

```ts
customInstructions: parsedCustom.customInstructions,
```

When invoking a slash command from `handleSubmit`, compute args from the current input:

```ts
const args = input.slice(matched.name.length);
handleSelectSlashCommand(matched, args);
```

When invoking from popup Enter, pass an empty string:

```ts
handleSelectSlashCommand(filteredSlashCommands[slashMenu.selectedIndex], '');
```

- [ ] **Step 8: Run frontend tests and verify GREEN**

Run: `node --experimental-strip-types frontend/src/lib/slash-commands-state.test.mts`

Expected: PASS.

Run: `node --experimental-strip-types frontend/src/lib/compact-body.test.mts`

Expected: PASS.

## Task 2: Backend Custom Focus Forwarding and Auto-Compact Generation

**Files:**
- Create: `backend/compact/custom_focus.py`
- Modify: `backend/api/compact.py`
- Modify: `backend/api/chat.py`
- Modify: `backend/agent/agent_runner.py`
- Modify: `tests/test_compact_api_endpoint.py`
- Modify: `tests/test_compact_agent_routes_to_runner.py`
- Modify: `tests/test_compact_prompt_plan.py`
- Modify: `tests/test_compact_chat_auto_trigger.py` or create focused test in existing compact auto-trigger file
- Modify: `tests/test_compact_agent_v2_path.py`

- [ ] **Step 1: Add failing helper tests**

Create `tests/test_compact_custom_focus.py`:

```python
from backend.compact.custom_focus import build_next_turn_focus_instruction


def test_build_next_turn_focus_instruction_includes_upcoming_user_instruction():
    text = build_next_turn_focus_instruction("Please inspect compact code")

    assert "After compaction, the user's next task" in text
    assert "Do not answer or execute the task" in text
    assert "Use it only to decide" in text
    assert "Upcoming user instruction:" in text
    assert "Please inspect compact code" in text


def test_build_next_turn_focus_instruction_returns_none_for_blank_text():
    assert build_next_turn_focus_instruction("   ") is None
    assert build_next_turn_focus_instruction(None) is None
```

- [ ] **Step 2: Run helper tests and verify RED**

Run: `./.sglang/bin/python -m pytest tests/test_compact_custom_focus.py -q`

Expected: FAIL because `backend.compact.custom_focus` does not exist.

- [ ] **Step 3: Implement helper**

Create `backend/compact/custom_focus.py`:

```python
from __future__ import annotations

from typing import Any


def compact_focus_text_from_content(content: Any) -> str | None:
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                value = item.get("text")
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
        return "\n".join(parts).strip() or None
    if content is None:
        return None
    text = str(content).strip()
    return text or None


def build_next_turn_focus_instruction(next_turn_user_message: Any) -> str | None:
    text = compact_focus_text_from_content(next_turn_user_message)
    if not text:
        return None
    return (
        "After compaction, the user's next task will be the instruction below. "
        "When writing the summary, prioritize preserving prior details that are relevant "
        "to that upcoming task. Do not answer or execute the task, and do not treat this "
        "focus note as part of the conversation being summarized. Use it only to decide "
        "which historical information needs extra detail.\n\n"
        "Upcoming user instruction:\n"
        f"{text}"
    )
```

- [ ] **Step 4: Run helper tests and verify GREEN**

Run: `./.sglang/bin/python -m pytest tests/test_compact_custom_focus.py -q`

Expected: PASS.

- [ ] **Step 5: Add failing manual agent forwarding test**

In `tests/test_compact_agent_routes_to_runner.py`, update the fake runner signature to capture custom instructions and add/assert request body custom text:

```python
async def _fake_run_compact_for_session(self, session_id, mode=None, trigger="manual", custom_instructions=None):
    called["session_id"] = session_id
    called["mode"] = mode
    called["trigger"] = trigger
    called["custom_instructions"] = custom_instructions
    yield CompactEvent.completed("compact-1", "<summary>ok</summary>")
```

For the existing agent-mode request body, set:

```python
"custom_instructions": "focus on agent memory",
```

Assert:

```python
assert called["custom_instructions"] == "focus on agent memory"
```

- [ ] **Step 6: Run manual agent forwarding test and verify RED**

Run: `./.sglang/bin/python -m pytest tests/test_compact_agent_routes_to_runner.py -q`

Expected: FAIL because `backend/api/compact.py` does not pass `custom_instructions` to `run_compact_for_session`.

- [ ] **Step 7: Forward manual custom instructions through agent compact**

Change `backend/api/compact.py` agent branch:

```python
async for ev in agent_runner.run_compact_for_session(
    request.session_id,
    mode="agent",
    trigger="manual",
    custom_instructions=request.custom_instructions,
):
```

Change `AgentRunner.run_compact_for_session` signature in `backend/agent/agent_runner.py`:

```python
async def run_compact_for_session(
    self,
    session_id: str,
    mode: str | None = None,
    trigger: str = "manual",
    custom_instructions: str | None = None,
) -> AsyncGenerator:
```

Add to its `CompactRequest(...)`:

```python
custom_instructions=custom_instructions,
```

- [ ] **Step 8: Run manual forwarding tests and verify GREEN**

Run: `./.sglang/bin/python -m pytest tests/test_compact_agent_routes_to_runner.py tests/test_compact_api_endpoint.py -q`

Expected: PASS.

- [ ] **Step 9: Add failing cache-first invariant test**

In `tests/test_compact_prompt_plan.py`, extend existing custom-instruction test or add:

```python
def test_cache_first_custom_focus_keeps_source_messages_unchanged():
    source = [
        Message(role="system", content="SYS"),
        Message(role="user", content="old user"),
    ]

    plan = build_cache_first_tail_plan(source, _prompts(), custom_instructions="Focus on old user")

    assert plan.messages[:-1] == source
    assert plan.messages[-1].role == "user"
    assert "Focus on old user" in plan.messages[-1].content
    assert "Focus on old user" not in str(plan.messages[:-1])
```

- [ ] **Step 10: Run cache-first invariant test**

Run: `./.sglang/bin/python -m pytest tests/test_compact_prompt_plan.py -q`

Expected: PASS if existing code already satisfies it; if it fails, fix only `build_cache_first_tail_plan` or `build_compact_control_instruction` minimally.

- [ ] **Step 11: Add failing chat auto-compact focus test**

In `tests/test_compact_chat_auto_trigger.py`, add or adapt a focused test that monkeypatches the compact orchestrator to capture `CompactRequest.custom_instructions` when `_generate_stream_response` auto-compacts. Assert:

```python
assert captured_req.custom_instructions is not None
assert "Upcoming user instruction:" in captured_req.custom_instructions
assert "new task after compact" in captured_req.custom_instructions
assert all("new task after compact" not in str(m.content) for m in captured_req.body_messages)
```

Use a current user message content of `"new task after compact"`.

- [ ] **Step 12: Run chat auto-compact focus test and verify RED**

Run: `./.sglang/bin/python -m pytest tests/test_compact_chat_auto_trigger.py -q`

Expected: FAIL because chat auto-compact currently does not set `custom_instructions`.

- [ ] **Step 13: Implement chat auto-compact focus**

In `backend/api/chat.py`, import:

```python
from ..compact.custom_focus import build_next_turn_focus_instruction
```

When constructing `CompactRequest` in `_generate_stream_response`, add:

```python
custom_instructions=build_next_turn_focus_instruction(
    current_user_message.content if current_user_message is not None else None
),
```

Also inspect and fix `_should_auto_compact_chat()` if necessary so the existing token-count body is reachable. The intended body is:

```python
    cfg = get_config()
    if not cfg.compact.enabled or not cfg.compact.auto_trigger:
        return False
    prompts_cfg = cfg.agent_prompts.compaction
    if prompts_cfg is None:
        return False
    try:
        from ..compact.threshold import compute_threshold, should_compact
        from ..compact.tokenizer import TokenCounter
        from ..llm_service import get_llm_service, resolve_backend_context_length

        service = get_llm_service()
        backend = service.active_backend or "sglang"
        max_ctx = resolve_backend_context_length(cfg, backend) or 0
        if max_ctx <= 0:
            return False
        threshold = compute_threshold(cfg.compact, max_ctx)
        tokens = TokenCounter.get().count_messages(messages)
        return should_compact(tokens, threshold)
    except Exception as exc:
        logger.debug(f"chat auto-compact check skipped: {exc}")
        return False
```

Keep `_build_reasoning_extra_body()` separate:

```python
    if enable_thinking is None:
        return None
    return {"chat_template_kwargs": {"enable_thinking": bool(enable_thinking)}}
```

- [ ] **Step 14: Run chat auto-compact tests and verify GREEN**

Run: `./.sglang/bin/python -m pytest tests/test_compact_chat_auto_trigger.py tests/test_compact_chat_threshold_alignment.py -q`

Expected: PASS.

- [ ] **Step 15: Add failing agent auto-compact focus test**

In `tests/test_compact_agent_v2_path.py`, add a test that monkeypatches `CompactOrchestrator.run_compact` or captures `CompactRequest` from `_run_compact_then_continue(session, trigger="auto", next_turn_user_message="new agent task")`. Assert:

```python
assert captured_req.custom_instructions is not None
assert "Upcoming user instruction:" in captured_req.custom_instructions
assert "new agent task" in captured_req.custom_instructions
assert all("new agent task" not in str(m.content) for m in captured_req.body_messages)
```

- [ ] **Step 16: Run agent auto-compact focus test and verify RED**

Run: `./.sglang/bin/python -m pytest tests/test_compact_agent_v2_path.py -q`

Expected: FAIL because `_run_compact_then_continue` does not accept/pass next-turn focus.

- [ ] **Step 17: Implement agent auto-compact focus**

In `backend/agent/agent_runner.py`, import helper inside `_run_compact_then_continue`:

```python
from ..compact.custom_focus import build_next_turn_focus_instruction
```

Change signature:

```python
async def _run_compact_then_continue(
    self,
    session,
    trigger: str,
    next_turn_user_message: str | None = None,
):
```

Add to `CompactRequest(...)`:

```python
custom_instructions=build_next_turn_focus_instruction(next_turn_user_message),
```

Change `run_stream()` auto-compact call:

```python
async for ev in self._run_compact_then_continue(
    session,
    trigger="auto",
    next_turn_user_message=message,
):
```

Leave manual `run_compact_for_session()` using the raw `custom_instructions` parameter.

- [ ] **Step 18: Run agent compact tests and verify GREEN**

Run: `./.sglang/bin/python -m pytest tests/test_compact_agent_v2_path.py tests/test_compact_agent_routes_to_runner.py tests/test_compact_agent_cache_first.py -q`

Expected: PASS.

## Task 3: Integration Verification and Cleanup

**Files:**
- Review all files changed by Tasks 1 and 2.
- No new production files unless Task 2 created `backend/compact/custom_focus.py`.

- [ ] **Step 1: Run focused frontend tests**

Run:

```bash
node --experimental-strip-types frontend/src/lib/slash-commands.test.mts
node --experimental-strip-types frontend/src/lib/slash-commands-state.test.mts
node --experimental-strip-types frontend/src/lib/compact-body.test.mts
node --experimental-strip-types frontend/src/lib/compact-client-body.test.mts
```

Expected: all PASS.

- [ ] **Step 2: Run focused backend tests**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_compact_custom_focus.py \
  tests/test_compact_prompt_control.py \
  tests/test_compact_prompt_plan.py \
  tests/test_compact_api_endpoint.py \
  tests/test_compact_agent_routes_to_runner.py \
  tests/test_compact_chat_auto_trigger.py \
  tests/test_compact_chat_threshold_alignment.py \
  tests/test_compact_agent_v2_path.py \
  tests/test_compact_agent_cache_first.py \
  -q
```

Expected: all PASS.

- [ ] **Step 3: Run static frontend check if dependencies are available**

Run from `frontend/`: `npm run type-check`

Expected: PASS. If dependencies are missing, report that type-check could not run and keep the focused Node tests as evidence.

- [ ] **Step 4: Self-review against spec**

Confirm:

- `/compact` still sends `custom_instructions: null`.
- `/compact <...>` sends the parsed inner instruction.
- Invalid `/compact foo` creates a local compact failure and does not call `handleSend()`.
- Chat auto-compact passes generated next-turn focus while excluding the current user message from compact source.
- Agent auto-compact passes generated next-turn focus while leaving `agent.run()` to append the real user task.
- Manual agent compact forwards user custom instruction through `/v1/compact/run` to `CompactRequest`.
- Cache-first source messages are not modified.
- No normal prompt compact-control clause is reintroduced.

- [ ] **Step 5: No git commands**

Do not commit in this repo unless the user explicitly grants git permission. Report changed files and test results instead.
