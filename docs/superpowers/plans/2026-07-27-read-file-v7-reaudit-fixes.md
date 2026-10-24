# Read File v7 Re-audit Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. This repository task must execute inline because the user prohibited delegation and Git operations.

**Goal:** Resolve all seven re-audit findings with reproducible RED/GREEN tests and complete local/Thor verification.

**Architecture:** Keep fixes within existing Read File, OCR attestation, benchmark, and tool-documentation boundaries. PDF chunks separate disjoint ownership from overlap context; OCR protocol validation precedes configurable quality classification; telemetry and security checks gain explicit lifecycle boundaries.

**Tech Stack:** Python 3.12, PyMuPDF, httpx, defusedxml, CairoSVG, FastAPI, psutil, tegrastats, pytest.

## Global Constraints

- No Git, sudo, delegation, or third-party source edits.
- Preserve `.xls`/`.ods` placeholders, omitted-bound full text output, canonical Agent history, and existing OCR precision/device policy.
- PDF overlap-expanded chunks never exceed the hard byte cap; impossible overlap leaves fail explicitly.
- Malformed OCR protocol and page indexes remain terminal.

---

### Task 1: Convergent PDF Ownership Splitting

**Files:**
- Modify: `backend/tools/read_file/pdf_chunker.py`
- Test: `tests/test_read_file_office_pdf.py`

**Interfaces:**
- Produce: `PDFChunk.owned_source_pages: tuple[int, ...]`
- Preserve: `source_pages` as emitted OCR order and `overlap_source_pages` as duplicate context.

- [ ] Add parameterized 2/4-page tests with byte limits derived from generated one-page and multi-page PDFs for overlap 0/1/2. Assert finite completion or explicit `FILE_TOO_LARGE`, strict ownership shrink, ordered ownership union equal to all source pages, and no duplicate ownership.
- [ ] Run exact tests and record RED recursion/non-convergence behavior.
- [ ] Refactor `_write_chunk` to accept ownership pages plus configured overlap, attach immediately preceding global pages only when materializing, recurse only on two non-empty strict ownership subsets, and reject an oversized one-owner emitted leaf.
- [ ] Run the exact reproductions and all PDF tests GREEN.

### Task 2: Real OCR Quality Classification and Orchestration

**Files:**
- Modify: `backend/config.py`
- Modify: `config/config-misc.yaml`
- Modify: `backend/tools/read_file/paddleocr_client.py`
- Test: `tests/test_read_file_paddleocr_client.py`
- Test: `tests/test_read_file_v7_integration.py`

**Interfaces:**
- Add config: `ocr_min_page_markdown_characters` and `ocr_severe_tilt_degrees`.
- Preserve: `_parse_response` protocol errors as `OCR_RESPONSE_INVALID`; quality errors become `OCR_PAGE_FAILED`.

- [ ] Add client tests for empty/short Markdown and official `prunedResult.doc_preprocessor_res.angle` evidence, plus malformed/index cases.
- [ ] Add a local HTTP `PaddleOCRClient` + `ReadFileService` test whose first valid response is low quality and whose unwarping response succeeds; assert payload sequence `False, True`. Add a malformed-index service test asserting one request and terminal error.
- [ ] Run exact tests RED.
- [ ] Split response processing into structure/index validation followed by quality classification using configured thresholds.
- [ ] Run exact client/service tests GREEN.

### Task 3: SVG CSS Escape and Runtime No-fetch Boundary

**Files:**
- Modify: `backend/tools/read_file/image_normalizer.py`
- Test: `tests/test_read_file_images.py`

**Interfaces:**
- CairoSVG conversion receives a deny-all `url_fetcher`.

- [ ] Add exact `fill="u\\72l(file:///...)"` regression and a monkeypatched fetcher assertion proving no resource retrieval is allowed.
- [ ] Run tests RED.
- [ ] Reject CSS escapes and suspicious functional values in resource-capable presentation attributes, then use `PNGSurface.convert(..., url_fetcher=deny_fetch)`.
- [ ] Run SVG tests GREEN.

### Task 4: Streaming XLSX Coordinate Validation

**Files:**
- Modify: `backend/tools/read_file/spreadsheet_parser.py`
- Test: `tests/test_read_file_spreadsheet.py`

**Interfaces:**
- `_validate_xlsx_bounds` stream-validates every worksheet row and cell coordinate before downstream parsing.

- [ ] Create a valid XLSX, rewrite worksheet XML to forge `<dimension ref="A1">` while retaining `<row r="200001"><c r="A200001">`, and assert `SPREADSHEET_INVALID` before MarkItDown.
- [ ] Run RED.
- [ ] Replace worksheet `fromstring` traversal with defusedxml `iterparse`; validate positive numeric row indexes and full A1 cell coordinates against row/column caps while counting cells and clearing elements.
- [ ] Run spreadsheet tests GREEN.

### Task 5: Complete Attestation and Worker Gates

**Files:**
- Modify: `backend/config.py`
- Modify: `backend/main.py`
- Modify: `scripts/paddle_ocr_preflight.py`
- Test: `tests/test_main_lifespan_wiring.py`
- Test: `tests/test_paddle_ocr_startup.py`

**Interfaces:**
- Add `OCR_ATTESTATION_SCHEMA_VERSION = 1` and `schemaVersion` in generated attestations.
- Require `gpuName` containing `Thor` and Transformers `>=5.10.0,<6.0`.

- [ ] Add synthetic missing/old schema, H100 GPU, and Transformers 6.0 rejection tests for reuse and backend health.
- [ ] Add direct startup validation tests for OCR enabled with configured or `WEB_CONCURRENCY` workers above one.
- [ ] Run RED.
- [ ] Add shared-equivalent predicates in preflight/backend, emit schema version, parse package version safely, add `OCRServiceConfig.enabled`, and invoke worker validation at app startup and direct `__main__` startup.
- [ ] Run attestation/startup tests GREEN.

### Task 6: Workload-scoped tegrastats Aggregation

**Files:**
- Modify: `scripts/benchmark_read_file_v7.py`
- Test: `tests/test_read_file_v7_integration.py`

**Interfaces:**
- Produce report `jetsonTelemetry` with `sampleCount`, bounded raw samples, and explicit parsed aggregates/high-water fields.

- [ ] Add fake tegrastats process/reader tests proving start-before-case, stop-after-case, multi-sample parsing, and maxima for RAM, temperatures, and power.
- [ ] Run RED.
- [ ] Replace `_optional_jetson_telemetry` with an owned workload sampler using `Popen`, a reader thread, bounded samples, deterministic termination, and aggregate parsing.
- [ ] Run benchmark tests and smoke GREEN.

### Task 7: Agent Password Contract and Final Evidence

**Files:**
- Modify: `backend/tools/file_ops.py`
- Test: `tests/test_read_file_routing.py`
- Modify: `.superpowers/sdd/tasks-2-11-report.md`

**Interfaces:**
- `password` documentation states encrypted PDF only.

- [ ] Add a documentation assertion rejecting Office-password claims.
- [ ] Run RED, narrow the docstring, and run GREEN.
- [ ] Run all exact reproductions and focused tests.
- [ ] Run Python compile, shell syntax, active top-level suite attribution, live Thor routes/20-request soak, benchmark smoke, and missing-corpus negative acceptance.
- [ ] Append commands, counts, live/benchmark results, and remaining concerns to `.superpowers/sdd/tasks-2-11-report.md`.
