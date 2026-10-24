# Unified Session Hub for Chat and Agent Lifecycles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement this plan task-by-task. Every implementation task also requires `superpowers:systematic-debugging`, `superpowers:test-driven-development`, and `superpowers:verification-before-completion`. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ordinary Chat and Agent mode use one backend Session Hub lifecycle and one frontend sequenced consumer, fix non-Agent Vision Automation acceptance, and eliminate the remaining Answer Now transport-ownership races.

**Architecture:** A shared `SessionHubRuntime` owns session lookup, canonical event publication, producer terminalization, cursor subscription, and deterministic release. Chat and Agent retain only source/projection adapters. The frontend uses one sequenced Session Hub transport and permanent attachment tokens; Answer Now remains provisional until replacement acceptance.

**Tech Stack:** Python 3.12, FastAPI/Starlette SSE, asyncio, Pydantic v2, pytest, Next.js 14, React, TypeScript, Node test runner.

**Spec:** `docs/superpowers/specs/2026-08-25-unified-session-hub-chat-agent-lifecycle-design.md`

## Global Constraints

- Read the complete spec and repository `AGENTS.md` before editing.
- Do not run any git command. Do not create a branch, worktree, stash, commit, or modify dependency source.
- Work in the existing workspace and preserve all unrelated changes made by other agents.
- Treat current code as authoritative; do not assume a prior review line number or implementation shape is still current.
- Use `session_id` as the only public recoverable conversation identity. Do not add a public `run_id`.
- Keep `request_id` for admission, request status, and targeted provider cancellation; it is not a recovery identity.
- Chat and Agent must share backend Hub admission/publication/subscription/terminal/release mechanics and frontend sequence/detach/terminal mechanics.
- Domain adapters may differ only in translating Chat chunks versus Agent timeline/tool events.
- No raw Chat SSE copy or cumulative full-text snapshots may remain in the canonical Hub journal.
- No Hub event, subscriber token, recovery descriptor, UI ID, or replay cursor may enter LLM prompts, canonical history, `memory.steps`, or tool traces.
- Preserve the Prefix-Cache Covenant, Vision Automation historical-image exception, compact prompt shapes, provider equivalence, and targeted-abort semantics.
- Stop, Answer Now, and recovery-button logic remain independent.
- Backend content and observations have no character limit. `webUI.content.bubble_character_limit: 20000` remains rendering-only.
- Text and Vision pages must remain behaviorally synchronized.
- Every production behavior change follows strict RED -> verify RED -> minimal GREEN -> verify GREEN -> refactor.
- Reuse the prior frontend lifecycle implementer for Tasks 4-6 if its agent context can be resumed; repository `AGENTS.md` context-reuse rules override the generic fresh-agent preference.
- Use no more than three implementation/review agents across this plan: one backend implementer reused for Tasks 1-3, the prior frontend lifecycle implementer reused for Tasks 4-6, and one independent final reviewer. If the prior frontend implementer can safely own the backend work too, prefer one serial implementer plus one reviewer.

## File and Responsibility Map

| File | Responsibility after this plan |
|---|---|
| `backend/agent/session_hub.py` | Existing immutable journal, sequence allocator, producer generations, cursor subscribers. No Chat/Agent domain policy. |
| `backend/session_hub_runtime.py` | Shared directory entry, producer publisher, canonical SSE encoder/subscriber, pinned release. |
| `backend/main.py` | Create one process-level `SessionHubRuntime` and expose it through `app.state`. |
| `backend/api/chat_session_store.py` | Chat session/context metadata and shared-Hub registration; no private subscription protocol. |
| `backend/api/chat.py` | Chat source adapter, stable `session_id`, shared producer publisher, and thin compatibility route only. |
| `backend/api/agent.py` | Agent source adapter using shared publisher/subscriber; thin compatibility route only. |
| `backend/agent/agent_runner.py` | Agent session/domain events and session-owned Vision display assets; no subscriber ownership. |
| `backend/api/session_hub.py` | Canonical `GET /v1/session/{session_id}/events` and explicit shared session deletion endpoint if needed by existing Clear behavior. |
| `frontend/src/lib/api.ts` | One Session Hub SSE reader for POST and recovery; known rejection and stable-terminal transport semantics. |
| `frontend/src/lib/session-hub-client.ts` | Sequence cursor, transport ownership token, domain-neutral payload dispatch contracts. |
| `frontend/src/lib/session-hub-recovery.ts` | Shared Chat/Agent bookmark and attachment state; migration from existing bookmarks/transcripts. |
| `frontend/src/lib/text-agent-session-handlers.ts` | Agent-domain projection adapter only. |
| `frontend/src/lib/chat-session-handlers.ts` | Chat-domain turn/delta projection adapter using canonical Hub events. |
| `frontend/src/lib/timeline-projection.ts` | Project `source: chat` and existing `source: coordinator_2` message deltas explicitly. |
| `frontend/src/lib/vision-automation/use-vision-automation.ts` | Provisional Answer Now replacement and buffered original terminal reconciliation. |
| `frontend/src/lib/vision-automation/page-integration.ts` | Permanent transport/attachment ownership helpers and terminal gates. |
| `frontend/src/app/page.tsx` | Thin Text page glue for shared Chat/Agent consumer and recovery. |
| `frontend/src/app/vision/page.tsx` | Thin Vision page glue, shared automation acceptance, and Answer Now ownership. |

