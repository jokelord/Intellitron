# Agent Session Lifecycle-Safe Disposal Design

## Scope

Replace raw top-level Agent session deletion with an async, manager-scoped disposal operation. The operation must never abort unrelated sessions or use abort-all, must not release canonical resources before workers exit, and must prevent an old disposal or worker from affecting a later session that reuses the same public ID.

Also restore the selected NVFP4 inference configuration to serialized compilation (`1/1`) and lock it with tests.

## Ownership And Admission

`AgentRunner` owns disposal. Under `_agent_admission_lock`, it pins the exact `AgentSession` object and creates one shared async disposal task per public session ID. Concurrent DELETE calls await that same task. Agent admission and `get_or_create_session()` reject the ID while disposal is active.

The final store removal is identity-checked: `SessionStore.delete_if_same(session_id, pinned_session)` removes only the pinned object. This prevents an old callback or disposal task from deleting a replacement session with the same ID.

Answer Now acquisition is also owned by `AgentRunner`. `try_acquire_answer_now(session_id, expected_session=None)` holds `_agent_admission_lock` across current-session lookup, optional exact-object validation, disposal-task exclusion, and `AgentSession.try_begin_answer_now()`. It returns only acquired/not-found/busy outcomes. Disposal and stale-object cases share the generic busy outcome and do not alter canonical history.

The non-streaming endpoint uses this operation directly. The streaming response uses it only when the ASGI response body is invoked, preserving deferred acquisition and lifecycle reservation cleanup. A streaming response pins the object resolved during route setup; if that public ID is later reused, the old response cannot acquire against the replacement.

## Disposal Sequence

One configured deadline covers the full operation:

1. Pin the top-level session and collect only child runs/sessions whose `manager_session_id` equals the target ID.
2. Capture each pinned session's active run scopes and current provider request ID before aborting.
3. Mark top-level and child sessions aborted, set per-run abort state, and send targeted provider aborts with `fallback_to_abort_all=False`.
4. Wait for all captured top-level, Answer Now, and child run scopes/workers to exit. Active child terminal barriers are included where needed.
5. Close each distinct owned PIL image once after the barriers. Remove cached Base64 attributes, release TaskStep/ActionStep image containers, and clear canonical memory/state references.
6. Clear `last_compactable_llm_messages`, compact agent name, prompt snapshot, compaction archive, vision binding/cleanup token, timeline sinks, request-manager state, and abort/run-abort state.
7. Remove manager-owned child run mappings, event buffers, subscribers, and child session contexts; notify subscriber queues to close.
8. Identity-delete children and then the pinned top-level session.

Missing/already-disposed IDs return idempotent success. A timeout or internal failure returns a bounded structured diagnostic and does not raw-delete a still-active session.

## Configuration

Add `agent.session_disposal_timeout_seconds` to `AgentConfig` and existing YAML parsing. The default is `30.0`; values must be numeric, finite, and strictly positive. Disposal reads only this setting and does not reuse child execution timeouts.

Set `config/qwen3__6-NVIDIA-NVFP4-config-infer.yaml` values `compile_max_jobs` and `triton_num_compile_threads` to `1`. The selected-config launcher regression must assert `1/1`.

## Tests

Backend regressions cover idle disposal, active targeted abort, barrier ordering, Answer Now scope inclusion, PIL close and container release, compact/prompt/vision state release, manager-owned child cleanup without sibling impact, idempotent repeated/concurrent deletion, bounded timeout diagnostics, and same-ID identity safety. Config tests cover default, override, zero, negative, NaN, and infinity. Launcher/config tests prove the selected NVFP4 file remains `1/1`.

No frontend or Git operation is in scope.
