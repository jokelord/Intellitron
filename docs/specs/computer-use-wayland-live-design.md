# Wayland live computer-use design

Status: approved for implementation and verification by the user. This document does not assert that live desktop capture or input has been verified.

## Problem and scope

The current default `desktop_disabled` result correctly enforces the session capability. Enabling it alone exposes further missing implementation: combined Portal authorization, PipeWire capture, EIS input, frame/event IPC and production Python composition. Development packages, Portal property discovery, helper compilation and fixture tests cannot establish live readiness.

Implement one selected full monitor on GNOME Wayland using GDBus, PipeWire and libei. Keep X11 explicitly unavailable. Preserve the current same-Agent `observe → inspect_region → interact_detail` protocol, six actions, trusted attachments and historical-image acceptance rules. Keep `allow_desktop_capture`, `allow_desktop_input` and `computer_use.enabled` false by default; do not invent calibration values. An explicitly configured isolated Agent session is the proposed integration target.

## Measured baseline

The current audit reported 118 Python tests passed with two skipped; native CTest reported two passed and one skipped because the sandbox denied `SO_PASSCRED`. Development packages were present. The sandbox Portal probe returned `SESSION_BUS_UNAVAILABLE`, which does not establish host-session availability. These results verify available fixture/diagnostic gates only; authenticated host IPC and all live desktop gates remain unverified.

## Ownership and lifecycle

A configured application-level backend registry lazily creates a runtime for an authorized top-level Agent context on its first observe. The runtime owns the native helper, binding, renderer, frame store, dispatcher and calibration evidence. Identity includes owner session, context nonce, binding id and helper generation. Child/sibling contexts cannot reuse it. Desktop authority is checked at each operation, including immediately before input.

Use the existing fixed-helper `CommandPolicy.evaluate_desktop_helper`, `EnvironmentBuilder.build_argv` and `ProcessSupervisor.start_managed` hooks. Preserve the minimal environment: the builder already supports session D-Bus, Wayland and runtime-directory variables; do not inherit loader overrides or the entire server environment. A missing graphical-session environment is an availability error, not evidence that the host lacks a Portal.

Application startup installs the configured registry without opening authorization dialogs. Session disposal, context replacement, capability revocation, helper death and application shutdown close runtime resources idempotently. Stop cancels current work and prevents late input; after any uncertain event delivery, require a new observation and intent. Do not attach a persistent helper to a stale per-turn abort callback. Replace the current backend shutdown behavior, which only clears dictionaries, with explicit native resource disposal. Use a shared seat arbitration mechanism across runtimes; the current per-Dispatcher lock alone does not serialize a physical seat across sessions, and coroutine locks must not be reused across the adapter's separate event loops.

## Native transports

### Portal

The GDBus state machine performs:

`RemoteDesktop.CreateSession → RemoteDesktop.SelectDevices(POINTER) → ScreenCast.SelectSources(MONITOR, multiple=false, same session) → RemoteDesktop.Start → ScreenCast.OpenPipeWireRemote → RemoteDesktop.ConnectToEIS`.

This combined POINTER flow is admitted only when both capture and input authority are present. Capture-only authority must never request POINTER: either provide a ScreenCast-only observation lifecycle with input disabled or explicitly reject the action-oriented public workflow before authorization. The current fixed-helper launch policy checks capture authority only, so input must be negotiated and checked separately.

Subscribe to each Request response before dispatch to avoid response races. Validate response code, session handle, granted devices and exactly one monitor stream. Distinguish method completion from user authorization. Bound each request, close pending requests on cancellation and invalidate the session on `Session.Closed` or Portal owner replacement. Late callbacks must carry a generation guard. ConnectToEIS occurs only after successful Start and once per session; never mix EIS with NotifyPointer methods.

Retain node id and optional stream serial, mapping id, logical metadata and source evidence. ScreenCast mapping id and PipeWire serial are version-dependent optional metadata (introduced in versions 5 and 6 respectively); do not demand an unavailable property while claiming generic Portal support. If the available metadata cannot prove correspondence to one EIS region, reject input instead of guessing.

### Capture

Connect the Portal-provided fd through `pw_context_connect_fd`, then create a stream against that core using `pw_stream_new`. `pw_stream_new_simple` must not silently connect to the unrestricted/default core. Prefer stream serial when supplied, with node identity scoped to the live Portal/core generation.

Negotiate explicitly supported CPU-readable packed formats and validate buffer count, planes, offsets, stride, dimensions, chunk bounds and memory type. Reject unsupported DMA-BUF/modifier/transform combinations; successful capture requires demonstrated full-monitor coverage. Copy into bounded packed RGB storage compatible with `CanonicalFrame`; requeue PipeWire buffers on every path. Seal a memfd against writes and resizing before transferring it.

Metadata includes pixel format/dimensions/stride/byte count, content hash, frame sequence, capture monotonic timestamp, stream generation, geometry version and coverage/transform evidence. Do not refresh a timestamp merely because an old cached buffer was requested again. Geometry, format, monitor, rotation or scale changes invalidate previous bindings and crops. Unsupported changes fail closed until a new valid binding is established.

### Input

Create a libei sender from the Portal EIS fd and service its event loop concurrently with capture/control. Bind the permitted seat capabilities, wait for an enabled/resumed absolute-pointer device, validate button/scroll support and prove the selected region maps to the selected stream. The EIS region is authoritative for event coordinates; Portal position/size must not be assumed numerically identical. Convert normalized monitor coordinates into the proven EIS region, with explicit endpoint semantics.

