# ASR Realtime Input Gain Implementation Plan

> **For the single agentic implementer:** Read the linked spec completely before editing. Use test-driven development task-by-task. This project explicitly permits only one coding sub-agent for this change; that same implementer completes all tasks serially and reuses context.

**Goal:** Add a browser-persisted input-gain slider for Dictation and Voice modes whose YAML-defined value is updated in real time inside the active backend ASR WebSocket session without rewriting already queued audio.

**Architecture:** YAML supplies the default/min/max/step contract. The frontend controller resolves a versioned localStorage value, renders one shared slider, and sends `set_input_gain` over the existing ASR WebSocket. The backend snapshots the current per-session gain into every PCM queue item at receive time, so control-message ordering remains correct even when the frame worker lags.

**Tech Stack:** Python 3, FastAPI WebSocket, Pydantic v2, NumPy, pytest, Next.js 14, React 18, TypeScript, Node test runner, Tailwind CSS.

**Spec:** `docs/2026-09-01-asr-realtime-input-gain-spec.md`

## Global Constraints

- Do not run any git command without explicit user permission; this plan intentionally omits commit steps.
- Do not modify third-party dependency source under `.sglang` or `frontend/node_modules`.
- Preserve the existing single-application rule: raw PCM remains unchanged in the browser and gain is applied exactly once in `backend/asr/session.py`.
- The slider range and step come only from `config/config-asr.yaml`; do not duplicate `-24`, `36`, or `0.5` as runtime constants in frontend/backend code.
- The browser cache key is `llm-asr-input-gain`; a valid cached value overrides the YAML default, while YAML always constrains range and step.
- Gain controls are session-local and must not mutate YAML or other ASR WebSocket sessions.
- A PCM queue item captures the gain at WebSocket receive time; the frame worker must never look up the mutable current gain for an older item.
- Text and Vision pages must remain synchronized, including Dictation and Voice rendering paths.
- Do not change keyboard/dictation transcript landing, Voice auto-send/resume, replay ownership, Vision acceptance, Cycle gating, Chat/Agent routing, or page visibility semantics.
- Use `npm run build:verify`, not `npm run build`, to avoid disturbing a running frontend dev server.

## File Map

| File | Responsibility in this change |
|---|---|
| `config/config-asr.yaml` | Default/min/max/step source of truth |
| `backend/asr/config.py` | Strict numeric, ordering, step-grid, and gain-factor validation |
| `backend/asr/session.py` | Per-session control protocol and receive-time PCM gain snapshot |
| `backend/main.py` | Browser-safe `/config.asr` range exposure |
| `tests/test_asr_config.py` | YAML schema and invalid combination coverage |
| `tests/test_asr_websocket.py` | Runtime protocol, ordering, nonfatal errors, and isolation |
| `frontend/src/lib/input-gain-storage.ts` | Pure cache parsing and normalization helpers |
| `frontend/src/lib/input-gain-storage.test.mts` | Cache precedence, corruption, clamping, and step tests |
| `frontend/src/lib/api.ts` | `/config` TypeScript response contract |
| `frontend/src/lib/voice-input.ts` | Config mapping, controller state, WS commands/events, startup ordering |
| `frontend/src/components/VoiceListeningIndicator.tsx` | Shared inline range control |
| `frontend/src/components/VoiceInputPanel.tsx` | Voice-panel forwarding boundary |
| `frontend/src/app/page.tsx` | Text page wiring only |
| `frontend/src/app/vision/page.tsx` | Vision page wiring only |
| `frontend/src/lib/voice-input-state.test.mts` | Voice configuration/protocol/controller tests |
| `frontend/src/lib/voice-input-pages.test.mts` | Shared UI and synchronized page-source contracts |
| `docs/ASR-部署-PRD.md` | User/deployer-facing config and protocol documentation |

---

## Task 1: Make the input-gain range a strict YAML and `/config` contract

**Files:**

- Modify: `config/config-asr.yaml`
- Modify: `backend/asr/config.py`
- Modify: `backend/main.py`
- Modify: `tests/test_asr_config.py`
- Modify: `tests/test_asr_websocket.py`

**Interfaces:**

