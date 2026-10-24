# Answer Now, Stop, and Global Prefix-Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Agent-only Answer Now wait for the interrupted worker to finish, generate a no-tool answer from backend-retained partial output, support SGLang and vLLM cancellation reliably, and enforce a backend-wide append-only Prefix-Cache Covenant.

**Architecture:** Capture the old Agent run-scope IDs, abort their actual provider request, and wait on a session condition until those workers complete rollback and cleanup. Freeze canonical Agent memory only after the barrier, preserve the normal system/runtime/tool prefix, append one explicit no-tool user tail, and run a single replacement request. Audit every backend local-LLM call site after this implementation and fix confirmed prefix drift without changing legitimate semantic boundaries.

**Tech Stack:** Python 3, FastAPI, asyncio/threading, smolagents, httpx/requests streaming, pytest, Next.js 14, React, TypeScript, Node test runner, SGLang, NVIDIA NGC vLLM.

## Global Constraints

- Answer Now is available only in Agent mode.
- Answer Now must wait without timeout until every run scope captured before cancellation has fully exited.
- The replacement prompt must explicitly say no tools and no tool calls for this Answer Now response only, without restricting later user turns; it must also forbid routing, handoff, and further planning.
- Tool calls emitted despite that instruction must never execute.
- Tool observations, including `read_file` text, remain `user` role; `tool_calls` remain present and paired with observations.
- Backend model input remains session-pinned system/runtime/tool prefix plus canonical serialized memory/messages plus append-only tails.
- Frontend bubbles, timeline events, dispatch text, routing deltas, UI state, session IDs, and usage diagnostics never enter prompt bodies.
- Preserve compact cache-first-tail behavior and do not change `performance_ratio`, `max_summary_tokens`, or other latency knobs.
- Do not modify `.sglang`, vLLM container package source, or other dependency source.
- Keep text and vision page behavior synchronized.
- Do not run Git commands without explicit user permission. The usual per-task commit steps are intentionally omitted.

## File Map

| File | Responsibility |
|---|---|
| `backend/agent/agent_runner.py` | Session run barrier, single-flight state, canonical Answer Now snapshot and replacement stream |
| `backend/api/agent.py` | Provider-neutral Stop/Answer Now API orchestration and HTTP conflict handling |
| `backend/llm_service.py` | Actual-provider request cancellation, upstream response closure, active-request iteration |
| `backend/main.py` | Active provider diagnostics in `/config` |
| `frontend/src/lib/api.ts` | Config typing and Answer Now SSE error/status handling |
| `frontend/src/lib/answer-now-ui.ts` | Shared button eligibility and terminal-state helpers |
| `frontend/src/app/page.tsx` | Text-page Answer Now glue |
| `frontend/src/app/vision/page.tsx` | Vision-page Answer Now glue |
| `tests/test_agent_session_context.py` | Run barrier, memory safety, no-tool prompt, disconnect, and role contracts |
| `tests/test_agent_stop_api.py` | API ordering, duplicate conflict, and provider-neutral cancellation |
| `tests/test_llm_service_*.py` | vLLM/SGLang request registry and cancellation contracts |
| `frontend/src/lib/answer-now-ui.test.mjs` | Shared frontend button and finalization behavior |
| `tests/test_web_ui_config.py` | `/config` active-provider diagnostics |
| `AGENTS.md` | Global Prefix-Cache Covenant and vLLM deployment/abort guidance |

---

### Task 1: Add a Captured Run-Scope Barrier and Answer Now Single Flight

**Files:**
- Modify: `backend/agent/agent_runner.py:2176-2315`
- Test: `tests/test_agent_session_context.py`

**Interfaces:**
- Produces: `AgentSession.capture_active_run_scopes() -> frozenset[str]`
- Produces: `AgentSession.wait_for_run_scopes(scope_ids: Collection[str], cancelled: Callable[[], bool] | None = None) -> bool`
- Produces: `AgentSession.try_begin_answer_now() -> bool`
- Produces: `AgentSession.finish_answer_now() -> None`
- Preserves: `begin_run()`, `finish_run()`, worker-owned rollback, and existing public abort behavior

- [ ] **Step 1: Write failing barrier and single-flight tests**

