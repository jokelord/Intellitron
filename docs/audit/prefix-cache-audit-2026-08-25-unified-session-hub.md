# Unified Session Hub Backend Prefix/Memory Audit (2026-08-25)

## Scope

Backend-only audit for Task 7. No model prompt, provider, compact, or frontend
changes were made during this audit.

## Hub memory and release evidence

- `backend/session_hub_runtime.py:SessionHubRuntime.release_if_same` checks
  entry identity before unregistering, then releases the exact Hub and invokes
  its resource callback. A same-string replacement therefore cannot be cleared
  by stale cleanup.
- `backend/agent/session_hub.py:AgentSessionHub.release` detaches subscribers
  and clears the journal; terminal completion does not release the Hub, leaving
  recovery records available.
- `backend/api/chat_session_store.py:ChatSessionStore.release_if_same` removes
  only the exact record/entry and delegates exact Hub release. TTL cleanup skips
  active producers.
- `backend/api/session_hub.py` deletion pins the exact entry, waits active Chat
  cleanup, and releases only after the producer is terminal.

Required memory/release tests passed:

```text
tests/test_session_hub_runtime.py -k 'memory or release or ttl or replacement or subscriber'
7 passed, 24 deselected

tests/test_chat_session_hub.py -k 'memory or release or ttl or replacement or subscriber'
4 passed, 12 deselected
```

`tests/test_session_store_cleanup.py` reached seven passing tests, then hit its
existing `dispose_idle_session...` async lifecycle test without completing under
a 20-second bound. It is reported as bounded rather than a pass; no production
change was inferred from an open-handle timeout.

## Prompt and prefix isolation

Chat request construction remains in `backend/api/chat.py`; Hub payloads are
published only through `SessionProducerPublisher` and are not passed to
`LLMService.chat_async` or stream message construction. Agent model input is
constructed by `backend/agent/agent_runner.py` and `backend/agent/local_llm_model.py`;
Hub sequence, attachment, request-acceptance, and terminal metadata remain
transport events. Existing prefix/compact invariants are covered by the
repository's compact and prefix-cache suites and were not altered here.

No Base64 image bodies, retained timeline payloads, request IDs, or replay
cursors are serialized into model messages by the audited Hub paths.

## Verification boundary

The complete Task 7 backend command was not reported as passing because the
session-store cleanup suite has the bounded open-handle case above. Focused
memory/release suites that completed are recorded with exact counts.

No git command was used.
