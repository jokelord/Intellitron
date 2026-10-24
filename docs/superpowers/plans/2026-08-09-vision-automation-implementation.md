# Vision Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a browser-local, synchronized multi-source vision sampling task to `/vision`, with bounded tick-bundle sampling, strict request/session isolation, deterministic historical-image omission, and Chat/Agent parity.

**Architecture:** The browser owns media permissions, capture cycles, synchronized tick barriers, bounded Blob resources, and UI projection. FastAPI owns configuration, strict multimodal admission, request idempotency/status, top-level Agent canonical omission, and provider-safe lifecycle barriers. Current-turn images remain image-first and source-grouped; older image blocks are staged and omitted only at an accepted logical-turn boundary.

**Tech Stack:** Next.js 14, React, TypeScript, browser MediaDevices/Canvas/Blob APIs, FastAPI, Pydantic, Pillow, smolagents, SGLang/vLLM, Node test runner, pytest.

## Global Constraints

- Read `docs/superpowers/specs/2026-08-09-vision-automation-design.md` and `AGENTS.md` before editing.
- Never call the browser sampling lifecycle a session. Use `vision automation sampling task` and `capture cycle`.
- `session` means an LLM/Agent context window or a manager-created child-agent context.
- Do not run any Git command unless the user explicitly authorizes it. This plan intentionally replaces commit steps with review checkpoints.
- Do not edit dependency source under `.sglang`.
- Preserve normal Chat, normal Vision, Agent, Cycle, Stop, Answer Now, compact, and auto-compact behavior.
- Never rewrite historical non-image text, roles, ordering, assistant outputs, `tool_calls`, or user-role observations.
- Historical omission is staged and becomes canonical only after request acceptance.
- Agent omission is top-level-session-only and must never scan child, sibling, or other manager sessions.
- All asynchronous capture/restart callbacks are pinned to page generation, Chat/Agent mode, and top-level context identity.
- One display source plus browser cameras share one synchronized tick barrier and one retained tick index set.
- Keep manual uploads/manual photos at 9 images maximum.
- Enforce 250 real images across the final logical inference prompt.
- Enforce 67,108,864 encoded Data URL bytes per inbound Vision request and 75,497,472 request-body bytes.
- Use JPEG quality ladder 85, 75, 65, 55, 45, 40 and dimension ratio 0.8 down to 256 px unless the configured max is lower.
- `capture_during_inference=false` by default.
- No automation image may enter the existing busy message queue.
- Do not log Base64, Blob bytes, image pixels, or unredacted Data URLs.
- Business-file ownership is exclusive per wave; agents must not edit files assigned to another concurrent agent.

## File Structure and Ownership

### Backend foundation

- Create `config/config-vision.yaml`: server defaults and ceilings.
- Modify `config/config-overall.yaml`: select the new file.
- Modify `backend/config.py`: `VisionAutomationConfig`, parser, validation, `AppConfig` field.
- Modify `backend/main.py`: current `/config`, router/service wiring.
- Create `backend/request_lifecycle.py`: idempotent acceptance/running/terminal registry.
- Create `backend/api/request_status.py`: provider-neutral status endpoint.
- Create `backend/vision/contracts.py`: Pydantic automation metadata and validated image types.
- Create `backend/vision/validation.py`: strict shared Chat/Agent image admission.
- Create `backend/vision/history_omission.py`: deterministic top-level Agent omission.
- Create `backend/vision/body_limit.py`: FastAPI request ceiling.

### Frontend core

