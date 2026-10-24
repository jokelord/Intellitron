# Computer Use v2.0 Square Model Canvas Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the existing two-stage computer-use renderer from aspect-preserving model images to square model-facing images while keeping the original monitor/crop geometry and independent `[0,1000]` coordinate axes intact.

**Architecture:** Preserve the immutable canonical frame and `crop_rect()` / `local_to_monitor()` geometry. Render the full Stage-1 frame and the exact clipped Stage-2 crop into the same configured `S×S` canvas, draw the eight-division grid only after Stage-2 resize, and bind prompts, metadata, probes, and calibration to the new renderer fingerprint. Physical input continues to use the original source dimensions and existing session-bound dispatch path.

**Tech Stack:** Python 3.12, Pillow, pytest, existing `backend/computer_use` modules, local SGLang for the optional measured offline probe.

**Spec:** [Pure-Vision-Two-Stage-Computer-Use-Grounding-Skill_v2.0.md](../../Pure-Vision-Two-Stage-Computer-Use-Grounding-Skill_v2.0.md), especially §§5, 10–22, 32–34, 49, 53–63.

## Global Constraints

- This is a geometry/renderer migration of the existing flow. Do not add another VLM call, public tool, input backend, desktop authority path, or frontend protocol.
- Stage-1 consumes the entire canonical monitor frame without grid. Stage-2 first clips the actual source crop, then resizes that exact rectangle; neither stage pads, letterboxes, rotates, or modifies the canonical frame.
- Both model-facing PNGs use one explicit positive integer `model_square_side_px=S`, with `S % 8 == 0`. `S=3840` is the deployment example, not the normalized coordinate domain; choose actual deployment `S` only after memory, serving, and calibration checks.
- Stage-1 and Stage-2 live wire X/Y each remain `[0,1000]`. The backend maps Stage-1 via `(X*Wp/1000,Y*Hp/1000)` and Stage-2 via `(x0+u*(x1-x0)/1000,y0+v*(y1-y0)/1000)` using the exact integer crop rect. Never add an aspect correction after this mapping.
- Stage-2 grid ticks on each axis are exactly `0,125,250,375,500,625,750,875,1000`. Internal guide positions are `k*S/8`, `k=1..7`; the continuous outer boundary `S` is drawn at final pixel index `S-1`. Each `x=…` and `y=…` tick label repeats once in each of the eight orthogonal cells, with no text stroke or shadow.
- Keep `computer_use.enabled=false` and desktop capture/input capabilities at their current disabled defaults. Existing v1.8 calibration is invalid for v2.0; never fill missing safety thresholds with constants or enable input based on offline fixture success.
- Write offline sample images, overlays, and reports under repository `computer-use-diagrams/v2.0/`, not `/tmp`. Preserve prior diagrams and v1.8 spec/results.
- The project `AGENTS.md` forbids git commands without separate user permission. This plan contains no automatic git or commit step. Reuse existing subject-matter subagent contexts during execution; do not create auditor subagents or per-step `.superpowers/` summaries.

## File Map and Ownership

| Work package | Production files | Verification files | Responsibility |
|---|---|---|---|
| 1. Config and crop contract | `backend/computer_use/config.py`, `contracts.py`, `runtime.py`, `config/config-agent-operation.yaml` | `tests/computer_use/test_contracts.py`, `test_runtime.py`, `test_coordinates.py`, `test_geometry_properties.py` | One `S`, immutable exact crop, square dimension metadata; retain source-based inverse mapping |
| 2. Renderer and grid | `backend/computer_use/render.py` | `tests/computer_use/test_render.py` | Both square PNGs, post-resize grid, repeated labels and render fingerprint |
| 3. Model coordinate boundary | `backend/computer_use/model_adapter.py` | `tests/computer_use/test_model_adapter.py` | Square-image prompts/schema, strict normalized decode, trusted crop binding |
| 4. Calibration and visual probe | `backend/computer_use/calibration.py`, `scripts/calibrate_computer_use.py`, `scripts/probe_computer_use_two_stage.py` | `tests/computer_use/test_calibration.py`, probe tests where appropriate | v2 fingerprint, repo-local fixtures/overlays, measured offline comparison |
| 5. Integration | Existing files above only as needed | `tests/computer_use/test_runtime.py`, `test_executable_backend.py`, `test_same_agent_two_stage.py` | End-to-end observation→crop→model point→source ratio; no input before live calibration |