---

### Task 1: Add the Shared Session Hub Runtime and Canonical Subscriber

**Owner:** backend implementer; reuse this implementer for Tasks 2 and 3.

**Files:**
- Create: `backend/session_hub_runtime.py`
- Create: `backend/api/session_hub.py`
- Modify: `backend/main.py`
- Create: `tests/test_session_hub_runtime.py`
- Modify: `tests/test_agent_session_hub.py`

**Interfaces:**
- Consumes: `backend.agent.session_hub.AgentSessionHub` and `SessionEventRecord`.
- Produces: `SessionHubEntry`, `SessionHubRuntime`, `SessionProducerPublisher`, `encode_session_hub_record()`, and `subscribe_session_hub()`.
- Produces: `GET /v1/session/{session_id}/events?after_sequence=N`.
- Later tasks register Chat and Agent entries without implementing another subscriber loop.

- [ ] **Step 1: Write RED tests for one directory and one canonical subscription path**

Create literal tests for:

```python
runtime = SessionHubRuntime()
entry = runtime.register(session_id="session-a", kind="chat", hub=AgentSessionHub())
assert runtime.resolve("session-a") is entry
with pytest.raises(SessionHubConflict):
    runtime.register(session_id="session-a", kind="agent", hub=AgentSessionHub())
```

Add an async test that appends `request_accepted`, `timeline_event`, and `done`, subscribes after sequence zero, and asserts exact `[1, 2, 3]` delivery. Add a second subscriber after sequence one and assert `[2, 3]`. Assert release of the exact entry makes resolution unavailable, while `release_if_same(old_entry)` cannot release a replacement registered under the same string ID.

- [ ] **Step 2: Run Task 1 tests and verify genuine RED**

Run:

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_session_hub_runtime.py \
  tests/test_agent_session_hub.py
```

Expected: import/collection failure for `backend.session_hub_runtime` or missing runtime interfaces. A pass is invalid RED and requires correcting the test.

- [ ] **Step 3: Implement the directory entry and pinned release**

Implement the following contracts without domain imports:

```python
@dataclass(slots=True)
class SessionHubEntry:
    session_id: str
    kind: Literal["chat", "agent"]
    hub: AgentSessionHub
    resolve_payload: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    release_resources: Callable[[], None] | None = None

class SessionHubRuntime:
    def register(self, *, session_id: str, kind: SessionKind,
                 hub: AgentSessionHub,
                 resolve_payload: PayloadResolver | None = None,
                 release_resources: ReleaseCallback | None = None) -> SessionHubEntry: ...
    def resolve(self, session_id: str) -> SessionHubEntry | None: ...
    def release_if_same(self, entry: SessionHubEntry) -> bool: ...
```

Use a lock for registry mutation. Validate non-empty session IDs. `release_if_same()` checks object identity before removing, calls `hub.release()`, then releases entry-owned resources exactly once.

- [ ] **Step 4: Implement shared encoding and subscription**

`encode_session_hub_record(record, entry)` must copy the frozen top-level payload, call the optional asset resolver on that copy, add `event_sequence`, and JSON-encode one SSE record. `subscribe_session_hub(entry, after_sequence)` must validate a non-negative integer cursor and iterate only `entry.hub.subscribe()`.

Do not add mode-specific filtering such as Chat `wire` suppression or Agent image logic here; those belong in payload resolvers/adapters.

- [ ] **Step 5: Write RED tests for producer acceptance/terminal invariants**

Construct a fake request-lifecycle registry and assert:

```python
publisher.accept(turn_started_payload)
publisher.accept(turn_started_payload)  # raises duplicate-accept error
publisher.finish("completed")
publisher.finish("completed")           # no second done/terminal transition
```

Mutation checks must catch: publishing a delta before acceptance, missing `request_accepted`, two `done` records, lifecycle terminal before Hub terminal publication, and stale producer release.

- [ ] **Step 6: Implement `SessionProducerPublisher`**

Expose a narrow stateful interface:

```python
class SessionProducerPublisher:
    def accept(self, turn_started: dict[str, Any]) -> None: ...
    def publish(self, payload: dict[str, Any]) -> SessionEventRecord: ...
    def finish(self, status: Literal["completed", "aborted", "error"],
               error: BaseException | None = None) -> None: ...
```

`accept()` marks request accepted/running and appends `request_accepted` followed by `turn_started`. `finish()` appends one error only when needed, appends one done payload, closes the Hub generation, marks request terminal, and releases the exact producer token in `finally` blocks.

- [ ] **Step 7: Wire one runtime into FastAPI and add the canonical endpoint**

Initialize `app.state.session_hub_runtime` in both module-level and lifespan app initialization paths. Include `backend.api.session_hub.router`. Tests that create isolated FastAPI apps may inject their own runtime.

- [ ] **Step 8: Run Task 1 GREEN verification**

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_session_hub_runtime.py \
  tests/test_agent_session_hub.py
./.sglang/bin/python -m py_compile \
  backend/session_hub_runtime.py \
  backend/api/session_hub.py \
  backend/main.py
```

