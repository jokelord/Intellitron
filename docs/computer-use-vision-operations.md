# Computer-use vision operations

For the current blocking conditions, failure-code meanings, Wayland/Portal/uinput
distinction, and the ordered troubleshooting plan, see
[`computer-use-vision-troubleshooting.md`](computer-use-vision-troubleshooting.md).

The capability remains disabled by default. Wayland Portal/PipeWire/EIS, sealed-frame IPC and the lazy per-Agent Python runtime are implemented. On the Jetson graphical login, Portal authorization, a live 3840×2400 full-monitor frame, and its 1920×1200 EIS logical mapping have been verified. The isolated full-screen test window received a left-button press/release and wheel event in three consecutive post-fix probes. On 2026-09-28, a separate windowed 2560×1440 controlled model-to-click probe verified a Stage-1 crop containing its Tk target, a Stage-2 model point inside the safe inset, a fresh pre-dispatch frame, and one in-target Tk press and release with no outside press. Model calibration, actual Agent/archery target interaction, and the full fault/geometry acceptance matrix remain **unverified**; production `ready` is false. Native X11 uses XGetImage capture, XRandR monitor binding and XTest input; its evidence is separate from these historical Wayland results. On 2026-10-09, the production NativeClient probes on a private 640×480 Xvfb desktop verified capture plus exactly one Tk button press/release and one wheel callback. This is isolated X11 transport/application evidence, not real-user-desktop or model-calibration acceptance. Compilation, fixtures, discovery and `ACTION_SENT` do not establish successful physical interaction.

The current renderer stretches the complete native monitor overview along X and Y
into a square of side configured `S=computer_use.model_square_side_px` (currently 1120), draws the eight-division grid, then
encodes a lossless PNG. This trusted computer-use observation bypasses the Agent's
ordinary image downscaling and JPEG conversion. The exact clipped Stage-2 crop
is independently stretched to a square `S×S` model image. Tool coordinates use
independent 0–1000 baselines for x and y. A Stage-1 model pixel `(px,py)` maps
to `(px*1000/S, py*1000/S)` on the model square; the backend then uses
the untouched source width and height separately for source coordinates. Stage-2
clips without padding before stretching. A centered 2× crop on a 3840×2400
monitor is 1920×1200 physical pixels; clipping at an edge may shorten either
axis. The Agent may select zoom 2, 4, 6 or 8; omission uses 4. `computer_use.stage1_zoom` remains only an offline fixture choice, not a live restriction. Here 2× sets the nominal crop field of view (`500/1000`
per axis), not a model-error margin or a uniform raster enlargement. Each
Stage-2 axis has 16 equal grid cells. Only even interior guides carry
labels `125,250,375,500,625,750,875`, prefixed with x= or y=.
Stage 1 retains its eight-division lines and border without text labels.
Decoding uses the exact integer source crop width and height, not a square source
rectangle. See the [v2.0 specification](Pure-Vision-Two-Stage-Computer-Use-Grounding-Skill_v2.0.md).

The repository includes the Linux ARM64 helper at
`bin/linux-aarch64/computer-use-helper`. See [binary packaging](../bin/README.md)
for runtime compatibility, permissions, checksums, and rebuild commands.
To rebuild and explicitly refresh the helper (no desktop capture/input):

```bash
cmake -S backend/computer_use/native -B build/computer-use-native \
  -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF
cmake --build build/computer-use-native --target computer-use-helper -j4
cmake --install build/computer-use-native --prefix "$PWD" \
  --component computer-use-helper
```

The active `computer_use.helper_binary` is
`bin/linux-aarch64/computer-use-helper`, resolved against the repository root.
Absolute YAML overrides remain supported; relative native paths do not depend
on the backend's working directory.
The native build is separate from optional evidence under `logs/computer-use/`.
Deleting the build directory or evidence leaves the distributed executable
intact. Evidence/fixture examples below must be regenerated if their artifacts
are absent, and historical results do not create runtime prerequisites. Missing or
non-executable helpers return `HELPER_NOT_BUILT` or `HELPER_NOT_EXECUTABLE`
before helper launch; the runtime does not automatically rebuild or select a
fallback. Native tests are optional and require a separate explicitly enabled
`BUILD_TESTING=ON` build. Rebuilding or installing a helper does not authorize
or verify desktop capture/input.