- Produces: `AudioConfig.input_gain_min_db: float`
- Produces: `AudioConfig.input_gain_max_db: float`
- Produces: `AudioConfig.input_gain_step_db: float`
- Produces: `AudioConfig.accepts_input_gain_db(value: object) -> bool`
- Produces: `/config.asr.input_gain_{db,min_db,max_db,step_db}`

- [ ] **Step 1: Extend the valid test fixture and write failing schema tests**

Add the new required keys to `_valid_config()` in `tests/test_asr_config.py`:

```python
"input_gain_db": 12.0,
"input_gain_min_db": -24.0,
"input_gain_max_db": 36.0,
"input_gain_step_db": 0.5,
```

Extend the successful-load test:

```python
assert config.audio.input_gain_min_db == -24.0
assert config.audio.input_gain_max_db == 36.0
assert config.audio.input_gain_step_db == 0.5
assert config.audio.accepts_input_gain_db(6.5)
assert not config.audio.accepts_input_gain_db(True)
assert not config.audio.accepts_input_gain_db(6.25)
```

Add parametrized failures that independently cover:

```python
@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"input_gain_min_db": 12.0, "input_gain_max_db": 12.0}, "min"),
        ({"input_gain_min_db": 13.0}, "input_gain_db"),
        ({"input_gain_max_db": 11.0}, "input_gain_db"),
        ({"input_gain_step_db": 0.0}, "step"),
        ({"input_gain_step_db": 100.0}, "step"),
        ({"input_gain_db": 12.25}, "step"),
        ({"input_gain_max_db": 36.25}, "step"),
    ],
)
def test_asr_config_rejects_invalid_input_gain_range(
    tmp_path: Path, updates: dict, message: str
):
    data = _valid_config(tmp_path)
    data["audio"].update(updates)
    with pytest.raises(ValidationError, match=message):
        load_asr_config(_write_config(tmp_path, data), project_root=tmp_path)
```

Also test `float("nan")`, positive infinity, negative infinity, and a finite dB value whose linear factor cannot be represented by the Float32 path. Use explicit assignments rather than YAML string spellings so the intended numeric type reaches Pydantic.

- [ ] **Step 2: Run the new config tests and confirm failure**

Run:

```bash
.sglang/bin/python -m pytest tests/test_asr_config.py -q
```

Expected: new assertions fail because the three fields and runtime acceptance helper do not exist.

- [ ] **Step 3: Implement the strict `AudioConfig` contract**

In `backend/asr/config.py`, import `math` and `numpy as np`. Replace the hard-coded `input_gain_db` bounds with finite fields:

```python
class AudioConfig(StrictModel):
    sample_rate_hz: Literal[16000]
    channels: Literal[1]
    sample_format: Literal["float32"]
    capture_frame_ms: int = Field(gt=0)
    input_gain_db: float = Field(allow_inf_nan=False)
    input_gain_min_db: float = Field(allow_inf_nan=False)
    input_gain_max_db: float = Field(allow_inf_nan=False)
    input_gain_step_db: float = Field(gt=0.0, allow_inf_nan=False)
    clip_limit: float = Field(gt=0.0, le=1.0)
    browser_constraints: BrowserConstraintsConfig
```

Add private helpers and one public runtime predicate:

```python
def _gain_step_aligned(value: float, minimum: float, step: float) -> bool:
    ticks = (value - minimum) / step
    return math.isclose(ticks, round(ticks), rel_tol=0.0, abs_tol=1e-7)


def _float32_gain_factor_is_valid(gain_db: float) -> bool:
    try:
        factor = 10.0 ** (gain_db / 20.0)
    except OverflowError:
        return False
    return 0.0 < factor <= float(np.finfo(np.float32).max)
```

Use an `@model_validator(mode="after")` to enforce min/max/default/step and representable linear factors. Add:

```python
def accepts_input_gain_db(self, value: object) -> bool:
    if type(value) not in (int, float):
        return False
    gain_db = float(value)
    return (
        math.isfinite(gain_db)
        and self.input_gain_min_db <= gain_db <= self.input_gain_max_db
        and _gain_step_aligned(
            gain_db, self.input_gain_min_db, self.input_gain_step_db
        )
        and _float32_gain_factor_is_valid(gain_db)
    )
```

The validator must call the same helpers used by runtime controls so startup and WebSocket validation cannot drift.

- [ ] **Step 4: Add the YAML fields**

Immediately after `input_gain_db` in `config/config-asr.yaml`, add documented values:

```yaml
  # Web 输入增益滑块允许的最小值、最大值和步长；后端实时控制使用同一约束。
  input_gain_min_db: -24
  input_gain_max_db: 36
  input_gain_step_db: 0.5
```

Update the existing `input_gain_db` comment to state that it is the browser-cache fallback and old-client default.

- [ ] **Step 5: Write the failing `/config` exposure assertions**

Extend `test_main_config_exposes_only_browser_safe_asr_values()` in `tests/test_asr_websocket.py`:

```python
assert payload["asr"]["input_gain_min_db"] == configured.audio.input_gain_min_db
assert payload["asr"]["input_gain_max_db"] == configured.audio.input_gain_max_db
assert payload["asr"]["input_gain_step_db"] == configured.audio.input_gain_step_db
```

Retain the assertion that `model_path` is not exposed.

- [ ] **Step 6: Expose the values in `backend/main.py`**

Add adjacent browser-safe keys:

```python
"input_gain_db": asr_config.audio.input_gain_db,
"input_gain_min_db": asr_config.audio.input_gain_min_db,
"input_gain_max_db": asr_config.audio.input_gain_max_db,
"input_gain_step_db": asr_config.audio.input_gain_step_db,
```

- [ ] **Step 7: Run Task 1 tests**

Run:

```bash
.sglang/bin/python -m pytest tests/test_asr_config.py tests/test_asr_websocket.py::test_main_config_exposes_only_browser_safe_asr_values -q
```

Expected: all selected tests pass.

---

## Task 2: Add ordered, session-local WebSocket gain control

**Files:**

- Modify: `backend/asr/session.py`
- Modify: `tests/test_asr_websocket.py`

**Interfaces:**

- Consumes: `AudioConfig.accepts_input_gain_db(value)` from Task 1
- Produces client message: `{type: "set_input_gain", input_gain_db: number}`
- Produces server event: `{type: "input_gain_changed", input_gain_db: number}`
- Produces server event: `{type: "control_error", code, message, input_gain_db}`
- Produces internal `QueuedAudioFrame` carrying `input_gain_db`

- [ ] **Step 1: Write failing protocol and ordering tests**

Add a VAD test double that retains copies of processed samples:

```python
class CapturingVad(FakeVad):
    def __init__(self):
        super().__init__()
        self.frames: list[np.ndarray] = []

    def accept(self, samples):
        self.frames.append(np.asarray(samples, np.float32).copy())
        return super().accept(samples)
```

Add these async production-path tests using `AsrService` + `ControlledWebSocket`:

1. `test_runtime_input_gain_changes_following_pcm_without_rewriting_previous_frame`
   - start with YAML/default 0 dB;
   - send PCM amplitude `0.05`;
   - wait for the first `CapturingVad.frames` entry;
   - send `set_input_gain` with `20.0` and await `input_gain_changed`;
   - send the same PCM and wait for the second frame;
   - assert first frame is about `0.05`, second about `0.5`.
2. `test_runtime_input_gain_can_change_after_recording_started`
   - configure a short SPL trigger;
   - send enough speech to observe `state_changed=recording`;
   - send a new valid gain and assert acknowledgement without socket closure;
   - send another PCM frame and assert the captured samples use the new value.
3. `test_invalid_runtime_input_gain_is_nonfatal_and_preserves_last_value`
   - send an off-step or out-of-range value;
   - assert `control_error.input_gain_db` equals the last valid value;
   - send valid PCM afterward and assert the session still emits level/state events.
4. `test_runtime_input_gain_is_isolated_per_websocket_session`
   - open two `AsrService` sessions;
   - change only one session;
   - send identical PCM to both and assert distinct processed amplitudes.
5. `test_legacy_client_uses_yaml_default_without_gain_control`
   - start + PCM only;
   - assert configured default gain remains applied.

Add a focused receive-time snapshot test by inspecting queued objects before the worker consumes them. It must prove that an item enqueued before changing `session._input_gain_db` retains the old value and the later item retains the new value.

- [ ] **Step 2: Run the new WebSocket tests and confirm failure**

Run:

```bash
.sglang/bin/python -m pytest tests/test_asr_websocket.py -q
```

Expected: the server rejects `set_input_gain` as an invalid control and queue items have no gain snapshot.

- [ ] **Step 3: Add a typed queue item and per-session value**