Record exact pass counts and any warnings in the task report. Do not use git for the checkpoint.

---

### Task 2: Migrate Ordinary Chat to Canonical Session Hub Events

**Owner:** same backend implementer as Task 1.

**Files:**
- Modify: `backend/api/chat.py`
- Modify: `backend/api/chat_session_store.py`
- Modify: `backend/vision/history_omission.py` only if the existing Chat commit hook must expose omission projection metadata
- Modify: `tests/test_chat_session_hub.py`
- Modify: `tests/test_request_lifecycle_integration.py`
- Create: `tests/test_chat_vision_history_omission.py`

**Interfaces:**
- Consumes: `SessionHubRuntime` and `SessionProducerPublisher` from Task 1.
- Produces: stable Chat `session_id`, sequential Chat turns, canonical `turn_started` and `message_delta` events.
- Removes: canonical reliance on `wire`, cumulative `content`, and cumulative `reasoning` Chat records.

- [ ] **Step 1: Write the exact RED regression for the reported stuck state**

Start the real Hub-backed Chat endpoint with a deterministic fake `_generate_stream_response()`. Supply a valid `session_id`, `request_id`, and Vision Automation metadata. Consume the POST SSE and assert the first two canonical payloads are:

```python
assert payloads[0] == {
    "event_sequence": 1,
    "type": "request_accepted",
    "request_id": request_id,
    "session_id": session_id,
    "status": "accepted",
}
assert payloads[1]["type"] == "timeline_event"
assert payloads[1]["event"]["kind"] == "turn_started"
```

The current code must fail because the Hub-backed response emits no `request_accepted`.

- [ ] **Step 2: Write RED tests for delta-only storage and subscriber independence**

Feed content deltas `"a"`, `"b"`, `"c"` and reasoning deltas `"r1"`, `"r2"`. Assert journal delta texts are exactly `['a', 'b', 'c', 'r1', 'r2']`, no payload has `type == 'wire'`, and no payload contains cumulative strings such as `abc` unless that exact string was one source delta.

Detach the initiating subscriber after acceptance, let the fake source complete, and assert request status terminal, producer released, and recovery replay contains all deltas and done.

- [ ] **Step 3: Run focused RED tests**

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_chat_session_hub.py \
  tests/test_request_lifecycle_integration.py -k 'chat and hub'
```

Expected: missing acceptance/canonical event assertions fail on current Chat producer behavior.

- [ ] **Step 4: Make Chat sessions sequential conversation sessions**

Replace `ChatSessionRecord.chat_session_id` with internal/public `session_id` while retaining a read-only compatibility property only if unchanged tests or callers require it. `ChatSessionStore.get_or_create(session_id)` returns the same record after a terminal turn. A new turn claims a new Hub producer generation and increments a session-local `turn_index`; it must not replace the journal.

Reject a second active producer for the same session with HTTP 409 before acceptance. Use pinned object identity for deletion/TTL.

- [ ] **Step 5: Add canonical `session_id` request validation**

Expose `ChatCompletionRequest.session_id`. If compatibility is required, accept `chat_session_id` as an input alias and normalize immediately. Add literal tests:

```python
assert ChatCompletionRequest(messages=[], session_id="s").session_id == "s"
assert "chat_session_id" not in legacy.model_dump()
assert response_for_conflicting_aliases.status_code == 422
```

Do not emit or store two IDs.

- [ ] **Step 6: Implement the Chat source adapter**

The detached Chat producer must:

1. claim the Hub producer token;
2. create `SessionProducerPublisher`;
3. publish acceptance and a Chat `turn_started` event before reading model deltas;
4. parse OpenAI chunks;
5. publish each new content/reasoning fragment as a `timeline_event` with `kind: message_delta`, `source: chat`, stable message ID, `display: chat_bubble`, and the `reasoning` flag;
6. finish exactly once.

The initiating POST returns `subscribe_session_hub(entry, start_sequence)` and does not own/close the source.

- [ ] **Step 7: Preserve Chat prompt and Vision omission boundaries**

Assert the exact `final_messages` sent to `LLMService` are unchanged relative to the pre-migration request for ordinary, compacted, and Vision Automation cases. Hub `turn_started` uses projection data only. It must not be inserted into `messages`, compact input, or `filter_messages_for_llm()`.

When omission has committed, publish only the projection metadata needed for recovery placeholders. Pre-accept validation/rejection must leave both model history and Hub turn history unchanged.

- [ ] **Step 8: Run Task 2 GREEN verification**

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_chat_session_hub.py \
  tests/test_request_lifecycle_integration.py \
  tests/test_request_lifecycle.py
./.sglang/bin/python -m py_compile \
  backend/api/chat.py \
  backend/api/chat_session_store.py
```