Required development components are GLib/GIO, JSON-GLib, PipeWire/SPA and libei, plus CMake/Ninja; current CMake also discovers X11 libraries. Package presence does not prove live capability. Sandbox `SESSION_BUS_UNAVAILABLE` says nothing about the host Portal; a `SO_PASSCRED` skip leaves authenticated IPC unverified until a host rerun. Doctor reports unperformed Portal, frame, input-outcome and calibration checks as `not_run`.

Configure `computer_use.helper_binary` and `calibration_report_path` with repository-relative or absolute paths; the YAML loader supplies absolute runtime paths. Keep defaults off until deployment measurements are available. The combined Portal workflow requires capture and input authority on the same top-level Agent context; capture-only receives `INPUT_AUTHORITY_REQUIRED` before POINTER authorization. Startup installs a lazy factory without opening a dialog. Helpers use the existing fixed-command policy, minimal environment and process supervisor.

Readiness needs six measured numeric safety values (observation age, sample age, validation-to-first-event duration, global difference, target difference, double-click interval), plus a fingerprint. Do not fill arbitrary numbers. Authorization and transport deadlines are separate configured values.

Compute current deployment identity:

```bash
./.sglang/bin/python -m scripts.calibrate_computer_use
```

For first calibration, generate known-target images without enabling desktop access:

```bash
./.sglang/bin/python -m scripts.calibrate_computer_use \
  --generate-fixtures
```

This exports landscape, portrait and edge-target overview/detail PNGs through the
actual production renderer, source PNGs, stage prompts/schemas and an empty
`samples.json`.
The Stage-1 model image contains the complete screenshot, stretched along
both axes to configured S×S before its translucent eight-division lines and
border are drawn, without text labels. The exact clipped crop stretches to
S×S before Stage-2's sixteen-division grid and labels. Source/crop geometry
and independent normalized 0–1000 coordinates remain unchanged.
Even Stage-2 guides carry values 125,250,...,875. `edge` puts these labels at
guide ends. Active `repeated` repeats each X value at Y positions
31.25,156.25,281.25,406.25,531.25,656.25,781.25,906.25; each Y value uses the same series for its X centers. There are
eight copies per value: 56 X and 56 Y labels. X text
is CCW90 just right of its vertical guide and vertically centered on each Y anchor;
Y text is horizontal, centered on each X anchor and just below its guide.
Odd guides do not carry their own coordinate values.
Black outlines and white
glyphs use 0.5 opacity on transparent overlays, with no image padding.
Generated
images and reports stay under the repository's
`logs/computer-use/v2.0/` directory.
Send each PNG with its matching prompt using the computer-use model adapter or
the controlled live probe, preserving the exact native Stage-1 PNG. Ordinary
Vision Agent upload follows a different YAML image-size policy and does not
establish computer-use calibration equivalence. Repeat each case at least
twice; keep production desktop capabilities off.
Do not send the answer-bearing `samples.json` target fields to the model. Record
actual outputs, never copy ground truth into outputs. These fixed known crops
calibrate coordinate interpretation/rendering; the live two-stage choice of crop
still needs separate end-to-end acceptance. Stage-1 is a rough locator: judge
it by whether its crop fully contains the target, not by final-point error.
Stage-2 is the precise grid-referenced point. Do not treat a Stage-1 point-error
score as the two-stage success rate.

For a paired offline comparison, add `--compare-grid` to the two-stage probe.
It uses one Stage-1 decision and the same source crop per case, sends both
gridded and gridless square details, and saves each exact sent PNG, SHA-256,
prompt, point overlay and score under
`logs/computer-use/v2.0/annotated/stage2/compare-grid/`. The precomputed
`case-*-stage2.png` fixtures are references, not these actual model inputs.

