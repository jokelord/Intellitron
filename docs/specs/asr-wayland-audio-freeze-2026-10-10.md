# ASR UI freeze on Ubuntu Wayland — 2026-10-10

## Observations

- The user previously observed approximately one hour of idle ASR without issues on X11. Under Ubuntu Wayland, the waveform stalled and the whole page became sluggish in both speech and dictation modes.
- Restarting frontend/backend did not restore normal behavior. Restarting PipeWire/WirePlumber helped for only approximately 2–3 seconds.
- A fully restarted Firefox 155 reproduced the freeze after approximately 40–50 seconds of idle ASR on the Vision page.
- During the reported freeze, audio transport continued at approximately 62 kB/s (expected approximately 64 kB/s for mono float32 16 kHz), ASR reported one active session and zero queued requests, and `pw-top` showed no growing xruns.
- The page opened two Firefox output streams: a 48 kHz capture-related stream and the 16 kHz ASR silent AudioContext output. Both initially used the NVIDIA HDMI/DP sink.
- Moving both page output streams to the analog stereo sink produced approximately 90 seconds of normal idle behavior, confirmed by the user. New ASR contexts also appeared on analog despite the system default remaining HDMI, consistent with per-stream routing restoration.
- Reverse test, moving both streams back to HDMI/DP: approximately 90 seconds also remained normal. Output routing is therefore **inconclusive as a cause**; both streams were restored to the original HDMI/DP output, as the user prefers.
- The user identified background/unfocused Firefox as a trigger: capture stopped after a dictation transcription while the browser was unfocused, and reactivating capture restored it. Removing the visibility gate alone still failed a minimized two-utterance test.
- The user's output-routing/reset workaround predates ASR: Ubuntu sometimes switches to built-in analog output and HDMI cannot be selected. That longstanding system-routing problem is distinct from the newly identified ASR continuation bug.

The initial analog trial does not establish a Firefox, Wayland, NVIDIA driver, or PipeWire defect. Neither routing trial repeats the previous one-hour idle test. No persistent analog-output mitigation is recommended.

## Background continuation fix

Both Text and Vision pages required `pageVisible` before restarting voice capture from `paused_for_response`. The visibility helper reads `document.visibilityState`, rather than window focus. After a transcription/response while hidden, this gate prevented automatic continuation, contradicting intentional background speech/dictation support.

The gate is removed on both pages. Loading/model-busy, pending-acceptance, and dictation magic-send protections remain. Vision Automation's lifecycle is unchanged. A behavioral regression showed the old Vision helper rejected an otherwise eligible hidden-page resume; it now permits resume and still rejects each protected state.

The controller also closed the microphone, AudioContext, and socket after each final transcript, then called `getUserMedia()` again. The [Media Capture specification](https://www.w3.org/TR/mediacapture-streams/) requires a capture request to wait for system focus. This explains why removing the visibility gate alone could not preserve background continuation.

Automatic continuation now retains the user-activated microphone/audio graph and normally the same ASR socket. The worklet and PCM forwarding pause during transcription/model waits, then resume only after backend reset readiness and current page guards permit it. A manual model submit during an unfinished recording/transcription retires and replaces only the ASR transport, discarding partial audio and late transcripts while keeping the microphone. Explicit Keyboard/Abort/retry, unmount, ended tracks, and unrecoverable errors retain full teardown semantics. Internal worklet epochs reject stale frames, acknowledgements, and overflow notifications across pauses.

Behavioral tests reproduce a browser that defers a background microphone request, and verify two utterances use one capture request/graph/socket. The user confirmed that fresh Firefox Vision Dictation recognized both phrases and continued while minimized. Native observation retained the same ASR socket (port 48574) and microphone source (666) through multiple transcriptions at 10:56:45, 10:56:57, and 10:57:12. These confirmed application defects do not establish that the original whole-UI stall has the same cause.

## Application cleanup fix

The shared Text/Vision voice controller previously discarded the `AudioContext.close()` promise, allowing replacement graph setup before old native teardown finished. It now waits for native audio teardown and socket cleanup concurrently under the existing cleanup deadline. Permission acquisition retains its synchronous invocation, and failed/hung native close remains recoverable.

Five behavior regressions failed before this fix; all ten cleanup tests now pass, including a previously retired transport. The fix improves lifecycle correctness and recovery, but the freeze reproduced after it, so it is not established as the freeze's cause. WebSocket PCM/control/event schemas remain unchanged: backend reset readiness uses the existing buffering state event, and epochs exist only in internal worklet messages. Model prompts, prefix-cache policy, and dependency sources are unchanged.

## Runtime routing and recovery

The diagnostic routing change was reversed. The system default and both Firefox page streams now target the original NVIDIA HDMI/DP sink. To inspect current routing without changing it:

```bash
pactl list short sinks
pactl list short sink-inputs
pactl list sink-inputs
pactl get-default-sink
```

Stream IDs are temporary. The HDMI/DP sink observed here is `alsa_output.platform-88090b0000.hda.HiFi__hw_HDA_3__sink`; the analog trial used `alsa_output.platform-sound.analog-stereo`. Per-stream routing restoration was observed during the trial, but is not evidence of an ASR fault.

The user can currently switch to Keyboard and re-enable voice input to recover capture. Recheck the output target afterward if needed. Do not assume frontend/backend restarts repair a separate OS audio-routing issue.

## Verification limits

TypeScript checking passes. The final focused controller/worklet/cleanup suite passes 73/73 tests. Runtime ASR backend suites pass 164/164 tests, including two-utterance readiness, blocked-send shutdown, and retiring-worker cleanup. The broader frontend library plus Vision Automation run passed 792/798 before two additional passing cleanup tests; its six existing source-contract failures concern unrelated recovery/timeline assertions (`timeline-projection.test.mts`:458,494,608,665; retained-session and local-transcript cases in `voice-input-pages.test.mts`). The full ASR run passed 177/199; all 22 failures are existing launcher/documentation contracts for obsolete script paths or a README dependency-version expectation.

The backend was restarted once through `scripts/4_start_backend.sh` with its original launcher environment; the user subsequently relaunched it (current observed uvicorn PID 231817, port 8090). Its ASR health remains ready with one active session and zero queued utterances during testing; the inference provider and HDMI default remain unchanged. Independent verification passed all 44 WebSocket tests, the 73 focused frontend tests, and TypeScript checking. Final review found no remaining findings.

Minimized Vision Dictation is user-confirmed. The user also confirmed unfocused Speech across multiple turns and idle: “测试并观察多轮，idle 后一切正常。” The same Speech connection (port 58604) and microphone source (691) remained active for over 90 seconds, streaming approximately 64 kB/s through transcriptions and model completions; waveform and UI remained normal during this test. The previous one-hour X11 idle baseline has not been repeated. The longstanding Ubuntu output-routing issue remains distinct, and no OS-level fix is claimed. No git command or dependency modification was performed. The diagnostic output routing was restored to HDMI; no persistent audio preference change is part of the fix.