- Create `frontend/src/lib/vision-automation/types.ts`: frozen cross-module interfaces.
- Create `frontend/src/lib/vision-automation/config.ts`: strict YAML/localStorage precedence.
- Create `frontend/src/lib/vision-automation/image-resources.ts`: idempotent Blob/Object URL ownership.
- Create `frontend/src/lib/vision-automation/bundle-sampler.ts`: synchronized bundle capacity/sampling.
- Create `frontend/src/lib/vision-automation/payload.ts`: compression tiers, Base64, ordering, manifest.
- Create `frontend/src/lib/vision-automation/state-machine.ts`: pure lifecycle reducer.
- Create `frontend/src/lib/vision-automation/media-source-registry.ts`: owner leases for camera/display streams.
- Create `frontend/src/lib/vision-automation/capture-source.ts`: video/canvas capture abstraction.
- Create `frontend/src/lib/vision-automation/tick-coordinator.ts`: synchronized capture barrier.
- Create `frontend/src/lib/vision-automation/history-omission.ts`: staged Chat/UI omission.
- Create `frontend/src/lib/vision-automation/send-contract.ts`: accepted-turn transport contract.
- Create `frontend/src/lib/vision-automation/use-vision-automation.ts`: React orchestration hook.

### Frontend UI/integration

- Create `frontend/src/components/VisionAutomationPanel.tsx`.
- Create `frontend/src/components/VisionAutomationStatusRing.tsx`.
- Create `frontend/src/components/VisionAutomationThumbnailMatrix.tsx`.
- Create `frontend/src/components/HistoricalImageSummaryCard.tsx`.
- Modify `frontend/src/components/CameraCapture.tsx`: consume shared media leases.
- Modify `frontend/src/components/ImageUploader.tsx`: copy/action order/panel wiring/mutual exclusion.
- Modify `frontend/src/lib/message-types.ts`: summary projection and explicit staged builders.
- Modify `frontend/src/lib/api.ts`: config, request ID, metadata, acceptance/status callbacks.
- Create `frontend/src/lib/backend-proxy-policy.ts`: exact route-aware body checks.
- Modify `frontend/src/app/api/backend/[...path]/route.ts`: reject before/while reading.
- Modify `frontend/src/app/vision/page.tsx`: sole page integration owner.

## Work Waves

| Wave | Worker A | Worker B | Worker C |
|---|---|---|---|
| 1 | Task 1: backend config/provider foundation | Task 2: frontend pure contracts/sampler/resources | Task 3: request lifecycle registry/status |
| 2 | Tasks 4-5: validation, API admission, Agent omission | Task 6: media/tick/UI | Task 7: frontend history/transport/proxy |
| 3 | Backend regression fixes | Payload/state/UI regression fixes | Task 8: `/vision` page integration |
| 4 | Full verification | Manual/soak evidence | Independent code audit |

---

### Task 1: Backend Configuration and Provider Foundation

**Exclusive files:**
- Create: `config/config-vision.yaml`
- Modify: `config/config-overall.yaml`
- Modify: `backend/config.py`
- Modify: `backend/main.py` only in config loading and `/config`
- Modify: `config/qwen3__6-NVIDIA-NVFP4-config-infer.yaml`
- Test: `tests/test_vision_automation_config.py`
- Test: `tests/test_vllm_container_scripts.py`

**Interfaces:**
- Produces `VisionAutomationConfig` as specified below.
- Produces `/config.vision.automation` consumed by frontend Task 2.
- Does not add request lifecycle, API validation, or Agent omission.

- [ ] **Step 1: Write failing config tests**

```python
def test_vision_automation_defaults_are_loaded_from_overall():
    config = load_config("config/config-overall.yaml")
    automation = config.vision_automation
    assert automation.interval_seconds == 1.0
    assert automation.max_frames_per_source == 16
    assert automation.capture_during_inference is False
    assert automation.max_encoded_payload_bytes == 67_108_864
    assert automation.max_request_body_bytes == 75_497_472

def test_config_endpoint_uses_reloaded_object(client, monkeypatch):
    replacement = make_config_with_interval(2.5)
    monkeypatch.setattr("backend.main.reload_config", lambda: replacement)
    assert client.get("/config").json()["vision"]["automation"]["interval_seconds"] == 2.5
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_vision_automation_config.py -q
```

Expected: failures for missing config file/dataclass and missing `/config.vision.automation`.