Also run the discovered Chat Vision omission tests. Record their exact command and pass count.

---

### Task 3: Register Agent Sessions in the Shared Runtime and Remove Duplicate Recovery Mechanics

**Owner:** same backend implementer as Tasks 1-2.

**Files:**
- Modify: `backend/api/agent.py`
- Modify: `backend/agent/agent_runner.py`
- Modify: `backend/api/session_hub.py`
- Modify: `tests/test_agent_session_recovery_api.py`
- Modify: `tests/test_agent_stop_api.py`
- Modify: `tests/test_session_store_cleanup.py`
- Modify: `tests/test_agent_session_context.py`

**Interfaces:**
- Consumes: Task 1 shared runtime/publisher/subscriber.
- Produces: Agent registration under the same canonical recovery endpoint used by Chat.
- Preserves: current Agent payload vocabulary, Answer Now Hub handoff, manager/child events, session-owned asset resolution, and run-scope cleanup barrier.

- [ ] **Step 1: Write RED parity tests for Chat and Agent canonical subscription**

Register one Chat and one Agent entry in the real runtime. Call `/v1/session/{id}/events` for each and assert both use the same sequence validation and SSE envelope. Patch or spy only the shared `subscribe_session_hub()` boundary and prove the legacy Agent route delegates to it rather than owning a second loop.

- [ ] **Step 2: Write RED tests for Agent publisher terminal ordering**

Using the existing deterministic top-level runner, assert journal order:

```python
assert types[0] == "request_accepted"
assert payloads[1]["event"]["kind"] == "turn_started"
assert types[-1] == "done"
assert cleanup_barrier_completed_before_done is True
assert lifecycle_terminal_after_done is True
```

Add producer failure and subscriber-detach cases. Existing Agent behavior should pass most semantic assertions; the new shared-publisher/runtime identity assertions provide RED.

- [ ] **Step 3: Run focused RED tests**

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_agent_session_recovery_api.py \
  tests/test_agent_stop_api.py -k 'session or hub or detach or answer_now'
```

Expected: shared runtime/delegation assertions fail before migration.

- [ ] **Step 4: Register exact Agent session objects with the runtime**

When a top-level or child `AgentSession` becomes recoverable, register its existing `event_hub`, payload asset resolver, and pinned release callback. If session creation loses a race, release only the rejected candidate entry. Session disposal and TTL call `runtime.release_if_same(entry)` after existing run-scope/tool cleanup barriers.

Do not create a second Hub object for an Agent session.

- [ ] **Step 5: Replace duplicated Agent publication finalization with the shared publisher**

Keep Agent admission timing: `request_accepted` cannot publish until the Agent run scope is registered. Keep Answer Now `claim_producer_after()` handoff. Replace only acceptance/terminal/error/release mechanics with `SessionProducerPublisher`; the domain source loop and cleanup barrier remain Agent-owned. The Agent adapter must await its captured run-scope cleanup barrier before it calls `publisher.finish(...)`. `SessionProducerPublisher` deliberately has no callback into Agent cleanup and cannot publish `done` early.

- [ ] **Step 6: Make old recovery endpoints thin adapters**

`/v1/agent/session/{id}/events` and `/v1/chat/session/{id}/events` may validate their legacy route parameters, then call the same runtime resolver/subscriber. Delete `_subscribe_agent_hub`, `replay_chat_session_events`, and other duplicate loops only after all callers use the shared implementation.

- [ ] **Step 7: Verify Stop, Answer Now, child, and disposal contracts**

Run targeted tests proving subscriber detach never triggers upstream abort; Stop still does targeted abort; Answer Now remains provider-independent and appends a new producer generation; child Hub records survive navigation; Trash/TTL release exact journal/assets.

- [ ] **Step 8: Run Task 3 GREEN verification**

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_agent_session_recovery_api.py \
  tests/test_agent_stop_api.py \
  tests/test_session_store_cleanup.py \
  tests/test_agent_session_context.py
./.sglang/bin/python -m py_compile \
  backend/api/agent.py \
  backend/agent/agent_runner.py \
  backend/api/session_hub.py
```

If the large `test_agent_session_context.py` suite is too slow, run the complete file once before final verification; focused `-k` runs are not sufficient final evidence.

---

### Task 4: Build One Frontend Session Hub Reader and Shared Chat Projection

**Owner:** resume the prior frontend lifecycle implementer. Do not dispatch a second frontend implementer in parallel.