For an offline diagnostic of both model stages with the current v2.0 fingerprint,
run the two-stage probe against freshly generated fixtures:

```bash
./.sglang/bin/python -m scripts.probe_computer_use_two_stage \
  --infer-stage1 --infer-stage2
```

The probe rebuilds Stage-2 images from those model-selected rough points using
the production renderer, submits the unmarked grid crops to local SGLang, and
writes a JSON report plus marked Stage-1 and Stage-2 contact sheets under
`logs/computer-use/v2.0/annotated/`. Red marks the model point, cyan
the known target, and yellow the Stage-1 crop. Old Stage-1 results have a
different fingerprint and cannot be reused as v2.0 measurements. This is an
offline synthetic probe: it never
opens a desktop Portal session or sends input, and it is not a production
calibration report. It uses `model_adapter.encode_stage()` prompts and a private
SGLang request; the live Agent instead sees this skill's instructions and
attached observations in its own history. Even when their coordinate rules
match, probe hit rates do not establish live-Agent performance. The scorer
below measures final-point error on decoded
ratios; its Stage-1 fixture rows must not be used as final-point acceptance.

The manifest's `normalization` maps an output coordinate into normalized monitor
ratios: for relative extent, `offset + span * coordinate / wire_max`; for
index-center semantics, `offset + span * (coordinate + 0.5) / (wire_max + 1)`.
Reject out-of-range/invalid model outputs as failures. Store converted ratios in
`outputs`; fill `desktop_geometry` only from verified deployment monitor/full-frame
measurements. Offline fixtures alone do not establish desktop geometry or input
readiness. Ordinary upload preprocessing must match production computer-use
attachments; if those pipelines differ, the resulting measurements are not valid
for this deployment. After verifying the effective model-visible image retains
the whole square and both relative axes, the operator can set
`serving_preprocessing: {"transform":"identity","verified":true}` in the
sample manifest. The fixture generator leaves this field null, and runtime
rejects missing or unknown transformation evidence before creating a native
helper. Failed scores are written with null invalid errors and
`passed:false` so evidence remains inspectable.

Collect repeated outputs from the actual same-Agent model using the production renderer, grid, decoder and serving preprocessing. Save JSON with `fingerprint`, `desktop_geometry`, and `cases`. Each case has unique `case_id`, positive `width`/`height`, known `target_ratio: [x,y]`, and at least two measured `outputs: [[x,y], ...]`. Ratios are normalized monitor coordinates after decoding, not raw detail coordinates. Use at least two distinct targets; include edges and non-square cases in real acceptance. `desktop_geometry` has `backend: "wayland"` or `backend: "x11"`, `logical_origin: [x,y]`, `logical_size: [w,h]` from the EIS region or selected XRandR monitor and `pixel_size: [w,h]` from the full-monitor frame.

Score with `python -m scripts.calibrate_computer_use --samples <absolute-json> --output <absolute-report> --max-axis-error <chosen-limit> --min-pass-rate <chosen-rate>`. This scorer does not generate model outputs, capture screenshots, grant authority or inject input. `max_axis_error` is a monitor-normalized center-point error limit, not a target-hit tolerance: it does not know each target's bounding box, clickable margin or physical size. The crop magnification does not justify loosening this global threshold; square resizing changes the image's axis scale but cannot prove that the output point falls inside a real target. Target-size-aware hit evidence requires measured target bounds and final-point hit outcomes, stratified by size. Current live operations do not carry trusted target bounds, so offline size-binned measurements cannot authorize clicks on unverified live target sizes. Calibration bootstrapping remains operator-led measurement, not a production readiness bypass. The runtime validates repeated/diverse measurement structure and recomputes errors/rate, checks the current fingerprint on operations and verifies selected-monitor logical/pixel geometry after authorization.