- [ ] **Step 3: Add the dataclass and strict parser**

```python
@dataclass(frozen=True)
class VisionAutomationConfig:
    enabled: bool = True
    interval_seconds: float = 1.0
    max_frames_per_source: int = 16
    thumbnail_size_px: int = 32
    capture_during_inference: bool = False
    tick_timeout_seconds: float = 10.0
    consecutive_tick_failure_limit: int = 3
    max_encoded_payload_mib: int = 64
    initial_jpeg_quality: int = 85
    minimum_jpeg_quality: int = 40
    minimum_image_dimension_px: int = 256
    base64_encode_concurrency: int = 4
    max_request_body_mib: int = 72
    request_status_ttl_seconds: int = 600

    @property
    def max_encoded_payload_bytes(self) -> int:
        return self.max_encoded_payload_mib * 1024 * 1024

    @property
    def max_request_body_bytes(self) -> int:
        return self.max_request_body_mib * 1024 * 1024
```

Reject invalid bounds instead of silently clamping. Validate `max_request_body_mib > max_encoded_payload_mib` and `minimum_jpeg_quality <= initial_jpeg_quality`.

- [ ] **Step 4: Expose current config, not stale startup config**

Use `current_config = reload_config()` throughout `/config`. Return every automation field under `vision.automation`.

- [ ] **Step 5: Remove the active vLLM single-image override**

Delete `limit_mm_per_prompt: {"image": 1}` from the active NVIDIA infer YAML. Keep launcher support for future explicit values. Add tests proving both vLLM launchers omit `--limit-mm-per-prompt` when absent.

- [ ] **Step 6: Run focused and regression tests**

```bash
./.sglang/bin/python -m pytest tests/test_vision_automation_config.py tests/test_vllm_container_scripts.py tests/test_runtime_config_limits.py tests/test_web_ui_config.py -q
bash -n scripts/1b_start_vllm_container_with_snapshots.sh
bash -n scripts/1c_start_vllm_container.sh
```

Expected: all pass, no shell syntax errors.

- [ ] **Step 7: Review checkpoint**

Report changed files and exact test output. Do not run Git.

---

### Task 2: Frontend Pure Contracts, Sampler, Resources, Payload, and Reducer

**Exclusive files:**
- Create everything under `frontend/src/lib/vision-automation/` except media, capture, tick, history, send-contract, and hook files assigned later.
- Test: matching `*.test.mts` files under the same directory.

**Interfaces:**
- Produces the frozen types used by all frontend agents.
- Produces `calculateBundleCapacity`, `TickBundleSampler`, `OwnedImageResource`, `prepareVisionPayload`, `buildVisionAutomationManifest`, and `reduceVisionAutomation`.
- Must not edit `page.tsx`, `api.ts`, `message-types.ts`, `ImageUploader.tsx`, or `CameraCapture.tsx`.

- [ ] **Step 1: Create frozen `types.ts` and contract tests**

Use the exact interfaces in the PRD and include:

```ts
export type VisionAutomationPhase =
  | 'idle' | 'starting' | 'capturing' | 'freezing'
  | 'waiting_for_model' | 'capturing_during_model'
  | 'waiting_for_sources' | 'paused_source_error'
  | 'retryable_submit_error' | 'aborting';

export interface AutomationBinding {
  pageInstanceId: string;
  pageGeneration: number;
  mode: 'chat' | 'agent';
  contextId: string;
  automationTaskId: string;
}

export interface SourceDescriptor {
  sourceId: string;
  kind: 'display' | 'camera';
  stableAlias: 'desktop' | `camera_${number}`;
  label: string;
  deviceId?: string;
}
```

- [ ] **Step 2: Write sampler RED tests**

Cover capacity 1, 2, N, random middle eviction, whole-bundle disposal, manual-image shrink, no resurrection, and 241 source ceiling.

```ts
assert.equal(calculateBundleCapacity({
  maxFramesPerSource: 16,
  manualImageCount: 9,
  sourceCount: 3,
}), 16);
```

