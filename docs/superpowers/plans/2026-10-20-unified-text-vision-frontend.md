# Text / Vision Frontend Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development`. The user explicitly selected execution now, reuse of implementer contexts, at most six agents, and no auditor/reviewer dispatch after coding. Those instructions override fresh-agent/reviewer/ledger/commit defaults. Checkboxes track work in this one plan; do not create per-task reports or summary documents.

> **Follow-up authorization (2026-10-21):** The user subsequently requested one independent auditor, comparison with their external review, and repairs of confirmed issues. That audit is complete; repairs reuse the original three implementers (four agents total including the auditor). No additional review stage, git operations or per-step report documents are authorized by this update.

**Goal:** One implementation of common conversation UI and ordinary application orchestration, preserving distinct Text/Vision capabilities, existing Chat/Agent protocols, and frozen Answer-now code.

**Architecture:** Keep both routes as composition/compatibility boundaries. Shared controlled components render the common interface; shared state and focused hooks/factories own ordinary bootstrap, recovery, sending, commands, voice and actions. Vision capability has a separate resolver; Vision media preparation, acceptance staging and terminal ownership remain in its route beside the frozen compatibility refs. Shared transport consumes prepared snapshots and explicit lifecycle ports. One Cycle controller enforces page capability and browser-local recovery ownership.

**Tech Stack:** Existing Next.js 14, React 18, TypeScript, Node 24 `node:test`, existing React component renderer, existing Python/FastAPI contracts; no new dependencies.

**Spec:** [Approved SPEC](/home/zk/Project/LLM-deployment/docs/superpowers/specs/2026-10-20-unified-text-vision-frontend-spec.md). The user approved this SPEC and requested plan creation followed immediately by subagent implementation on 2026-10-20 (America/Los_Angeles).

## Global Constraints

- Work in `/home/zk/Project/LLM-deployment`; no git commands, commits, branches or worktrees without separate user permission. Do not run skill scripts that invoke git implicitly.
- The original implementation used three implementers. The user's follow-up authorized one independent auditor; repairs reuse the original implementer contexts, for four agents total, below the limit of six. No worker may spawn agents or reviewers.
- The initial execution excluded independent review. The authorized follow-up audit is complete; implementers verify repairs and the coordinator runs integration checks directly, without another reviewer stage.
- Only this implementation plan is written. No `.superpowers` ledger, per-step summaries, task report files or chapter completion documents. Report progress/results through agent messages.
- Preserve every SPEC §2.1 frozen Answer-now function/state declaration/feature branch/test. Keep route feature callbacks in their original files, without relocation or rewriting. Existing active helpers in frozen modules remain callable unchanged. Route thinness is secondary.
- Ordinary Chat/Agent API bodies, provider transport, backend execution, tools, canonical memory, prefix-cache rules and all scoped history exceptions remain unchanged. No backend or deployment changes are planned.
- Keep existing public routes and Text-to-Vision routing semantics. Capability comes from route plus server config, never `agentMode` or cached model type alone.
- Text cannot acquire/upload/capture images or start camera Cycle programs; display-only images from legitimate existing history/tools remain permitted.
- Text camera rejection covers ordinary, nested and scheduled messages, saved/default/programmatic input, before any run/state/storage/device/network/timer side effect. Do not strip or rewrite programs.
- Cycle browser ownership must not enter POST bodies, fingerprints, run IDs or model history. Text must not adopt, clear or stop foreign/unknown legacy runs.
- Clear cancels a pending, unsubmitted Cycle start; it preserves a submitted Cycle's owner, bookmark, subscription and running controls. Cycle Stop remains the explicit producer-stop action.
- Route ownership and media capability are distinct. Direct Vision with a Text backend retains templates and ordinary Cycle controls, while both editor and controller reject ordinary/nested/scheduled photo requests before start effects. Runtime photo capability never enters backend requests or bookmarks.
- Hub recovery controllers and canonical journal request identities participate in Stop, mode retirement and replacement ownership. Recovery failures publish their result while the settling attachment still owns it; superseded completions remain rejected.
- Configuration uses existing cached-key-presence precedence, including `false`, `0` and empty strings. Preserve current maxTokens validation and pinned runtime context.
- Treat Vision as primary only for verified common-path drift D1–D3; capability differences and unresolved issues are not blanket-copy instructions. Report additional differences in messages.
- Test new behavior before implementing it; preserve passing behavioral tests during extraction. Source-contract migrations retain the underlying safety assertions and never rewrite frozen Answer-now tests.
- Preserve the existing non-Agent Cycle placeholder; implementing actual non-Agent Cycle inference is out of scope.