**Files:**
- Create: `frontend/src/lib/session-hub-client.ts`
- Create: `frontend/src/lib/session-hub-client.test.mts`
- Create: `frontend/src/lib/chat-session-handlers.ts`
- Create: `frontend/src/lib/chat-session-handlers.test.mts`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/api.test.mts`
- Modify: `frontend/src/lib/timeline-projection.ts`
- Modify: `frontend/src/lib/timeline-projection.test.mts`

**Interfaces:**
- Produces: `readSessionHubStream()`, `SessionHubPayloadHandlers`, `SessionAttachmentToken`, `dispatchSessionHubPayload()`.
- Consumes: canonical events from Tasks 1-3.
- Later page tasks use these interfaces for both live POST responses and recovery GET responses.

- [ ] **Step 1: Write RED sequence and terminal tests against the wished-for reader**

Use real `ReadableStream` fixtures. Cover:

```typescript
await readSessionHubStream(response, {
  afterSequence: 0,
  expectedRequestId: "req-1",
  handlers,
});
assert.deepEqual(sequences, [1, 2, 3]);
assert.equal(terminal.kind, "terminal");
```

Separate tests assert duplicate suppression, exact gap error (`expected 2, received 3`), known non-2xx rejection before any SSE record, and successful-response EOF without done returning `detached` rather than terminal.

- [ ] **Step 2: Run RED tests**

```bash
cd frontend
node --test --experimental-strip-types \
  src/lib/session-hub-client.test.mts \
  src/lib/api.test.mts
```

Expected: missing module/interfaces or behavior failure. If the full API file retains the known open-handle problem, run exact `--test-name-pattern` cases and document the timeout separately; do not report exit 124 as PASS.

- [ ] **Step 3: Implement the domain-neutral reader and attachment token**

Use interfaces equivalent to:

```typescript
export interface SessionAttachmentToken {
  readonly sessionId: string;
  readonly token: symbol;
}

export type SessionHubReadResult =
  | { kind: "terminal"; lastSequence: number }
  | { kind: "detached"; lastSequence: number; error?: Error };

export async function readSessionHubStream(
  response: Response,
  input: SessionHubReadInput,
): Promise<SessionHubReadResult>;
```

The token is permanent. A registry method such as `isCurrent(token)` decides whether a callback may mutate attachment state. Do not infer authority from Stop/Answer Now flags or from an `AbortController` object stored in a ref.

- [ ] **Step 4: Refactor `LLMClient` to share parsing without changing request bodies**

`chatStream`, `agentChatStream`, `answerNowStream`, and the canonical recovery method all invoke `readSessionHubStream()` for sequenced Hub payloads. Keep non-Hub OpenAI compatibility only where the backend truly returns unsequenced legacy chunks during migration; isolate it behind one adapter and remove it once Task 3 guarantees canonical events.

Known rejection parsing remains shared. No non-2xx path may call `onTransportEof` or request status.

- [ ] **Step 5: Write RED tests for Chat turn reconstruction**

Feed literal `turn_started`, content delta, reasoning delta, and done records twice (live then replay duplicate). Assert one user bubble, one assistant bubble, content `AB`, reasoning `R`, stable IDs derived from session/turn, and no duplicate text.

- [ ] **Step 6: Implement `chat-session-handlers.ts` and timeline projection**

The Chat adapter applies `turn_started` and `source: chat` message deltas. Update `projectTimelineEvent()` so only these sources project:

```typescript
const bubbleSource = event.source === "coordinator_2" || event.source === "chat";
if (event.kind === "message_delta" && bubbleSource && event.display === "chat_bubble") { ... }
```

Do not project arbitrary future sources implicitly.

- [ ] **Step 7: Run Task 4 GREEN verification**

```bash
cd frontend
node --test --experimental-strip-types \
  src/lib/session-hub-client.test.mts \
  src/lib/chat-session-handlers.test.mts \
  src/lib/timeline-projection.test.mts
npm run type-check
```

Record focused API test results honestly if its known process-liveness issue persists.

---

### Task 5: Unify Chat/Agent Recovery and Fix Non-Agent Vision Automation

**Owner:** same frontend lifecycle implementer as Task 4.

**Files:**
- Create: `frontend/src/lib/session-hub-recovery.ts`
- Create: `frontend/src/lib/session-hub-recovery.test.mts`
- Modify: `frontend/src/lib/agent-session-recovery.ts`
- Modify: `frontend/src/lib/chat-transcript-storage.ts`
- Modify: `frontend/src/lib/message-types.ts`
- Modify: `frontend/src/lib/vision-automation/page-integration.ts`
- Modify: `frontend/src/lib/vision-automation/page-integration.test.mts`
- Modify: `frontend/src/lib/vision-automation/use-vision-automation.test.mts`
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Modify: `frontend/src/lib/text-agent-session-handlers.test.mts`
- Modify: `frontend/src/lib/vision-agent-session-recovery.test.mts`

**Interfaces:**
- Consumes: shared reader and Chat/Agent adapters from Task 4.
- Produces: one bookmark schema and one page attachment/busy lifecycle.
- Fixes: non-Agent Vision Automation acceptance and terminal visual state.

- [ ] **Step 1: Write a page-integration RED test for the reported bug**

Drive the real Vision Automation controller with the Chat submission adapter and a canonical Hub stream:

1. freeze a batch;
2. stage the page mutation;
3. deliver `request_accepted`;
4. deliver content/reasoning deltas;
5. deliver done/stable terminal.

Assert acceptance commits exactly once, final content is visible, `isLoading` is false, phase is `capturing`, Retry Submit/Status are false, and the assistant does not remain `思考中...`.

- [ ] **Step 2: Write RED tests for one bookmark/recovery path**

Use a storage fake and assert the persisted object is exactly:

```typescript
{
  schemaVersion: EXPECTED_VERSION,
  sessionId: "session-1",
  pageMode: "vision",
  interactionMode: "chat",
  createdAt: 123,
}
```

Assert no messages, image data URLs, observations, request ID, or cursor are serialized. Add Agent mode and Text page cases. Duplicate restore clicks must create one attachment.

- [ ] **Step 3: Run Task 5 RED tests**

```bash
cd frontend
node --test --experimental-strip-types \
  src/lib/session-hub-recovery.test.mts \
  src/lib/vision-automation/page-integration.test.mts \
  src/lib/vision-automation/use-vision-automation.test.mts
