# Manual Compact Audit Findings (2026-06-15)

Scope: record-only notes for issues that are not being fixed in this step.

## Issue 3: compact card disappears on failure [maybe outdated]

- `frontend/src/lib/compact-projection.ts:195-206` handles `compact_failed` by filtering out the message whose `compactionBoundaryId` matches the failed summary id, then rolling back `isPreCompact` marks.
- This means the UI removes the compact summary card entirely on failure instead of leaving a failed card visible.
- `frontend/src/components/CompactSummaryPanel.tsx:94-97` has UI for a failed compression state, but the current projection removal path can prevent that failed state from remaining visible for normal failed compact attempts.
- Plausible symptom: if compact starts, streams some text, then validation fails, the user can see the compact card briefly and then see it disappear.

## Issue 4: manual compact callback can capture stale page state

- `frontend/src/app/page.tsx:1085-1175` defines `handleSelectSlashCommand` and uses `settings.agentMode`, `settings.systemPrompt`, `runtimeContextConfig.enable_runtime_time_context`, `runtimeTimeContext`, and `buildRuntimeTimeContext()` while the dependency array at `frontend/src/app/page.tsx:1174` only includes `[disabledCommandMap, agentSessionId]`.
- `frontend/src/app/vision/page.tsx:1043-1126` has the same pattern; it uses the same runtime/settings values while the dependency array at `frontend/src/app/vision/page.tsx:1125` only includes `[disabledCommandMap, agentSessionId]`.
- Plausible risk: after settings/runtime context changes, manual `/compact` may build its request body with stale values until the callback is recreated for some other dependency reason.
- This note is only a finding; no fix is included in this step.

## Metadata: chat/cycle compact can lose compact metadata (貌似已经让 AI 修过一轮了)

- `frontend/src/lib/compact-client.ts:145-149` maps `Message[]` to compact wire messages by forwarding only an existing `m.metadata` field.
- `frontend/src/lib/compact-body.test.mts:78-109` explicitly documents that `buildCompactRequestBody` does not translate local UI flags such as `isPreCompact`; callers must attach wire metadata earlier.
- `frontend/src/lib/message-types.ts:112-126` contains the local-to-wire compact metadata translation (`isPreCompact -> metadata.pre_compact`, `bubbleKind === "compact_summary" -> metadata.compact_summary`), but that translation is performed inside `truncateMessagesForContext()`, not inside `buildCompactRequestBody()`.
- The manual compact path in `frontend/src/app/page.tsx:1104-1140` and `frontend/src/app/vision/page.tsx:1058-1091` passes `messagesRef.current` directly into `buildCompactRequestBody()`.
- `backend/api/compact.py:35-39` accepts `metadata` on `CompactRunMessage`, but `backend/api/compact.py:140-143` converts request messages to backend `Message(role=m.role, content=m.content)` and drops `m.metadata`.
- Plausible risk: chat/cycle manual compact requests may not preserve previously compacted boundaries or other compact metadata through the compact endpoint. Agent mode is less directly affected because `backend/api/compact.py:88-126` routes mode `agent` to `AgentRunner.run_compact_for_session()` instead of building `body_messages` from request messages.