## Verification Focus

1. A stale callback after Stop, mode change or reconnect cannot unlock/finalize a new request; Task 3/4 tests current attachment and request identity.
2. Unknown acceptance/EOF cannot consume Vision omission permissions or release terminal busy; Task 3/6 tests rejection versus unknown versus stable terminal.
3. A saved nested/scheduled camera program or malformed recursive data cannot bypass Text start validation; Task 5 checks no effects and original-data preservation.
4. A legacy or foreign Cycle bookmark cannot trigger Text status/reconnect/Stop or erase another page's pointer; Task 5 covers recovery, cleanup and replacement races.
5. Moving controls cannot resurrect Answer-now, detach its retained compatibility refs, or create duplicate state owners; Task 6 checks frozen snapshots and normal route behavior.

## Ownership and Integration Map

| Owner | Exclusive writable scope | Inputs / outputs |
| --- | --- | --- |
| UI implementer (new context) | `frontend/src/components/conversation/*` and their new component tests | Controlled prop interfaces below; no route edits, transport, persistence or media acquisition |
| Frontend integrator (`shared_contract_tests_audit`, reused) | BOTH route files; `frontend/src/lib/conversation/*`; `vision-automation/use-vision-conversation-adapter.ts`; related non-frozen integration/source-contract tests | Wires UI components and Cycle hook; owns all route changes and shared ordinary application state |
| Cycle implementer (`backend_contract_audit`, reused) | New `cycle-capability.ts`, `conversation-cycle.ts`, `use-conversation-cycle.ts`; additive `cycle-page.ts`; `CycleEditor.tsx`; Cycle tests | Pure program validation, owned bookmark selection, shared Cycle controller/hook; no route/API/backend edits |
| Coordinator | This plan; execution/test orchestration and frozen snapshots in temporary storage | Resolves interfaces; integrates via the same implementers; no reviewer dispatch |

No two agents edit the same production file concurrently. If an interface needs adjustment, agree by message before changing its producer or consumer. Shared module names below are the implementation targets; do not introduce parallel reducers or cosmetic renames of existing helpers.

### Dependency / consistency check

| Tasks | Shared interface or file | Resolution |
| --- | --- | --- |
| 1 → 2/4/6 | Controlled UI prop interfaces → route state/actions | UI owns components; integrator alone changes both route consumers |
| 2 → 3/4 | Ordinary state/ref aliases and bootstrap settings | Integrator owns producer and consumers serially; frozen route aliases stay available |
| 3 ↔ 4 | Prepared turn, terminal/interruption callbacks, current transport | Generic compatibility inputs; no new Answer-now adapter or duplicate terminal owner |
| 5 → 6 | Cycle hook and owned recovery metadata | Cycle worker owns standalone controller; integrator supplies Vision-only hooks and removes duplicated route lifecycle |
| 1–6 | Frozen source blocks and existing behavioral contracts | Snapshot before edits; compare exact frozen bodies/declarations and whole frozen helper/API files after integration |
| Each task | Its tests, exported signatures and allowed files | Files/signatures below match the map; each task ends in its named scoped verification |

## Baseline and Test Commands

Before product edits, `npm run type-check` passed. The initial 79-file Node suite reported 923 tests: 912 passed and 11 failed. Eight failures were existing page-source assertions in `chat-input-alignment.test.mjs`, `timeline-projection.test.mts`, and `voice-input-pages.test.mts`. Three failures came from the legacy `frontend/tests/stream-timeout.test.mjs` harness, which did not finish and was terminated after approximately 136 seconds. Preserve that distinction; do not claim a clean baseline or change frozen code to erase failures.

Existing focused `api.test.mts` + `cycle-page.test.mts` passed 103 tests. Baseline output is `/tmp/llm-ui-baseline-node.log`; original route files and frozen hashes are under `/tmp/llm-ui-unification-mtm6ampk/`.

