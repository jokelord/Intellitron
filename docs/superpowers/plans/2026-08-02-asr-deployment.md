# Qwen3-ASR Local Voice Input Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add robust Keyboard/Voice input to both Web UI pages using browser PCM capture, backend Silero/ring-buffer utterance detection, and a dedicated SGLang Qwen3-ASR service on port 30003.

**Architecture:** Android/Desktop Chromium captures 16 kHz mono Float32 PCM with AudioWorklet and sends bounded frames over a session-local WebSocket. FastAPI applies configured gain, performs dBFS + Silero VAD + the four-state recording machine, then sends a completed in-memory WAV to the dedicated SGLang `/v1/audio/transcriptions` endpoint. Final text reuses the existing Text/Vision Chat or Agent send path and never changes historical prompt shape.

**Tech Stack:** Python 3.12, Pydantic v2, NumPy, Silero VAD 6.2 ONNX CPU, FastAPI WebSocket, httpx, SGLang 0.5.15, Next.js 14, React 18, TypeScript, AudioWorklet, Tailwind CSS, pytest, Node test runner.

## Global Constraints

- ASR inference is SGLang-only; do not add qwen-asr, Transformers, vLLM, or cloud fallback.
- Install `silero-vad==6.2.0` into the existing `.sglang` uv environment; never modify dependency source.
- Default Qwen3-ASR model path is `/home/zk/Project/LLM-weight/Qwen3-ASR-1.7B`; default port is `30003`.
- All important runtime values come from `config/config-asr.yaml` through a strict typed config object.
- Default `audio.input_gain_db=12.0`, `transcription.streaming=false`, and `transcription.auto_send=true`.
- SGLang health/model probes use configured `sglang.health_timeout_seconds=2.0`, never the transcription timeout.
- Voice-to-Keyboard abort is session-local, awaits worker cleanup, and never falls back to abort-all.
- Text and Vision pages must both use shared voice modules/components.
- Browser performs only capture, target-rate AudioContext resampling, waveform display, and transport; Jetson performs gain, VAD, buffering, WAV encoding, and inference.
- Android LAN microphone use requires a trusted HTTPS secure context and an explicit user click on Voice.
- ASR diagnostics, partial transcripts, waveform state, request IDs, and timeline data never enter LLM messages.
- Do not modify or revert the user's unrelated OCR script deletion/rename worktree changes.
- Do not create commits; the user authorized read-only Git commands only.

## File Map

**Configuration and launch**

- Create `config/config-asr.yaml`: authoritative ASR/VAD/transport/TLS values.
- Modify `config/config-overall.yaml`: route the ASR config.
- Create `scripts/asr_preflight.py`: strict model/config/package/SGLang capability checks and CLI argument serialization.
- Create `scripts/2c_start_asr_sglang.sh`: launch dedicated SGLang from serialized config.
- Create `scripts/generate_asr_tls_cert.sh`: generate local CA/server certificate from configured SANs.
- Modify `scripts/3_start_backend.sh`: optional Uvicorn TLS using ASR config.
- Modify `scripts/4_start_frontend.sh`: optional Next HTTPS and trusted backend CA.
- Modify `requirements_for_sglang.txt`: pin Silero 6.2.

**Backend**

- Create `backend/asr/config.py`: Pydantic models and standalone ASR YAML loader.
- Create `backend/asr/models.py`: frame, utterance, result, and event types.
- Create `backend/asr/audio.py`: gain, clipping, dBFS, WAV encoding.
- Create `backend/asr/ring_buffer.py`: fixed ring with global sample indices.
- Create `backend/asr/vad.py`: Silero ONNX adapter and 512-sample assembler.
- Create `backend/asr/state_machine.py`: four recording states and completed utterances.
- Create `backend/asr/sglang_client.py`: health/model probe and cancellable multipart/SSE transcription.
- Create `backend/asr/session.py`: one WebSocket generation lifecycle.
- Create `backend/asr/service.py`: session admission, shared ASR semaphore, health, shutdown.
- Create `backend/api/asr.py`: `/v1/asr/stream` router.
- Modify `backend/config.py`: attach `AsrConfig` without merging generic ASR sections into global `_data`.
- Modify `backend/main.py`: lifespan wiring, router registration, `/config`, and `/health` projection.

**Frontend**

