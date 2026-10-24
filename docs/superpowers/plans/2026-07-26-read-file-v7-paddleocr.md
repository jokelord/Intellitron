# Read File v7 PaddleOCR-VL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the Agent `read_file` tool into a secure, cacheable document reader backed by the official PaddleOCR-VL-1.6 full Pipeline, with an isolated uv environment and graceful backend startup behavior.

**Architecture:** Keep `backend.tools.file_ops.read_file` as the Agent-facing string facade. Route document work through focused modules under `backend/tools/read_file/`; run the official PaddleX service in `.paddle-ocr` and communicate only through `127.0.0.1:30001/layout-parsing`. P0 must prove the full Transformers Pipeline runs on Jetson Thor before any P1-P4 business implementation starts.

**Tech Stack:** Python 3.12, uv, PaddleOCR 3.6.x, PaddleX 3.6.x, Transformers 5.10+, CUDA 13.0 AArch64 PyTorch, FastAPI/httpx, PyMuPDF, Pillow, MarkItDown, LibreOffice, SQLite, pytest.

## Global Constraints

- Target hardware is NVIDIA Jetson Thor, Ubuntu 24.04, AArch64, SM110a, 128 GB UMA.
- `.paddle-ocr` must not use `--system-site-packages` and must not contain PaddlePaddle, SGLang, or vLLM.
- Use the official complete PaddleOCR-VL-1.6 Pipeline; standalone VLM inference is forbidden.
- PaddleOCR must satisfy `>=3.6.0,<3.7.0`; Transformers must satisfy `>=5.10.0,<6.0`.
- OCR binds only `127.0.0.1:30001` and uses `engine=transformers` on CUDA
  device 0. The VLM uses BF16/SDPA; `PP-DocLayoutV3`, orientation, and UVDoc
  use the approved CUDA FP32/eager compatibility exception because their
  official Transformers paths cross NumPy boundaries that cannot consume
  BF16 tensors.
- PaddleX 3.6.1 only constructs UVDoc when the nested initialization switch is
  true. Keep it initialized, but require every project-owned normal request to
  explicitly send `useDocUnwarping=false`; only distorted retries send true.
- P0 failure stops P1-P4. Do not substitute Paddle Engine, CPU, SGLang, vLLM, or standalone Transformers VLM.
- `.xls` and `.ods` receive explicit placeholder errors only. Do not implement LibreOffice Calc conversion.
- `.doc/.docx/.ppt/.pptx` remain in scope through LibreOffice Writer/Impress to PDF.
- Missing `start_line` and `end_line` returns the full result without automatic truncation.
- Preserve structured tool calls and user-role observations in Agent memory. Do not alter prefix-cache history behavior.
- Do not modify third-party package source.
- Do not run Git commands unless the user separately grants permission. Commit steps are intentionally replaced by verification checkpoints.

## File Map

**Environment and startup**

- Modify `.gitignore`: ignore `.paddle-ocr/` and generated OCR runtime attestations.
- Create `requirements/paddle-ocr/requirements.in`: direct OCR environment constraints.
- Create `requirements/paddle-ocr/requirements.lock`: exact hash-pinned AArch64 production resolution.
- Create `config/paddleocr-vl-thor.yaml`: exported PaddleOCR 3.6.x Pipeline config with reviewed overrides.
- Create `scripts/paddle_ocr_preflight.py`: architecture, dependency, config, API, canary, and attestation checks.
- Create `scripts/2_start_OCR_LM.sh`: official PaddleX service lifecycle.
- Modify `scripts/_workspace_paths.sh`: shared `.paddle-ocr` path helpers.
- Modify `scripts/3_start_backend.sh`: non-destructive OCR availability warning.

**Backend configuration and facade**

- Modify `requirements_for_sglang.txt`: bounded non-model document-processing dependencies only.
- Modify `config/config-server.yaml`: OCR endpoint configuration.
- Modify `config/config-misc.yaml`: `read_file` limits, security, cache, and resource settings.
- Modify `backend/config.py`: `OCRServiceConfig` and `ReadFileConfig`.
- Modify `backend/tools/file_ops.py`: retain the public facade and move text reading behind the document service.
- Modify `backend/main.py`: nested OCR diagnostics in `/health` without degrading non-OCR backend startup.

**Read-file package**

- Create `backend/tools/read_file/__init__.py`: public package exports.
- Create `backend/tools/read_file/models.py`: internal immutable result records.
- Create `backend/tools/read_file/errors.py`: stable error enum and exception.
- Create `backend/tools/read_file/text_reader.py`: existing encoding detection and line rendering.
- Create `backend/tools/read_file/router.py`: route classification only.
- Create `backend/tools/read_file/mime_detector.py`: extension and magic classification.
- Create `backend/tools/read_file/file_validator.py`: path, size, archive, image, and PDF safety checks.
- Create `backend/tools/read_file/office_renderer.py`: Writer/Impress conversion only.
- Create `backend/tools/read_file/pdf_chunker.py`: PDF validation, bounded overlapping chunks, and source-page maps.
- Create `backend/tools/read_file/image_normalizer.py`: safe image/frame/page normalization.
- Create `backend/tools/read_file/temporary_object_server.py`: loopback one-time object URLs.
- Create `backend/tools/read_file/paddleocr_client.py`: official API request and response validation.
- Create `backend/tools/read_file/result_assembler.py`: provenance Markdown and partial-failure merge.
- Create `backend/tools/read_file/spreadsheet_parser.py`: MarkItDown `.xlsx` parsing.
- Create `backend/tools/read_file/spreadsheet_media_extractor.py`: OOXML media-anchor extraction.
- Create `backend/tools/read_file/cache.py`: SQLite/WAL content-addressed cache.
- Create `backend/tools/read_file/resource_guard.py`: bounded queue and UMA admission.
- Create `backend/tools/read_file/service.py`: end-to-end orchestration.