Node 24 runs `.mts` tests directly. Scoped examples, from repository root:

```bash
node --test --test-timeout=30000 frontend/src/lib/api.test.mts frontend/src/lib/cycle-page.test.mts
node --test --test-timeout=30000 frontend/src/lib/conversation/*.test.mts
node --test --test-timeout=30000 frontend/src/components/conversation/*.test.mts
node --test --test-timeout=30000 frontend/src/lib/cycle-capability.test.mts frontend/src/lib/conversation-cycle.test.mts
```

The coordinator enumerates all frontend `.test.mts`/`.test.mjs` files with Python and invokes `node --test --test-concurrency=4 --test-timeout=30000` without a shell-glob omission. Capture complete output to a temporary log. Do not install dependencies or change package module type to suppress the existing Node module-type warning.

### Task 1: Shared controlled conversation presentation

**Files:** Create `frontend/src/components/conversation/ConversationHeader.tsx`, `ConversationSettingsDialog.tsx`, `ConversationMessageList.tsx`, `ConversationComposer.tsx`, `ConversationTextInputActions.tsx`, and `conversation-presentation.test.mts`.

**Interfaces:** Export each component's props. Reuse existing application and component types rather than copying catalog/session/voice definitions.

- `ConversationMessageListProps`: `messages: readonly Message[]`, `contentRef?: React.Ref<HTMLElement>`, `webUIConfig?: WebUIConfig`, `settings: Pick<ChatSettings,'agentMode'|'showReasoning'>`, `bubbleCharacterLimit: number`, `foldThinkingAfterFinalAnswer: boolean`, `stoppedMessageIds: ReadonlySet<string>`, `collapsedCompact: ReadonlySet<string>`, `onToggleCompact(id: string): void`, `onToggleReasoning(id: string): void`, `onOpenImage(src: string): void`, `getUserText(message: Message): string`, `emptyContent: ReactNode`.
- `ConversationHeaderProps`: `pageMode`, `webUIConfig`, `modelName`, `agentMode`, `connectionStatus`, `inputMode`, `onVoiceModeChange`, `voiceDisabled`, `voiceDisabledReason`, `canRecover`, `onRecover`, `enableThinking`, `onToggleThinking`, `enableLocalMemory`, `onToggleLocalMemory`, `onOpenSettings`, `onClear`.
- `ConversationSettingsDialogProps`: `open`, `pendingSettings`, `setPendingSettings`, `settingsTab`, `setSettingsTab`, `pendingSkinId`, `setPendingSkinId`, `maxContextLength`, `agentToolCatalog`, `sessionRecord`, `toolNotice`, `resetDisabled`, optional `visionAutomationArmed`, `onReset`, `onCancel`, `onDismiss`, `onConfirm: () => void | Promise<void>`.
- `ConversationComposerProps`: `footerRef`, `webUIConfig`, `busy`, `cycleRunning`, `connectionError`, `sendDisabled`, `input`, `inputRef`, `inputMode`, optional `enterToSend` (fallback to existing `DEFAULT_SETTINGS.enterToSend`), `voiceController`, `showInputActions`, `onToggleInputActions`, `onInputChange(value: string,selectionStart: number): void`, `onKeyDown`, `onSubmit`, `onStop`, `onSwitchToKeyboard`, `onOpenDictationSettings`, `slashMenu`, `filteredSlashCommands`, `disabledCommandMap`, `onSelectSlashCommand`, `onCloseSlashMenu`, `actions: ReactNode`, `actionIndicator?: ReactNode`, `attachments?: ReactNode`, `notices?: ReactNode`.

- [x] Write rendered behavioral tests for leading/trailing Agent steps and dispatch cards, compact toggling, reasoning toggling, stopped content, user images/history summaries, replay/memory display, settings edit/reset/confirmation and composer keyboard/voice/Stop actions. Run them before the new components exist, confirming the missing implementation rather than a fixture error.
- [x] Implement components using current Vision presentation and existing leaves; no pre-compact opacity, but retain semantic flags and the shared Agent mode indicator. Keep frozen rendering helpers unchanged. No media, storage, fetch or state ownership in these controlled components. Non-media input actions share one controlled toolbar without hiding templates or ordinary Cycle behind an image-capability gate.
- [x] Verify the component suite and report prop interfaces/results to the integrator. The integrator wires routes in Task 6.