In `backend/asr/session.py`, add:

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QueuedAudioFrame:
    generation: int
    payload: bytes
    start_sample_index: int
    input_gain_db: float
```

Initialize:

```python
self._input_gain_db = config.audio.input_gain_db
self._frame_queue: asyncio.Queue[QueuedAudioFrame] = asyncio.Queue(
    maxsize=config.transport.max_buffered_frames
)
```

In `_enqueue_binary()`, snapshot the current value:

```python
item = QueuedAudioFrame(
    generation=self._frame_generation,
    payload=payload,
    start_sample_index=start_sample_index,
    input_gain_db=self._input_gain_db,
)
```

Update `_frame_worker()` and `_process_binary()` so `apply_gain()` receives `item.input_gain_db`, never `self._config.audio.input_gain_db`:

```python
async def _process_binary(
    self,
    payload: bytes,
    start_sample_index: int,
    input_gain_db: float,
) -> None:
    raw = np.frombuffer(payload, dtype="<f4").astype(np.float32, copy=False)
    gain = apply_gain(raw, input_gain_db, self._config.audio.clip_limit)
```

- [ ] **Step 4: Extend `_accept_control()` without weakening the handshake**

Keep `abort` first. Handle `set_input_gain` as a dedicated branch:

```python
if payload.get("type") == "set_input_gain":
    if not self._handshake_complete:
        await self._send_input_gain_error("Start handshake required")
        return True
    value = payload.get("input_gain_db")
    if not self._config.audio.accepts_input_gain_db(value):
        await self._send_input_gain_error(
            "Input gain is outside the configured range or step"
        )
        return True
    self._input_gain_db = float(value)
    await self._send(
        {"type": "input_gain_changed", "input_gain_db": self._input_gain_db}
    )
    return True
```

The error helper must send:

```python
{
    "type": "control_error",
    "code": "invalid_input_gain",
    "message": message,
    "input_gain_db": self._input_gain_db,
}
```

Return `True` so invalid gain controls do not terminate the receive loop. Preserve the existing one-start-handshake rule and fatal behavior for unrelated invalid control messages.

- [ ] **Step 5: Run backend ASR regression tests**

Run:

```bash
.sglang/bin/python -m pytest \
  tests/test_asr_config.py \
  tests/test_asr_websocket.py \
  tests/test_asr_audio.py \
  tests/test_asr_state_machine.py -q
```

Expected: all pass, including existing abort, frame queue, VAD gating, replay, shutdown, and transcription tests.

---

## Task 3: Implement cache resolution and frontend controller protocol

**Files:**

- Create: `frontend/src/lib/input-gain-storage.ts`
- Create: `frontend/src/lib/input-gain-storage.test.mts`
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/voice-input.ts`
- Modify: `frontend/src/lib/voice-input-state.test.mts`

**Interfaces:**

- Consumes: `/config.asr` gain fields from Task 1
- Produces: `INPUT_GAIN_STORAGE_KEY = "llm-asr-input-gain"`
- Produces: `normalizeInputGainDb(value, range) -> number`
- Produces: `readInputGainDb(storage, range) -> number`
- Produces: `writeInputGainDb(storage, value) -> void`
- Produces: `VoiceInputController.inputGain: VoiceInputGainControl | null`

- [ ] **Step 1: Write failing pure cache tests**

Create `frontend/src/lib/input-gain-storage.test.mts`. Use a tiny in-memory implementation of `getItem`, `setItem`, and `removeItem`; do not depend on jsdom.

Cover these exact cases:

```ts
const range = { defaultDb: 0, minDb: -24, maxDb: 36, stepDb: 0.5 };
```

- absent key returns `0`;
- `{version:1,inputGainDb:6.5}` returns `6.5` and beats default;
- malformed JSON is removed and returns default;
- wrong version, missing field, string, `NaN`, and infinities return default;
- cached `100` clamps to `36` and rewrites `36`;
- cached `-100` clamps to `-24` and rewrites `-24`;
- cached `6.26` snaps to `6.5` and rewrites `6.5`;
- storage methods throwing do not escape and return the in-memory/default value;
- `writeInputGainDb` writes `{version:1,inputGainDb:value}`.

- [ ] **Step 2: Run the cache test and confirm module-not-found failure**

Run from `frontend/`:

```bash
node --test --experimental-strip-types src/lib/input-gain-storage.test.mts
```

- [ ] **Step 3: Implement browser-safe pure storage helpers**

Create `frontend/src/lib/input-gain-storage.ts` with no top-level `window` access:

```ts
export const INPUT_GAIN_STORAGE_KEY = "llm-asr-input-gain";

export interface InputGainRange {
  defaultDb: number;
  minDb: number;
  maxDb: number;
  stepDb: number;
}

export interface InputGainStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}
```

Normalization must use the YAML minimum as the step-grid origin:

```ts
export function normalizeInputGainDb(value: number, range: InputGainRange): number {
  const bounded = Math.max(range.minDb, Math.min(range.maxDb, value));
  const ticks = Math.round((bounded - range.minDb) / range.stepDb);
  const snapped = range.minDb + ticks * range.stepDb;
  return Math.max(range.minDb, Math.min(range.maxDb, Number(snapped.toFixed(10))));
}
```

`readInputGainDb()` catches JSON and storage exceptions, removes malformed entries when possible, uses the default only for missing/invalid cache, and rewrites a valid but noncanonical cache value. `writeInputGainDb()` catches storage exceptions so unavailable localStorage never disables voice input.

- [ ] **Step 4: Extend the frontend config contract and mapping tests**

In `frontend/src/lib/api.ts`, add:

```ts
input_gain_min_db: number;
input_gain_max_db: number;
input_gain_step_db: number;
```

In `VoiceInputConfig`, add camelCase fields and map them explicitly in `createVoiceInputConfig()`.

Update the existing `browser-safe server config maps explicitly` test fixture and expected object in `voice-input-state.test.mts` with:

```ts
input_gain_min_db: -24,
input_gain_max_db: 36,
input_gain_step_db: 0.5,
```

and:

```ts
inputGainMinDb: -24,
inputGainMaxDb: 36,
inputGainStepDb: 0.5,
```

- [ ] **Step 5: Write failing protocol parser and controller source-contract tests**

Extend `AsrServerEvent` tests to require parsing:

```ts
assert.deepEqual(
  parseAsrServerEvent('{"type":"input_gain_changed","input_gain_db":6.5}'),
  { type: "input_gain_changed", inputGainDb: 6.5 },
);

assert.deepEqual(
  parseAsrServerEvent(
    '{"type":"control_error","code":"invalid_input_gain",' +
    '"message":"bad","input_gain_db":0}'
  ),
  {
    type: "control_error",
    code: "invalid_input_gain",
    message: "bad",
    inputGainDb: 0,
  },
);
```

Add source assertions proving the `ready` branch sends an object whose exact fields are `type: "set_input_gain"` and `input_gain_db: inputGainDbRef.current` before `resources.socketReady = true`. Also extract the `setInputGainDb` callback body and prove it contains no `activateVoice`, `deactivateVoice`, or `releaseCurrent` call.

- [ ] **Step 6: Implement the controller-owned gain state**

In `voice-input.ts`, define:

```ts
export interface VoiceInputGainControl {
  valueDb: number;
  minDb: number;
  maxDb: number;
  stepDb: number;
  setValueDb: (valueDb: number) => void;
}
```

Extend `VoiceInputController` with:

```ts
inputGain: VoiceInputGainControl | null;
```

Inside `useVoiceInput()`:

- add `useState` to the React import;
- keep `inputGainDbRef`, `lastConfirmedInputGainDbRef`, and `inputGainInitializedForConfigRef`;
- when a new non-null config arrives, build the range, call `readInputGainDb(window.localStorage, range)` in an effect, and seed state/refs;
- if config becomes null, expose `inputGain: null` without deleting cache;
- implement a stable `setInputGainDb` callback that normalizes, updates state/ref, persists, and sends `set_input_gain` only when the current socket is open, handshake-ready, and not closing;
- do not change capture generation or resource ownership.

The new `ready` branch order must be:

```ts
socket.send(JSON.stringify({
  type: "set_input_gain",
  input_gain_db: inputGainDbRef.current,
}));
resources.socketReady = true;
dispatch({ type: "ready", generation, sessionId: event.sessionId });
```

WebSocket send ordering guarantees that subsequent binary `socket.send(frame)` calls follow the gain control.

Handle new server events:

```ts
case "input_gain_changed":
  lastConfirmedInputGainDbRef.current = event.inputGainDb;
  break;

case "control_error":
  inputGainDbRef.current = event.inputGainDb;
  setInputGainDbState(event.inputGainDb);
  writeInputGainDb(window.localStorage, event.inputGainDb);
  callbacksRef.current.onError?.({
    code: "backend_error",
    message: event.message,
    recoverable: true,
  });
  break;
```

Do not call `reportFailure()` for `control_error`, because that disposes the active voice session.

- [ ] **Step 7: Run Task 3 tests and type-check**

Run from `frontend/`:

```bash
node --test --experimental-strip-types \
  src/lib/input-gain-storage.test.mts \
  src/lib/voice-input-state.test.mts
npm run type-check
```

Expected: all tests and type-check pass.

---

## Task 4: Render one shared, always-live slider in all voice input paths

**Files:**

- Modify: `frontend/src/components/VoiceListeningIndicator.tsx`
- Modify: `frontend/src/components/VoiceInputPanel.tsx`
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Modify: `frontend/src/lib/voice-input-pages.test.mts`

**Interfaces:**

- Consumes: `VoiceInputGainControl` from Task 3
- `VoiceListeningIndicatorProps.inputGain: VoiceInputGainControl | null`
- `VoiceInputPanelProps.inputGain: VoiceInputGainControl | null`

- [ ] **Step 1: Update failing shared-component source tests**

Replace the old assertion that `VoiceInputPanel` contains no `<input>` with assertions scoped to `VoiceListeningIndicator`:

```ts
assert.match(indicator, /type="range"/);
assert.match(indicator, /min=\{inputGain\.minDb\}/);
assert.match(indicator, /max=\{inputGain\.maxDb\}/);
assert.match(indicator, /step=\{inputGain\.stepDb\}/);
assert.match(indicator, /value=\{inputGain\.valueDb\}/);
assert.match(indicator, /onChange=.*inputGain\.setValueDb/);
assert.match(indicator, /aria-label="Input gain"/);
assert.match(indicator, /aria-valuemin/);
assert.match(indicator, /aria-valuemax/);
assert.match(indicator, /aria-valuenow/);
assert.doesNotMatch(indicator, /disabled=.*recording|recording.*disabled/s);
```

Keep the existing assertion that `VoiceInputPanel` has no editable transcript `<textarea>`; narrow it so the intentional range input does not invalidate the test.

- [ ] **Step 2: Add failing Text/Vision synchronized wiring assertions**

For each page, require both render paths to pass the same object:

```tsx
<VoiceInputPanel
  state={voiceController.state}
  inputGain={voiceController.inputGain}
  disabled={isLoading || cycleStatus === 'running'}
  onRetry={() => void voiceController.activateVoice()}
  onSwitchToKeyboard={() => void handleVoiceModeChange('keyboard')}
/>

<VoiceListeningIndicator
  state={voiceController.state}
  inputGain={voiceController.inputGain}
/>
```

Assert the pages no longer display `voiceConfig?.inputGainDb`, because that is only the YAML default and would make the label stale after dragging.

- [ ] **Step 3: Implement the shared inline slider**

In `VoiceListeningIndicator.tsx`:

- import `VoiceInputGainControl` as a type from `../lib/voice-input`;
- replace optional `inputGainDb` with `inputGain: VoiceInputGainControl | null`;
- compute the label from `inputGain?.valueDb ?? 0`;
- convert the secondary text row into a flex-wrap container;
- keep the current phase announcement and waveform unchanged.

The control shape should be:

```tsx
{inputGain ? (
  <input
    type="range"
    min={inputGain.minDb}
    max={inputGain.maxDb}
    step={inputGain.stepDb}
    value={inputGain.valueDb}
    onChange={(event) => inputGain.setValueDb(event.currentTarget.valueAsNumber)}
    aria-label="Input gain"
    aria-valuemin={inputGain.minDb}
    aria-valuemax={inputGain.maxDb}
    aria-valuenow={inputGain.valueDb}
    className="h-2 min-w-[120px] flex-1 cursor-pointer accent-nvidia-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nvidia-500"
  />
) : null}
```

Place it immediately after the span containing `gain ±x.x dB`. Do not attach `disabled` to state phase, `isLoading`, `cycleStatus`, SPL trigger state, or recording state.