Add tests that start two scopes, capture them, wait in another thread, and prove the waiter remains blocked after only one `finish_run()`. Finish the second scope and assert the waiter returns. Start a third scope after capture and assert it does not extend the original wait. Add a cancellation callback test and a single-flight test:

```python
def test_wait_for_run_scopes_waits_only_for_captured_scopes():
    session = make_agent_session()
    first = session.begin_run()
    second = session.begin_run()
    captured = session.capture_active_run_scopes()
    result = []
    waiter = threading.Thread(
        target=lambda: result.append(session.wait_for_run_scopes(captured)),
    )
    waiter.start()
    session.finish_run(first)
    assert waiter.is_alive()
    later = session.begin_run()
    session.finish_run(second)
    waiter.join(timeout=1)
    assert result == [True]
    session.finish_run(later)


def test_answer_now_is_single_flight():
    session = make_agent_session()
    assert session.try_begin_answer_now() is True
    assert session.try_begin_answer_now() is False
    session.finish_answer_now()
    assert session.try_begin_answer_now() is True
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `./.sglang/bin/python -m pytest tests/test_agent_session_context.py -k 'run_scopes or answer_now_is_single_flight' -q`

Expected: FAIL because the four new methods do not exist.

- [ ] **Step 3: Implement condition-backed scope tracking**

Add one `threading.Condition` using the same lock that protects active scopes, plus a boolean single-flight field. `finish_run()` must perform rollback/cleanup ordering as it already does at the worker call site, then remove the exact scope and call `notify_all()`. The wait loop must compare only captured IDs:

```python
def capture_active_run_scopes(self) -> frozenset[str]:
    with self._run_condition:
        return frozenset(self._active_run_scopes)

def wait_for_run_scopes(self, scope_ids, cancelled=None) -> bool:
    pending = frozenset(scope_ids)
    with self._run_condition:
        while pending.intersection(self._active_run_scopes):
            if cancelled is not None and cancelled():
                return False
            self._run_condition.wait(timeout=0.1)
        return True

def try_begin_answer_now(self) -> bool:
    with self._run_condition:
        if self._answer_now_in_progress:
            return False
        self._answer_now_in_progress = True
        return True

def finish_answer_now(self) -> None:
    with self._run_condition:
        self._answer_now_in_progress = False
        self._run_condition.notify_all()
```

Use the existing active-scope field names rather than creating duplicate state. Do not make SSE generator cleanup call `finish_run()`.

- [ ] **Step 4: Run focused and existing run-scope regression tests**

Run: `./.sglang/bin/python -m pytest tests/test_agent_session_context.py -k 'run_scope or generator_close or single_flight' -q`

Expected: PASS, including the existing worker-owned cleanup contracts.

---

### Task 2: Make Provider Cancellation and Stop-All Provider Neutral

**Files:**
- Modify: `backend/llm_service.py:114-260,2031-2100`
- Modify: `backend/api/agent.py:156-230`
- Test: `tests/test_agent_stop_api.py`
- Test: the existing `tests/test_llm_service_*.py` file that owns request-registry tests

**Interfaces:**
- Consumes: existing request-ID response-closer/provider registry
- Produces: `abort_request_by_id(...)` with actual-provider behavior unchanged for callers
- Produces: a small active request-ID snapshot/iteration helper for stop-all
- Produces: one API-local helper that marks the session aborted and targets its current request

- [ ] **Step 1: Add failing vLLM, SGLang, stale-ID, and stop-all tests**

Cover these exact cases:

```python
def test_vllm_abort_closes_upstream_without_sglang_abort(...): ...
def test_sglang_abort_closes_upstream_and_calls_targeted_abort(...): ...
def test_pending_abort_before_response_registration_closes_on_register(...): ...
def test_old_request_cleanup_does_not_clear_new_request(...): ...
def test_stop_all_closes_each_active_vllm_response_without_sglang_abort_all(...): ...
```

The API test must set the active backend to vLLM, register two response closers, call `/v1/agent/stop-all`, and assert both close exactly once while `abort_sglang_all` is not called.

- [ ] **Step 2: Run the focused tests and verify the missing behavior**

Run: `./.sglang/bin/python -m pytest tests/test_agent_stop_api.py tests/test_llm_stream_usage_propagation.py -k 'abort or stop_all or request_id' -q`

Expected: at least the vLLM stop-all test fails because stop-all currently invokes only SGLang abort-all.

- [ ] **Step 3: Extract one targeted session-cancellation helper in the API**

Replace duplicated Stop and Answer Now request-ID cancellation blocks with a helper that:

```python
def _abort_agent_session_request(agent_runner, session_id: str) -> str | None:
    agent_runner.abort_session(session_id)
    request_id = agent_runner.get_current_request_id(session_id)
    if request_id:
        abort_request_by_id(request_id, _sglang_url(), fallback_to_abort_all=False)
    return request_id