The fingerprint covers effective local model/vision/inference/provider configuration, renderer/protocol code, installed processor/tokenizer versions, model metadata hashes and weight file identities. Weight identities currently use path/size/mtime rather than full weight-content hashes. It cannot independently attest the running remote/container inference revision or compositor build. Verify those actual deployment versions and recollect calibration after changes; do not present local fingerprint matching as remote runtime attestation.

The Agent does not receive a `computer_use` tool. The public skill invokes the
allowlisted `vision_desktop` executable through restricted `exec_command`
(`backend/skills/computer-use-vision/bin/vision_desktop`). Direct shell
execution of that marker returns `BACKEND_BRIDGE_REQUIRED`; only the
session-bound backend intercept may perform a live operation.
The existing public `exec_command(command: str)` shape is retained, but a
command whose first token is `vision_desktop` is parsed by a fixed non-shell
argv parser; shell syntax and arbitrary paths are rejected.
Read-only observation/inspection requests never require physical input.
For an interaction requested by the user, the first operation is `observe`; the same Agent then emits rough
Stage-1 coordinates for a configured `inspect_region`, receives the ticked detail image,
and emits precise Stage-2 coordinates for `interact_detail`. For a user-requested interaction, when its point is identifiable in the detail, the next
desktop call must be `interact_detail`; no additional centering, margins or full
outer-object visibility are needed. Only if that point itself cannot be located
does the live Agent call `observe`, retaining the target in context, BEFORE another
`inspect_region`; the Agent then chooses a new rough point and requests another
chosen-zoom detail. Live refreshes have no count ceiling; accepted refreshes renew
the per-overview detail-inspection budget (default three). The runtime
attaches executable image envelopes to the current Agent ActionStep so no
private VLM context is created. Session, binding, request, crop and capability
values are injected by trusted backend code. Supported actions are
`left_click`, `right_click`, `left_double_click`, `right_double_click`,
`scroll_up` and `scroll_down` (optional steps1..5, default1); legacy `scroll_up_1` and `scroll_down_1` remain one step.

Allowed executable operations are bare `observe`, `inspect_region`, and
`interact_detail`. A scroll interaction emits N signed unit wheel events in one
ledger transaction, not repeated interactions. A unit does not promise a pixel
distance; partial delivery is uncertain and must not be replayed. Click actions
reject explicit steps. Legacy optional observe action/target flags remain
backward compatible; an explicitly bound action must match the interaction.
Do not pass arbitrary shell text or native
identifiers. Only images with provenance `computer_use_observation` are
eligible for the documented historical-image placeholder exception; older
computer-use screenshots may be omitted after a newer observation is accepted,
while user-uploaded and ordinary Vision Automation images remain unchanged.
Dependent operations are serialized; an in-flight observe/inspect or
its following Agent inference cannot be overtaken by another operation. The
historical-image omission is staged after an accepted executable ActionStep and
committed only when the next ordinary Agent inference request crosses its
acceptance barrier.

After `ACTION_SENT`, the intent is consumed. Bare `observe` is the single capture
and refresh command; each successful accepted image creates a new UUID intent.
It also recovers after uncertainty without replaying any old action or crop.
A failed capture does not replace the old state or leave its detail eligible for
input. Observation requires no target, action or reason. Choose the action at
`interact_detail`; an optional legacy `observe --action ...` still binds that
explicit action, so later interaction must match it.

Calibration must be rerun after any fingerprint component
changes. Native readiness is fail-closed when development dependencies or
full-monitor/input evidence are unavailable.

Under default binding_only, dispatch checks the live binding/geometry and bounds validation-to-first-event time; it does not sample or compare screen pixels. The optional diagnostic frame_and_binding policy additionally checks a new frame. Shared seat serialization spans contexts; later events and cleanup carry bounded transaction deadlines. A successful transport result still needs visible/application outcome verification.

After success, cancellation or uncertainty, explicitly call `observe` for a new intent. It cannot revive or replay the old ledger. `DESKTOP_SESSION_RESET_REQUIRED` means the helper connection ended: dispose the Agent context and create a new one. The runtime never silently reopens authorization or retries old input.