- Create `frontend/src/lib/voice-input-state.ts`: pure reducer, capability errors, URL construction.
- Create `frontend/src/lib/voice-input.ts`: browser capture/WebSocket controller.
- Create `frontend/public/asr-pcm-worklet.js`: fixed-frame PCM worklet.
- Create `frontend/src/components/VoiceModeToggle.tsx`: responsive Layout A switch.
- Create `frontend/src/components/VoiceInputPanel.tsx`: waveform/status/pending transcript.
- Modify `frontend/src/lib/api.ts`: ASR config and health response types.
- Modify `frontend/src/app/page.tsx`: Text glue and explicit-text send entry.
- Modify `frontend/src/app/vision/page.tsx`: Vision glue and image-preserving send entry.
- Modify `frontend/src/app/globals.css`: voice waveform and responsive header styles.

**Tests and docs**

- Create `tests/test_asr_config.py`.
- Create `tests/test_asr_audio.py`.
- Create `tests/test_asr_ring_buffer.py`.
- Create `tests/test_asr_state_machine.py`.
- Create `tests/test_asr_sglang_client.py`.
- Create `tests/test_asr_websocket.py`.
- Create `tests/test_asr_launcher.py`.
- Create `frontend/src/lib/voice-input-state.test.mts`.
- Create `frontend/src/lib/voice-input-pages.test.mts`.
- Modify `README.md`: ASR launch and Android CA trust steps.

---

### Task 1: Strict ASR Configuration and Dependency

**Ownership:** Main session only.

**Files:**
- Create: `backend/asr/__init__.py`
- Create: `backend/asr/config.py`
- Create: `config/config-asr.yaml`
- Modify: `config/config-overall.yaml`
- Modify: `backend/config.py`
- Modify: `requirements_for_sglang.txt`
- Test: `tests/test_asr_config.py`

**Interfaces:**
- Produces: `load_asr_config(path: Path, *, project_root: Path) -> AsrConfig`
- Produces: `AsrConfig.sglang.base_url -> str`
- Produces: `AsrConfig.audio.frame_samples -> int`
- Produces: `AppConfig.asr: AsrConfig`

- [ ] **Step 1: Write failing strict-schema tests**

```python
def test_asr_config_defaults_and_derived_values(tmp_path: Path):
    path = write_valid_asr_yaml(tmp_path, input_gain_db=12.0, port=30003)
    config = load_asr_config(path, project_root=tmp_path)
    assert config.audio.frame_samples == 320
    assert config.sglang.base_url == "http://127.0.0.1:30003"
    assert config.transcription.streaming is False
    assert config.transcription.auto_send is True

def test_asr_config_rejects_unknown_and_cross_field_errors(tmp_path: Path):
    path = write_valid_asr_yaml(tmp_path, extra="unknown: true")
    with pytest.raises(ValidationError):
        load_asr_config(path, project_root=tmp_path)
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `.sglang/bin/python -m pytest tests/test_asr_config.py -q`  
Expected: FAIL because `backend.asr.config` and `config/config-asr.yaml` do not exist.

- [ ] **Step 3: Implement strict Pydantic models and loader**

```python
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

class AudioConfig(StrictModel):
    sample_rate_hz: Literal[16000]
    channels: Literal[1]
    sample_format: Literal["float32"]
    capture_frame_ms: PositiveInt
    input_gain_db: float = Field(ge=-24.0, le=36.0)
    clip_limit: float = Field(gt=0.0, le=1.0)

    @computed_field
    @property
    def frame_samples(self) -> int:
        samples = self.sample_rate_hz * self.capture_frame_ms
        if samples % 1000:
            raise ValueError("capture_frame_ms must produce an integer sample count")
        return samples // 1000
