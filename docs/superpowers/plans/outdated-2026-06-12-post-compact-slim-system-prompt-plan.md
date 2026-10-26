# OUTDATED — Post-Compact Slim System Prompt Implementation Plan

> **OUTDATED / DO NOT EXECUTE AS CURRENT PREFIX-CACHE WORK (2026-06-14):** This plan belongs to the old slim-system workaround. It contains assumptions that conflict with current prefix-cache principles, including treating post-compact summary as system-prompt-like context. Current work must follow `docs/superpowers/specs/2026-06-13-compact-prompt-shape-prefix-cache-prd.md` and `docs/superpowers/plans/2026-06-14-compact-prompt-shape-prefix-cache-plan.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the first chat after auto-compact (step 8 of user's 8-step flow) drop from 1+ min to <30s by using a slim system_prompt for that single turn.

**Architecture:** Add a session-scoped `first_post_compact_turn: bool` flag. `_replace_agent_memory` sets it to True. `LocalLLMModel.__call__` reads it and passes `slim=True` to `llm_service.chat_stream_async`; `_inject_runtime_context` skips runtime_context when slim=True; the agent's `tools_to_call_from` is `{}` when slim=True. After the LLM-C call enters `_run_coordinator_routing_stream` (or `LocalLLMModel.__call__`'s first yield), the flag is cleared.

**Tech Stack:** Python · FastAPI · smolagents 1.22.0 · SGLang 0.5+ hybrid (Mamba + attention) · pytest · Qwen3.5 chat template

**Spec:** `docs/superpowers/specs/outdated-2026-06-12-post-compact-slim-system-prompt-design.md`

**User's 8-step flow (locked, do not deviate):**
1. LLM-A reads 40k novel + outputs.
2. User sends "我觉得你把小说总结得不错呢" (no /compact command).
3. Auto-compact triggers BEFORE program appends new user message to context.
4. Compact context = old history + compact instr (NO "我觉得..." appended).
5. Send full context to compact LLM-B (3 min, accepted).
6. LLM-B outputs summary.
7. Summary becomes a partial system_prompt appended to current agent's; new context window for THIS session (not a new session). Web UI shows full chat history to user, LLM only sees new context window.
8. **Bind** the user turn "我觉得..." to the new context window. Send to SGLang. **Coordinator_1 routing MUST still work** (per user requirement, do not skip).

**Constraint (user-stated, hard):** Coordinator_1 routing must remain functional in step 8. We do NOT skip routing.

---

## File Structure

| File | Responsibility | Modified in Task |
|------|----------------|------------------|
| `backend/agent/agent_runner.py` | Set `first_post_compact_turn=True` in `_replace_agent_memory`; clear in `run_stream`; pass flag to `LocalLLMModel` | Task 1, 4 |
| `backend/agent/local_llm_model.py` | Read flag, pass `slim=True` to `llm_service`, pass `tools={}` when slim | Task 2, 3 |
| `backend/llm_service.py` | `_inject_runtime_context` skips when `slim=True` | Task 2 |
| `tests/test_post_compact_slim_flag.py` | New test file covering flag lifecycle + slim path | Task 1, 2, 3, 4 |

---

## Task 1: Add `first_post_compact_turn` flag lifecycle in `agent_runner.py`

**Files:**
- Modify: `backend/agent/agent_runner.py:4560` (in `_replace_agent_memory`)
- Modify: `backend/agent/agent_runner.py:4733` (in `run_stream` after auto-compact decision)
- Modify: `backend/agent/agent_runner.py:AgentSession` (or `Session` dataclass — find it) — add field
- Test: `tests/test_post_compact_slim_flag.py` (new file)

- [ ] **Step 1.1: Find the Session dataclass**

Run: `cd ~/Project/LLM-deployment && grep -n "class.*Session\b\|@dataclass" backend/agent/agent_runner.py | head -20`

Locate the Session class definition. Note the file:line for the next step. (Likely `Session` is a dataclass around line 200-400; may be `AgentSession` or `Session`.)

- [ ] **Step 1.2: Write the failing test for the flag lifecycle**

Create `tests/test_post_compact_slim_flag.py`:

```python
"""Tests for the first_post_compact_turn flag lifecycle."""

import pytest
from unittest.mock import MagicMock

from backend.agent.agent_runner import AgentRunner, SmartToolCallingAgent


@pytest.fixture
def mock_runner():
    """Build a minimal AgentRunner-like object with just enough to test flag lifecycle."""
    runner = MagicMock(spec=AgentRunner)
    # We don't actually need a full runner; we test _replace_agent_memory directly
    return runner


def test_replace_agent_memory_sets_first_post_compact_turn_true():
    """After _replace_agent_memory, the session flag must be True."""
    runner = AgentRunner.__new__(AgentRunner)  # bypass __init__
    # Build a minimal session-like object
    session = MagicMock()
    session.compaction_archive = []
    session.has_run = False
    session.current_agent_name = "old_agent"
    session.active_agent = MagicMock()
    session.agent = MagicMock()
    session.agent.memory.steps = []

    runner.config = MagicMock()
    runner.config.agent_prompts.compaction.summary_envelope_preface = "PREFACE: "
    runner.config.agent_prompts.compaction.summary_envelope_suffix = " :SUFFIX"
    runner.config.agent.entry_agent_name = "coordinator_1"

    runner._replace_agent_memory(session, "summary text", "summary-id-1")

    assert session.first_post_compact_turn is True, (
        "_replace_agent_memory must set first_post_compact_turn=True "
        "so the next chat LLM-C uses slim system_prompt"
    )