Work packages 1 and 2 agree on `config.model_square_side_px` and `crop.detail_width=detail_height=model_width=model_height=S` before parallel edits. Package 3 can work in its existing model-adapter context while 1/2 proceed. Package 4 starts after the renderer fingerprint is stable. Root owns cross-package integration and validation; agents do not edit each other's primary files.

## Review Focus

1. **Near-edge nominal crop:** clipping may yield a non-square, unpredictable source rectangle; Task 1 tests its exact bounds, and Task 2 tests that its entire content fills `S×S` without padding.
2. **Canvas endpoint versus pixel index:** using `S-1` for internal `k*S/8` guides creates uneven cells; Task 2 tests exact internal lines and the separately clipped 1000 boundary.
3. **Source-axis bias hidden by pixels:** a 1920×624 crop has unequal source pixels per normalized unit; Tasks 1 and 5 test each inverse axis separately and the probe reports both absolute and crop-relative errors.
4. **Stale calibration:** a v1.8 report or changed `S` must not authorize dispatch; Task 4 tests fingerprint invalidation and Task 5 tests refusal before native creation.
5. **Implicit serving resize/crop:** an `S×S` PNG does not prove what the VLM actually sees; Task 4 records the request image and serving/preprocessor identity, and Task 5 requires measured probe results rather than assuming square preprocessing inside the model.

---

### Task 1: Establish the square-size and exact-crop contract

**Files:** Modify `backend/computer_use/config.py`, `contracts.py`, `runtime.py`, `config/config-agent-operation.yaml`; test `tests/computer_use/test_contracts.py`, `test_runtime.py`, `test_coordinates.py`, `test_geometry_properties.py`.

**Interfaces:** `ComputerUseConfig.model_square_side_px: int` replaces the two long-edge configuration fields in the active path. `GroundingRenderer.crop(...) -> DetailCrop` sets `detail_width=detail_height=model_width=model_height=S`. `crop_rect()` and `local_to_monitor()` retain their signatures and source-based formulas. `DetailCrop` exposes derived, exact scale properties `model_scale_x = Fraction(model_width, crop_width)` and `model_scale_y = Fraction(model_height, crop_height)`; do not store redundant mutable copies.

- [ ] Add failing config/contract tests: reject `S<=0`, bool, noninteger and `S%8!=0`; accept 1000/1536/3840; reject rectangular `DetailCrop` model/detail dimensions; leave default `enabled=false` and all seven readiness requirements unchanged. Use a safe disabled config default `S=1000`; the host may explicitly choose 3840 after resource checks. Reject legacy long-edge YAML keys rather than silently applying v1.8 geometry.
- [ ] Run `./.sglang/bin/python -m pytest -q tests/computer_use/test_contracts.py` and confirm the new tests fail for the intended missing contract.
- [ ] Implement the config/YAML migration and `DetailCrop` square validation. Keep the exact physical `x0,y0,x1,y1` and `source_width/source_height` fields. Switch `GroundingRenderer.crop()` to `(S,S)`; remove `raster_extent()` / `render_extent()` from production imports, retaining historical helpers only if an isolated offline comparator still needs them.
- [ ] Add geometry tests: `crop_rect(3840,2400,500,500,2)=(960,600,2880,1800)` and `crop_rect(3840,2400,500,10,2)=(960,0,2880,624)`; on the latter, local `(500,500)` maps to source `(1920,312)` and monitor ratio `(1/2,13/100)`. The result must not depend on `S`. Cover portrait and all four corners in the existing deterministic 10,000-case suite without rendering 10,000 large PNGs.
- [ ] Run focused contract/coordinate/runtime/property tests; expect all pass. Remove only tests asserting the old rectangular *model bitmap*, never those proving the real source crop remains rectangular.