```

Expected: current Chat path lacks canonical acceptance/shared attachment and fails the new assertions.

- [ ] **Step 4: Implement the unified bookmark and migration**

Write new accepted sessions only through `session-hub-recovery.ts`. Read the old Agent bookmark and current Chat transcript keys only as migration inputs. Once a canonical Hub recovery succeeds, remove the migrated legacy key. Do not delete a legacy transcript merely because the backend is temporarily unavailable.

- [ ] **Step 5: Replace page-local Chat recovery with shared Hub recovery**

Both pages call one attach helper with `{sessionId, pageMode, interactionMode}`. Recovery subscribes to `/v1/session/{sessionId}/events?after_sequence=0`, selects the Chat or Agent projection adapter, sets busy on `request_accepted`/active turn, and clears busy only on stable terminal or an explicitly detached current token.

Remove direct calls to separate `chatSessionEvents()`/`agentSessionEvents()` after their compatibility tests are retained at the client boundary.

- [ ] **Step 6: Route live Chat Automation acceptance through the shared payload dispatcher**

The exact matching `request_accepted.request_id` invokes `automationInput.lifecycle.onRequestAccepted`, commits `AcceptanceMutationRegistry`, commits historical omission, and clears pending acceptance. Terminal goes through the same gate used by Agent mode. Page callbacks must not synthesize acceptance from first content.

- [ ] **Step 7: Preserve image omission and prefix boundaries during recovery**

Add literal replay fixtures with two automated image turns. Assert historical images become the existing placeholder text while latest-turn images resolve fully. Add a non-automation fixture and assert no image is replaced. Verify recovered UI translation, not Hub metadata, produces subsequent Chat/Agent wire messages.

- [ ] **Step 8: Run Task 5 GREEN verification**

```bash
cd frontend
node --test --experimental-strip-types \
  src/lib/session-hub-recovery.test.mts \
  src/lib/chat-session-handlers.test.mts \
  src/lib/text-agent-session-handlers.test.mts \
  src/lib/vision-agent-session-recovery.test.mts \
  src/lib/vision-automation/page-integration.test.mts \
  src/lib/vision-automation/use-vision-automation.test.mts
npm run type-check
```

---

### Task 6: Implement Provisional Answer Now and Permanent Callback Ownership

**Owner:** same frontend lifecycle implementer as Tasks 4-5.

**Files:**
- Modify: `frontend/src/lib/vision-automation/use-vision-automation.ts`
- Modify: `frontend/src/lib/vision-automation/use-vision-automation.test.mts`
- Modify: `frontend/src/lib/vision-automation/state-machine.ts`
- Modify: `frontend/src/lib/vision-automation/state-machine.test.mts`
- Modify: `frontend/src/lib/vision-automation/page-integration.ts`
- Modify: `frontend/src/lib/vision-automation/page-integration.test.mts`
- Modify: `frontend/src/lib/agent-session-recovery.ts`
- Modify: `frontend/src/lib/text-agent-session-handlers.ts`
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`

**Interfaces:**
- Consumes: permanent `SessionAttachmentToken` from Task 4.
- Produces: `ProvisionalAnswerNowReplacement` with acceptance/rejection/terminal reconciliation.
- Removes: immediate authoritative request-ID rewrite and restoration of aborted controllers.

- [ ] **Step 1: Write the lost-original-terminal RED test**

Use the exact sequence:

```typescript
await original.onRequestAccepted({ type: "request_accepted", requestId: originalId });
const replacement = controller.beginAnswerNowReplacement(replacementId);
await original.onStableTerminal({ requestId: originalId, status: "terminal" });
await replacement.onKnownRejection(retryable503);
assert.equal(controller.getState().phase, "capturing");
assert.equal(controller.getState().activeRequestId, undefined);
assert.deepEqual(statusQueries, []);
```

The current implementation must fail because it rewrites `transaction.requestId` and ignores the original terminal.

- [ ] **Step 2: Write delayed obsolete-detach RED tests for Text and Vision**

Create original and replacement attachment tokens. Reject replacement, then invoke delayed original `onDetached`. Assert session ID and bookmark remain attached/recoverable and current busy/terminal state is unchanged. Repeat after replacement acceptance and assert old callbacks are ignored.

- [ ] **Step 3: Write controller ownership RED tests**