```

Implement nested `RecordingConfig`, `TriggerConfig`, `VadConfig`, `SGLangAsrConfig`, `TranscriptionConfig`, `TransportConfig`, `RuntimeConfig`, `BrowserSecurityConfig`, `ObservabilityConfig`, and cross-field validation exactly as the design spec.

- [ ] **Step 4: Attach ASR config to existing AppConfig**

Load the path named by `overall["asr"]` separately. Do not add ASR's generic `audio`, `runtime`, or `sglang` keys to `ConfigLoader._data`.

```python
asr_path = overall.get("asr")
asr = load_asr_config(PROJECT_ROOT / asr_path, project_root=PROJECT_ROOT) if asr_path else disabled_asr_config()
```

- [ ] **Step 5: Add complete YAML and dependency pin**

Copy the approved YAML from design section 5 verbatim and add `silero-vad==6.2.0` to `requirements_for_sglang.txt` beside model/audio dependencies.

- [ ] **Step 6: Run focused and existing config tests**

Run: `.sglang/bin/python -m pytest tests/test_asr_config.py tests/test_runtime_config_limits.py tests/test_sglang_config.py -q`  
Expected: PASS.

### Task 2: Audio Primitives and Ring Buffer

**Ownership:** Backend coding agent. This agent may only edit `backend/asr/{models,audio,ring_buffer}.py` and `tests/test_asr_{audio,ring_buffer}.py`.

**Files:**
- Create: `backend/asr/models.py`
- Create: `backend/asr/audio.py`
- Create: `backend/asr/ring_buffer.py`
- Test: `tests/test_asr_audio.py`
- Test: `tests/test_asr_ring_buffer.py`

**Interfaces:**
- Consumes: `AudioConfig`, `RecordingConfig`
- Produces: `apply_gain(samples, gain_db, clip_limit) -> GainResult`
- Produces: `rms_dbfs(samples) -> float`
- Produces: `encode_wav(samples, sample_rate_hz) -> bytes`
- Produces: `IndexedAudioFrame(samples, start_sample_index, end_sample_index)`
- Produces: `PretriggerRingBuffer.append(frame)` and `.snapshot(min_start_index=...)`

- [ ] **Step 1: Write failing gain/dBFS/WAV tests**

```python
def test_gain_is_applied_once_and_reports_clipping():
    result = apply_gain(np.array([0.1, 0.5], np.float32), 12.0, 0.999)
    np.testing.assert_allclose(result.samples[0], 0.398107, rtol=1e-5)
    assert result.samples[1] == pytest.approx(0.999)
    assert result.clipping_ratio == pytest.approx(0.5)

def test_wav_is_mono_16khz():
    payload = encode_wav(np.zeros(320, np.float32), 16000)
    info = sf.info(io.BytesIO(payload))
    assert (info.samplerate, info.channels, info.frames) == (16000, 1, 320)
```

- [ ] **Step 2: Write failing ring wrap/snapshot/dedup tests**

```python
def test_ring_wraps_and_snapshot_is_ordered():
    ring = PretriggerRingBuffer(capacity_samples=4)
    ring.append(frame([0, 1, 2], start=0))
    ring.append(frame([3, 4, 5], start=3))
    snap = ring.snapshot()
    assert snap.start_sample_index == 2
    assert snap.samples.tolist() == [2, 3, 4, 5]
```

- [ ] **Step 3: Run tests and confirm RED**

Run: `.sglang/bin/python -m pytest tests/test_asr_audio.py tests/test_asr_ring_buffer.py -q`  
Expected: FAIL on missing modules.

- [ ] **Step 4: Implement minimal vectorized primitives**

Use NumPy operations per frame, `np.clip`, stable silence handling, `soundfile.write` to `BytesIO`, and a preallocated ring with write pointer/valid length/global start index. Reject discontinuous frames instead of concatenating them.

- [ ] **Step 5: Run focused tests**

Run: `.sglang/bin/python -m pytest tests/test_asr_audio.py tests/test_asr_ring_buffer.py -q`  
Expected: PASS.

### Task 3: Silero Adapter and Recording State Machine

**Ownership:** Same backend coding agent after Task 2. It may additionally edit `backend/asr/{vad,state_machine}.py` and `tests/test_asr_state_machine.py`.

**Files:**
- Create: `backend/asr/vad.py`
- Create: `backend/asr/state_machine.py`
- Test: `tests/test_asr_state_machine.py`

**Interfaces:**
- Consumes: audio/ring interfaces from Task 2 and config from Task 1.
- Produces: `VadObservation(probability: float, speech_started: bool, speech_ended: bool, is_speech: bool)`
- Produces: `SileroVadAdapter.accept(samples) -> list[VadObservation]`, `.reset()`
- Produces: `RecordingStateMachine.accept(frame, dbfs, vad_probability, vad_is_speech) -> list[RecordingEvent]`
- Produces: `CompletedUtterance(samples, start_sample_index, end_sample_index, metadata)`

- [ ] **Step 1: Write failing four-state tests using fake VAD observations**

```python
def test_pretrigger_trigger_frame_and_tail_are_each_present_once():
    machine = build_machine(pretrigger_ms=40, end_silence_ms=40, tail_ms=20)
    events = feed(machine, speech_scenario())
    utterance = only_completed(events)
    assert_monotonic_without_duplicate_samples(utterance)
    assert utterance.pretrigger_samples == 640