Real host acceptance covers Portal authorization/cancellation, complete monitor capture, EIS mapping, actual button/scroll effects, resolution/scale/hotplug, Portal restart/revoke, stalled capture, stream recreation, EIS loss during input, Stop and helper crash. The acceptance-manifest script creates an honest checklist and does not mark unrun tests passed; X11 is not a Wayland acceptance requirement.

## Isolated live transport probe (no input events)

From the actual graphical login terminal (Wayland or X11), explicitly authorize only this probe:

```bash
./.sglang/bin/python -m scripts.probe_computer_use_live --authorize --helper-binary /absolute/path/to/computer-use-helper --authorization-timeout 120 --transport-timeout 5
```

The helper path must point to the built executable. This command creates a fresh session capability from a deep-copied configuration with capture and POINTER authority enabled only in the probe process. It does not write YAML, change backend defaults, enable production computer-use readiness, or send input events. Wayland Portal may display a permission dialog. X11 uses the authenticated X connection and displays no Portal dialog; CLI authorization still scopes the probe capability. Output contains only frame hash, geometry and transport status; physical input and calibration remain `not_run`, and overall `ready` stays false. The client closes the helper after the bounded probe.

## Isolated desktop-input application-outcome probe

After the no-input live probe verifies a full frame and backend-specific monitor mapping, run
the separate input probe from the same graphical login terminal:

```bash
./.sglang/bin/python -m scripts.probe_computer_use_input --authorize \
  --helper-binary /absolute/path/to/computer-use-helper \
  --authorization-timeout 120 --transport-timeout 15
```

This operator-run check opens a temporary full-screen green Tk test surface
after native startup. On X11 the surface is placed at the selected XRandR monitor origin; Wayland retains the fullscreen behavior. It verifies that the surface is visible in a new
captured frame before sending one pointer motion, left press/release and one
wheel step to its center. It reports `verified_in_test_window` only when the
window receives button and wheel callbacks. Do not interact with the desktop
while it runs. Any missing surface/frame evidence prevents input; a partial
delivery is reported as uncertain and never retried. The result does not
replace model calibration or the full application acceptance matrix, so
`ready` remains false.

## Controlled live model-to-click probe

With local SGLang running on `127.0.0.1:30000`, run the following from the
graphical login terminal. On Wayland choose the monitor that contains the test
window in the Portal dialog and allow remote interaction. X11 selects the primary
XRandR monitor, or the sole monitor when no primary is available:

```bash
./.sglang/bin/python -m scripts.probe_computer_use_skill \
  --authorize --send-test-click \
  --helper-binary "$PWD/bin/linux-aarch64/computer-use-helper" \
  --authorization-timeout 120 --transport-timeout 15 \
  --max-reobserves 2
```

This explicit probe uses an in-memory desktop capability and a temporary
windowed green Tk canvas with one magenta target. It records the canvas's
actual screen position and size, maps both the canvas and target through the
selected full-monitor binding, and verifies the captured magenta fill and
green pixels across that mapped panel. A moved canvas or failed pixel match
prevents input. The probe sends the complete source screenshot stretched to a square
with an eight-division coordinate grid to Stage 1, then a chosen-zoom crop with the
16-division Stage-2 grid to local SGLang. If Stage 2 requests `observe`, the
probe captures a new overview and asks Stage 1 for another rough point, within
the configured two-retry maximum. It sends one click only if the target is
visibly inside the crop, the decoded Stage-2 point lies inside the target, and
the binding, frame geometry and target are rechecked immediately before input.
The test surface alternates two visibly distinct green backgrounds while
waiting for the first frame and for the final pre-dispatch frame. A newly
accepted backend frame is required (PipeWire on Wayland, XGetImage on X11); repaint requests alone do not establish
freshness, and failure to receive one prevents input.
The Tk window must receive the click for `verified_in_test_target`. Every
attempt saves exact model input PNGs, prompts, point overlays and `result.json`
under `logs/computer-use/live-probes/`.

