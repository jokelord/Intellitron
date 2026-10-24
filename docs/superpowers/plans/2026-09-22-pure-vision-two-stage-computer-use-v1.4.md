# Pure-Vision Computer Use v1.4 Implementation Plan

> **Status:** approved architecture, implementation begins 2026-09-22.
> **Spec:** [Pure-Vision-Two-Stage-Computer-Use-Grounding-Skill_v1.8.md](../../Pure-Vision-Two-Stage-Computer-Use-Grounding-Skill_v1.8.md). The coordinate and grid rules in this historical v1.4 plan are superseded by v1.8.

## Goal

Make desktop computer use observable and stateful inside the existing top-level Agent context. Every task must first receive a real desktop observation through the allowlisted `vision_desktop` executable; the same Agent then emits Stage-1 coarse relative coordinates, receives a ticked crop from the same immutable frame, emits Stage-2 precise local coordinates, and only then permits trusted physical dispatch. Remove the old public `computer_use` tool and the backend-private VLM grounding calls. Preserve fail-closed native readiness and the project Prefix-Cache Covenant, with the explicit `computer_use_observation` historical-image exception defined by the SPEC.

## Non-goals

- Do not claim live Wayland capture/input until Portal authorization, PipeWire frame acquisition/canonicalization, libei lifecycle and dispatch are implemented and host-validated.
- Do not add OCR, AT-SPI, DOM, semantic element IDs, keyboard or drag actions.
- Do not rewrite ordinary user-uploaded images, Vision Automation images, roles, tool calls, tool observations, or unrelated sessions.
- Do not use git commands, install packages, modify dependency source, or create a new frontend SSE protocol.

## Required invariants

1. The first computer-use operation in a new top-level Agent request is `vision_desktop observe`; `inspect_region`, `interact_detail`, and physical dispatch are rejected until a successful observation is attached to the same Agent context.
2. The executable is reached through the existing `exec_command(command: str)` public schema, but a leading `vision_desktop` command is parsed by a fixed non-shell argv parser. No shell metacharacters, arbitrary executable path, native handle, capability, session ID, crop ID, or request ID is model-controlled.
3. The executable observation result is a trusted multimodal Agent observation with provenance `computer_use_observation`; forged stdout or model-provided provenance never grants image-history eligibility.
4. Dependent operations are serialized. Stage-1 and Stage-2 results carry the observation/crop generation they saw; stale or mismatched generations fail closed.
5. Stage-1 and Stage-2 use the same Agent context. No `LLMService.chat_stream_async` call is made by the computer-use service for grounding.
6. ActionStep observations preserve authentic computer-use images while still removing only duplicate task-image references. The serialized next Agent request must contain the latest screenshot image and placeholders for eligible older screenshots after the omission commit barrier.
7. Historical-image omission is staged after an accepted executable ActionStep and committed only after the next ordinary Agent inference request crosses its acceptance/transport barrier. Pre-accept failure, empty fallback, Stop, Answer Now, compact and failed reobserve do not commit it; successful reobserve creates a new eligible latest image.
8. `interact_detail` receives only Stage-2 coordinates and the trusted requested action. Crop/session/native IDs remain runtime state and are never model-controlled.
9. Any missing native capability, calibration, frame freshness, geometry coverage, or dispatch certainty returns a structured fail-closed result and never replays physical input.

## Implementation sequence

### 1. Failing contracts first

Add focused tests before production edits:

- `tests/computer_use/test_executable_observation_protocol.py`: restricted parser accepts only the four operations, requires observe target/action, rejects shell syntax/unknown flags/native identifiers, and binds trusted provenance only after a verified result.
- `tests/computer_use/test_same_agent_two_stage.py`: a fake Agent transcript must perform observe → Stage-1 inspect → detail → Stage-2 interact in one context; the service must never call `LLMService.chat_stream_async`; concurrent or stale generation results are rejected.
- `tests/test_computer_use_image_history.py`: serialize ActionSteps containing computer-use images and ordinary images; preserve newest computer-use image, replace only older eligible blocks with one deterministic placeholder per logical image block, preserve all non-image content, and stage/rollback on transport failure.
- Extend existing skill/tool tests to assert no public `computer_use` tool is registered and the first executable operation is observe.

Run only these new tests first and record their expected failures. Do not weaken assertions to fit the existing private-grounding code.

### 2. Executable protocol and service state machine