def test_end_timer_requires_low_level_and_non_speech_concurrently():
    machine = build_machine(end_silence_ms=40)
    feed_frame(machine, dbfs=-60, vad_probability=.8)
    feed_frame(machine, dbfs=-20, vad_probability=.1)
    assert machine.end_silence_samples == 0
```

- [ ] **Step 2: Add rejection/overlap/max-duration tests**

Assert candidate timeout invokes no completed event, successful commit advances `last_committed_sample_index`, failed/cancelled inference does not, and max duration produces one forced completion.

- [ ] **Step 3: Run tests and confirm RED**

Run: `.sglang/bin/python -m pytest tests/test_asr_state_machine.py -q`  
Expected: FAIL on missing adapters/state machine.

- [ ] **Step 4: Implement Silero ONNX adapter**

Resolve `package://silero_vad/data/silero_vad.onnx` with `importlib.resources`, load with `load_silero_vad(onnx=True)`, aggregate exactly 512 samples, preserve model state per session, and expose reset. Dependency import must happen during enabled service initialization, not at module import for disabled configurations.

- [ ] **Step 5: Implement the four-state machine**

Keep trigger-frame append index-aware. Use instantaneous VAD probability for combined end silence. Treat `TRANSCRIBING` as session lifecycle outside this class.

- [ ] **Step 6: Run focused backend-core tests**

Run: `.sglang/bin/python -m pytest tests/test_asr_audio.py tests/test_asr_ring_buffer.py tests/test_asr_state_machine.py -q`  
Expected: PASS.

### Task 4: SGLang Client, Session Service, and WebSocket API

**Ownership:** Main session after backend agent returns.

**Files:**
- Create: `backend/asr/sglang_client.py`
- Create: `backend/asr/session.py`
- Create: `backend/asr/service.py`
- Create: `backend/api/asr.py`
- Modify: `backend/api/__init__.py`
- Modify: `backend/main.py`
- Test: `tests/test_asr_sglang_client.py`
- Test: `tests/test_asr_websocket.py`
- Modify test: `tests/test_main_lifespan_wiring.py`

**Interfaces:**
- Consumes all Task 1-3 backend interfaces.
- Produces: `SGLangAsrClient.probe() -> AsrUpstreamStatus`
- Produces: `SGLangAsrClient.transcribe(wav_bytes, abort_event) -> AsrResult`
- Produces: `AsrService.start_session(websocket)`, `.health()`, `.shutdown()`

- [ ] **Step 1: Write failing httpx transport tests**

```python
@pytest.mark.asyncio
async def test_transcribe_posts_configured_non_streaming_form(mock_transport):
    result = await client.transcribe(valid_wav, asyncio.Event())
    assert result.text == "测试文本"
    request = mock_transport.requests[0]
    assert request.url.path == "/v1/audio/transcriptions"
    assert b'name="stream"' in request.content and b"false" in request.content
```

- [ ] **Step 2: Write failing WebSocket protocol and abort-isolation tests**

Use FastAPI TestClient with injected fake VAD/SGLang. Assert handshake-before-binary, malformed Float32 rejection, ready/state/transcript event ordering, one-session abort not cancelling a sibling, and disconnect waiting for fake worker barrier.

- [ ] **Step 3: Run tests and confirm RED**

Run: `.sglang/bin/python -m pytest tests/test_asr_sglang_client.py tests/test_asr_websocket.py -q`  
Expected: FAIL on missing client/service/router.

- [ ] **Step 4: Implement cancellable client and bounded service**

Use one `httpx.AsyncClient`, one configured semaphore, one frame queue and one utterance slot per session. Stream the upstream HTTP response context so task cancellation closes it. Never call SGLang abort-all.

- [ ] **Step 5: Wire backend lifespan and projections**