**Tests**

- Create `tests/test_paddle_ocr_startup.py`.
- Create `tests/test_read_file_models.py`.
- Create `tests/test_read_file_routing.py`.
- Create `tests/test_read_file_validation.py`.
- Create `tests/test_read_file_office_pdf.py`.
- Create `tests/test_read_file_images.py`.
- Create `tests/test_read_file_paddleocr_client.py`.
- Create `tests/test_read_file_spreadsheet.py`.
- Create `tests/test_read_file_cache_resource.py`.
- Create `tests/test_read_file_v7_integration.py`.
- Modify `tests/test_file_ops.py`.
- Modify `tests/test_get_tools_no_load_images.py`.
- Modify `tests/test_main_lifespan_wiring.py` or add a focused health test if that file's fixtures are unsuitable.

---

### Task 1: P0 Thor Environment and Full-Pipeline Gate

**Files:**
- Modify: `.gitignore`
- Create: `requirements/paddle-ocr/requirements.in`
- Create: `requirements/paddle-ocr/requirements.lock`
- Create: `scripts/paddle_ocr_preflight.py`
- Create: `scripts/2_start_OCR_LM.sh`
- Modify: `scripts/_workspace_paths.sh`
- Create after official export: `config/paddleocr-vl-thor.yaml`
- Test: `tests/test_paddle_ocr_startup.py`

**Interfaces:**
- Produces: `get_shared_paddle_ocr_python(project_root: str) -> str` shell helper output.
- Produces: `paddle_ocr_preflight.py check-env --config PATH` exit 0 only for valid AArch64 CUDA Transformers setup.
- Produces: `paddle_ocr_preflight.py probe --url URL` exit 0 only for the PaddleX layout API.
- Produces: `paddle_ocr_preflight.py canary --url URL --attestation PATH` atomic JSON attestation.
- Produces: a live official `POST /layout-parsing` service on loopback port 30001.

- [ ] **Step 1: Write startup contract tests**

```python
def test_ocr_requirements_forbid_conflicting_engines():
    text = Path("requirements/paddle-ocr/requirements.in").read_text()
    assert 'paddleocr[doc-parser]>=3.6.0,<3.7.0' in text
    assert 'paddlex[serving]>=3.6.0,<3.7.0' in text
    assert 'transformers>=5.10.0,<6.0' in text
    assert "paddlepaddle" not in text.lower()
    assert "sglang" not in text.lower()
    assert "vllm" not in text.lower()


def test_start_script_is_loopback_and_non_destructive():
    text = Path("scripts/2_start_OCR_LM.sh").read_text()
    assert "127.0.0.1" in text
    assert "30001" in text
    assert "--engine" in text and "transformers" in text
    assert "kill -KILL" not in text
    assert "pkill" not in text
```

- [ ] **Step 2: Run tests and confirm the missing-artifact failure**

Run: `.sglang/bin/pytest tests/test_paddle_ocr_startup.py -v`

Expected: FAIL because requirements, preflight, and startup files do not exist.

- [ ] **Step 3: Add the direct constraints and path helpers**

`requirements/paddle-ocr/requirements.in` must contain:

```text
--extra-index-url https://download.pytorch.org/whl/cu130
paddleocr[doc-parser]>=3.6.0,<3.7.0
paddlex[serving]>=3.6.0,<3.7.0
transformers>=5.10.0,<6.0
safetensors>=0.7.0
torch==2.11.0+cu130
torchvision==0.26.0+cu130
httpx>=0.28.0,<1.0
PyYAML>=6.0,<7.0
```

Add shell helpers that resolve `$SHARED_WORKSPACE_ROOT/.paddle-ocr/bin/python` and `/bin/activate` without invoking Git.

- [ ] **Step 4: Implement preflight subcommands**

Use `argparse` and return nonzero for every failed invariant. Environment checks must include:

```python
checks = {
    "architecture": platform.machine() == "aarch64",
    "cuda_available": torch.cuda.is_available(),
    "transformers_version": Version(transformers.__version__) >= Version("5.10.0"),
    "paddle_absent": importlib.util.find_spec("paddle") is None,
    "sglang_absent": importlib.util.find_spec("sglang") is None,
    "vllm_absent": importlib.util.find_spec("vllm") is None,
}
```

`probe` must fetch `/openapi.json` and require a `POST /layout-parsing`
operation. `canary` must run content-aware image, one-page PDF, and rotated
fixtures with `useDocUnwarping=false`. It must also run the pinned official
photographed/warped fixture with both false and true, require materially correct
expected text from true without expected-phrase regression, and validate
DocPreprocessor execution evidence before atomically writing the attestation.