Create a trusted Python bridge around the native helper, with an explicit operation enum and context-bound state:

- Parse the command string with a non-shell fixed argv parser before the general shell path; reject metacharacters and arbitrary paths.
- `observe(target_description, action)` freezes one frame, creates the overview image envelope, records observation/frame/geometry generation, and returns an attachable image payload.
- `inspect_region(x, y, zoom)` validates Stage-1 coordinates and binds the result to the currently accepted observation generation; it creates the exact crop/detail image and returns its image envelope without exposing crop IDs.
- `interact_detail(x, y, action)` validates Stage-2 wire coordinates against the current crop generation and delegates deterministic geometry/dispatch. The runtime injects crop/request/binding identity.
- `reobserve(reason)` invalidates the active crop, obtains a new observation, and follows the same provenance and history rules.
- Enforce one dependent operation at a time per top-level context and an at-most-once dispatch identity.
- Native unavailable responses remain structured and truthful; no synthetic screenshot or fake ready state is emitted.

Keep geometry modules and native helper boundaries intact. Reuse existing capability and abort validation rather than adding a second authorization path.

### 3. Agent tool registration and multimodal attachment

- Remove `computer_use` from `_get_tools`, profile snapshots/allowlists, and any public tool schema. Keep old implementation modules only as internal migration shims until tests prove no production path imports them.
- Add a session-aware executable command result type that can carry text metadata plus an in-memory image/provenance attachment. The existing command output string remains available for ordinary commands.
- In the Agent tool execution path, promote only verified `vision_desktop` envelopes to `ActionStep.observations_images` with provenance metadata; ordinary command stdout cannot create an image observation.
- Ensure the next LocalLLM multimodal conversion preserves that image in the current request. Do not put image base64 in textual observations.
- Reject a same-batch dependent operation and reject an operation whose generation does not match the image used by the model.

### 4. Historical screenshot omission

Implement a pure projection helper used by Agent memory serialization:

- Identify only logical image blocks marked `computer_use_observation`.
- Keep the newest accepted block real; replace each older logical block with one deterministic placeholder at the same location.
- Preserve message role, text, tool calls, tool observations, ordering, and all other image provenance byte-for-byte.
- Stage the projection after the executable ActionStep is accepted. Apply it only when the next ordinary Agent inference request crosses the transport/acceptance barrier; rollback pending state on validation/transport failure, empty fallback, Stop, Answer Now or compact.
- Scope state to the bound top-level Agent context. Child/sibling/Chat/other sessions must be untouched.
- Emit timeline/audit metadata `computer_use_historical_image_omission` without adding a new frontend SSE protocol.

The implementation must fix the current unconditional `ActionStep.observations_images = None` behavior without restoring duplicate user task images that the existing policy intentionally strips.

### 5. Remove private grounding inference

Refactor `backend/computer_use/grounding.py`, `model_adapter.py`, and `service.py` so the service is an executable observation/geometry/dispatch state machine. Delete or make unreachable `_infer`/`chat_stream_async` grounding calls and private stage request IDs. Model coordinate encode/decode remains available for the same Agent's Stage-1/Stage-2 operation schemas. Any old direct service call must fail closed or route through the new session-bound executable path; it must never silently open a second VLM context.

### 6. Verification and native truthfulness

Run focused tests, then the existing `tests/computer_use` and relevant Agent multimodal/history suites. Build the native helper and run its parser/readiness tests. A sandbox `SESSION_BUS_UNAVAILABLE`, missing Portal, missing libei or incomplete PipeWire path remains an unavailable diagnosis; do not convert it into a passing live capability.

Before claiming completion, verify:

- no `computer_use` tool appears in model tool schemas;
- the first operation in an actual parser → tool wrapper → ActionStep → next LLM message fixture is observe;
- the next message contains the latest screenshot and correct placeholders;
- no private grounding call is made;
- stale/generation/abort/delivery-unknown paths are fail-closed;
- ordinary historical/user-uploaded/Vision Automation images remain unchanged.

## File ownership

- Agent A: `backend/agent/agent_runner.py`, `backend/agent/local_llm_model.py`, history/projection helpers and Agent tests.
- Agent B: `backend/tools/sys_ops.py`, new `backend/computer_use/executable_protocol.py`/bridge, tool registration/configuration and executable protocol tests.
- Root: service/grounding migration, SPEC consistency, integration tests, verification and review. Agents must not edit one another's primary files.