Initialize `app.state.asr_service` after config and before `yield`; shutdown it before LLM service shutdown. Add safe `/config.asr` and `/health.asr` projections. SGLang unavailability yields `degraded`, while invalid enabled local configuration remains startup-fatal.

- [ ] **Step 6: Run focused backend integration tests**

Run: `.sglang/bin/python -m pytest tests/test_asr_*.py tests/test_main_lifespan_wiring.py -q`  
Expected: PASS.

### Task 5: SGLang Launcher and Model Preflight

**Ownership:** Main session only.

**Files:**
- Create: `scripts/asr_preflight.py`
- Create: `scripts/2c_start_asr_sglang.sh`
- Test: `tests/test_asr_launcher.py`

**Interfaces:**
- Consumes: `load_asr_config` from Task 1.
- Produces: `validate_asr_model(config) -> ModelPreflightReport`
- Produces CLI: `python scripts/asr_preflight.py --config ... --shell`

- [ ] **Step 1: Write failing preflight and script-contract tests**

```python
def test_preflight_rejects_missing_index_shard(tmp_path):
    model = write_fake_qwen_asr_model(tmp_path, missing_shard=True)
    with pytest.raises(ModelPreflightError, match="model-00002"):
        validate_asr_model(config_for(model))

def test_launcher_uses_yaml_port_and_no_model_fallback():
    text = LAUNCHER.read_text()
    assert "asr_preflight.py" in text
    assert "--port" in text
    assert "MODEL_PATH:-" not in text
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `.sglang/bin/python -m pytest tests/test_asr_launcher.py -q`  
Expected: FAIL because launcher/preflight do not exist.

- [ ] **Step 3: Implement preflight**

Validate architecture, tokenizer/config/index, every unique shard, readable paths, registry support, `.sglang` executable, and free port. Serialize shell-safe assignments with Python, not YAML parsing duplicated in shell.

- [ ] **Step 4: Implement launcher**

Build an `ARGS` array containing model path, host, port 30003 from YAML, served name, dtype, memory fraction, max running requests, and trust flag. Reuse existing workspace helpers and Thor environment variables. Do not kill an occupied port.

- [ ] **Step 5: Run launcher tests**

Run: `.sglang/bin/python -m pytest tests/test_asr_launcher.py -q`  
Expected: PASS.

### Task 6: Frontend Voice State, Controller, Worklet, and Components

**Ownership:** Frontend coding agent. It may only create the shared files listed below and their tests; it must not edit `page.tsx`, `vision/page.tsx`, `api.ts`, or scripts.

**Files:**
- Create: `frontend/src/lib/voice-input-state.ts`
- Create: `frontend/src/lib/voice-input.ts`
- Create: `frontend/public/asr-pcm-worklet.js`
- Create: `frontend/src/components/VoiceModeToggle.tsx`
- Create: `frontend/src/components/VoiceInputPanel.tsx`
- Create: `frontend/src/lib/voice-input-state.test.mts`

**Interfaces:**
- Produces: `VoiceInputConfig`, `VoiceInputState`, `VoiceInputEvent`
- Produces: `voiceInputReducer(state, event) -> state`
- Produces: `getVoiceCapabilityError(environment) -> VoiceError | null`
- Produces: `buildAsrWebSocketUrl(config, location, backendPort) -> string`
- Produces: `useVoiceInput({config,isLoading,onFinalTranscript,onError})`
- Produces component props documented in exported TypeScript interfaces.

- [ ] **Step 1: Write failing reducer/capability/URL tests**

```typescript
test("LAN HTTP is rejected before requesting microphone", () => {
  assert.equal(getVoiceCapabilityError({isSecureContext:false, hasMediaDevices:true, hasAudioWorklet:true})?.code, "insecure_context");
});

test("explicit public wss URL wins", () => {
  assert.equal(buildAsrWebSocketUrl({publicWebSocketUrl:"wss://voice.test/v1/asr/stream", websocketPath:"/ignored"}, location, 8090), "wss://voice.test/v1/asr/stream");
});
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `node --test --experimental-strip-types src/lib/voice-input-state.test.mts` from `frontend/`  
Expected: FAIL on missing module.

- [ ] **Step 3: Implement pure state and stale-generation guards**