### Task 2: One ordinary state owner and config-first initialization

**Files:** Create `frontend/src/lib/conversation/use-conversation-state.ts`, `conversation-bootstrap.ts`, `use-conversation-bootstrap.ts`, `conversation-bootstrap.test.mts`; modify both route files only through the integrator.

**Interfaces:** `useConversationState()` exposes the existing ordinary flat state/setter/ref aliases. It excludes Answer-now-owned loading/clicked declarations, which remain unchanged in each route. `useConversationBootstrap()` receives `pageMode`, typed state ports, `onConfig`, and the existing Text redirect callback; Vision options remain an explicit callback/adapter.

- [x] Add config-first behavioral tests: failing `/models` does not prevent config/settings/ASR initialization; cached `false`/`0`/empty keys win; server values fill missing keys; later refresh does not overwrite live mode/thinking/local-memory choices. Verify failure against the old coupled path or absent shared implementation.
- [x] Extract ordinary state/refs without changing frozen callback bodies or captured alias identity. Unify hydration/persistence, templates, skin, model/config loading, connection polling and Cycle defaults. One effect owns each subscription/poll; stale async bootstrap cannot write after cleanup.
- [x] Wire both routes to common state/bootstrap, keeping Text cached/server routing and Vision configuration validation. Optional model enrichment is isolated from config success.
- [x] Run bootstrap behavior tests, existing settings/local-memory/tool configuration tests and type-check; report any newly exposed mismatch by SPEC D number or new evidence.

### Task 3: Shared ordinary recovery, send and actual transport

**Files:** Create `frontend/src/lib/conversation/use-conversation-recovery.ts`, `conversation-transport.ts`, `conversation-tool-admission.ts`, `use-conversation-send.ts`, related behavior tests, and `frontend/src/lib/vision-automation/use-vision-conversation-adapter.ts`; modify both routes. Extend `SessionHubRecoveryCoordinator` with an ownership-guarded settlement callback to publish recovery outcomes before token retirement; retain existing API/builders/Agent handlers unchanged. The Vision adapter resolves capability; route-specific lifecycle ports remain explicit rather than relocating their frozen compatibility dependencies.

**Interfaces:** Export a prepared immutable turn snapshot with `pageMode`, `requestId`, `controller`, `interactionMode`, `sessionId`, `historyMessages`, `userContent`, `agentText`, optional `images`/`visionAutomation`, `preserveFullHistory`, `hasUserText`, `userMessage`, `assistantMessage`, `agentRun`. Shared transport accepts that snapshot plus typed current state/ref ports, the existing handler environment, current attachment ownership and lifecycle callbacks. Vision ports provide preparation/page-commit status, candidate completion, stable-terminal ownership, finally cleanup and bound-Agent detach handling. Text supplies immediate acceptance-free page mutation and no media/gate.

- [x] Add real-client/fake-network behavioral tests for Text/Vision × Chat/Agent request bodies, local-memory freezing, structured handler projection, exact tool configuration admission, rejected versus detached versus terminal, and stale callbacks after replacement. Hand-check wire fixtures; do not derive expected messages with the builder under test.
- [x] Extract Hub recovery and bookmark transactions around existing handler factories, preserving synchronous Vision message-ref updates and current attachment guards. No duplicate replay reducer or generic UI-to-canonical serialization.
- [x] Centralize ordinary turn snapshots/admission/queue and shared API invocation/callbacks. Retain Chat/Agent wire adapters, history windows, compact anchors and typed tool rejection. Vision preparation keeps omission/page mutation staged until acceptance and keeps terminal-gate ownership distinct from candidate completion/EOF.
- [x] Leave all frozen replacement/Answer-now code in-route. Expose existing objects/refs needed by those callbacks. If a tightly coupled slice cannot move without altering a frozen branch, keep that small slice and message the coordinator with the precise boundary.
- [x] Run prepared-turn/transport/recovery tests plus existing API/Hub/projection/image-history/vision automation suites and type-check. Assert no request/session/schema/prompt drift from the extraction.