- [ ] **Step 3: Implement minimal sampler and resource ownership**

`dispose()` and `disposeBundle()` must be idempotent. Track created Object URLs explicitly and revoke each exactly once. Never retain an all-captured history.

- [ ] **Step 4: Write payload and manifest RED tests**

Assert manual-first ordering, desktop-first source groups, shared two-decimal relative times, no `Shared ticks`, exact Data URL UTF-8 byte count, JPEG tier sequence, whole-bundle byte eviction, and bounded encoding concurrency.

- [ ] **Step 5: Implement payload preparation**

```ts
export function estimateDataUrlBytes(blobBytes: number, mime = 'image/jpeg'): number {
  const header = `data:${mime};base64,`;
  return new TextEncoder().encode(header).byteLength + 4 * Math.ceil(blobBytes / 3);
}
```

Apply one compression tier to every automation frame. Preserve bundle atomicity when dropping for byte pressure.

- [ ] **Step 6: Write and implement reducer tests**

The reducer must prove that `capture_during_inference=true` does not reset a live cycle on terminal callbacks, while false waits for stable terminal. Abort creates bound cleanup state and invalidates old cycle generations.

- [ ] **Step 7: Run frontend pure tests and type-check**

```bash
node --test --experimental-strip-types src/lib/vision-automation/config.test.mts src/lib/vision-automation/bundle-sampler.test.mts src/lib/vision-automation/image-resources.test.mts src/lib/vision-automation/payload.test.mts src/lib/vision-automation/state-machine.test.mts
npm run type-check
```

Expected: all new tests pass; any type-check failure outside owned files must be reported, not hidden.

- [ ] **Step 8: Review checkpoint**

Report public exports, disposal invariants, and exact test output. Do not run Git.

---

### Task 3: Provider-Neutral Request Lifecycle Registry

**Exclusive files:**
- Create: `backend/request_lifecycle.py`
- Create: `backend/api/request_status.py`
- Test: `tests/test_request_lifecycle.py`
- Test: `tests/test_request_status_api.py`
- Do not modify `backend/main.py`; return the exact wiring hook to Task 4.

**Interfaces:**

```python
class RequestLifecycleState(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    TERMINAL = "terminal"

class RequestLifecycleRegistry:
    def reserve(self, *, request_id: str, request_kind: str,
                context_identity: str | None, payload_fingerprint: str): ...
    def accept(self, reservation_token: object) -> None: ...
    def mark_running(self, reservation_token: object) -> None: ...
    def mark_terminal(self, reservation_token: object, outcome: str) -> None: ...
    def get_status(self, request_id: str) -> Literal[
        "unknown", "accepted", "running", "terminal"]: ...
    def cleanup_expired(self, now: float | None = None) -> int: ...
```

- [ ] **Step 1: Write RED tests for reservation/idempotency**

Same ID + same fingerprint returns the existing record and must not grant a second worker start. Same ID + different payload, session, or mode raises a 409-mappable conflict.

- [ ] **Step 2: Write RED tests for registration tokens and TTL**

Old tokens cannot mark a newer record terminal. Active records never expire. TTL begins at stable terminal and defaults to 600 seconds.

- [ ] **Step 3: Implement the thread-safe bounded registry**

Use a lock. Never recycle an active ID. Keep terminal cleanup bounded and deterministic. Do not store prompt/image bytes.

- [ ] **Step 4: Implement the status router**

```python
@router.get("/v1/request-status/{request_id}")
async def get_request_status(request_id: str, raw_request: Request):
    registry = raw_request.app.state.request_lifecycle_registry
    return {"request_id": request_id, "status": registry.get_status(request_id)}
```

- [ ] **Step 5: Run tests**

```bash
./.sglang/bin/python -m pytest tests/test_request_lifecycle.py tests/test_request_status_api.py -q
```

- [ ] **Step 6: Review checkpoint**

Return the exact lifespan/router wiring needed. Do not edit unrelated files and do not run Git.