The historical 2026-09-28 windowed run (its old report is no longer present and is not a runtime prerequisite)
captured a 2560×1440 monitor. Its Stage-1 crop contained the Tk target; the
Stage-2 point passed the safe target inset; a fresh frame and unchanged target
bounds were revalidated before dispatch. Tk recorded one in-target button-down,
one in-target button-up, and zero outside button-down events. This establishes
the controlled Tk outcome only. Calibration and the actual Agent/archery target
remain unverified, and production `ready` remains false.

Only if the requested interaction point itself is absent, cut off or
unidentifiable does the live Agent call
`observe` and receive
its fresh overview BEFORE another `inspect_region`. The target remains in user
context; target text is optional; no action or reason is required to capture. Choose the action only at interaction unless explicitly prebound. Invalid quoting (such as
an unescaped apostrophe inside single quotes) is an argv error, not an app-specific
restriction. Live observe has no count ceiling; accepted refreshes reset
the detail-inspection budget. Never derive overview
coordinates from detail or reuse a remembered point from an older overview;
never guess a click. For a user-requested interaction, an identifiable point instead requires the next
desktop call to be interact_detail, even if the surrounding object is clipped.
Do not recover for perfect centering, extra margins or a different zoom.
A failed absent-target/wrong-region crop is negative evidence:
visually locate the closest rough target center in the latest overview and do not
consecutively repeat its failed `(x,y)` or replay that command, even with a different
zoom. Do not invent arbitrary offsets; stop without input if the target cannot be
located. Tool observations contain no coordinate echoes or crop
bounds. The latest detail alone supplies the precise point, and older overview
pixels are omitted after the detail is accepted. The isolated typed offline
adapter also exposes zero-argument `observe` as its fresh-overview recovery operation;
it also requires that new overview before another crop. Earlier trusted
computer-use image blocks in the same top-level Agent context are replaced
with placeholders after the next model request is accepted, retaining only
the latest such image; unrelated uploads and Vision Automation images retain
their normal history policy. This checks the real Portal, model, renderer and input chain in a controlled
window. It uses the model adapter's isolated prompts, while the live Agent
uses this skill's instructions in its own context. It neither supplies a
calibration report nor enables production Agent desktop authority; `ready`
remains false until measured calibration and the full acceptance matrix pass.

## Generic operator sessions without release readiness

The current deployment explicitly enables `computer_use.operator_session_enabled`
for new top-level web Agent contexts, with bounded timing/difference settings in
`config/computer-use-operator-session.yaml`. `/config` reports
`operator_session_configured`, `operator_session_active`, `runtime_mode` and
`restart_required`; `release_ready` remains false. The ordinary release path
still requires its calibration report when operator sessions are disabled.
The common backend performs Portal authorization, exact session/capability
binding and geometry checks, seat locking and at-most-once input
dispatch. It does not recognize targets or inspect or score application results.
The user judges the result of each input.
The default `computer_use.pre_action_validation: binding_only` skips per-click
freeze, screenshot age and pixel-difference vetoes. Screen changes do not cancel
input. A revoked/disconnected native session or changed binding still blocks
it. `frame_and_binding` is an explicit strict diagnostic policy.

After changing YAML, restart the backend from the graphical login with
`bash scripts/4_start_backend.sh` and create a new basic Agent context. Existing
contexts retain their pinned capabilities. Ask the Agent to read
`computer-use-vision`, observe the actual screen, choose a chosen-zoom crop, and locate
the requested target in the latest detail image. Thinking remains controlled
by the existing web UI toggle.

The separate CLI uses the same common backend for one isolated real Agent
context. It requires an explicit target description and input authorization:

```bash
./.sglang/bin/python -m scripts.run_computer_use_agent_test \
  --authorize-test-input --target-description 'the visible button requested by the user'
```

Add `--enable-thinking` to enable thinking, or `--check-only` to validate the
configuration without opening Portal or sending input. CLI operation states
are recorded under `logs/computer-use/operator-agent-tests/`.
`ACTION_SENT` means native dispatch, not a verified application result.
No fixture heartbeat or repaint instrumentation is required or supplied.