### Task 2: Square overview/detail rendering and exact eight-cell grid

**Files:** Modify `backend/computer_use/render.py`; test `tests/computer_use/test_render.py`.

**Interfaces:** `render_overview(frame, config) -> ImageArtifact` and `render_detail_base(frame, crop, config) -> ImageArtifact` keep their public signatures and return `(S,S)` PNGs. `overlay_detail(base,crop,labels,config) -> ImageArtifact` requires a square base and keeps its post-resize role.

- [ ] Write failing renderer tests using a non-square source with asymmetric colored landmarks: full Stage-1 content occupies all of `S×S` with no grid or blank border; center `1920×1200` and clipped-edge `1920×624` real crops each yield `S×S` detail; the edge image excludes pixels outside the actual rect and keeps last-row/last-column landmarks visible (allowing interpolation differences, not requiring byte-identical pixels).
- [ ] Run `./.sglang/bin/python -m pytest -q tests/computer_use/test_render.py` to confirm the new square assertions fail on the v1.8 renderer.
- [ ] Replace the aspect-preserving `_scaled_size()` / `raster_extent()` active paths with explicit Pillow `resize((S,S), Image.Resampling.LANCZOS)` after source read or exact crop. Validate crop/frame binding and square metadata before rendering. Bump the renderer fingerprint version and include `S`, square policy, resampler and label policy; do not encode every PipeWire frame or add runtime image files.
- [ ] Update the grid test to require lines at `k*S/8` for `k=1..7` on both axes, boundary lines at 0 and `S-1`, no remainder cell, the nine permitted tick values, and eight copies per axis/tick. Scale the existing small no-stroke font in proportion to `S` (9 px at `S=1000`, `round_half_up(9*S/1000)` elsewhere), keeping labels inside the canvas and avoiding overlap/small-target occlusion.
- [ ] Run the renderer tests at `S=1000` and at least one other divisible-by-eight size. A plain `grid_enabled=false` detail must remain a square base unchanged by overlay.

### Task 3: Make prompts and decoding describe only the square relative axes

**Files:** Modify `backend/computer_use/model_adapter.py`; test `tests/computer_use/test_model_adapter.py`.

**Interfaces:** `describe_stage(stage,image,crop,request)` rejects non-square live `relative_1000` / `custom_isotropic_1000` images and a Stage-2 image inconsistent with its crop's `S` dimensions. `encode_stage()` tells the Agent that the *displayed* image was stretched, X/Y both mean fractions of that image, and the backend handles source-pixel conversion. `decode_stage()` returns canonical `InspectRegion` / `InteractDetail` values without an additional aspect correction.

- [ ] Write failing live-protocol tests: 3840² overview and edge detail still have X/Y schema maxima 1000; the square center `(500,500)` decodes to source center for both stages; the prompt has no rectangular-image claim, physical pixel coordinate instruction, or model-controlled `crop_id`/native identifier. Retain historical `image_pixel` tests only as explicitly offline adapter comparisons.
- [ ] Run `./.sglang/bin/python -m pytest -q tests/computer_use/test_model_adapter.py`; confirm the new assertions fail on the current rectangular wording/schema.
- [ ] Update Stage-1/Stage-2 prompts to explain square model display and independent normalized axes without asking the model to undo stretching. Remove `crop_id` from Stage-2 model prompt/tool arguments; bind the already-owned `instance.crop.crop_id` inside `decode_stage()` after strict `{x,y,action}` validation. Keep requested-action checking and `Reobserve` behavior. Do not send source dimensions or OS input geometry for model arithmetic.
- [ ] Run adapter tests and the two-stage executable integration tests. Preserve model output error feedback: invalid types, out-of-range values, extra fields, and wrong action still fail closed.

### Task 4: Re-fingerprint calibration and produce reviewable offline square probes