---

### Task 4: Shared Vision Admission, Body Limits, and Lifecycle Integration

**Depends on:** Tasks 1 and 3.

**Exclusive files:**
- Create: `backend/vision/__init__.py`
- Create: `backend/vision/contracts.py`
- Create: `backend/vision/validation.py`
- Create: `backend/vision/body_limit.py`
- Modify: `backend/api/chat.py`
- Modify: `backend/api/agent.py`
- Modify: `backend/main.py` outside Task 1 config block
- Modify: `backend/llm_service.py`
- Test: `tests/test_vision_request_validation.py`
- Test: `tests/test_request_lifecycle_integration.py`

**Interfaces:**

```python
class VisionAutomationTransportMetadata(BaseModel):
    history_image_policy: Literal["aggregate_prior_image_blocks"]
    manual_image_count: int
    source_count: int
    retained_tick_count: int

def validate_chat_vision_request(messages, config) -> VisionValidationResult: ...
def validate_agent_vision_request(images, config) -> VisionValidationResult: ...
```

- [ ] **Step 1: Write parity and safety RED tests**

Cover Chat and Agent: malformed Base64, MIME mismatch, corrupt image, per-image diagnostics, 250 accepted in final logical prompt, 251 rejected, 67,108,864 inbound bytes accepted, one byte over rejected with 413, manual count 10 rejected, source count 242 rejected, and forged formula rejected.

- [ ] **Step 2: Implement strict shared admission**

Use `base64.b64decode(payload, validate=True)` and Pillow detection. Do not silently skip a bad image. Redact Data URLs from logs.

- [ ] **Step 3: Add exact 72 MiB body enforcement**

Reject oversized declared `Content-Length` before buffering and count actual ASGI receive bytes. Apply only to Chat, Agent, and `/v1/responses` Vision JSON paths.

- [ ] **Step 4: Wire lifecycle reservation and acceptance**

Reserve after schema/image/identity validation and before canonical mutation. Emit a transport `request_accepted` event after worker/run-scope registration. Duplicate same request ID must not start a second worker; conflict returns 409.

- [ ] **Step 5: Make terminal status worker-owned**

Mark terminal only after Chat worker cleanup or Agent `finish_run()`/run-scope release. Raw SSE generator close and early `done` text are not stable terminal.

- [ ] **Step 6: Remove Base64-as-text token estimation and payload logging**

Image Data URL bytes must not reduce vLLM `max_tokens` as text tokens. SGLang/vLLM/OpenAI adapters keep equivalent messages and log only roles, image counts, encoded totals, and redacted content.

- [ ] **Step 7: Run focused backend tests**

```bash
./.sglang/bin/python -m pytest tests/test_vision_request_validation.py tests/test_request_lifecycle.py tests/test_request_status_api.py tests/test_request_lifecycle_integration.py tests/test_agent_api_images.py tests/test_multimodal_request_preparation.py tests/test_llm_backend_selection.py -q
```

- [ ] **Step 8: Review checkpoint**

Report acceptance ordering, terminal ordering, routes protected by body limits, and test output. Do not run Git.

---

### Task 5: Top-Level Agent Historical-Image Omission and Compact Ordering

**Depends on:** Task 4.

**Exclusive files:**
- Create: `backend/vision/history_omission.py`
- Modify: `backend/agent/agent_runner.py`
- Modify: `backend/agent/local_llm_model.py` only if stale compactable snapshots require clearing at capture time
- Test: `tests/test_agent_vision_history_omission.py`

**Interfaces:**

```python
def format_historical_image_placeholder(*, count: int,
    sources: tuple[str, ...], relative_start: float | None,
    relative_end: float | None) -> str: ...

def stage_agent_history_image_omission(session, metadata): ...
def commit_agent_history_image_omission(session, staged) -> None: ...
```

- [ ] **Step 1: Write RED tests for exact canonical preservation**