- [ ] **Step 5: Implement the non-destructive OCR startup script**

The script must:

```bash
set -euo pipefail
OCR_HOST="${OCR_HOST:-127.0.0.1}"
OCR_PORT="${OCR_PORT:-30001}"
OCR_URL="http://${OCR_HOST}:${OCR_PORT}"
```

Serialize startup with `flock`. Existing service reuse requires a fresh
attestation matching config/manifest hashes, PID start identity, cmdline,
executable, and listener socket ownership; a mere compatible OpenAPI response
is insufficient. For a new service, run `paddlex --serve --pipeline
"$PIPELINE_CONFIG" --engine transformers --device gpu:0 --host "$OCR_HOST"
--port "$OCR_PORT"` as a child, wait for readiness, run canaries, and use a
bounded TERM-only shutdown for `$OCR_PID`.

- [ ] **Step 6: Create the isolated environment and install a provisional resolution**

Run:

```bash
uv venv .paddle-ocr --python 3.12
uv pip install --python .paddle-ocr/bin/python -r requirements/paddle-ocr/requirements.in
```

Expected: installation succeeds using AArch64 wheels. If uv attempts an x86_64 artifact or source-builds a required native dependency unexpectedly, record the package and stop P0.

- [ ] **Step 7: Export and minimally edit the official Pipeline config**

Run from the OCR environment:

```bash
.paddle-ocr/bin/paddlex --get_pipeline_config PaddleOCR-VL
```

Move the generated official file to `config/paddleocr-vl-thor.yaml`. Apply only the reviewed v1.6/Transformers/BF16/CUDA/SDPA/layout/chart/image-block/queue/ignore-label/Serving overrides. Do not invent keys absent from the exported 3.6.x schema.

- [ ] **Step 8: Generate the exact lock**

Run:

```bash
uv pip compile requirements/paddle-ocr/requirements.in --python-version 3.12 --python-platform aarch64-manylinux_2_28 --generate-hashes --emit-index-url -o requirements/paddle-ocr/requirements.lock
```

Then recreate or verify the environment from the lock with `uv pip sync
--python .paddle-ocr/bin/python
requirements/paddle-ocr/requirements.lock`. At least once, use a new empty uv
cache and new environment so this is a lock-only cold reproduction. Confirm the
lock retains `https://download.pytorch.org/whl/cu130`, the NVIDIA wheel tag is
`manylinux2014_sbsa`, its CUDA library is ELF AArch64, and CUDA matrix execution
succeeds.

- [ ] **Step 9: Run static and environment tests**

Run:

```bash
.sglang/bin/pytest tests/test_paddle_ocr_startup.py -v
.paddle-ocr/bin/python scripts/paddle_ocr_preflight.py check-env --config config/paddleocr-vl-thor.yaml
```

Expected: all tests PASS and every preflight check reports true.

- [ ] **Step 10: Start the service and execute both hardware canaries**

Run: `bash scripts/2_start_OCR_LM.sh`

Expected: the official full service starts on `127.0.0.1:30001`; image, PDF,
rotation, and photographed/warped comparison canaries return materially correct
expected text; the attestation reports `aarch64`, `transformers`, `cuda:0`,
all four immutable models, process/listener identity, fixture provenance, and
`canaryPassed=true`; measured peak UMA is recorded.

- [ ] **Step 11: Enforce the P0 gate**

If any actual Pipeline submodel uses PaddlePaddle/CPU, the API cannot process both canaries, or memory reserve cannot be maintained, stop here and write a blocker report under `docs/`. Do not start Task 2.

Verification checkpoint: P0 is independently usable through the official API and no business `read_file` files have been modified.

---

### Task 2: Typed Configuration and Backend OCR Probe

**Files:**
- Modify: `requirements_for_sglang.txt`
- Modify: `config/config-server.yaml`
- Modify: `config/config-misc.yaml`
- Modify: `backend/config.py`
- Modify: `scripts/3_start_backend.sh`
- Test: `tests/test_paddle_ocr_startup.py`
- Test: `tests/test_runtime_config_limits.py`

**Interfaces:**
- Produces: `AppConfig.ocr: OCRServiceConfig`.
- Produces: `AppConfig.read_file: ReadFileConfig`.
- Produces: backend startup warning without changing process ownership.

- [ ] **Step 1: Add failing config and script tests**

```python
def test_read_file_v7_defaults_are_loaded():
    cfg = ConfigLoader().load(reload=True)
    assert cfg.ocr.host == "127.0.0.1"
    assert cfg.ocr.port == 30001
    assert cfg.read_file.pdf_chunk_pages == 24
    assert cfg.read_file.pdf_chunk_overlap_pages == 1
    assert cfg.read_file.main_llm_memory_reserve_gb == 16
    assert cfg.read_file.ocr_queue_limit == 2


def test_backend_script_never_kills_ocr_port():
    text = Path("scripts/3_start_backend.sh").read_text()
    ocr_block = text[text.index("OCR_SERVICE_URL"):text.index("cleanup()")]
    assert "check_and_kill_port 30001" not in ocr_block
    assert "2_start_OCR_LM.sh" in ocr_block
```