### Task 4: Shared voice, commands, Clear/Stop and mode transitions

**Files:** Create `use-conversation-voice.ts`, `use-conversation-commands.ts`, `conversation-actions.ts` under `frontend/src/lib/conversation/`, with focused behavior tests; modify routes and in-scope non-frozen source-contract tests. Actions are pure functions over explicit state ports rather than an additional hook owner.

**Interfaces:** Hooks consume the ordinary state ports from Task 2 and prepared-send entry from Task 3. Generic busy/pending-acceptance and explicit Vision cleanup callbacks are inputs. Expose the existing aliases needed by frozen route callbacks; no Answer-now-specific adapter or changed frozen helpers.

- [x] Add tests for mode transition capturing the old browser transport/token, rejecting its late callbacks, retaining backend producer/session, and preserving active new ownership. Cover voice startup failure/Retry, keyboard/voice/dictation transitions, hidden-to-visible resume, replay release, template immediate send and manual compact mode selection/rollback.
- [x] Extract common voice/dictation/template/slash/keyboard/compact glue. Keep current per-turn image snapshots and request-busy checks; source text from typed drafts, never future UI state or timeline reconstruction.
- [x] Extract Clear/Stop/common setting confirmation using existing frozen Stop/render helper calls unchanged. Apply Vision's ordinary transport-retirement semantics to both pages; automation-armed guards and media invalidation remain Vision-only. Stop uses existing targeted contracts and does not rewrite history.
- [x] Migrate only non-frozen page-source assertions affected by extraction to behavior plus minimal route-wiring checks. Preserve mixed-file Answer-now test cases exactly. Update obsolete ordinary test fixtures only where the existing documented wire contract proves the fixture wrong; no production protocol change to satisfy tests.
- [x] Run voice/commands/actions behavior suites, existing Stop/projection/settings tests and unchanged Answer-now no-button check; report frozen baseline failures separately rather than editing them.

### Task 5: Shared Cycle capability, ownership and lifecycle

**Files:** Create `frontend/src/lib/cycle-capability.ts`, `conversation-cycle.ts`, `use-conversation-cycle.ts`, their behavior tests; modify additive helpers in `cycle-page.ts` and `components/CycleEditor.tsx`, with scoped Cycle tests. No route edits by this worker.

**Interfaces:**

```ts
type CyclePageMode = 'text' | 'vision';
interface CycleProgram { steps: CycleStep[]; scheduledMessages: ScheduledMessage[] }
interface CycleProgramError { path: string; code: 'invalid_program' | 'text_camera_forbidden'; message: string }
type CycleProgramValidation =
  | { ok: true; program: CycleProgram; hasPhotos: boolean }
  | { ok: false; error: CycleProgramError };
validateCycleProgram(pageMode: CyclePageMode, steps: unknown, scheduledMessages: unknown): CycleProgramValidation;

interface OwnedCycleBookmark extends CycleBookmark { version: 2; pageMode: CyclePageMode }
selectCycleRecovery(storage: CycleStorage, pageMode: CyclePageMode): CycleRecoverySelection;
writeOwnedCycleBookmark(storage: CycleStorage, bookmark: OwnedCycleBookmark): void;
clearOwnedCycleBookmark(storage: CycleStorage, pageMode: CyclePageMode, expectedRunId: string): void;
migrateConfirmedVisionCycleBookmark(storage: CycleStorage, original: CycleBookmark, confirmed: CycleBookmark): void;
```

`CycleRecoverySelection` distinguishes owned, legacy-vision, foreign-legacy and none. New owned keys are page-scoped and versioned; legacy helpers remain for compatibility. Migration compare-clears only the original pointer after successful confirmation.