Retain the existing dispatch transaction and at-most-once ledger. Native event results describe local validation, submitted event index and cleanup state. libei event APIs do not provide proof that a target application received a click. A disconnect after possible submission is `ACTION_DELIVERY_UNKNOWN`; never retry automatically. A successful transport result is not physical-click verification. Ensure button cleanup is bounded and any uncertain release remains visible as uncertainty. Link the native target against libei in CMake.

## IPC and Python composition

Retain authenticated SOCK_SEQPACKET envelopes, strict fields, nonce, generation, monotonic sequence, bounded packet size and sender credentials. Define typed payload schemas for authorization/start, status/binding resolution, freeze, individual dispatch events and close. Separate authorization timeout from short transport timeout. Freeze returns either a frame with exactly one fd or a structured error with none; receive logic must safely admit these alternatives without weakening frame-fd checks. Reject duplicate/unknown fields, nonfinite numbers, unexpected descriptors and stale identities; close all received fds on failure.

`NativeClient.start` establishes the authorized native binding instead of unconditionally rejecting its handshake. `resolve_binding` returns validated current readiness and geometry. `freeze` validates seals, file size, bounded arithmetic, format, stride, hash and identity before constructing immutable RGB `CanonicalFrame`; always close mappings/fds. `send_event` preserves precise delivery uncertainty. Expose cancellation and deadline handling rather than allowing Portal authorization to block indefinitely.

Public `inspect_region` and `reobserve` are already Python grounding operations. Native only supplies frames and input: do not duplicate crop construction or trusted crop identities in C. `reobserve` obtains a new native sample through the existing Python observe flow.

The missing production composition also includes:

- A renderer adapter implementing `overview`, `crop` and `detail` from the existing render/coordinate/protocol-label functions. Bind detail artifacts to the original immutable frame; populate all trusted `DetailCrop` identity, calibration and geometry fields.
- Fresh pre-dispatch capture and binding resolution. The existing dispatcher compares a stored frame to itself and therefore misses real desktop changes. Compare the observed original against a newly sampled current frame, require liveness and bounded ages, and compute both global and target-crop differences. The configured target threshold must actually participate. Preserve the validation-to-dispatch deadline and recheck geometry before event submission.
- Post-action recovery and new-intent lifecycle. `_interact` currently drops backend state after `ACTION_SENT`, while the executable bridge only allows `reobserve` from its terminal phase; `observe` is rejected and `reobserve` has no state. Preserve enough state for required recovery and issue a fresh trusted request/ledger identity for each new intent. An uncertain action remains terminal in its old ledger; reobservation must not silently replay it. Test successful-action continuation and uncertainty recovery through the full bridge/backend path.
- Calibration report loading and verification against a current deployment-derived fingerprint. A nonempty configured string is insufficient. `ComputerUseConfig.ready` currently requires six numeric thresholds plus one fingerprint, not seven numeric thresholds. The separate calibration evaluator's axis-error/pass-rate limits also require deployment evidence.

Portal authorization, native capture readiness, native input readiness and calibration readiness remain separate. Installation of a configured factory is not a readiness assertion; operation admission requires the appropriate complete evidence. Missing renderer, freshness or calibration wiring must produce a clear unavailable reason.

## Bounded work packages and acceptance

| Package | Deliverable | Automated evidence |
|---|---|---|
| Portal lifecycle | Combined GDBus state machine and owned fds | Fake Portal responses, denial, timeout, close, owner change and late-callback rejection |
| Capture and input | Portal-core capture and mapped libei sender | Buffer validation/conversion, region matching, device pause/removal, event uncertainty, geometry invalidation |
| IPC client | Typed frame/event exchange and cleanup | Real helper socket tests, malformed metadata, seals/hash/bounds, unexpected fds, death and descriptor leaks |
| Python composition | Lazy per-context runtime, renderer, freshness and teardown | Same-Agent executable path, changed-screen/target detection, cancellation, cross-context denial, seat serialization |
| Calibration and operations | Report/fingerprint validation and evidence-based doctor | Missing/changed report rejection; diagnostics cannot promote fixture success to live readiness |

A read-only baseline reproduction with a fake native that would return a changed white frame yielded `ACTION_SENT`, zero new-frame requests and four emitted events. This demonstrates the pre-dispatch self-comparison defect independently of live transport availability.

Run existing Python and native regression suites plus the new focused gates. Preserve unchanged prompt history, tool observations and computer-use omission acceptance behavior. Sandbox skips remain unverified gates.

Real desktop acceptance requires the actual Jetson Thor graphical login and interactive Portal authorization: capture a known monitor, verify pixel/region mapping, run the same Agent through overview/detail and a controlled visible click/scroll target, and record target application outcomes. Cover scale/resolution changes, monitor hotplug, Portal revoke/restart, stream recreation, EIS loss during input, Stop and helper crash. No inference from `ACTION_SENT` alone is sufficient. Calibration uses repeated samples on the actual model/protocol/renderer/grid/preprocessing deployment; persist failed samples and measured errors without logging private desktop content.

Doctor reports separate package discovery, Portal interface support, live frame evidence, live input acceptance evidence and calibration evidence, each with performed/not-performed status. Until all required real desktop gates pass, report the backend as implemented-but-unverified or unavailable as appropriate; never claim the live MVP is complete.