- [ ] **Step 4: Forward the object through `VoiceInputPanel`**

Change the prop and forward it without owning state:

```tsx
export interface VoiceInputPanelProps {
  state: VoiceInputState;
  inputGain: VoiceInputGainControl | null;
  // existing retry/switch/disabled/className props remain
}

<VoiceListeningIndicator state={state} inputGain={inputGain} compact />
```

- [ ] **Step 5: Wire both pages identically**

In both `frontend/src/app/page.tsx` and `frontend/src/app/vision/page.tsx`, replace the two old default-value props with:

```tsx
inputGain={voiceController.inputGain}
```

Do not add page-local gain state or localStorage access. Do not alter the existing `inputMode` conditions.

- [ ] **Step 6: Run focused UI tests and type-check**

Run from `frontend/`:

```bash
node --test --experimental-strip-types \
  src/lib/input-gain-storage.test.mts \
  src/lib/voice-input-state.test.mts \
  src/lib/voice-input-pages.test.mts
npm run type-check
```

Expected: tests pass and Text/Vision props remain in sync.

---

## Task 5: Update ASR documentation and run end-to-end verification

**Files:**

- Modify: `docs/ASR-部署-PRD.md`
- Verify all files from Tasks 1–4

**Interfaces:**

- Consumes the implemented YAML, WebSocket, cache, and UI contracts.
- Produces deployer-facing documentation consistent with the approved spec.

- [ ] **Step 1: Update the ASR PRD configuration section**

Document the four YAML fields together, including:

- cache-over-default precedence;
- YAML min/max/step authority;
- backend restart requirement after range edits;
- strict validation and no hidden range fallback.

Add the three new fields to the configuration validation table.

- [ ] **Step 2: Update the ASR PRD protocol and state sections**

Add the exact `set_input_gain`, `input_gain_changed`, and `control_error` JSON shapes from the spec. State explicitly:

- controls are per WebSocket session;
- invalid gain is nonfatal;
- gain remains mutable in BUFFERING/CANDIDATE/RECORDING;
- each PCM queue item snapshots the gain at receive time;
- historical/pre-trigger/recorded frames are not rewritten;
- “SPL” remains post-gain RMS dBFS, not calibrated physical SPL.

- [ ] **Step 3: Run the complete backend ASR verification**

From repository root:

```bash
.sglang/bin/python -m pytest \
  tests/test_asr_config.py \
  tests/test_asr_websocket.py \
  tests/test_asr_audio.py \
  tests/test_asr_ring_buffer.py \
  tests/test_asr_state_machine.py \
  tests/test_asr_sglang_client.py \
  tests/test_asr_launcher.py -q
```

Record the exact pass/fail count. Investigate any failure before continuing; do not dismiss it as unrelated without evidence.

- [ ] **Step 4: Run the complete focused frontend verification**

From `frontend/`:

```bash
node --test --experimental-strip-types \
  src/lib/input-gain-storage.test.mts \
  src/lib/voice-input-state.test.mts \
  src/lib/voice-input-pages.test.mts
npm run type-check
npm run build:verify
```

Record test counts and all three exit codes.

- [ ] **Step 5: Perform a source-level invariant audit**

Run from repository root:

```bash
rg -n "input_gain_(db|min_db|max_db|step_db)|inputGain(Db|MinDb|MaxDb|StepDb)|set_input_gain|input_gain_changed|control_error" \
  config backend frontend/src tests docs/ASR-部署-PRD.md
```

Confirm:

1. `-24`, `36`, and `0.5` occur as configured/test fixture values, not hidden runtime slider bounds.
2. `apply_gain()` is still called only in the backend PCM path for live ASR.
3. `_process_binary()` uses its queue-item gain argument.
4. both page files pass `voiceController.inputGain` twice: Voice panel and Dictation indicator.
5. no page writes the gain cache directly.
6. no recording/candidate phase disables the range input.

- [ ] **Step 6: Summarize implementation evidence for the main agent**

Report:

- files changed;
- exact backend and frontend verification results;
- the test that proves receive-time ordering across a gain update;
- the test that proves changing gain during RECORDING;
- the test that proves session isolation;
- any deviations from this plan and why they were necessary;
- any remaining manual browser/Jetson verification that cannot be completed in the local test environment.

Do not run git commands or create a commit.