`useConversationCycle(options)` accepts `pageMode`, `showThumbnails`, `setMessages`, `getRequestSettings(): Pick<CycleStartRequest,'agent_mode'|'enable_thinking'|'enable_local_memory'|'tool_configuration'>`, optional `allowPhotos`, `isStartBlocked(): boolean` and `beforeCameraStart(signal: AbortSignal): Promise<void>`. Text always rejects camera requests; Vision supplies its server-confirmed capability through `allowPhotos`, independently of bookmark ownership. The controller checks capability before start effects and again after asynchronous preparation. It returns `cycleStatus`, `cycleSessionId`, `cycleEvents`, `cycleValidationError`, `cycleRecoveryNotice`, `cycleStartGuardRef` (active/begin/isCurrent/finish/cancel), `handleCycleStart`, `handleCycleStop`, `cancelCycleStart`, and `resetCycle({clearBookmark?})`. Clear supplies `cancelCycleStart` through a pending-start-only action port; submitted runs remain intact. The underlying non-React controller has injected client/storage/ID/state sinks for behavioral tests; the hook owns state and fresh option refs.

- [x] Write negative tests for ordinary/nested/scheduled/default/saved/programmatic photos, malformed/cyclic containers and malformed photo arrays. Text nonempty photos reject with exact path and no effects; empty photo arrays succeed; source programs remain unchanged. Confirm red before implementation.
- [x] Implement pure recursive validation and independent request snapshots. Add editor validation before `isStarting` or timer work; replace the false skipped message with the precise rejection. The controller independently validates before IDs/tokens/busy/bookmarks/device work/POST.
- [x] Write owned-bookmark and controller tests: Text never probes/resumes/stops/clears foreign or legacy runs; migration preserves original until successful Vision confirmation; cursors remain monotonic; stale Stop/status/events cannot affect a replacement. Malformed pointers do not become owned runs.
- [x] Implement one ordinary/scheduled Cycle projection/start/recover/stop controller. Unmount detaches only; Clear/reset does not implicitly Stop. Preserve scheduled timing, fingerprints, event ordering, Agent projection and the non-Agent placeholder. Reject foreign images and report an impossible photo event in a Text-owned run without cancelling an external producer.
- [x] Invoke Vision-only camera release/wait only for validated photo programs; recheck start token/busy after waiting. Use an independent generic guard, not a Vision module on Text. Report owned-resource preparation requirements to the integrator.
- [x] Verify Cycle capability/controller/bookmark/editor tests and existing Cycle tests. Deliver the hook interface to the integrator for both routes.

### Task 6: Route composition, scoped regression migration and delivery checks

**Files:** Both routes (integrator only); in-scope tests listed above; no new documentation beyond checkbox updates in this plan.

**Interfaces:** Consume all component/hook contracts above. Existing Answer-now lexical references remain available in their original routes; Vision-only automation refs are not mounted on Text.

- [x] Wire the four shared UI components, common state/bootstrap/recovery/send/voice/commands/actions and Cycle hook into both routes; remove the superseded ordinary implementations instead of leaving shadow code. Keep genuinely capability-specific and frozen legacy sections explicit.
- [x] Provide Vision-owned media preparation only for Cycle camera programs. Bind automation to its current context/generation and keep existing acceptance/terminal barriers. Display Text validation/recovery notices and a Vision recovery link for ambiguous legacy pointers.
- [x] Run meaningful rendered/hook/transport integration cases across Text/Vision × Chat/Agent: live/replay, acceptance/rejection/unknown, Stop/Clear/mode changes, compact/tools/local-memory, voice/templates/queues and current Cycle behavior. No real model, camera or production service calls are necessary for automated verification.
- [x] Compare frozen route callback bodies/declarations and complete frozen helper/API/backend files with the temporary baseline; confirm no Answer-now code/test changes and no backend/config/dependency changes. Report any required deferred slice explicitly.
- [x] Run full enumerated frontend Node tests with per-file timeout, `npm run type-check`, and `npm run build:verify`. Resolve in-scope newly introduced failures; name remaining baseline/frozen/harness failures rather than hiding them.
- [x] Run scoped existing Python contracts using `.sglang/bin/python -m pytest`: Cycle API/config/recovery, request lifecycle, Chat/Agent Stop, compact and multimodal/history omission contracts. Do not alter backend behavior or dependencies to force a pass.
- [x] Report changed architecture, SPEC D1–D6 alignments, any additional confirmed drift, exact verification outcomes and material unverified limits to the user. Stop after implementation/verification; do not dispatch an auditor or reviewer or perform git integration.