```

Keep provider selection inside `abort_request_by_id()` so API code does not guess from configuration.

- [ ] **Step 4: Implement active-request snapshot cancellation for stop-all**

Expose only the minimum registry operation needed to snapshot active IDs safely. Stop-all marks all Agent sessions aborted, calls targeted abort for each active request ID, and calls `abort_sglang_all()` only if the actual active backend is SGLang. Never use SGLang abort-all for vLLM/OpenAI proxy.

- [ ] **Step 5: Suppress intentional-close error text**

In both provider stream exception paths, check the exact request's aborted state before yielding error content. Intentional cancellation should terminate the stream without synthesizing model text.

- [ ] **Step 6: Run cancellation/API regressions**

Run: `./.sglang/bin/python -m pytest tests/test_agent_stop_api.py tests/test_llm_stream_usage_propagation.py tests/test_cycle_executor.py -k 'abort or stop' -q`

Expected: PASS with no global abort used by a single-session operation.

---

### Task 3: Implement the Safe Answer Now Handoff and Explicit No-Tool Tail

**Files:**
- Modify: `backend/agent/agent_runner.py:3180-3217,4972-5147`
- Modify: `backend/api/agent.py:233-300`
- Test: `tests/test_agent_session_context.py:1707-2261`
- Test: `tests/test_agent_stop_api.py:65-155`

**Interfaces:**
- Consumes: Task 1 barrier/single-flight methods
- Consumes: Task 2 targeted cancellation helper
- Produces: shared async Answer Now preparation/handoff used by stream and non-stream APIs
- Produces: `_build_answer_now_prompt()` with an explicit no-tool contract
- Preserves: canonical `write_memory_to_messages()` roles and tool traces

- [ ] **Step 1: Add failing ordering and memory-safety tests**

Use events to hold an old worker open after abort. Start Answer Now and assert the model has not been called. Append the old partial `ActionStep.model_output`, perform rollback/cleanup, release the old scope, and assert the replacement messages contain the exact partial text before the final instruction.

Add tests proving:

```python
assert model.generate_stream.call_count == 0  # before old scope exits
assert replacement_messages[:-1] == canonical_messages_after_old_rollback
assert replacement_messages[-1].role == MessageRole.USER
assert "Do not call or execute any tool in this Answer Now response" in replacement_tail
assert "Use no tools for this response only" in replacement_tail
assert "This restriction does not apply to later user turns" in replacement_tail
```

Also prove old rollback cannot remove the replacement final `ActionStep`, disconnect during the barrier never calls the model, and a second Answer Now receives `409` without another run.

- [ ] **Step 2: Add failing no-tool enforcement and role tests**

Make the replacement model emit a tool call. Assert the tool executor is never invoked. If a corrective retry is used, assert its messages equal the first request plus one appended correction tail. Add/retain a memory serialization assertion:

```python
messages = agent.write_memory_to_messages()
assert observation_message.role == MessageRole.USER
assert "read_file output" in observation_message.content
assert calling_message.role == MessageRole.ASSISTANT
assert calling_message.tool_calls
```

- [ ] **Step 3: Run tests and verify the current overlap/prompt failures**

Run: `./.sglang/bin/python -m pytest tests/test_agent_session_context.py tests/test_agent_stop_api.py -k 'answer_now or write_memory' -q`

Expected: new barrier-ordering, duplicate-conflict, and explicit no-tool tests FAIL.

- [ ] **Step 4: Replace prompt wording with one self-contained no-tool tail**

The prompt must include equivalent explicit text, without allowing even a small tool call:

```text
This is an internal Answer Now instruction, not a new user request. Based only on the preceding conversation, retained partial reasoning, and completed tool observations, provide the best immediate final answer now. Do not call or execute any tool in this Answer Now response. Use no tools for this response only. This restriction does not apply to later user turns. Do not route, hand off, continue planning, or start further investigation. Return only the final answer for the user.
```

Do not inject the session ID, UI transcript, timeline, route receipt, or dispatch state.

- [ ] **Step 5: Implement shared preparation and barrier ordering**

Both APIs must acquire single flight before cancellation, capture old scopes before marking abort, target the old request ID, await old-scope completion without blocking the event loop, freeze canonical messages, and only then create the replacement scope/request ID. Use `asyncio.to_thread(session.wait_for_run_scopes, ...)` or the existing executor pattern.

Pass an SSE/client cancellation callback to the barrier. If cancelled while waiting, release single flight and return without creating a run or request ID.

- [ ] **Step 6: Preserve the stable tool-schema prefix while enforcing no execution**

Send the same `tools_and_managed_agents` shape used by the active normal Agent call so the prompt/tool prefix does not drift. Handle returned tool calls in Answer Now code, never through `process_tool_calls()`. Append one corrective user tail and retry once; fail explicitly after a second violation.

- [ ] **Step 7: Commit final memory only after successful terminal output**

Append exactly one `ActionStep` containing the final answer. Keep exact request-ID cleanup and worker-owned `finish_run()` in the replacement worker's `finally`. Always release Answer Now single flight, including exceptions and disconnects.

- [ ] **Step 8: Run Answer Now and memory-role regressions**

Run: `./.sglang/bin/python -m pytest tests/test_agent_session_context.py tests/test_agent_stop_api.py tests/test_write_memory_tool_calls.py -k 'answer_now or write_memory or tool_call' -q`

Expected: PASS; observations remain user role and no replacement tool executes.

---

### Task 4: Fix vLLM-Capable Answer Now UI and Active-Backend Diagnostics

**Files:**
- Modify: `backend/main.py:413-482`
- Modify: `frontend/src/lib/api.ts:152-272,849-937`
- Modify: `frontend/src/lib/answer-now-ui.ts:214-223`
- Modify: `frontend/src/app/page.tsx:1405-1466,2133-2165`
- Modify: `frontend/src/app/vision/page.tsx:1364-1425,2097-2128`
- Test: `tests/test_web_ui_config.py`
- Test: `frontend/src/lib/answer-now-ui.test.mjs`

**Interfaces:**
- Produces: `/config.inference.active_backend: "sglang" | "vllm" | "openai_proxy" | "mock" | null`
- Produces: `/config.inference.supports_targeted_abort: boolean`
- Extends: `AnswerNowButtonState` with `agentSessionId?: string | null`
- Preserves: Answer Now visibility only for `agentMode && isLoading`

- [ ] **Step 1: Add failing config and button tests**

Assert `/config` reports the actual backend. Add shared helper tests:

```javascript
assert.equal(shouldDisableAnswerNowButton({
  agentMode: true,
  isLoading: true,
  isAnswerNowLoading: false,
  agentSessionId: null,
}), true)