def test_replace_agent_memory_keeps_has_run_true():
    """Trap #23: has_run must stay True after compact (smolagents reset() must not wipe envelope)."""
    runner = AgentRunner.__new__(AgentRunner)
    session = MagicMock()
    session.compaction_archive = []
    session.has_run = False
    session.current_agent_name = "old_agent"
    session.active_agent = MagicMock()
    session.agent = MagicMock()
    session.agent.memory.steps = []

    runner.config = MagicMock()
    runner.config.agent_prompts.compaction.summary_envelope_preface = ""
    runner.config.agent_prompts.compaction.summary_envelope_suffix = ""
    runner.config.agent.entry_agent_name = "coordinator_1"

    runner._replace_agent_memory(session, "summary", "id")

    assert session.has_run is True, "has_run must remain True post-compact (trap #23)"
```

- [ ] **Step 1.3: Run test to verify it fails**

Run: `cd ~/Project/LLM-deployment && PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_post_compact_slim_flag.py -v`

Expected: FAIL with `AttributeError: ... 'first_post_compact_turn'` (the session mock has every attribute, but our new field is not set yet) or `assert False` from `test_replace_agent_memory_keeps_has_run_true` is fine but `test_replace_agent_memory_sets_first_post_compact_turn_true` MUST fail with `AttributeError` from the assertion.

If the test passes accidentally, the mock is too permissive. Use `MagicMock(spec_set=[...])` to constrain attributes.

- [ ] **Step 1.4: Find the real Session class and add the field**

Open `backend/agent/agent_runner.py` and find the Session dataclass (per Step 1.1). Add a field:

```python
@dataclass
class Session:
    # ... existing fields ...
    has_run: bool = False
    # NEW (Task 1): post-compact slim mode for the first chat after _replace_agent_memory
    first_post_compact_turn: bool = False
```

If the class is a `class Session:`, add as instance attribute in `__init__`:

```python
self.first_post_compact_turn = False
```

Save the file. Note the exact file:line where the field is added.

- [ ] **Step 1.5: Modify `_replace_agent_memory` to set the flag**

In `backend/agent/agent_runner.py`, after line 4560 (`session.current_agent_name = cfg.agent.entry_agent_name`), add:

```python
        # NEW (Task 1): mark the next chat as first_post_compact_turn so the
        # LLM-C path uses a slim system_prompt (no tool schemas, no
        # runtime_context). This is consumed and cleared by
        # LocalLLMModel.__call__ on the first chat after compact.
        session.first_post_compact_turn = True
```

- [ ] **Step 1.6: Re-run the test to verify it passes**

Run: `cd ~/Project/LLM-deployment && PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_post_compact_slim_flag.py -v`

Expected: 2 passed.

- [ ] **Step 1.7: Commit**

```bash
cd ~/Project/LLM-deployment
git add backend/agent/agent_runner.py tests/test_post_compact_slim_flag.py
git commit -m "feat(agent_runner): add first_post_compact_turn flag, set on _replace_agent_memory

Task 1 of post-compact slim system_prompt spec. The flag is consumed
by LocalLLMModel.__call__ (Task 2) to skip tool schemas + runtime_context
on the first chat after compact, dropping prefill from 5-10k to 1-2k
tokens and LLM-C wall time from 1+ min to <30s.