- [ ] **Step 2: Verify failure**

Run: `.sglang/bin/pytest tests/test_runtime_config_limits.py tests/test_paddle_ocr_startup.py -v`

Expected: FAIL because `ocr` and `read_file` config objects do not exist.

- [ ] **Step 3: Add typed dataclasses and parsers**

Define `OCRServiceConfig` with host, port, base URL, timeouts, Pipeline path, and attestation path. Define `ReadFileConfig` with every PRD limit and TTL. Add both to `AppConfig` and parse them from server/misc YAML without hardcoding operational values in consumers.

- [ ] **Step 4: Add and install bounded backend document dependencies**

Append only the non-model packages below to `requirements_for_sglang.txt`:

```text
PyMuPDF>=1.26.0,<2.0
markitdown[xlsx]>=0.1.6,<0.2
python-magic>=0.4.27,<0.5
pillow-heif>=1.1.0,<2.0
CairoSVG>=2.8.0,<3.0
defusedxml>=0.7.1,<1.0
ImageHash>=4.3.2,<5.0
```

Install these packages with `uv pip install --python .sglang/bin/python` and verify the resolver does not change the existing SGLang/Transformers/Torch pins. System prerequisites are `libmagic1`, LibreOffice Writer/Impress, and the local SVG rasterizer libraries; report a missing system package rather than silently disabling its format.

- [ ] **Step 5: Add the backend startup probe**

Before backend launch, use `curl --max-time 2` against `/openapi.json`. Print one of: healthy OCR, missing OCR with the exact launch command, or incompatible listener. Never pass the OCR port to `check_and_kill_port` and never add OCR ownership to `cleanup()`.

- [ ] **Step 6: Verify configuration and startup policy**

Run: `.sglang/bin/pytest tests/test_runtime_config_limits.py tests/test_paddle_ocr_startup.py -v`

Expected: PASS.

Verification checkpoint: backend can start with or without OCR and exposes typed settings to later tasks.

---

### Task 3: Result Models, Errors, Text Reader, and Agent Facade

**Files:**
- Create: `backend/tools/read_file/__init__.py`
- Create: `backend/tools/read_file/models.py`
- Create: `backend/tools/read_file/errors.py`
- Create: `backend/tools/read_file/text_reader.py`
- Create: `backend/tools/read_file/service.py`
- Modify: `backend/tools/file_ops.py`
- Test: `tests/test_read_file_models.py`
- Modify: `tests/test_file_ops.py`
- Modify: `tests/test_get_tools_no_load_images.py`

**Interfaces:**
- Produces: `ReadFileResult`, `PageResult`, `SheetResult`, `EmbeddedImageResult`, `ParseError` dataclasses.
- Produces: `ReadFileErrorCode` and `ReadFileError`.
- Produces: `read_text(path, encoding_override) -> tuple[str, str]`.
- Produces: `render_line_range(text, start_line, end_line, header) -> str`.
- Produces: `ReadFileService.read(file_path: str, start_line: int | None = None, end_line: int | None = None, encoding: str | None = None, password: str | None = None, document_mode: str = "auto", recognize_seals: bool = False, check_abort: Callable[[], bool] | None = None) -> str`.

- [ ] **Step 1: Write failing model and facade tests**

```python
def test_partial_result_remains_successful():
    result = ReadFileResult(
        success=True,
        markdown="ok",
        source_path="/home/zk/example.pdf",
        source_format="pdf",
        normalized_format=None,
        engine="PaddleOCR-VL-1.6",
        model_version="1.6",
        pages=[],
        spreadsheet_sheets=[],
        warnings=["page 2 failed"],
        errors=[ParseError(code="OCR_PAGE_FAILED", message="page 2", source_page=2)],
        cache_hit=False,
        elapsed_ms=10,
    )
    assert result.success is True


def test_binary_options_are_exposed_but_abort_is_hidden():
    tool = next(t for t in _get_tools(enable_wikipedia=False) if t.name == "read_file")
    assert "password" in tool.inputs
    assert "document_mode" in tool.inputs
    assert "recognize_seals" in tool.inputs
    assert "check_abort" not in tool.inputs
```

- [ ] **Step 2: Verify failure**

Run: `.sglang/bin/pytest tests/test_read_file_models.py tests/test_file_ops.py tests/test_get_tools_no_load_images.py -v`

Expected: model imports and new schema assertions FAIL.

- [ ] **Step 3: Implement immutable result and error contracts**

Use dataclasses with `to_dict()`/`from_dict()` for cache serialization. Include every PRD field and explicit source page/sheet/image metadata. `ReadFileError.__str__` must include `[read_file failed]`, `code`, and a safe message.

- [ ] **Step 4: Move text helpers without changing behavior**

Move encoding detection to `text_reader.py` and keep `file_ops.py` imports/aliases needed by existing tests. Preserve BOM handling, CJK fallbacks, line numbering, invalid encoding fallback, and full output when bounds are absent.

- [ ] **Step 5: Add the facade signature and initial text-only service route**

`file_ops.read_file` delegates all parameters to a lazily constructed `ReadFileService`. At this task, `ReadFileService` handles text and returns `UNSUPPORTED_FORMAT` for document routes until their modules land. Do not instantiate config or network clients at import time.