State phases are exactly `keyboard`, `requesting_permission`, `listening`, `candidate`, `recording`, `finalizing`, `transcribing`, `transcript_ready`, `paused_for_response`, and `error`. Reducer ignores events whose generation does not match.

- [ ] **Step 4: Implement AudioWorklet and hook**

Worklet aggregates render quanta to configured `frameSamples`, posts transferable Float32Array frames, and holds no growing history. Hook requests media only inside `activateVoice`, validates `AudioContext.sampleRate`, sends start handshake then binary frames, parses backend events, and stops tracks/context/socket on abort/visibility/unmount.

- [ ] **Step 5: Implement Layout A components**

Toggle uses two labeled 44 px targets and responsive wrapping. Panel draws a bounded CSS/SVG sample history, displays post-gain estimate/state, and renders read-only pending transcript. It never renders a textarea.

- [ ] **Step 6: Run frontend focused test and type-check**

Run: `node --test --experimental-strip-types src/lib/voice-input-state.test.mts && npm run type-check`  
Expected: PASS.

### Task 7: Frontend API Types and Text/Vision Integration

**Ownership:** Main session after frontend agent returns.

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`
- Modify: `frontend/src/app/globals.css`
- Create: `frontend/src/lib/voice-input-pages.test.mts`

**Interfaces:**
- Consumes shared frontend exports from Task 6 and backend `/config.asr` from Task 4.
- Produces page-local `submitUserMessage(explicitText?: string)` behavior shared by keyboard and voice callbacks.

- [ ] **Step 1: Write failing page synchronization/source-contract tests**

Assert both pages import `useVoiceInput`, `VoiceModeToggle`, and `VoiceInputPanel`; both pass final transcript through an explicit-text send entry; textarea is conditional on keyboard mode; Vision callback retains `images`.

- [ ] **Step 2: Extend ConfigResponse types**

```typescript
asr?: {
  enabled: boolean;
  sample_rate_hz: number;
  capture_frame_ms: number;
  input_gain_db: number;
  clip_limit: number;
  auto_send: boolean;
  streaming: boolean;
  websocket_path: string;
  public_websocket_url?: string | null;
  require_secure_context: boolean;
  browser_constraints: Record<string, boolean>;
};
```

- [ ] **Step 3: Refactor send functions to accept explicit text**

Use an optional function argument rather than `setInput` followed by stale callback execution. Keyboard sends remain behaviorally identical. Voice manual send reads pending transcript. Switching to Keyboard from transcript-ready copies text into `input`; switching during active phases aborts/discards.

- [ ] **Step 4: Add Layout A and conditional footer to both pages**

Place the segmented switch in each header. In Voice mode render `VoiceInputPanel` where textarea/SlashCommandMenu normally appears. Disable Voice during cycle execution; after auto-send pause until `isLoading` returns false, then resume only while visible and generation-valid.

- [ ] **Step 5: Run frontend tests, type-check, and build**

Run: `node --test --experimental-strip-types src/lib/voice-input-state.test.mts src/lib/voice-input-pages.test.mts && npm run type-check && npm run build:verify`  
Expected: PASS.

### Task 8: HTTPS Launch and Android Certificate Workflow

**Ownership:** Main session only.

**Files:**
- Create: `scripts/generate_asr_tls_cert.sh`
- Modify: `scripts/3_start_backend.sh`
- Modify: `scripts/4_start_frontend.sh`
- Extend test: `tests/test_asr_launcher.py`
- Modify: `README.md`

**Interfaces:**
- Consumes `browser_security` config from Task 1.
- Produces CA/cert/key at configured paths and TLS-enabled launch flags.

- [ ] **Step 1: Add failing script tests**

Assert HTTPS false leaves existing commands unchanged; HTTPS true validates CA/cert/key, adds Uvicorn `--ssl-keyfile/--ssl-certfile`, adds Next `--experimental-https-key/--experimental-https-cert/--experimental-https-ca`, sets `NODE_EXTRA_CA_CERTS`, and never sets `NODE_TLS_REJECT_UNAUTHORIZED=0`.

- [ ] **Step 2: Implement certificate generator**

Use OpenSSL to create a local CA and server certificate whose SAN extension is generated from YAML strings, classifying IP entries as `IP:` and names as `DNS:`. Set private key mode `0600`; never overwrite an existing CA without an explicit `--force` argument.

- [ ] **Step 3: Wire optional TLS into backend/frontend launchers**

Read ASR config through Python once. With HTTPS disabled preserve current behavior. With HTTPS enabled fail on missing files, start Uvicorn TLS, start Next HTTPS, use the CA for Node's server-side proxy, and print `https://<LAN-IP>:3000` plus Android CA path.