Assert replacement rejection never assigns an already-aborted original `AbortController` to the current transport ref. Assert replacement acceptance atomically promotes the replacement token/request and buffered original terminal is discarded. Assert each stable terminal settles once.

- [ ] **Step 4: Run RED tests**

```bash
cd frontend
node --test --experimental-strip-types \
  src/lib/vision-automation/use-vision-automation.test.mts \
  src/lib/vision-automation/state-machine.test.mts \
  src/lib/vision-automation/page-integration.test.mts \
  src/lib/text-agent-session-handlers.test.mts \
  src/lib/vision-agent-session-recovery.test.mts
```

- [ ] **Step 5: Implement provisional replacement state**

Use an explicit object equivalent to:

```typescript
interface ProvisionalAnswerNowReplacement {
  originalRequestId: string;
  replacementRequestId: string;
  replacementToken: SessionAttachmentToken;
  replacementAccepted: boolean;
  bufferedOriginalTerminal: RequestStatusResult | null;
}
```

Do not mutate `transaction.requestId` on `beginAnswerNowReplacement()`. Original terminal callbacks buffer while provisional admission is unresolved. Acceptance promotes replacement ownership and discards the buffer. Known rejection discards provisional state and applies the buffered terminal through the ordinary stable-terminal function.

- [ ] **Step 6: Make ownership token authoritative in page integration**

Every detach/error/EOF/terminal closure captures its transport token. Replace checks such as `abortControllerRef.current === controller` where they authorize session deletion/detach with `attachmentRegistry.isCurrent(token)`. An aborted controller may remain as an inert local variable but cannot be restored as current ownership.

- [ ] **Step 7: Preserve Stop and Answer Now UI semantics**

Keep existing Stop/Answer Now display suppression, button loading, and targeted backend calls. Flags may choose rendering behavior but must not authorize attachment cleanup. Recovery-button visibility remains based on bookmark/attachment, not Stop/Answer Now flags.

- [ ] **Step 8: Run Task 6 GREEN verification**

```bash
cd frontend
node --test --experimental-strip-types \
  src/lib/vision-automation/use-vision-automation.test.mts \
  src/lib/vision-automation/state-machine.test.mts \
  src/lib/vision-automation/page-integration.test.mts \
  src/lib/text-agent-session-handlers.test.mts \
  src/lib/vision-agent-session-recovery.test.mts \
  src/lib/answer-now-session-dispatch.test.mts
npm run type-check
```

---

### Task 7: Memory/Compatibility Audit and Whole-System Verification

**Owner:** implementation remains with the relevant prior implementer; independent reviewer performs the final review after verification artifacts exist.

**Files:**
- Modify as required by findings: `backend/session_hub_runtime.py`, `backend/api/chat_session_store.py`, `backend/agent/agent_runner.py`
- Modify tests as required: `tests/test_session_hub_runtime.py`, `tests/test_chat_session_hub.py`, `tests/test_session_store_cleanup.py`
- Modify as required: `frontend/src/lib/session-hub-client.ts`, `frontend/src/lib/session-hub-recovery.ts`
- Create: `docs/prefix-cache-audit-2026-08-25-unified-session-hub.md`

**Interfaces:**
- Consumes all previous tasks.
- Produces release/memory evidence, prompt-shape audit, final verification report, and independent review verdict.

- [ ] **Step 1: Write RED memory-release tests before any cleanup correction**

Measure behavior, not private constant text. Create many small Chat deltas and assert retained payload text equals the sum of deltas within a small fixed envelope, not the sum of cumulative prefixes. Attach two subscribers and assert the journal object count/text does not double. Release a session and assert journal size, asset count, subscriber count, directory resolution, and request registrations reach zero.

Add same-string replacement and TTL tests that prove old cleanup cannot release the new entry.

- [ ] **Step 2: Run memory RED tests**

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_session_hub_runtime.py \
  tests/test_chat_session_hub.py \
  tests/test_session_store_cleanup.py -k 'memory or release or ttl or replacement or subscriber'
```

If they pass before cleanup changes, record that no production change is needed; do not manufacture a refactor.

- [ ] **Step 3: Apply only evidence-backed release corrections**

Ensure release ordering is: stop/wait active producer when explicitly deleting -> atomically unregister the exact directory entry -> call `hub.release()` to detach subscribers and clear the journal -> release session assets/tool resources in a `finally`-protected callback. Terminal completion releases producer/source/request resources but retains journal/assets for recovery policy.

Any new retention setting must be modeled in existing configuration and tested through `/config`; do not introduce a hard-coded TTL/capacity knob.

- [ ] **Step 4: Audit prompt and prefix-cache isolation**

Trace both Chat and Agent request construction from page state through API parsing to the exact `LLMService` messages. Record evidence that Hub-only fields are absent and that compact/omission behavior is unchanged. Save the audit document with exact test names and code anchors; do not include Base64 bodies or sensitive prompt content.

- [ ] **Step 5: Run complete backend verification**

At minimum:

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_session_hub_runtime.py \
  tests/test_chat_session_hub.py \
  tests/test_agent_session_hub.py \
  tests/test_agent_session_recovery_api.py \
  tests/test_agent_stop_api.py \
  tests/test_request_lifecycle_integration.py \
  tests/test_request_lifecycle.py \
  tests/test_session_store_cleanup.py \
  tests/test_agent_session_context.py
```