- [ ] **Step 6: Verify regressions and schema**

Run: `.sglang/bin/pytest tests/test_read_file_models.py tests/test_file_ops.py tests/test_get_tools_no_load_images.py tests/test_write_memory_tool_calls.py -v`

Expected: PASS, including historical tool-trace tests.

Verification checkpoint: current text behavior and Agent memory contracts are preserved behind the new package boundary.

---

### Task 4: MIME, Path, Archive, and Format Routing

**Files:**
- Create: `backend/tools/read_file/router.py`
- Create: `backend/tools/read_file/mime_detector.py`
- Create: `backend/tools/read_file/file_validator.py`
- Test: `tests/test_read_file_routing.py`
- Test: `tests/test_read_file_validation.py`

**Interfaces:**
- Produces: `FileRoute` enum values `TEXT`, `VISUAL_OFFICE`, `PDF`, `IMAGE`, `XLSX`, `PLACEHOLDER_SPREADSHEET`.
- Produces: `detect_file_type(path: Path) -> DetectedFileType`.
- Produces: `validate_source(path, config, password=None) -> ValidatedSource`.
- Consumes: `ReadFileConfig`, `ReadFileError`.

- [ ] **Step 1: Write route and safety tests**

Cover every extension in the PRD, `.xls/.ods` placeholder classification, unknown binary rejection, unknown text acceptance, fake `.pdf` rejection, symlink escape, allowlisted symlink target, oversized source, and ZIP member count/ratio/expanded-size rejection.

- [ ] **Step 2: Verify failure**

Run: `.sglang/bin/pytest tests/test_read_file_routing.py tests/test_read_file_validation.py -v`

Expected: FAIL because classifiers and validators do not exist.

- [ ] **Step 3: Implement extension plus magic detection**

Use `python-magic` as the primary MIME signal and conservative signatures as a deterministic test fallback. Mismatch policy must reject executable/binary masquerading and permit documented Office ZIP containers only after OOXML member validation.

- [ ] **Step 4: Implement resolved-path and archive validation**

Resolve before allowlist comparison with `Path.is_relative_to`. Never compare raw string prefixes. Inspect ZIP central-directory metadata before extraction; reject encrypted or suspicious entries, absolute paths, `..`, symlinks, and configured bomb thresholds.

- [ ] **Step 5: Implement pure route classification**

`router.py` may inspect only `DetectedFileType`; it must not open, parse, convert, or call OCR. `.xls/.ods` return `PLACEHOLDER_SPREADSHEET`.

- [ ] **Step 6: Verify routing and validation**

Run: `.sglang/bin/pytest tests/test_read_file_routing.py tests/test_read_file_validation.py -v`

Expected: PASS.

Verification checkpoint: unsafe input is rejected before converters or OCR receive bytes.

---

### Task 5: LibreOffice Writer/Impress and PDF Chunking

**Files:**
- Create: `backend/tools/read_file/office_renderer.py`
- Create: `backend/tools/read_file/pdf_chunker.py`
- Test: `tests/test_read_file_office_pdf.py`

**Interfaces:**
- Produces: `render_office_to_pdf(source, config, check_abort) -> TemporaryPDF`.
- Produces: `inspect_pdf(path, password, config) -> PDFInfo`.
- Produces: `iter_pdf_chunks(path, info, config) -> Iterator[PDFChunk]`.
- `PDFChunk` carries `path`, `source_pages`, and overlap metadata.

- [ ] **Step 1: Write failing subprocess and chunk-map tests**

Assert exact Writer/Impress filters, unique `UserInstallation`, process-group creation, timeout group termination, no Calc filter, PDF validation, 24-page chunks, one-page overlap, 64 MB splitting, and source-page deduplication metadata.

- [ ] **Step 2: Verify failure**

Run: `.sglang/bin/pytest tests/test_read_file_office_pdf.py -v`

Expected: FAIL.

- [ ] **Step 3: Implement Office rendering**

Build argument arrays without `shell=True`. Use `start_new_session=True`, `communicate(timeout=config.office_timeout_seconds)`, `os.killpg` on timeout, and a unique temp/profile/output directory. Accept only `.doc/.docx/.ppt/.pptx`; reject `.xls/.ods` before subprocess creation.

- [ ] **Step 4: Implement PDF inspection and password handling**

Use PyMuPDF only for integrity, encryption, page count, splitting, and metadata. Authenticate the password in memory. Never call `page.get_text()` for final content.

- [ ] **Step 5: Implement adaptive chunk production**

Emit initial 24-page/64 MB chunks with one-page overlap and stable original page maps. Add helper functions that produce 8-page and single-page retry chunks without changing source numbering.

- [ ] **Step 6: Verify Office/PDF behavior**

Run: `.sglang/bin/pytest tests/test_read_file_office_pdf.py -v`

Expected: PASS.

Verification checkpoint: Office files become validated PDFs and PDFs become safe, source-mapped OCR units.

---

### Task 6: Safe Image Normalization

**Files:**
- Create: `backend/tools/read_file/image_normalizer.py`
- Test: `tests/test_read_file_images.py`