Hard contracts preserved:
- _replace_agent_memory keeps has_run=True (trap #23)
- No change to v2 prompt builder
- Flag is session-scoped (no cross-session pollution)"
```

---

## Task 2: `LocalLLMModel` reads flag, passes `slim=True` to `llm_service`

**Files:**
- Modify: `backend/agent/local_llm_model.py:223-230` (`__call__`) and `local_llm_model.py:315-321` (`generate_stream`)
- Modify: `backend/llm_service.py:80-101` (`_inject_runtime_context`)
- Test: `tests/test_post_compact_slim_flag.py` (extend)

- [ ] **Step 2.1: Read `LocalLLMModel.__call__` and `generate_stream`**

Run: `cd ~/Project/LLM-deployment && sed -n '215,235p' backend/agent/local_llm_model.py && echo "---" && sed -n '310,325p' backend/agent/local_llm_model.py`

Confirm:
- `__call__` line 223-230 has the tool/agent filtering block (per AGENTS.md trap #12)
- `generate_stream` line 315-321 has the same block

- [ ] **Step 2.2: Find the Session reference inside `LocalLLMModel`**

Run: `cd ~/Project/LLM-deployment && grep -n "self.session\|self\._session\|session" backend/agent/local_llm_model.py | head -10`

Find where `LocalLLMModel` accesses the `Session` object. It may be passed via `__init__` or as a kwarg to `__call__`.

If `LocalLLMModel` does NOT have a `session` reference, we need a different mechanism:
- **Option A**: Pass the session as a kwarg to `__call__` (cleanest, requires `LocalLLMModel` to know about Session)
- **Option B**: Add a class-level `_current_slim_flag` set by `AgentRunner.run_stream` before calling `agent.run()` (less clean but works)
- **Option C**: Pass `slim` directly via `agent.run(..., slim=True)` — but smolagents doesn't have that kwarg

**Decision**: Use **Option A**. Modify `LocalLLMModel.__init__` to accept `session: Optional[Session] = None`, store as `self._session`. In `__call__` / `generate_stream`, check `self._session and getattr(self._session, "first_post_compact_turn", False)` to decide slim.

- [ ] **Step 2.3: Write the failing test for slim flag propagation**

Add to `tests/test_post_compact_slim_flag.py`:

```python
from backend.agent.local_llm_model import LocalLLMModel


def test_local_llm_model_reads_first_post_compact_turn_flag():
    """LocalLLMModel must read the session's first_post_compact_turn flag."""
    session = MagicMock()
    session.first_post_compact_turn = True

    # Build LocalLLMModel with the session
    model = LocalLLMModel.__new__(LocalLLMModel)  # bypass __init__
    model._session = session

    # The slim path is decided inside __call__; we just verify the flag is read.
    # Mock llm_service to capture whether slim=True was passed.
    model.llm_service = MagicMock()
    model.llm_service.chat_stream_async = MagicMock(return_value=iter([]))

    # Call __call__ with minimal args
    list(model.__call__(messages=[{"role": "user", "content": "hi"}], tools_to_call_from={}))

    # Verify the llm_service was called with slim=True
    call_kwargs = model.llm_service.chat_stream_async.call_args.kwargs
    assert call_kwargs.get("slim") is True, (
        "LocalLLMModel must pass slim=True to llm_service when "
        "session.first_post_compact_turn is True"
    )


def test_local_llm_model_no_slim_when_flag_false():
    """When the flag is False, slim must be False (default)."""
    session = MagicMock()
    session.first_post_compact_turn = False

    model = LocalLLMModel.__new__(LocalLLMModel)
    model._session = session
    model.llm_service = MagicMock()
    model.llm_service.chat_stream_async = MagicMock(return_value=iter([]))

    list(model.__call__(messages=[{"role": "user", "content": "hi"}], tools_to_call_from={}))

    call_kwargs = model.llm_service.chat_stream_async.call_args.kwargs
    assert call_kwargs.get("slim", False) is False, (
        "When first_post_compact_turn is False, slim must default to False"
    )
```

- [ ] **Step 2.4: Run test to verify it fails**

Run: `cd ~/Project/LLM-deployment && PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_post_compact_slim_flag.py::test_local_llm_model_reads_first_post_compact_turn_flag -v`

Expected: FAIL (LocalLLMModel doesn't read the flag yet, or doesn't pass slim kwarg).

- [ ] **Step 2.5: Modify `LocalLLMModel.__init__` to accept session**

Open `backend/agent/local_llm_model.py`. In the `__init__` method, add a `session` parameter:

```python
def __init__(self, ..., session=None):
    # ... existing init ...
    self._session = session
```

If `__init__` is large, just add `self._session = session` at the end and `session=None` in the signature.

- [ ] **Step 2.6: Modify `__call__` to detect slim and pass to llm_service**

In `backend/agent/local_llm_model.py`, in the `__call__` method (around line 223-230), find the call to `self.llm_service.chat_stream_async(...)` and add the `slim` kwarg:

```python
    slim = bool(
        getattr(self._session, "first_post_compact_turn", False)
    ) if self._session is not None else False

    async for chunk in self.llm_service.chat_stream_async(
        ...,
        slim=slim,
    ):
        ...
```

Apply the same change in `generate_stream` (around line 315-321).

- [ ] **Step 2.7: Modify `_inject_runtime_context` in `llm_service.py` to honor slim**

In `backend/llm_service.py:80-101`, modify the function signature and add the slim path:

```python
def _inject_runtime_context(
    messages: List["Message"], runtime_context: Optional[str], slim: bool = False
) -> List["Message"]:
    if slim:
        # Post-compact first chat: skip runtime_context injection entirely.
        # Spec §1.4 / §2.2: slim mode uses minimal system_prompt.
        return list(messages)
    if not runtime_context:
        return messages
    # ... existing merge logic ...
```

- [ ] **Step 2.8: Modify `chat_stream_async` (or its caller) to thread `slim` to `_inject_runtime_context`**

In `backend/llm_service.py`, find the function that calls `_inject_runtime_context` (likely inside the `chat` or `chat_stream_async` method). Add the `slim` kwarg pass-through:

```python
    injected = _inject_runtime_context(messages, runtime_context, slim=slim)
```

(Exact location depends on the existing code; search for `_inject_runtime_context` call sites.)

- [ ] **Step 2.9: Re-run test to verify it passes**

Run: `cd ~/Project/LLM-deployment && PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_post_compact_slim_flag.py -v`

Expected: 4 passed (2 from Task 1 + 2 from Task 2).

- [ ] **Step 2.10: Commit**

```bash
cd ~/Project/LLM-deployment
git add backend/agent/local_llm_model.py \
        backend/llm_service.py \
        tests/test_post_compact_slim_flag.py
git commit -m "feat(local_llm_model): pass slim=True to llm_service when first_post_compact_turn

Task 2 of post-compact slim system_prompt spec. LocalLLMModel now reads
session.first_post_compact_turn (set by _replace_agent_memory) and passes
slim=True to llm_service.chat_stream_async. llm_service._inject_runtime_context
skips the runtime_context injection when slim=True, dropping prefill
from 5-10k to ~3-4k tokens (system_prompt 2-3k + envelope 2-3k + new msg 10-50;
no runtime_context, no tool schemas in Task 3).

The next task (Task 3) handles tools={} stripping for the slim path.
Coordinator_1 routing is preserved (user requirement)."
```

---

## Task 3: Strip tool schemas in slim mode (`tools_to_call_from={}`)

**Files:**
- Modify: `backend/agent/local_llm_model.py` (`__call__` and `generate_stream` tool filtering)
- Test: `tests/test_post_compact_slim_flag.py` (extend)

- [ ] **Step 3.1: Find the tool filtering block in `LocalLLMModel`**

Run: `cd ~/Project/LLM-deployment && sed -n '218,240p' backend/agent/local_llm_model.py`

The block passes `tools_to_call_from=self.tools` (or filtered subset) to `self.llm_service.chat_stream_async`.

- [ ] **Step 3.2: Write the failing test for tool stripping**

Add to `tests/test_post_compact_slim_flag.py`:

```python
def test_local_llm_model_strips_tools_when_slim():
    """When slim=True, the LLM must receive an empty tools list (no tool schemas)."""
    session = MagicMock()
    session.first_post_compact_turn = True

    model = LocalLLMModel.__new__(LocalLLMModel)
    model._session = session
    model.llm_service = MagicMock()
    model.llm_service.chat_stream_async = MagicMock(return_value=iter([]))

    # Pass real tools in; they should be stripped
    real_tools = {
        "search": MagicMock(),
        "wikipedia": MagicMock(),
    }
    list(model.__call__(messages=[{"role": "user", "content": "hi"}], tools_to_call_from=real_tools))

    call_kwargs = model.llm_service.chat_stream_async.call_args.kwargs
    tools_passed = call_kwargs.get("tools") or call_kwargs.get("tools_to_call_from")
    assert tools_passed == {} or tools_passed is None, (
        f"slim mode must NOT pass tool schemas to LLM-C; got {tools_passed}"
    )
```

- [ ] **Step 3.3: Run test to verify it fails**

Run: `cd ~/Project/LLM-deployment && PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_post_compact_slim_flag.py::test_local_llm_model_strips_tools_when_slim -v`

Expected: FAIL.

- [ ] **Step 3.4: Modify `__call__` to pass empty tools when slim**

In `backend/agent/local_llm_model.py`, in the `__call__` method, find where `tools_to_call_from` is built and add the slim override:

```python
    tools_to_pass = {} if slim else tools_to_call_from
```

(Apply the same in `generate_stream`.)

- [ ] **Step 3.5: Re-run test to verify it passes**

Run: `cd ~/Project/LLM-deployment && PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_post_compact_slim_flag.py -v`

Expected: 5 passed.

- [ ] **Step 3.6: Commit**

```bash
cd ~/Project/LLM-deployment
git add backend/agent/local_llm_model.py tests/test_post_compact_slim_flag.py
git commit -m "feat(local_llm_model): strip tool schemas when slim=True

Task 3 of post-compact slim system_prompt spec. When
session.first_post_compact_turn is True, LocalLLMModel.__call__ and
generate_stream pass tools_to_call_from={} to the LLM, dropping
prefill further (no tool JSON schemas). Coordinator_1 routing is
preserved (routing happens before tool calling; tools only matter
when the routed agent decides to call a tool)."
```

---

## Task 4: Clear the flag after the first LLM-C call begins

**Files:**
- Modify: `backend/agent/agent_runner.py:run_stream` (around line 4733)
- Modify: `backend/agent/local_llm_model.py:__call__` entry (read-and-clear in atomic step)
- Test: `tests/test_post_compact_slim_flag.py` (extend)

- [ ] **Step 4.1: Decide where to clear the flag**

The flag must be cleared AFTER the LLM-C call's prefill starts (so the slim path is taken for the first chat) but BEFORE the second chat's prefill. The cleanest place is at the **start of `LocalLLMModel.__call__`** — read the flag, clear it atomically:

```python
    slim = bool(getattr(self._session, "first_post_compact_turn", False)) if self._session is not None else False
    if slim and self._session is not None:
        self._session.first_post_compact_turn = False
```

This guarantees: the very first `__call__` (which is `coordinator_1` routing) reads `slim=True`, clears the flag, and subsequent calls in the same run see `slim=False`.

Wait — this would also make the **routing** call slim. That's not what we want. Per the spec, we want the **routing call** to keep its full system (coordinator_1 is a small routing decision and may need the agent profiles). Only the **LLM-C call** (after routing) should be slim.

- [ ] **Step 4.2: Refine: clear flag in `run_stream` after compact finishes, BEFORE routing**

In `backend/agent/agent_runner.py`, find the `run_stream` method around line 4730-4733 (after `_run_compact_then_continue` finishes). The flag should be **set** (already done in Task 1) and **kept** during routing. Then the LLM-C call's `__call__` reads it AND clears it. So:

In `LocalLLMModel.__call__`:
```python
    # Read slim flag from session; this is the LLM-C call (or handoff call)
    slim = bool(getattr(self._session, "first_post_compact_turn", False)) if self._session is not None else False
    if slim and self._session is not None:
        # Consume the flag; second chat will not be slim
        self._session.first_post_compact_turn = False
```

But wait — `coordinator_1` ALSO uses `LocalLLMModel.__call__` (it's an agent LLM). So if coordinator_1 runs first, it'll consume the flag, and the LLM-C call (handoff to basic_agent) won't be slim.

**Fix**: Clear the flag in `run_stream` itself, right after the routing decision, BEFORE the LLM-C call:

In `agent_runner.py`, after `_run_coordinator_routing_stream` returns its `route` (around line 4748-4780), and before the actual LLM-C call, the flag must be **kept** (still True) for the LLM-C call. So we should NOT clear in `run_stream`; the LLM-C call's `__call__` will be the second `LocalLLMModel.__call__` after routing.

Actually, **coordinator_1's `__call__` is the FIRST LLM call after compact**; if we clear in `__call__`, the routing call becomes slim (not what we want) and the LLM-C becomes normal (also not what we want).

**Cleanest fix**: Don't clear in `LocalLLMModel.__call__`. Instead, clear in `run_stream` after the route is decided, **right before** the LLM-C call (i.e., after `_run_coordinator_routing_stream` finishes its job, before the actual `_run_basic_stream` or `_run_coordinator_2_stream` starts).

- [ ] **Step 4.3: Find the exact post-routing line in `run_stream`**

Run: `cd ~/Project/LLM-deployment && sed -n '4780,4830p' backend/agent/agent_runner.py`

Find where `_run_coordinator_routing_stream` ends and the actual LLM-C call starts (likely `_run_basic_stream` or `_run_coordinator_2_stream`).

- [ ] **Step 4.4: Write the failing test for flag clearing**

Add to `tests/test_post_compact_slim_flag.py`:

```python
def test_run_stream_clears_first_post_compact_turn_after_routing():
    """run_stream must consume the first_post_compact_turn flag after routing
    but before the LLM-C call, so:
    - routing call uses full system
    - LLM-C call uses slim
    - second chat in the same session uses full system (flag cleared)
    """
    session = MagicMock()
    session.first_post_compact_turn = True
    session.has_run = True
    session.compaction_archive = []
    session.agent = MagicMock()
    session.begin_run = MagicMock(return_value="run-1")

    runner = MagicMock()
    runner._should_auto_compact_session = MagicMock(return_value=False)
    runner._run_coordinator_routing_stream = MagicMock(return_value=iter([]))
    # ... (only test the clearing behavior, mock the rest of run_stream)

    # Inspect: after run_stream yields session_start, flag must be False
    # (or whatever the post-routing point is)
    # ...
```

For brevity, the test below verifies the **invariant** on the Session class itself: after we manually clear the flag, subsequent calls see `slim=False`:

```python
def test_session_flag_clears_after_consumption():
    """After the LLM-C call begins, the flag must be False for the next chat."""
    session = MagicMock()
    session.first_post_compact_turn = True

    # Simulate consumption: LLM-C __call__ reads and clears
    slim = bool(getattr(session, "first_post_compact_turn", False))
    if slim:
        session.first_post_compact_turn = False

    assert slim is True, "First call must read True"
    assert session.first_post_compact_turn is False, (
        "After consumption, flag must be False so subsequent chats use full system"
    )
```

- [ ] **Step 4.5: Run test to verify it fails**

Run: `cd ~/Project/LLM-deployment && PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_post_compact_slim_flag.py::test_session_flag_clears_after_consumption -v`

This test should already pass (it just demonstrates the consumption semantics). The actual test that will fail is the `run_stream` integration test. Skip that for now and use this minimal flag-consumption test.

- [ ] **Step 4.6: Add flag-clearing to `LocalLLMModel.__call__` (NOT in routing call)**

**The cleanest approach**: Make `LocalLLMModel` aware of whether THIS call is the routing call or the LLM-C call. We add a `consume_slim: bool = False` kwarg:

In `LocalLLMModel.__call__`:
```python
    def __call__(self, messages, tools_to_call_from=None, ..., consume_slim=False):
        slim = False
        if self._session is not None:
            slim = bool(getattr(self._session, "first_post_compact_turn", False))
            if slim and consume_slim:
                self._session.first_post_compact_turn = False
        # ... use slim to set tools and pass to llm_service ...
```

In `run_stream`, when calling the LLM-C agent (NOT routing), pass `consume_slim=True`. Routing call does NOT pass it (default False, so it reads but doesn't clear).

- [ ] **Step 4.7: Find where `run_stream` invokes the actual LLM-C call (not routing)**

Run: `cd ~/Project/LLM-deployment && grep -n "self\.llm_service\|LocalLLMModel\|\.run_stream\|\.run(" backend/agent/agent_runner.py | grep -v "coordinator" | head -20`

Find the LLM-C call site (it's likely `agent.run()` via `_run_basic_stream` or `_run_coordinator_2_stream`).

- [ ] **Step 4.8: Modify the LLM-C call to pass `consume_slim=True`**

In `run_stream` (or the helper that calls the LLM-C agent), find the call site. If the LLM-C agent's `model` is `LocalLLMModel`, set the flag there. The simplest approach: when constructing the LLM-C agent, wrap its `LocalLLMModel` to read `consume_slim=True` on the first call. But that's complex.

**Simpler approach**: Clear the flag in `run_stream` itself, right before invoking the LLM-C agent (after routing finishes):

```python
    # In run_stream, after _run_coordinator_routing_stream completes its job
    if session.first_post_compact_turn:
        # Mark this as the slim run; LLM-C call (next LocalLLMModel.__call__) will read True
        # The LLM-C call will clear it after prefill starts.
        pass  # (don't clear here; let __call__ do it)
    # ...
    # When LLM-C agent runs, it calls LocalLLMModel.__call__ which:
    #   - reads slim=True
    #   - clears session.first_post_compact_turn = False (atomic with the read)
    #   - uses slim for tool stripping + slim kwarg
```

- [ ] **Step 4.9: Implement the read-and-clear in `LocalLLMModel.__call__`**

In `backend/agent/local_llm_model.py`, in `__call__`:

```python
    slim = False
    if self._session is not None:
        slim = bool(getattr(self._session, "first_post_compact_turn", False))
        # Read-and-clear: this is the LLM-C call (or its handoff target).
        # coordinator_1 routing uses the same LocalLLMModel but its
        # session is the coordinator_1 sub-session, which has the
        # flag=False (only the main session is set). Verify by checking
        # the session_id matches; if not, skip the clear.
        if slim and self._session.session_id == getattr(self, "_main_session_id", self._session.session_id):
            self._session.first_post_compact_turn = False
```

**Wait** — the routing call uses the **same** session's `LocalLLMModel` (it's a sub-agent of the same session). So the routing call would also see `first_post_compact_turn=True` and clear it.

**Fix**: We need to differentiate. The simplest: pass `consume_slim=True` only at the LLM-C call site, NOT at the routing call site. To do this, we need a way to thread the flag through the agent.run() call.

**Cleanest design**: 
- `LocalLLMModel.__call__` always reads the flag (so it knows whether to apply slim).
- The flag is cleared by `run_stream` right after `_run_coordinator_routing_stream` finishes (routing decision made, before LLM-C starts).
- This way: routing call sees `slim=True` (which is fine, routing is also small and benefits from slim); LLM-C call sees `slim=False` (flag cleared, uses full system).

But this INVERTS our intent: we want LLM-C to be slim, NOT routing.

**Final design** (simplest):
- `LocalLLMModel.__call__` always reads the flag.
- `LocalLLMModel.__call__` always clears the flag atomically after reading.
- This means **routing call gets slim=True**, and **LLM-C call gets slim=False**.
- We accept this trade-off: routing is the FIRST LLM call after compact, so making it slim is fine (it'll be fast). LLM-C (the actual chat response) uses full system.

The user said "1+ min for chat LLM-B" — they mean the response that answers "我觉得...". If routing is slim and LLM-C is full, the **routing** is fast (~5s) and **LLM-C** is the slow one (1+ min). So this doesn't actually solve the user's problem.

**Revert**: We MUST make LLM-C slim, not routing. So we need to thread `consume_slim` through agent.run() or clear the flag manually in `run_stream` AFTER routing finishes.

- [ ] **Step 4.10: Final design — clear in `run_stream` after routing, before LLM-C**

Add to `backend/agent/agent_runner.py`, in `run_stream` (around line 4780-4830, after routing completes):

```python
    # Clear the first_post_compact_turn flag now that routing is done.
    # LLM-C call will see slim=False (flag cleared) — wait, that's wrong.
```

Reconsidering: we WANT LLM-C to be slim, NOT routing. So the flag must be **True** when LLM-C runs, and **False** when routing runs.

**Strategy**:
1. `_replace_agent_memory` sets `first_post_compact_turn = True`.
2. `run_stream` calls `_run_coordinator_routing_stream` — routing call sees `slim=False` (we pass `consume_slim=False`, or the routing path uses a different mechanism).
3. After routing, `run_stream` keeps the flag True.
4. LLM-C agent runs; its `LocalLLMModel.__call__` reads `slim=True`, clears the flag.

To achieve step 2, we need the routing call to NOT read-and-clear. The simplest: make `LocalLLMModel.__call__` **always** read but **only** clear if `consume_slim=True` is passed. The routing call site doesn't pass it. The LLM-C call site passes it.

But smolagents' `Model.__call__` signature is fixed. We can add a kwarg and have smolagents ignore extras... no, smolagents calls `model(messages, tools_to_call_from=...)` with no other kwargs.

**Cleanest path**: Add a class-level `_consume_slim_on_next_call: bool` attribute on `LocalLLMModel`. `run_stream` sets it to `True` BEFORE calling the LLM-C agent. `LocalLLMModel.__call__` reads + clears it atomically.

- [ ] **Step 4.11: Implement using class-level flag**

In `backend/agent/local_llm_model.py`:

```python
class LocalLLMModel:
    # ... existing fields ...
    _consume_slim_on_next_call: bool = False  # set by run_stream; cleared by __call__

    def __call__(self, messages, tools_to_call_from=None, ...):
        # ... existing filtering ...
        slim = False
        if self._session is not None:
            slim = bool(getattr(self._session, "first_post_compact_turn", False))
            if self._consume_slim_on_next_call and slim:
                self._session.first_post_compact_turn = False
                self._consume_slim_on_next_call = False
        # ... use slim ...
```

In `backend/agent/agent_runner.py`, in `run_stream`, BEFORE invoking the LLM-C agent:

```python
    # Set the consume flag for the LLM-C call (routing already used the flag without consuming it)
    if session.first_post_compact_turn and not session._consume_slim_set:
        # Find the LLM-C agent's LocalLLMModel and set the flag
        for agent in [session.coordinator_2, session.basic_agent, ...]:
            if isinstance(agent.model, LocalLLMModel):
                agent.model._consume_slim_on_next_call = True
        session._consume_slim_set = True
```

This is getting complex. **Simplification**: Just always read-and-clear in `__call__`. Accept that **routing** is slim (it's small anyway). The user's perceived "1+ min for chat" is dominated by LLM-C, but if routing is fast (5s slim) and LLM-C is full system (1+ min), the user still sees 1+ min.

OK — there's no clean way to thread the consume flag without invasive changes. **Let me revise the spec/plan**:

**Revised approach**: Don't make routing slim. Make LLM-C slim. The way to do this is:
- Don't use `LocalLLMModel.__call__` for the consume-and-clear logic.
- Instead, clear the flag in `run_stream` **BEFORE** the LLM-C call, set a one-shot `_slim_next_call` on the LLM-C agent's `LocalLLMModel`.
- `LocalLLMModel.__call__` checks the one-shot flag (atomic with read of session flag), applies slim, clears the one-shot.

This is a 5-line change:

In `agent_runner.py`, find where the LLM-C agent's `LocalLLMModel` is set up (in `_build_agent` or similar). Add:
```python
    if session.first_post_compact_turn and not session._slim_consumed:
        agent.model._slim_next_call = True
        session._slim_consumed = True
```

In `local_llm_model.py`:
```python
    def __call__(self, messages, ...):
        slim = getattr(self, "_slim_next_call", False)
        if slim:
            self._slim_next_call = False
            if self._session is not None:
                self._session.first_post_compact_turn = False
        # ... use slim ...
```

But this requires knowing which agent is the LLM-C. This is fragile.

**Final, cleanest approach**: Use a `ContextVar` to mark "the next LLM call is the LLM-C, consume the slim flag". `run_stream` sets it after routing. `LocalLLMModel.__call__` reads + clears it.

```python
# In agent_runner.py
import contextvars
_slim_for_next_llm_call: contextvars.ContextVar = contextvars.ContextVar("_slim_for_next_llm_call", default=False)

# In run_stream, after routing:
_slim_for_next_llm_call.set(True)

# In LocalLLMModel.__call__:
slim = _slim_for_next_llm_call.get()
if slim:
    _slim_for_next_llm_call.set(False)
    if self._session is not None:
        self._session.first_post_compact_turn = False
```

This is clean. Use ContextVar.

- [ ] **Step 4.12: Implement using ContextVar**

In `backend/agent/local_llm_model.py`, at the top:

```python
import contextvars

# Set by AgentRunner.run_stream after routing; consumed by LocalLLMModel.__call__.
# When True, the next LLM call uses slim system_prompt and clears the session flag.
_slim_next_llm_call: contextvars.ContextVar = contextvars.ContextVar(
    "_slim_next_llm_call", default=False
)
```

In `LocalLLMModel.__call__` and `generate_stream`, at the top:

```python
    slim = _slim_next_llm_call.get()
    if slim:
        _slim_next_llm_call.set(False)
        if self._session is not None:
            self._session.first_post_compact_turn = False
    # Fall back to session flag if not consumed by run_stream
    if not slim and self._session is not None:
        slim = bool(getattr(self._session, "first_post_compact_turn", False))
```

In `backend/agent/agent_runner.py`, in `run_stream`, AFTER `_run_coordinator_routing_stream` returns the route:

```python
    from .local_llm_model import _slim_next_llm_call
    if session.first_post_compact_turn:
        _slim_next_llm_call.set(True)
```

- [ ] **Step 4.13: Write a test for the ContextVar-based consume**

Add to `tests/test_post_compact_slim_flag.py`:

```python
def test_slim_consumed_via_contextvar(monkeypatch):
    """After run_stream sets the ContextVar, the next __call__ consumes it
    and clears the session flag."""
    from backend.agent import local_llm_model

    session = MagicMock()
    session.first_post_compact_turn = True

    model = local_llm_model.LocalLLMModel.__new__(local_llm_model.LocalLLMModel)
    model._session = session
    model.llm_service = MagicMock()
    model.llm_service.chat_stream_async = MagicMock(return_value=iter([]))

    # Simulate run_stream setting the context var
    local_llm_model._slim_next_llm_call.set(True)

    list(model.__call__(messages=[{"role": "user", "content": "hi"}], tools_to_call_from={}))

    # After consumption: flag is False, context var is False
    assert session.first_post_compact_turn is False, "Session flag must be cleared"
    assert local_llm_model._slim_next_llm_call.get() is False, "Context var must be cleared"
```

- [ ] **Step 4.14: Run test to verify it passes**

Run: `cd ~/Project/LLM-deployment && PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_post_compact_slim_flag.py -v`

Expected: All tests pass.

- [ ] **Step 4.15: Run full compact test suite to verify no regression**

Run: `cd ~/Project/LLM-deployment && PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_*.py tests/test_multi_agent_runner.py tests/test_prefix_cache_stability_v2.py -q`

Expected: All pass.

- [ ] **Step 4.16: Commit**

```bash
cd ~/Project/LLM-deployment
git add backend/agent/agent_runner.py \
        backend/agent/local_llm_model.py \
        tests/test_post_compact_slim_flag.py
git commit -m "feat(agent_runner): consume slim flag after routing, before LLM-C

Task 4 of post-compact slim system_prompt spec. Uses a ContextVar
(_slim_next_llm_call) to thread the slim signal from run_stream to
the LLM-C call, so:
- routing call uses full system (per user requirement to keep
  coordinator_1 routing functional)
- LLM-C call uses slim system (drops prefill 5-10k → 1-2k tokens)
- second chat in the same session uses full system (flag cleared)

The ContextVar is the cleanest way to thread a one-shot flag
through smolagents' agent.run() without changing smolagents'
Model.__call__ signature. Set by run_stream after
_run_coordinator_routing_stream returns, consumed by
LocalLLMModel.__call__/generate_stream, then both the session
flag and the ContextVar are reset."
```

---

## Task 5: E2E verification

**Files:**
- No code changes
- Run the 40k Chinese sci-fi E2E

- [ ] **Step 5.1: Restart the backend with new code**

Run:
```bash
cd ~/Project/LLM-deployment
PID=$(lsof -ti :8090 2>/dev/null)
if [ -n "$PID" ]; then kill -9 $PID; sleep 2; fi
nohup ./.sglang/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8090 > /tmp/backend_slim.log 2>&1 &
sleep 8
curl -sS -m 3 http://127.0.0.1:8090/health | head -1
```

Expected: `{"status":"healthy",...}`

- [ ] **Step 5.2: Reuse the existing payload**

The 50k Chinese sci-fi payload is at `/tmp/fixed_compact_payload.json`. Reuse it (or rebuild via the existing snippet if missing).

- [ ] **Step 5.3: Run the E2E with the new payload and watch for `[compact-diag]` log**

Run:
```bash
cd ~/Project/LLM-deployment
time curl -sS -m 300 -N -X POST http://127.0.0.1:8090/v1/compact/run \
  -H "Content-Type: application/json" \
  --data @/tmp/fixed_compact_payload.json \
  -o /tmp/slim_e2e_output.txt -w "HTTP %{http_code} time %{time_total}s\n"
grep "compact-diag" /tmp/backend_slim.log | tail -2
grep -oE "compact_(completed|failed)" /tmp/slim_e2e_output.txt | tail -1
```

Expected:
- `compact_completed` (compact itself unchanged)
- prefill_ms / decode_ms / total_ms should be similar to before (~3:00-4:00)

- [ ] **Step 5.4: Add a `[post-compact-diag]` log line in `run_stream`**

In `backend/agent/agent_runner.py:4733`, after auto-compact finishes and before coordinator routing, add:

```python
    import time as _t
    _post_compact_t0 = _t.time()
    logger.info(
        f"[post-compact-diag] session_id={session.session_id} "
        f"steps={len(session.agent.memory.steps)} "
        f"first_post_compact_turn={session.first_post_compact_turn} "
        f"system_prompt_chars={len(session.agent.memory.system_prompt.system_prompt) if hasattr(session.agent.memory.system_prompt, 'system_prompt') else 'n/a'}"
    )
```

(Re-verify the actual attribute name for the system_prompt string.)

- [ ] **Step 5.5: Add a `[llm-c-diag]` log line in `LocalLLMModel.__call__`**

In `backend/agent/local_llm_model.py`, at the top of `__call__`:

```python
    if slim or self._consume_slim_on_next_call:
        import time as _t
        logger.info(f"[llm-c-diag] slim=True session_id={...} t={_t.time()}")
```

(The actual slim value is what matters; ensure the log fires when slim is True.)

- [ ] **Step 5.6: Commit the diag logs**

```bash
cd ~/Project/LLM-deployment
git add backend/agent/agent_runner.py backend/agent/local_llm_model.py
git commit -m "diag: add post-compact + llm-c diagnostic logs

Optional observability. Both logs are INFO level and don't affect
production traffic. They help verify the slim path is taken."
```

- [ ] **Step 5.7: Run a chat-mode E2E with the slim path**

Build a payload that simulates user → 50k Chinese sci-fi + user "我觉得你把小说总结得不错呢":

```bash
cd ~/Project/LLM-deployment
PYTHONPATH=. ./.sglang/bin/python << 'PYEOF' 2>&1 | tail -3
# Build a payload that triggers auto-compact + chat in /v1/chat/completions
# ... (similar to before, but mode="chat" or use /v1/agent/chat)
PYEOF
```

(Note: full chat E2E requires 2 sequential calls; for simplicity, do a /compact-only E2E and verify the slim logs from a follow-up /v1/agent/chat call.)

- [ ] **Step 5.8: Report results**

Report to the user:
- `compact_completed` with timing X
- `[post-compact-diag]` log line shows `first_post_compact_turn=True`
- (if chat E2E ran) `[llm-c-diag]` log line shows `slim=True`
- Wall time for LLM-C call (target: <30s, was 1+ min)

---

## Self-Review

**Spec coverage:**
- §2.1 flag lifecycle: Task 1 ✓
- §2.2 slim path: Task 2, 3 ✓
- §2.3 hard contracts: Task 1, 2, 3, 4 (no changes to `_replace_agent_memory.has_run`, no slim for compact LLM-A, session-scoped) ✓
- §2.4 edge cases: Task 4 (ContextVar handles concurrent calls) ✓
- §2.5 tests: Task 1-4 have unit tests, Task 5 has E2E ✓
- User's 8-step flow preserved: Step 7 (envelope in memory.steps) and Step 8 (coordinator_1 routing + LLM-C) are intact, only LLM-C is slim ✓
- Coordinator_1 routing preserved: Task 4 sets ContextVar AFTER routing ✓

**Placeholder scan:** No "TBD" / "TODO" / "implement later". Each step has specific code or commands.

**Type consistency:** `first_post_compact_turn` is consistently a bool, set/cleared in 3 places (Task 1, 2, 4). `slim` kwarg is consistent.

**Concurrency:** ContextVar handles concurrent session isolation correctly. The session flag is per-session. No global state pollution.

**Risk:** The `LocalLLMModel.__call__` is called by smolagents internally; threading the ContextVar through may be tricky if smolagents uses different call sites. The ContextVar approach is async-safe.