Also run the complete Vision historical-image omission/validation backend set. The required list at plan-writing time is:

```bash
./.sglang/bin/python -m pytest -q \
  tests/test_chat_vision_history_omission.py \
  tests/test_agent_vision_history_omission.py \
  tests/test_vision_request_validation.py \
  tests/test_request_lifecycle_integration.py
```

- [ ] **Step 6: Run complete focused frontend verification**

```bash
cd frontend
node --test --experimental-strip-types \
  src/lib/session-hub-client.test.mts \
  src/lib/session-hub-recovery.test.mts \
  src/lib/chat-session-handlers.test.mts \
  src/lib/text-agent-session-handlers.test.mts \
  src/lib/vision-agent-session-recovery.test.mts \
  src/lib/vision-automation/send-contract.test.mts \
  src/lib/vision-automation/state-machine.test.mts \
  src/lib/vision-automation/page-integration.test.mts \
  src/lib/vision-automation/use-vision-automation.test.mts \
  src/lib/answer-now-session-dispatch.test.mts \
  src/lib/timeline-projection.test.mts
npm run type-check
npm run build:verify
```

Run exact focused cases from `src/lib/api.test.mts`. If the full file still leaves an open handle and exits 124, report the focused passing tests and the unresolved harness liveness issue separately; never call the full file PASS.

- [ ] **Step 7: Perform deterministic browser smoke tests when the local stack is available**

Use the same browser and backend instance for:

1. `/vision`, Chat mode, start Vision Automation, send, observe acceptance/content/terminal, confirm no permanent `思考中...`.
2. Close/refresh after acceptance, reopen `/vision`, click `恢复运行中的任务`, observe replay then live final answer.
3. Repeat in Agent mode.
4. Trigger Answer Now while automation inference runs; cover accepted replacement and known rejection fixtures if backend test controls exist.
5. Invoke delayed old subscriber close/navigation and confirm bookmark/session remain.

Record timestamps, session IDs, request IDs, and event sequence ranges; redact image bodies.

- [ ] **Step 8: Dispatch independent whole-change review**

The reviewer must read this plan, its spec, implementation reports, verification outputs, and a file-based change package prepared without git commands. Review specifically for:

- two remaining lifecycle frameworks disguised as adapters;
- missing Chat acceptance or terminal ordering;
- request/session identity conflation;
- stale callback authority;
- Answer Now lost terminal;
- prompt/prefix pollution;
- O(n²) Chat journal retention;
- Text/Vision drift;
- image omission regression;
- false PASS claims from timed-out tests.

Critical or Important findings return to the owning prior implementer for a RED/GREEN fix and scoped re-review. The primary agent does not edit implementation code.

## Execution Order and Agent Allocation

1. Primary agent confirms the current workspace still matches the spec assumptions using read-only inspection.
2. Backend implementer executes Tasks 1 -> 2 -> 3 serially, preserving its context.
3. Prior frontend lifecycle implementer executes Tasks 4 -> 5 -> 6 serially, preserving its context. If it also owns backend work safely, keep one implementer rather than adding another.
4. Owning implementers complete Task 7 verification corrections serially.
5. One independent reviewer performs task-scoped reviews at each checkpoint when practical and the final whole-change review.
6. Primary agent independently verifies final commands and reports exact evidence.

No implementation agents work concurrently on overlapping files. No agent uses git commands.

## Plan Self-Review

### Spec coverage

- Unified backend directory, producer, subscriber, and release: Tasks 1-3.
- Chat acceptance bug and delta-only memory behavior: Task 2.
- Agent/Chat canonical recovery endpoint: Tasks 1 and 3.
- One frontend sequenced consumer: Task 4.
- Unified Chat/Agent bookmark and Text/Vision parity: Task 5.
- Vision Automation acceptance and historical-image semantics: Tasks 2 and 5.
- Answer Now provisional ownership and delayed callback races: Task 6.
- Memory release, prefix-cache isolation, complete verification, and independent review: Task 7.

### Type consistency

- `SessionHubEntry`, `SessionHubRuntime`, and `SessionProducerPublisher` are introduced in Task 1 and consumed unchanged by Tasks 2-3.
- `SessionAttachmentToken` and `readSessionHubStream()` are introduced in Task 4 and consumed unchanged by Tasks 5-6.
- `ProvisionalAnswerNowReplacement` is introduced and fully exercised in Task 6.
- Public identity names remain `session_id` and `request_id`; no step introduces `run_id`.

### Placeholder scan

The plan contains no `TBD`, `TODO`, “implement later,” unspecified “appropriate error handling,” or unbounded “write tests for the above” step. Each behavior-changing task names its RED command, expected failure, production interface, GREEN command, and review checkpoint.