**Interfaces:**
- Produces: `normalize_image_document(source, config, document_mode, check_abort) -> list[NormalizedPage]`.
- `NormalizedPage` carries normalized path/bytes, source page/frame, SHA-256, dimensions, and warnings.

- [ ] **Step 1: Write failing image tests**

Cover EXIF rotation, RGB conversion, alpha background, BMP/WEBP/TIFF/GIF frame order, GIF perceptual dedupe, HEIC/AVIF/JP2 plugin errors, PBM/PGM/PPM, ICO, max dimensions/pixels/decoded bytes, aspect-ratio scaling, and malicious SVG external/script/file references.

- [ ] **Step 2: Verify failure**

Run: `.sglang/bin/pytest tests/test_read_file_images.py -v`

Expected: FAIL.

- [ ] **Step 3: Implement raster image limits before full decode**

Inspect dimensions/frame counts first, set `Image.MAX_IMAGE_PIXELS`, estimate decoded memory, apply EXIF transpose, preserve aspect ratio, and use bounded worker count from config.

- [ ] **Step 4: Implement multi-page/frame normalization and dedupe**

Normalize each retained page/frame into a unique temporary file. Deduplicate consecutive GIF frames by perceptual hash while preserving original frame metadata.

- [ ] **Step 5: Implement restricted SVG rasterization**

Parse XML with hardened settings, reject forbidden elements/attributes/URLs before calling the rasterizer, disable resource fetching, and enforce output dimensions and pixel limits.

- [ ] **Step 6: Verify normalization**

Run: `.sglang/bin/pytest tests/test_read_file_images.py -v`

Expected: PASS.

Verification checkpoint: every accepted image reaches OCR as a bounded PNG/JPEG/TIFF page.

---

### Task 7: Temporary Object Service and Official PaddleX Client

**Files:**
- Create: `backend/tools/read_file/temporary_object_server.py`
- Create: `backend/tools/read_file/paddleocr_client.py`
- Test: `tests/test_read_file_paddleocr_client.py`

**Interfaces:**
- Produces: `TemporaryObjectServer.register(path, ttl_seconds) -> OneTimeObjectURL`.
- Produces: `PaddleOCRClient.parse_file(path, file_type, options, check_abort) -> list[OCRPage]`.
- Produces: `PaddleOCRClient.probe() -> OCRServiceStatus`.

- [ ] **Step 1: Write failing transport and protocol tests**

Assert Base64 at `<=16 MB`, loopback opaque URL above threshold, single-use GET, TTL expiry, traversal rejection, exact request switches, `errorCode` validation, page count/order, non-empty `markdown.text`, no image payloads, and retry only for connection/timeout/5xx.

- [ ] **Step 2: Verify failure**

Run: `.sglang/bin/pytest tests/test_read_file_paddleocr_client.py -v`

Expected: FAIL.

- [ ] **Step 3: Implement the token registry and loopback server**

Bind `127.0.0.1` on a configured or ephemeral port, map `secrets.token_urlsafe(32)` to resolved files, atomically consume tokens on successful open, and remove expired entries. Never serve arbitrary paths.

- [ ] **Step 4: Implement exact official API payloads**

Include `useDocOrientationClassify`, `useDocUnwarping`, `useLayoutDetection`, `useChartRecognition`, `useSealRecognition`, `useOcrForImageBlock`, `formatBlockContent`, `mergeLayoutBlocks`, `layoutShapeMode`, `prettifyMarkdown`, `showFormulaNumber`, `restructurePages`, `mergeTables`, `relevelTitles`, `returnMarkdownImages=False`, and `visualize=False`.

- [ ] **Step 5: Validate responses and bounded retries**

Reject malformed JSON, nonzero `errorCode`, missing result arrays, count mismatch, missing text, and unexpected order. Preserve full failed attempts internally; never return partial malformed service text.

- [ ] **Step 6: Verify the client**

Run: `.sglang/bin/pytest tests/test_read_file_paddleocr_client.py -v`

Expected: PASS.

Verification checkpoint: backend has a tested provider-neutral document client that only speaks the official PaddleX API.

---

### Task 8: Page Assembly and Unified Visual Service Path

**Files:**
- Create: `backend/tools/read_file/result_assembler.py`
- Modify: `backend/tools/read_file/service.py`
- Test: `tests/test_read_file_v7_integration.py`

**Interfaces:**
- Produces: `assemble_pages(source, pages, metadata) -> ReadFileResult`.
- Consumes all P1 conversion and OCR interfaces.

- [ ] **Step 1: Write failing orchestration tests**

Use fake validators/converters/client to assert PDF, Word, PowerPoint, image, multi-page TIFF/GIF routes; provenance comments; slide numbers; overlap dedupe; 24-to-8-to-1-to-unwarping retry; failed-page placeholders; continuation after one-page failure; all-page failure; abort boundaries; and final line slicing/full-output behavior.

- [ ] **Step 2: Verify failure**

Run: `.sglang/bin/pytest tests/test_read_file_v7_integration.py -v`

Expected: FAIL because service orchestration is incomplete.

- [ ] **Step 3: Implement page assembly**

Generate stable comments with source page/slide, source format, engine, model version, and Pipeline version. Strip temporary image links and never return normalized paths.