One historical `TaskStep.task_images` container becomes one placeholder. Current turn remains real. Task text, roles, order, ActionSteps, tool calls, observations, and user-role tool responses remain byte-equivalent except the image block replacement.

- [ ] **Step 2: Write RED tests for session isolation**

Manager A omission must not touch manager B, child A1/A2, or siblings. Reject child session targets explicitly.

- [ ] **Step 3: Implement strict manifest parsing and placeholder grammar**

Parse only the project-generated `<vision_automation_manifest>`. If invalid/missing, use `sources=none; relative_range=unavailable`; never guess. Count actual `task_images`.

- [ ] **Step 4: Implement staged/accepted mutation**

Pre-accept failure leaves memory unchanged. Commit after lifecycle reservation/run registration and before acceptance. Release removed PIL resources only after commit.

- [ ] **Step 5: Fix auto-compact ordering**

Omission must precede eligibility, `last_compactable_llm_messages` snapshots, cache-first source, and current TaskStep append. Clear stale compactable payloads that still contain removed images.

- [ ] **Step 6: Implement bound post-Abort cleanup token**

Token carries page generation, mode, and top-level context identity. Only the first accepted ordinary user turn that commits omission consumes it. Compact, Stop, Answer Now, pre-accept failure, and another context do not consume it.

- [ ] **Step 7: Run omission and compact regressions**

```bash
./.sglang/bin/python -m pytest tests/test_agent_vision_history_omission.py tests/test_write_memory_tool_calls.py tests/test_local_llm_compactable_payload.py tests/test_compact_agent_cache_first.py tests/test_compact_agent_apply.py tests/test_agent_session_context.py -q
```

- [ ] **Step 8: Review checkpoint**

Report memory mutation boundaries and evidence that child/tool histories are unchanged. Do not run Git.

---

### Task 6: Browser Media Registry, Synchronized Tick Coordinator, and UI Components

**Depends on:** Task 2 frozen types.

**Exclusive files:**
- Create: `frontend/src/lib/vision-automation/media-source-registry.ts`
- Create: `frontend/src/lib/vision-automation/capture-source.ts`
- Create: `frontend/src/lib/vision-automation/tick-coordinator.ts`
- Create: `frontend/src/components/VisionAutomationPanel.tsx`
- Create: `frontend/src/components/VisionAutomationStatusRing.tsx`
- Create: `frontend/src/components/VisionAutomationThumbnailMatrix.tsx`
- Create: `frontend/src/components/HistoricalImageSummaryCard.tsx`
- Modify: `frontend/src/components/CameraCapture.tsx`
- Modify: `frontend/src/components/ImageUploader.tsx`

- [ ] **Step 1: Write media ownership RED tests**

Two owners can lease a camera stream. Releasing manual capture does not stop automation. Releasing the final owner stops tracks exactly once. Display acquisition requires the direct Start/Retry call.

- [ ] **Step 2: Extract the shared registry**

Move module-global camera caches/pending starts behind owner leases. Remove permanent cleanup ownership from `CameraCapture`; preserve manual photo processing through `/v1/process-image-binary`.

- [ ] **Step 3: Write synchronized coordinator RED tests**

All source captures start in one synchronous pass. Commit only after all settle successfully. One failure discards the whole bundle. No overlap/catch-up. Three consecutive source failures pause all. Track ended pauses all. Freeze settles/discards the current tick.

- [ ] **Step 4: Implement coordinator and capture sources**

Use `Promise.allSettled` plus timeout and canonical timestamp from coordinator start. Do not serialize sources. Dispose every partial frame on failed bundle.

- [ ] **Step 5: Implement the approved inline UI**

Action order is upload, manual photo, automation, Cycle. Rename every “摄像头拍照” label to “手动拍照”. Use one shared horizontal scroller. Folding unmounts presentation only. The plus status ring remains visible while armed and honors reduced motion.

- [ ] **Step 6: Enforce mutual exclusion and locked controls**