- [ ] **Step 4: Document Android steps**

Document generate cert, set `https_enabled: true`, start backend/frontend, install the CA on Android, revisit the HTTPS URL, click Voice, and grant microphone permission. State that HTTP text remains available but LAN HTTP voice is intentionally blocked.

- [ ] **Step 5: Run script tests**

Run: `.sglang/bin/python -m pytest tests/test_asr_launcher.py tests/test_start_backend_searxng.py tests/test_worktree_script_paths.py -q`  
Expected: PASS.

### Task 9: Dependency Installation and Integrated Verification

**Ownership:** Main session only.

**Files:** No manual source edits unless failures reveal defects.

- [ ] **Step 1: Install the pinned ASR dependency into `.sglang`**

Run: `uv pip install --python .sglang/bin/python "silero-vad==6.2.0"`  
Expected: package installed without replacing the project CUDA Torch/TorchAudio builds.

- [ ] **Step 2: Verify package/runtime identity**

Run: `.sglang/bin/python -c "import silero_vad, torch, torchaudio, onnxruntime; print(silero_vad.__version__, torch.__version__, torchaudio.__version__, onnxruntime.__version__)"`  
Expected: Silero `6.2.0`; existing CUDA-compatible Torch/TorchAudio remain importable; ONNX CPU provider remains available.

- [ ] **Step 3: Run all focused backend tests**

Run: `.sglang/bin/python -m pytest tests/test_asr_*.py tests/test_main_lifespan_wiring.py -q`  
Expected: PASS.

- [ ] **Step 4: Run frontend focused tests and build**

Run: `node --test --experimental-strip-types src/lib/voice-input-state.test.mts src/lib/voice-input-pages.test.mts && npm run type-check && npm run build:verify` from `frontend/`  
Expected: PASS.

- [ ] **Step 5: Run broad regression suites**

Run: `.sglang/bin/python -m pytest tests -q`  
Expected: PASS, excluding only explicitly documented hardware/network tests already skipped by the repository.

- [ ] **Step 6: Run model preflight without launching the GPU server**

Run: `.sglang/bin/python scripts/asr_preflight.py --config config/config-asr.yaml --check-only`  
Expected: model files complete, architecture supported, port 30003 free or clearly reported occupied.

- [ ] **Step 7: Run real SGLang short-WAV smoke test when resources permit**

Start `scripts/2c_start_asr_sglang.sh`, then send a known short speech WAV to `/v1/audio/transcriptions`. If the main GPU model is occupying memory and cannot safely coexist, do not kill it; report this hardware verification as blocked with exact evidence.

### Task 10: Independent Audit, Fixes, and Final Evidence

**Ownership:** Auditor subagent is read-only; main session fixes confirmed findings.

**Files:** Any ASR file only when a finding is verified.

- [ ] **Step 1: Dispatch auditor with explicit boundary checklist**

Audit config hardcoding, model/provider boundary, ring ordering, gain-once semantics, VAD/end logic, session-local abort and worker barriers, queue bounds, privacy, Prefix-Cache Covenant, Android secure-context/TLS, Text/Vision synchronization, and test gaps. Auditor must distinguish real bugs from scope suggestions.

- [ ] **Step 2: Reproduce each actionable finding**

Add a failing regression test before changing implementation. Reject speculative findings that cannot be tied to code behavior or the approved spec.

- [ ] **Step 3: Implement minimal fixes and rerun focused suites**

Run the exact test that reproduces each finding, then the backend/frontend focused suites.

- [ ] **Step 4: Run final full verification**

Run backend ASR + full pytest, frontend Node tests + type-check + build, shell syntax checks for changed scripts, and model preflight. Record commands and fresh outputs in the completion response.

- [ ] **Step 5: Report residual hardware steps honestly**

List Android CA installation/permission and live microphone/GPU smoke results separately from automated passing tests. Never claim those passed if no physical device/server run occurred.