- [ ] **Step 4: Implement visual routing and adaptive failure recovery**

The service calls validation, route classification, conversion/normalization, OCR, and assembly in order. Check abort before each new chunk/retry. Do not inspect native PDF text and do not add another OCR backend.

- [ ] **Step 5: Implement safe facade rendering**

Expected failures become a string containing the stable code and safe message. Partial results include warnings and page errors. Apply line slicing only after the complete result has been assembled.

- [ ] **Step 6: Verify P1 and regressions**

Run:

```bash
.sglang/bin/pytest tests/test_read_file_v7_integration.py tests/test_file_ops.py tests/test_write_memory_tool_calls.py tests/test_get_tools_no_load_images.py -v
```

Expected: PASS.

Verification checkpoint: P1 supports all visual routes while preserving the Agent contract.

---

### Task 9: XLSX MarkItDown and Embedded Image Anchors

**Files:**
- Create: `backend/tools/read_file/spreadsheet_parser.py`
- Create: `backend/tools/read_file/spreadsheet_media_extractor.py`
- Modify: `backend/tools/read_file/service.py`
- Test: `tests/test_read_file_spreadsheet.py`

**Interfaces:**
- Produces: `parse_xlsx(path) -> list[SheetResult]`.
- Produces: `extract_embedded_images(path, limits) -> list[EmbeddedImageSource]`.
- Consumes: image normalizer and PaddleOCR client.

- [ ] **Step 1: Write failing spreadsheet tests**

Create OOXML fixtures in tests and cover sheet order, Chinese names, merged cells, formulas with cached values, drawing relationships, one-cell/two-cell anchors, repeated image SHA dedupe, image OCR insertion, malformed relationship rejection, and `.xls/.ods` placeholder responses that never invoke LibreOffice.

- [ ] **Step 2: Verify failure**

Run: `.sglang/bin/pytest tests/test_read_file_spreadsheet.py -v`

Expected: FAIL.

- [ ] **Step 3: Implement MarkItDown `.xlsx` parsing**

Use the narrow local conversion API and return ordered sheet Markdown. Do not use MarkItDown for Word, PowerPoint, or PDF.

- [ ] **Step 4: Implement safe OOXML media mapping**

Read only the validated ZIP entries under `xl/media`, drawings, relationships, and worksheets. Resolve relationship targets inside the archive, parse anchors into A1 ranges, and deduplicate media by SHA-256.

- [ ] **Step 5: OCR and insert embedded media**

OCR each unique bitmap once, then insert its Markdown under `### 嵌入图片：Sheet!B4:F18` plus the PRD metadata comment at the corresponding sheet section.

- [ ] **Step 6: Verify P2**

Run: `.sglang/bin/pytest tests/test_read_file_spreadsheet.py tests/test_read_file_v7_integration.py -v`

Expected: PASS.

Verification checkpoint: `.xlsx` is complete and `.xls/.ods` remain explicit non-implemented placeholders.

---

### Task 10: Cache, Resource Admission, and Cancellation

**Files:**
- Create: `backend/tools/read_file/cache.py`
- Create: `backend/tools/read_file/resource_guard.py`
- Modify: `backend/tools/read_file/service.py`
- Test: `tests/test_read_file_cache_resource.py`

**Interfaces:**
- Produces: `ReadFileCache.get(key) -> ReadFileResult | None` and `put(key, result, status)`.
- Produces: `build_cache_key(source_sha, parser_version, model_revision, config_hash, options) -> str`.
- Produces: `ResourceGuard.acquire(check_abort) -> context manager`.

- [ ] **Step 1: Write failing cache/resource tests**

Cover deterministic key changes, password plaintext exclusion, model/config invalidation, 30d/7d/1h TTLs, expired row cleanup, transaction rollback, one active/two queued behavior, third waiter `RESOURCE_BUSY`, configured available-UMA floor refusal, abort while queued, and release after exceptions.

- [ ] **Step 2: Verify failure**

Run: `.sglang/bin/pytest tests/test_read_file_cache_resource.py -v`

Expected: FAIL.

- [ ] **Step 3: Implement SQLite/WAL cache**

Create schema versioning, JSON serialization through model methods, explicit expiry, atomic transactions, and lazy stale cleanup. Store no password and write no document body to logs.

- [ ] **Step 4: Implement bounded admission**

Use a lock/condition to count one running and two queued operations. Poll abort while waiting. Check `psutil.virtual_memory().available` before admission and before each OCR chunk against the configured reserve.

- [ ] **Step 5: Wire cache and guard into orchestration**

Validate the source before cache lookup, calculate the source hash by streaming, acquire resources only on misses, cache complete/partial/failure with the correct TTL, and release in `finally`.

- [ ] **Step 6: Verify P3 core reliability**

Run: `.sglang/bin/pytest tests/test_read_file_cache_resource.py tests/test_read_file_v7_integration.py -v`

Expected: PASS.

Verification checkpoint: repeated reads reuse stable results and concurrent OCR cannot overcommit Thor.

---

