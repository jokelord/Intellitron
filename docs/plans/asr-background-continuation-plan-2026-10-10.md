# ASR background continuation: specification and implementation plan

## Problem and required behavior

Speech/dictation must continue after an utterance or model response while Firefox is unfocused, minimized, or on a background tab. The current controller closes its microphone, AudioContext, and socket after every final transcript, then calls `getUserMedia()` again. The browser may defer a new permission/capture request until the document has system focus. Removing the page visibility gate alone therefore does not fulfill background continuity.

Initial activation remains an explicit user action with normal browser permission checks. Automatic continuation must reuse that activation's live microphone and audio graph; it must not invoke `getUserMedia()` or reopen an AudioContext. During transcription or model-response waits, the worklet and PCM forwarding are paused. Keyboard, explicit deactivate/Abort, page unmount, microphone ending, and unrecoverable transport/setup errors still perform full resource-local teardown. Explicit mode selection/retry retains its fresh-start behavior.

Retain generation isolation, bounded transport, input gain, replay ownership, automatic/manual speech behavior, empty transcript handling, dictation fallback, magic-send guards, and Text/Vision parity. Loading, pending-acceptance, and magic-send conditions still govern automatic resume. No model messages, prefix-cache policy, Agent semantics, Vision Automation lifecycle, dependencies, security settings, or browser/system audio preferences change.

## Proposed lifecycle

Keep one ASR socket with its user-activated microphone/graph across successful utterances and subsequent response pauses. Add a distinct `resumeVoice()` controller operation for the pages' automatic continuation paths; explicit `activateVoice()` continues to replace the activation. A same-generation reducer resume event restores listening and clears completed display text without touching the already handed-off replay attachment.

A manual model submit during an unfinished utterance, including transcription in flight, retires only the ASR socket using its existing targeted abort barrier. Reconnect that transport before continuing on the retained graph. This discards partial audio and late transcripts without requiring a new microphone request. Repeated resume requests cannot create duplicate sockets, and full teardown waits for any retiring transport under the shared cleanup deadline.

The backend currently sends a final transcript before its utterance reset and before `_inference_task` is cleared. Receiving the transcript alone is therefore insufficient permission to forward new PCM. After reset and inference-worker retirement, send the existing `state_changed` event with `state: buffering`. No event type or schema is added. The controller records this readiness, and a requested automatic resume only unpauses once readiness and page request guards allow it. Late reset/level events cannot overwrite a deliberate response pause. Recoverable error continuation also respects this readiness barrier.

Each asynchronous readiness/reconnect callback rechecks the latest loading, pending-acceptance, and magic-send guards. An immediate resource-local PCM gate blocks queued frames during pauses; internal worklet frame, acknowledgement, and overflow messages carry a capture epoch so stale queued messages cannot affect resumed capture. The WebSocket binary PCM payload remains unchanged. Retiring inference workers remain tracked and cancellable while sending readiness.

## Implementation and validation

1. Add event-driven regressions exercising the actual voice controller with controlled native audio/socket boundaries: multiple hidden utterances use one permission request/graph/socket; model wait forwards no PCM; readiness must precede resume; explicit shutdown invalidates stale events; manual transcript/replay and retry remain valid. Retain existing cleanup regressions.
2. Implement retained resources and same-generation resume, updating both page automatic paths. Keep explicit fresh activation and full teardown paths intact.
3. Add backend readiness/two-utterance regressions, then emit the existing buffering event only after reset and worker retirement. Verify recoverable errors and targeted teardown.
4. Run focused controller/backend tests, frontend type checking, and relevant broader frontend suites. Report existing unrelated source-contract failures separately. Perform a fresh user-activated browser test with two phrases while Firefox remains minimized/unfocused, including model response pause and recovery.

Runtime validation completed: the user confirmed two Dictation phrases and continued capture while Firefox Vision was minimized, then confirmed unfocused Speech across multiple turns and approximately 90 seconds of idle with normal waveform/UI. Native observations retained one microphone and socket across each test's utterances/responses. The prior one-hour X11 idle baseline was not repeated, and the longstanding Ubuntu output-routing issue remains separate. The backend was restarted once to load the fix; no dependency installation was required.