## Internal computer-use diagnostics

Bounded JSONL records are written to `logs/computer-use/vision-desktop.jsonl`
under the project root. `computer_use.diagnostics_enabled`, `diagnostics_path`,
`diagnostics_max_bytes` and `diagnostics_backup_count` control logging (currently
1 MiB per file and three backups, at most 4 MiB of records). Relative paths
resolve against the repository. `.lock` serializes writes and rotation across
backend processes; logging failures never change safety or delivery outcomes.

Records correlate session, request, sequence, frame, crop and geometry with
operation/result, elapsed time and internal coordinate mappings. Image result
records include PNG size and SHA-256. `llm_request` records describe prepared
client request image counts and the latest trusted PNG identity and presence;
`llm_completed` records returned/error/abort/closed transport outcomes, and
`llm_accepted` separately identifies accepted responses, cancellation and
empty-stream fallback. These describe client submission and acceptance,
without asserting what server preprocessing or the model perceived.
No images, base64, prompts, reasoning, target prose or application scoring are
logged. Records never enter model observations.

Restart the existing backend and create a new top-level Agent context after
changing zoom, operator policy or diagnostics configuration. Restarting does
not establish release readiness or a verified application outcome.

To inspect the exact computer-use PNGs sent to the model, enable `computer_use.debug_save_images` and restart the backend with a new Agent context. Files are private and retained under project `logs/computer-use/vision/`; `debug_image_max_files` (64) and `debug_image_max_bytes` (134217728) bound owned artifacts. The switch defaults off and does not change upload or Vision Automation processing.


The YAML switch `computer_use.grid_label_placement` uses `edge` for endpoint
labels or `repeated` for X values at Y=31.25,156.25,...,906.25 and Y values at X=31.25,156.25,...,906.25 (active deployment:
`repeated`). X labels rotate 90° counterclockwise; Y labels stay horizontal.
Stage-2 odd guides do not carry their own coordinate values. `grid_label_shadow_width_px` is the
black outline radius at a 1000-pixel canvas baseline (default 1); actual radius
is half-up rounded `width*S/1000`, with a minimum of one pixel when enabled.
Zero disables the outline. White glyphs and black outline both use 0.5 opacity
on a transparent overlay. These settings affect both Computer Use stages and
the renderer/deployment fingerprints; uploads and Vision Automation are unchanged.


## X11 selection and diagnostic evidence

Explicit XDG_SESSION_TYPE=x11 selects X11 even with a stale Wayland variable.
Explicit wayland stays on Portal/PipeWire/EIS even when DISPLAY exists for
XWayland; a Wayland failure never falls back to X11. Without session type,
WAYLAND_DISPLAY takes priority; only DISPLAY without a Wayland indication
selects X11. These variables select a backend but never prove readiness.
Preserve the graphical login environment through controlled EnvironmentBuilder.

X11 binds the primary or sole XRandR monitor; ambiguous selection fails closed.
source=xrandr, root ID, monitor atom/name, output IDs and
mapping_id=xrandr:<root-id>:<monitor-atom> identify the root region. XGetImage
reads that region and returns the same sealed RGB IPC frames as Wayland.
MIT-SHM/XShm is not implemented. Pointer coordinates are integer root
coordinates including monitor origin; XTest submits input. Geometry/topology
changes invalidate the binding and old crops.

Doctor requires backend-specific packages: json-glib plus x11/xext/xtst/xrandr
for X11, or gio-unix/json-glib plus PipeWire/SPA/libei for Wayland. The native
X11 build is independent of Wayland development packages. Live checks stay
not_run; packages and DISPLAY cannot establish readiness. X11 probes report
portal_authorization and eis_mapping as not_applicable, and separately record
x11_connection, xrandr_mapping and xtest_input. A no-input probe leaves XTest
outcome not_run; application callbacks are needed for input outcome. X11
acceptance includes capture, button/scroll outcome, root/monitor changes,
connection loss, Stop and helper crash. Probes never change persisted YAML or
production permissions.