Automation and Cycle disable each other without silently stopping. Settings/source selection lock from Start through Abort. Manual image actions remain available up to 9.

- [ ] **Step 7: Run tests/type-check**

```bash
node --test --experimental-strip-types src/lib/vision-automation/media-source-registry.test.mts src/lib/vision-automation/tick-coordinator.test.mts src/lib/vision-automation/components.test.mts src/lib/api.test.mts
npm run type-check
```

- [ ] **Step 8: Review checkpoint**

Report stream lease ownership, failed-bundle cleanup, and UI contract evidence. Do not run Git.

---

### Task 7: Frontend Staged History, Transport, Status, and Proxy

**Depends on:** Tasks 2, 3, and 4 contracts.

**Exclusive files:**
- Create: `frontend/src/lib/vision-automation/history-omission.ts`
- Create: `frontend/src/lib/vision-automation/send-contract.ts`
- Create: `frontend/src/lib/vision-automation/use-vision-automation.ts`
- Modify: `frontend/src/lib/message-types.ts`
- Modify: `frontend/src/lib/api.ts`
- Create: `frontend/src/lib/backend-proxy-policy.ts`
- Modify: `frontend/src/app/api/backend/[...path]/route.ts`

- [ ] **Step 1: Write staged omission RED tests**

Replace each continuous historical image part run with one aggregate placeholder at the same position. Preserve all non-image parts byte-for-byte. `commit()` updates UI summaries and clears image references; pre-accept failure does nothing.

- [ ] **Step 2: Implement exact placeholder and manifest contracts**

Use stable aliases and two-decimal ranges. Do not place IDs or UI diagnostics in prompt text.

- [ ] **Step 3: Add request acceptance and status transport**

Pass the same request ID in Chat and Agent. Parse `request_accepted`. Add `getRequestStatus(requestId)`. Do not treat EOF as stable terminal.

- [ ] **Step 4: Implement the orchestration hook**

Bind to page generation/mode/context ID. Validate every async callback token. Keep `images` manual-only. On ambiguous acceptance, query status first; retry only if unknown. Never enqueue automation batches while model busy.

- [ ] **Step 5: Add exact proxy limits**

Check `Content-Length` before body read and actual bytes while reading. Return 413 above 75,497,472 bytes. Apply only to large Vision JSON paths.

- [ ] **Step 6: Run transport/proxy tests**

```bash
node --test --experimental-strip-types src/lib/vision-automation/history-omission.test.mts src/lib/vision-automation/send-contract.test.mts src/lib/vision-automation/use-vision-automation.test.mts src/lib/backend-proxy-route.test.mts src/lib/api.test.mts src/lib/message-types.test.mts
npm run type-check
```

- [ ] **Step 7: Review checkpoint**

Report pre/post-accept behavior, EOF handling, and proxy evidence. Do not run Git.

---

### Task 8: `/vision` Page Integration

**Depends on:** Tasks 4-7.

**Exclusive files:**
- Modify: `frontend/src/app/vision/page.tsx`
- Modify: `frontend/src/app/globals.css` only if Tailwind utilities cannot express the approved reduced-motion behavior
- Test: `frontend/src/lib/vision-automation/page-contract.test.mts`

- [ ] **Step 1: Write page contract RED tests**

Assert the page creates a Chat generation, preallocates Agent session ID at Trigger Start, keeps manual and automation images separate, renders the plus ring, disables mode switch while armed, wires Abort into clear/pagehide/unmount, and never queues automation images.

- [ ] **Step 2: Integrate config and controller**

Create `pageInstanceIdRef`, `chatConversationGenerationRef`, and `useVisionAutomation`. Increment generation on clear. Bind Agent sampling only after `createAgentSessionId()`.

- [ ] **Step 3: Integrate the accepted send transaction**

Freeze, prepare, stage omission, submit, commit only on acceptance, then restart or wait according to config. Preserve the normal send path when no automation task/cleanup token applies.

- [ ] **Step 4: Integrate stable terminal paths**