### Task 11: Health Attestation, Diagnostics, and Full Regression

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/tools/read_file/paddleocr_client.py`
- Test: `tests/test_read_file_v7_integration.py`
- Modify or create focused test: `tests/test_main_lifespan_wiring.py`

**Interfaces:**
- Produces: nested `/health` `ocr` object with `status`, `pipeline`, `pipelineVersion`, `engine`, `modelDevice`, `architecture`, `cudaAvailable`, `canaryPassed`, and `queueDepth`.

- [ ] **Step 1: Write failing health tests**

Assert healthy live probe plus matching non-stale attestation, absent service degradation limited to `ocr`, stale PID/timestamp rejection, and queue depth reporting without document content.

- [ ] **Step 2: Verify failure**

Run: `.sglang/bin/pytest tests/test_main_lifespan_wiring.py tests/test_read_file_v7_integration.py -v`

Expected: FAIL because OCR diagnostics are absent.

- [ ] **Step 3: Add non-blocking diagnostics**

Read and validate the attestation, make a short live probe, query `ResourceGuard`, and return nested diagnostics. Never make `/health` wait for model inference and never mark the LLM backend unhealthy solely because OCR is down.

- [ ] **Step 4: Run all focused tests**

Run:

```bash
.sglang/bin/pytest \
  tests/test_paddle_ocr_startup.py \
  tests/test_read_file_models.py \
  tests/test_read_file_routing.py \
  tests/test_read_file_validation.py \
  tests/test_read_file_office_pdf.py \
  tests/test_read_file_images.py \
  tests/test_read_file_paddleocr_client.py \
  tests/test_read_file_spreadsheet.py \
  tests/test_read_file_cache_resource.py \
  tests/test_read_file_v7_integration.py \
  tests/test_file_ops.py \
  tests/test_get_tools_no_load_images.py \
  tests/test_write_memory_tool_calls.py -v
```

Expected: PASS.

- [ ] **Step 5: Run the complete backend suite**

Run: `.sglang/bin/pytest tests -q`

Expected: PASS with hardware-only OCR integration tests skipped unless explicitly enabled.

Verification checkpoint: no regression in Agent tools, memory serialization, startup, or backend health.

---

### Task 12: Thor Acceptance Corpus and Performance Baseline

**Files:**
- Create: `scripts/benchmark_read_file_v7.py`
- Create: `docs/read-file-v7-thor-acceptance.md`
- Test: hardware-marked cases in `tests/test_read_file_v7_integration.py`

**Interfaces:**
- Produces: JSON/Markdown benchmark report with cold/warm start, first result, total time, UMA/GPU/CPU/disk/output/retry metrics.

- [ ] **Step 1: Add hardware test markers and explicit corpus manifest**

The manifest names every PRD PDF, Word, PowerPoint, XLSX, and image case. `.xls/.ods` entries expect placeholder errors only.

- [ ] **Step 2: Implement benchmark collection**

Collect monotonic stage timings, `psutil` process/system memory, `tegrastats` or approved Jetson metrics when present, temporary disk high-water mark, output characters, and retries. Missing optional telemetry is a warning, not fabricated zero data.

- [ ] **Step 3: Run functional hardware acceptance**

Run: `RUN_PADDLE_OCR_INTEGRATION=1 .sglang/bin/pytest tests/test_read_file_v7_integration.py -m paddle_ocr_integration -v`

Expected: all available corpus cases pass; every page has content or an explicit failure marker; no overlap duplicates or temporary links appear.

- [ ] **Step 4: Run the PRD benchmark matrix**

Run: `.sglang/bin/python scripts/benchmark_read_file_v7.py --manifest docs/read-file-v7-thor-acceptance.md --output logs/read_file_v7_benchmark.json`

Expected: report includes 1/10/50/200-page PDFs, 20-page DOCX, 50-page PPTX, 20-sheet XLSX, and 100-image XLSX when fixtures are available.

- [ ] **Step 5: Run the 20-request soak test**

Expected: no OOM, model reload, CPU fallback, silent page loss, reserve violation, or sustained memory growth.

- [ ] **Step 6: Record acceptance and defer unproven optimization**

Document failures and measured values. Keep SDPA unless separate correctness, build, and memory evidence justifies FlashAttention 2.

Verification checkpoint: P4 has evidence-based Thor results rather than assumed throughput claims.

## Final Verification Checklist

- [ ] P0 full Pipeline gate passed on Thor or implementation stopped with a blocker report.
- [ ] `.paddle-ocr` is isolated, reproducible from the hash-pinned lock, and contains no forbidden engine.
- [ ] OCR and temporary object services bind loopback only.
- [ ] Backend absence warning does not kill or own port 30001.
- [ ] Current text encoding behavior and default full output remain intact.
- [ ] PDF never uses native text as final content or OCR eligibility.
- [ ] Word/PowerPoint use only Writer/Impress conversion.
- [ ] `.xlsx` uses MarkItDown and embedded image OCR.
- [ ] `.xls/.ods` contain no conversion implementation.
- [ ] Partial page failure continues and emits explicit markers.
- [ ] Cache, queue, memory reserve, abort, path, archive, image, SVG, subprocess, token, and logging tests pass.
- [ ] Canonical Agent tool-call/observation history remains unchanged.
- [ ] Full focused and backend test suites pass.
- [ ] Independent auditor reports no unresolved critical/high defects or scope violations.