**Files:** Modify `backend/computer_use/runtime.py` (`deployment_fingerprint()` only after Task 1), `calibration.py`, `scripts/calibrate_computer_use.py`, `scripts/probe_computer_use_two_stage.py`; test `tests/computer_use/test_calibration.py` and focused probe tests.

**Interfaces:** Existing `deployment_fingerprint(app_config) -> str` includes `model_square_side_px`, renderer implementation hash and serving preprocessing; `calibration_fingerprint()` uses a new version. Fixture and annotated output defaults are `computer-use-diagrams/v2.0/`, including Stage-1 coarse point and Stage-2 final-point overlays; the probe's JSON continues to identify the exact output files and original source/crop dimensions.

- [ ] Write failing tests that changing `S`, resize policy, grid/label policy or serving preprocessing invalidates the report; a v1.8 report cannot pass the v2.0 verifier. Generated portrait, landscape, center and edge fixtures must each expose square model inputs while recording their original source size and actual crop rect. Fixture generation remains offline and sends no mouse events.
- [ ] Run `./.sglang/bin/python -m pytest -q tests/computer_use/test_calibration.py`; confirm failures reflect old renderer/report assumptions.
- [ ] Update deployment/renderer fingerprint and fixture metadata. Version the fixture output path under `computer-use-diagrams/v2.0/` so existing reports and PNGs remain intact. Update annotation math to place marks by `X/1000*S` or `u/1000*S` on model-facing images while computing final source points only from the exact crop rect. Report `|Δx|,|Δy|` in source pixels, each divided by crop width/height, and actual `|log(crop_width/crop_height)|` for the user-observed directional pattern.
- [ ] Run calibration/probe unit tests. Then generate offline fixtures with `./.sglang/bin/python -m scripts.calibrate_computer_use --generate-fixtures` and inspect the contact sheets visually; run `./.sglang/bin/python -m scripts.probe_computer_use_two_stage --infer-stage1 --infer-stage2` only when SGLang is available. Compare v1.8 and v2.0 using the same source/target and fixed rough crop when isolating Stage-2 accuracy. Save actual results; never synthesize a passing threshold or claim host readiness from an offline run.

### Task 5: Integration, limits and fail-closed verification

**Files:** Test `tests/computer_use/test_runtime.py`, `test_executable_backend.py`, `test_same_agent_two_stage.py`; update `docs/computer-use-vision-operations.md` only for the new offline command/output location if the implementation changes operator instructions.

**Interfaces:** No native Portal/PipeWire/EIS/X11 interface change. Existing session-bound `observe → inspect_region → interact_detail` remains the only live route; dispatch still consumes monitor ratios from `local_to_monitor()` and requires the current measured deployment report.

- [ ] Add one integration case from a 3840×2400 canonical frame through a square Stage-1 observation and a clipped `1920×624` Stage-2 image: final local `(500,500)` must map to source `(1920,312)` and monitor ratio `(1/2,13/100)`. Verify frame/binding/geometry IDs and the real crop rect survive rendering and decoding unchanged.
- [ ] Test report rejection *before native helper creation* for old v1.8 fingerprint, changed `S`, failed measurements, or unknown serving transformation. Preserve the existing no-authority/no-calibration/unknown-delivery behavior; no test should infer permission from a square PNG.
- [ ] Run the focused suites and `./.sglang/bin/python -m pytest -q tests/computer_use tests/test_computer_use_image_history.py`. Broaden only if these expose interaction with another suite. Verify no old `overview_long_edge_px` / `detail_long_edge_px` references remain in the active runtime/config path and no prompt describes a rectangular live image.
- [ ] Record memory and latency for candidate `S`, especially 3840² (about 42.2 MiB per uncompressed RGB image before intermediate copies). Only after real SGLang samples, effective preprocessing inspection, and a new calibration report can a later operator choose live-session verification. Do not change capability defaults or activate desktop input in this task.

## Handoff

Execution can use the already-authorized subagent approach while reusing each agent's prior subject-matter context and keeping file ownership separate. Root checks each integrated result. This plan does not request or imply automatic git operations; follow the repository's explicit git-permission rule if commits are later desired.