assert.equal(shouldDisableAnswerNowButton({
  agentMode: true,
  isLoading: true,
  isAnswerNowLoading: false,
  agentSessionId: 'vllm-agent-session',
}), false)
```

No provider field appears in the disable predicate.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `./.sglang/bin/python -m pytest tests/test_web_ui_config.py -q && node --test frontend/src/lib/answer-now-ui.test.mjs`

Expected: FAIL because diagnostics/session-aware button state are absent.

- [ ] **Step 3: Add backend diagnostics and frontend types**

Return the active backend already resolved in `backend/main.py`. Set `supports_targeted_abort` true for SGLang, vLLM, and OpenAI proxy because all have request-scoped cancellation in this application. Do not use these values to hide Answer Now.

- [ ] **Step 4: Update the shared disable predicate**

Implement:

```typescript
return !state.agentMode
  || !state.isLoading
  || !state.agentSessionId
  || state.isAnswerNowLoading;
```

- [ ] **Step 5: Pass `agentSessionId` from both pages and preserve one-bubble behavior**

Update both button calls identically. Keep handler refs as immediate click guards. Ensure old-stream terminal callbacks retain their existing Answer Now suppression and cannot clear replacement loading state.

- [ ] **Step 6: Run frontend/config tests and type/build checks**

Run: `./.sglang/bin/python -m pytest tests/test_web_ui_config.py -q`

Run: `node --test frontend/src/lib/answer-now-ui.test.mjs frontend/src/lib/timeline-projection.test.mts`

Run: `npm run build` from `frontend/`

Expected: all PASS; the text and vision pages compile with identical button rules.

---

### Task 5: Audit and Enforce the Global Prefix-Cache Covenant

**Files:**
- Audit: all `backend/**/*.py` local-LLM call sites
- Audit: `backend/compact/*.py`
- Audit: `backend/agent/agent_runner.py`
- Audit: `backend/agent/local_llm_model.py`
- Audit: `backend/llm_service.py`
- Modify: only confirmed violating files
- Test: existing compact, Agent context, chat streaming, and provider request tests

**Interfaces:**
- Consumes: stable Answer Now request shape from Task 3
- Produces: an audit table of every call site, its stable prefix source, appended tail, and any legitimate boundary
- Produces: regression tests for every confirmed violation fixed

- [ ] **Step 1: Dispatch an independent audit sub-agent**

Instruct it not to assume UI-visible text belongs in prompts. It must enumerate every call to `LLMService.generate`, `generate_stream`, model `__call__`, `generate_stream`, compact generation, coordinator routing, manager/future Agent generation, retries, and fallbacks. For each call, record:

```text
call site | system/runtime source | history source | tool schema source |
dynamic fields | append-only? | legitimate boundary? | test coverage
```

- [ ] **Step 2: Add failing prefix-shape tests for confirmed drift**

Use captured model call arguments, not token counts. For stable consecutive calls, assert:

```python
assert next_messages[:len(previous_messages)] == previous_messages
```

For serialized provider requests, compare deterministic JSON/message structures. Add explicit negative assertions for `session_id`, `context_pack`, UI/timeline/dispatch/routing text, and usage chunks.

- [ ] **Step 3: Preserve tool trace roles and ordering globally**

Assert all canonical Agent serialization retains:

```text
assistant: Calling tools ...
user: Observation ...
```

Never replace observations with summaries for cache reuse. Keep `read_file` output in user-role observation content.

- [ ] **Step 4: Fix confirmed middle-prefix mutation with the smallest change**

Move per-turn control data to a final self-contained tail, remove UI/audit data from prompt bodies, or reuse the canonical `memory.steps` serializer. Do not change intentional profile system prompts, multimodal representation, or compact memory replacement merely to force a hit.

- [ ] **Step 5: Verify compact invariants explicitly**

Run tests proving cache-first receives the untrimmed normal prefix first, `source_token_budget` gates only enabled reliable fallback, failed cache-first deltas remain buffered, thinking remains disabled, and `last_compactable_llm_messages` remains the preferred Agent source.

- [ ] **Step 6: Produce the exception report**

List each unavoidable boundary: first request, successful compact replacement, intentional Agent profile/tool-schema switch, required multimodal protocol change, or provider tokenizer/chat-template difference. For each, state why byte identity is impossible and where append-only stability resumes.

- [ ] **Step 7: Run the focused cache/message suites**

Run: `./.sglang/bin/python -m pytest tests -k 'compact or prefix or cache or write_memory or session_context or chat_streaming_filter' -q`

Expected: PASS. Any environment-only integration skip must be reported, not treated as a pass.

---

### Task 6: Update AGENTS.md with Global Covenant and vLLM Guidance

**Files:**
- Modify: `AGENTS.md:17-33,63-74,127-149,279-287`

**Interfaces:**
- Consumes: verified implementation and audit findings from Tasks 1-5
- Produces: future-agent guidance matching actual code and launch paths

- [ ] **Step 1: Rename and expand the covenant**

Rename `Compact Prefix-Cache Covenant` to `Prefix-Cache Covenant`. Retain every compact-specific rule, then add global append-only rules for chat, Agent, Answer Now, Stop-following turns, routing/handoff, retries, and both providers.

- [ ] **Step 2: Document the tool-role hard contract**

State explicitly that tool observations such as `read_file` output remain user role, `tool_calls` must not be stripped, and action/observation pairing is both a correctness and cache-stability requirement.

- [ ] **Step 3: Add concise vLLM container background**

Document:

- NVIDIA NGC vLLM is launched by `scripts/1b_start_vllm_container_with_snapshots.sh` for Hugging Face snapshot layout or `scripts/1c_start_vllm_container.sh` for a normal model directory.
- Active model/inference YAML comes through `config/config-overall.yaml`; vLLM launch values must not be hardcoded in shell.
- vLLM cancellation is request-scoped upstream response closure, unlike SGLang's additional `/abort_request`.
- The frontend must not provider-gate Agent Answer Now.
- request ID registration, pending abort, exact cleanup, and waiting for worker-owned run scope are mandatory.
- The container path commonly uses port 8000 and host networking according to selected config; the actual backend exposed by `/config` is diagnostic truth.

- [ ] **Step 4: Record the Answer Now handoff trap**

State that abort acknowledgement is not worker completion. Replacement generation starts only after captured old scopes finish rollback/cleanup. Its final user tail explicitly prohibits tools, and emitted tool calls are never executed.

- [ ] **Step 5: Re-read updated guidance against code**

Verify every named script, config field, endpoint, and helper exists. Remove stale line-number claims where code movement makes them fragile.

---

### Task 7: Independent Review and Full Verification

**Files:**
- Review: all files changed by Tasks 1-6
- Modify: only files needed to address confirmed review findings

**Interfaces:**
- Consumes: completed implementation, audit report, and documentation
- Produces: reviewed code plus verification evidence

- [ ] **Step 1: Dispatch a specification-compliance reviewer**

Require findings-first review for:

- old worker fully exits before replacement inference;
- exact partial output reaches Answer Now;
- explicit no-tool prompt and zero tool execution;
- vLLM upstream response closure;
- worker-owned run-scope cleanup;
- user-role tool observations;
- text/vision parity;
- global Prefix-Cache Covenant and documented exceptions.

- [ ] **Step 2: Dispatch a code-quality reviewer after compliance passes**

Ask it to inspect deadlocks, lost condition notifications, stale request-ID cleanup, SSE disconnect races, duplicate terminal state, provider mis-detection, and test false positives.

- [ ] **Step 3: Fix confirmed findings with focused failing tests first**

For every accepted finding, add or tighten a test, run it to observe failure, apply the smallest fix, and rerun the focused test. Do not accept speculative refactors unrelated to the design.

- [ ] **Step 4: Run backend verification**

Run focused suites:

```bash
./.sglang/bin/python -m pytest \
  tests/test_agent_session_context.py \
  tests/test_agent_stop_api.py \
  tests/test_write_memory_tool_calls.py \
  tests/test_search_and_fetch.py \
  tests/test_cycle_executor.py \
  tests/test_web_ui_config.py -q
```

Then run cache-related tests:

```bash
./.sglang/bin/python -m pytest tests -k \
  'compact or prefix or cache or chat_streaming_filter or llm_stream_usage' -q
```

Expected: PASS with only pre-existing/environment-specific skips explicitly identified.

- [ ] **Step 5: Run frontend verification**

From `frontend/` run:

```bash
node --test src/lib/answer-now-ui.test.mjs src/lib/timeline-projection.test.mts
npm run build
```

Expected: tests and production build PASS.

- [ ] **Step 6: Run broader available regression suite**

Run: `./.sglang/bin/python -m pytest tests -q`

Expected: PASS or a precise report of unrelated/pre-existing/environment-only failures. Never claim completion from focused tests alone if the broad suite reveals a regression.

- [ ] **Step 7: Report cache boundaries and verification evidence**

The final report must identify every unavoidable non-byte-identical boundary, tests/commands run, pass/fail counts, skipped integrations, and files changed. It must explicitly confirm that `read_file` and other tool observations still reach the LLM as user-role messages.