Forward backend terminal status after worker cleanup. `capture_during_inference=true` keeps the active cycle across final/error/Stop/Answer Now. False starts a new T=0 only after terminal status.

- [ ] **Step 5: Integrate rendering and controls**

Render the inline panel and historical summary cards. Fold without stopping. Keep manual image removal wired to sampler capacity. Enforce Cycle mutual exclusion.

- [ ] **Step 6: Run frontend regressions**

```bash
node --test --experimental-strip-types src/lib/vision-automation/*.test.mts src/lib/api.test.mts src/lib/message-types.test.mts src/lib/backend-proxy-route.test.mts src/lib/timeline-projection.test.mts
npm run type-check
npm run build:verify
```

- [ ] **Step 7: Review checkpoint**

Report the exact page lifecycle and regression output. Do not run Git.

---

### Task 9: Integrated Verification and Resource Soak

**Owner:** Lead coordinator, after all implementation workers finish.

- [ ] **Step 1: Run focused backend suites**

```bash
./.sglang/bin/python -m pytest tests/test_vision_automation_config.py tests/test_vision_request_validation.py tests/test_request_lifecycle.py tests/test_request_status_api.py tests/test_request_lifecycle_integration.py tests/test_agent_vision_history_omission.py -q
```

- [ ] **Step 2: Run backend regressions**

```bash
./.sglang/bin/python -m pytest tests/test_agent_api_images.py tests/test_multimodal_request_preparation.py tests/test_write_memory_tool_calls.py tests/test_local_llm_compactable_payload.py tests/test_compact_agent_cache_first.py tests/test_compact_agent_apply.py tests/test_agent_session_context.py tests/test_agent_stop_api.py tests/test_llm_backend_selection.py tests/test_vllm_container_scripts.py -q
```

- [ ] **Step 3: Run complete frontend verification**

```bash
node --test --experimental-strip-types src/lib/vision-automation/*.test.mts src/lib/api.test.mts src/lib/message-types.test.mts src/lib/backend-proxy-route.test.mts src/lib/timeline-projection.test.mts
npm run type-check
npm run build:verify
```

- [ ] **Step 4: Run the 10,000-tick virtual soak**

Assert bounded bundle/resource count, exact Object URL revocation, no retained all-frame diagnostics, and no more than one sent batch plus one current pool.

- [ ] **Step 5: Run syntax and payload-leak checks**

```bash
bash -n scripts/1b_start_vllm_container_with_snapshots.sh
bash -n scripts/1c_start_vllm_container.sh
bash -n scripts/4_start_frontend.sh
```

Search captured test logs for `data:image/` and fail if Base64 payloads were logged.

- [ ] **Step 6: Record manual/hardware blockers honestly**

If real Chromium media devices, target vLLM, or Jetson Thor are unavailable, report those release gates as unverified. Do not claim 250-image provider or UMA acceptance from unit tests.

---

### Task 10: Independent Code Audit

**Owner:** Fresh read-only auditor with no implementation assignment.

- [ ] **Step 1: Review the PRD, plan, and all feature-related code**

Prioritize behavioral bugs, race conditions, session crossing, history corruption, cleanup leaks, image count/byte bypasses, prompt-boundary violations, incomplete minimum UI, and missing tests.

- [ ] **Step 2: Audit memory ownership**

Trace MediaStream tracks, hidden video/canvas elements, Blob and thumbnail ownership, Object URLs, Base64 arrays, in-flight Promise closures, frozen batches, historical UI images, request lifecycle records, and PIL images.

- [ ] **Step 3: Audit scope boundaries**

Confirm normal Vision/Text/Cycle/compact/Stop/Answer Now behavior remains unchanged outside the documented exception. Confirm manager omission cannot touch child/sibling/other sessions.

- [ ] **Step 4: Return findings first**

Report severity-ordered findings with exact file/line references, then residual test/hardware gaps. The lead fixes confirmed findings and reruns Task 9 before completion.
