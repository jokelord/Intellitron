# Read File v7 PaddleOCR-VL Design

## Status

- Date: 2026-07-26
- Source PRD: `docs/read_file-tool-全面升级-V7.MD`
- Target: NVIDIA Jetson Thor, Ubuntu 24.04, AArch64, SM110a
- Decision: use the official full PaddleOCR-VL-1.6 Pipeline through PaddleX Basic Serving with the Transformers engine
- P0 implementation status: passed on Thor after the review-hardening gate described below

## Goals

Upgrade the Agent `read_file` tool from a text-only reader into a document reader that:

- preserves current text and source-code reading behavior;
- visually parses PDF, Word, PowerPoint, and supported image formats with the full PaddleOCR-VL-1.6 Pipeline;
- parses `.xlsx` cell structure with MarkItDown and OCRs embedded bitmap images;
- returns Markdown with page, slide, and sheet provenance;
- isolates PaddleOCR, PaddleX, Torch, and Transformers from `.sglang` in a root-level `.paddle-ocr` uv environment;
- remains safe under long documents, malformed files, cancellation, and Jetson Thor UMA pressure;
- starts the OCR service explicitly and lets the backend continue with a warning when OCR is absent.

## Explicit Scope Decisions

- `.xls` and `.ods` are recognized but not converted or parsed in this version. They return a clear placeholder error stating that conversion is planned but unavailable.
- No LibreOffice Calc code is written for `.xls` or `.ods`.
- LibreOffice Writer and Impress conversion remains in scope for `.doc`, `.docx`, `.ppt`, and `.pptx`.
- If both `start_line` and `end_line` are omitted, `read_file` returns the complete result. It does not impose an automatic 200-line or character cap.
- The default path allowlist is configurable and initially includes the project root, `~`, and system temporary directories.
- Only the model-serving stack is isolated in `.paddle-ocr`. MarkItDown, PyMuPDF, MIME detection, image normalization, and other non-model parsing dependencies remain backend dependencies in `.sglang`.
- The solution does not modify PaddleOCR, PaddleX, Transformers, or any other installed package source.

## Official Documentation Findings

The design is based on the following material reviewed on 2026-07-26:

- ModelScope model page: `https://modelscope.cn/models/PaddlePaddle/PaddleOCR-VL-1.6`
- PaddleOCR-VL usage tutorial: `https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PaddleOCR-VL.html`
- NVIDIA Blackwell tutorial: `https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PaddleOCR-VL-NVIDIA-Blackwell.html`
- Inference engine configuration: `https://www.paddleocr.ai/latest/en/version3.x/inference_deployment/local_inference/inference_engine.html`
- PaddleX serving: `https://www.paddleocr.ai/latest/en/version3.x/inference_deployment/serving/serving.html`

Relevant conclusions:

- The complete Pipeline combines layout analysis, region cropping, VLM recognition, reading-order recovery, and Markdown assembly. Running the standalone VLM is not equivalent.
- The official full service uses `paddlex --serve` and exposes `POST /layout-parsing`.
- The service response contains `result.layoutParsingResults[]`, with page Markdown at `markdown.text`.
- The Transformers engine requires `transformers>=5.10.0` in the current official engine documentation.
- Official docs recommend separate environments because Transformers, vLLM, and SGLang dependency requirements can conflict.
- The ModelScope README links PaddlePaddle GPU Linux package indexes such as `https://www.paddlepaddle.org.cn/packages/stable/cu126/`. The desktop Blackwell guide uses cu129 and validates desktop SM120 examples, not Jetson Thor SM110a.
- The Transformers route can use AArch64 CUDA 13.0 PyTorch wheels from `https://download.pytorch.org/whl/cu130` and does not require installing PaddlePaddle.
- PaddleOCR 3.6.0 is available as a platform-neutral wheel and constrains PaddleX to `>=3.6.0,<3.7.0` for its document parser extra.

Thor SM110a is not explicitly certified by the cited desktop Blackwell guide. Full Pipeline initialization and execution on Thor is therefore a P0 feasibility gate, not an assumption.

## Architecture

### Process Boundary

The system has two Python environments and one HTTP boundary:

```text
Agent / backend (.sglang)
    -> file validation, conversion, normalization, chunking, cache
    -> HTTP POST http://127.0.0.1:8118/layout-parsing
PaddleX full Pipeline service (.paddle-ocr)
    -> layout analysis + PaddleOCR-VL-1.6 recognition
    -> page-level Markdown and structured metadata
```

The backend never imports PaddleOCR, PaddleX, or the OCR environment's Transformers package. The OCR process never imports SGLang or backend LLM code.

### Agent Compatibility Facade

`backend/tools/file_ops.py::read_file()` remains the only Agent-facing entry point. This preserves:

- the existing `backend.agent.agent_runner._create_tool_wrapper()` path;
- the current string return type;
- the existing `start_line`, `end_line`, and `encoding` arguments;
- canonical assistant tool-call and user observation history required by the prefix-cache covenant.

The facade delegates to `backend/tools/read_file/service.py`. Proposed signature:

```python
def read_file(
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    encoding: str | None = None,
    password: str | None = None,
    document_mode: str = "auto",
    recognize_seals: bool = False,
    check_abort: Callable[[], bool] | None = None,
) -> str:
    ...
```

`check_abort` is hidden from the LLM schema and injected by the existing wrapper. Passwords are never logged, written to cache metadata, or included in plaintext cache keys.

### Module Boundaries

```text
backend/tools/read_file/
├── __init__.py
├── service.py
├── router.py
├── text_reader.py
├── file_validator.py
├── mime_detector.py
├── office_renderer.py
├── pdf_chunker.py
├── image_normalizer.py
├── spreadsheet_parser.py
├── spreadsheet_media_extractor.py
├── paddleocr_client.py
├── temporary_object_server.py
├── result_assembler.py
├── cache.py
├── resource_guard.py
├── models.py
└── errors.py
```

Responsibilities:

- `service.py`: orchestration and conversion of the internal result to the Agent string contract.
- `router.py`: extension/MIME classification only; no parsing.
- `text_reader.py`: current encoding detection and line rendering behavior.
- `file_validator.py`: resolved path, allowlist, size, symlink, archive, password, and structural checks.
- `mime_detector.py`: extension plus libmagic classification and mismatch policy.
- `office_renderer.py`: Writer/Impress to PDF only. Calc conversion is absent.
- `pdf_chunker.py`: page-count and byte-size bounded chunks with overlap and source-page mapping.
- `image_normalizer.py`: EXIF orientation, colorspace, frame/page expansion, deduplication, scaling, SVG rasterization, and format normalization.
- `spreadsheet_parser.py`: MarkItDown `.xlsx` conversion and sheet ordering.
- `spreadsheet_media_extractor.py`: OOXML relationships, drawing anchors, embedded bitmap extraction, and SHA-256 deduplication.
- `paddleocr_client.py`: official API request/response protocol only.
- `temporary_object_server.py`: loopback-only, opaque, one-time URLs for inputs larger than 16 MB.
- `result_assembler.py`: page/sheet/image merge, provenance comments, warnings, and failed-page placeholders.
- `cache.py`: content-addressed result persistence and TTL.
- `resource_guard.py`: OCR queue, concurrency, UMA reserve, and abort-aware admission.
- `models.py` and `errors.py`: typed internal contract and stable error codes.

## Routing and Data Flow

### Text

Known text and source-code files retain automatic encoding detection and line-number output. An unknown extension may use the text route only when MIME/content checks indicate text and the payload does not look binary.

### Word and PowerPoint

```text
.doc/.docx -> isolated LibreOffice Writer -> validated PDF -> PDF route
.ppt/.pptx -> isolated LibreOffice Impress -> validated PDF -> PDF route
```

Every conversion uses a unique temporary directory and LibreOffice profile. The subprocess has a hard timeout and its own process group. The backend validates return status, output existence, PDF readability, and positive page count before OCR.

### PDF

Every PDF is visually parsed. Native PDF text may be used for neither final content nor OCR eligibility.

The initial chunk policy is 24 pages, 64 MB, and one overlap page. The merge stage maps service results to original page numbers and removes duplicated overlap pages. A failed chunk retries at 8 pages, then one page, then one page with unwarping. A final page failure produces the PRD placeholder and processing continues.

### Images

Supported inputs are normalized to RGB PNG, JPEG, or TIFF as appropriate. EXIF orientation is applied. TIFF and GIF become ordered pages/frames, with duplicate GIF frames removed by perceptual hash. Image dimensions, total pixels, estimated decoded bytes, page/frame counts, and normalized output size are bounded before OCR.

SVG is parsed and rasterized locally under a no-network resource policy. Scripts, external URLs, local file references, external fonts, and recursive references are rejected.

### XLSX

MarkItDown is authoritative for cell/sheet content and order. The extractor reads OOXML relationships and drawing anchors to map each embedded bitmap to a worksheet and cell range. Each unique image SHA is OCRed once and inserted beneath an explicit anchor heading and metadata comment.

Native Excel Chart reconstruction is out of scope. `.xls` and `.ods` return a dedicated placeholder error without invoking LibreOffice.

### Final Tool Output

The complete `ReadFileResult` is assembled and cached before line selection. `start_line` and `end_line` are applied to final Markdown for every route. Missing bounds preserve the current full-output behavior. The returned observation includes total lines, selected range, source format, engine, cache status, warnings, and line-numbered Markdown.

## OCR Environment and Dependencies

### Environment

The implementation creates:

```bash
uv venv .paddle-ocr --python 3.12
```

It must not use `--system-site-packages`. `.paddle-ocr/` is ignored by Git.

The direct dependency baseline is:

- PaddleOCR `>=3.6.0,<3.7.0`, with `doc-parser`;
- PaddleX `>=3.6.0,<3.7.0`, with serving support;
- Transformers `>=5.10.0,<6.0`;
- `safetensors>=0.7.0`;
- AArch64 CUDA 13.0 Torch, baseline `2.11.0+cu130`;
- the matching AArch64 torchvision build if required by the resolved Pipeline.

PaddlePaddle, SGLang, and vLLM are forbidden in this environment. Dependency resolution and P0 imports determine the final lock. Direct constraints live in `requirements/paddle-ocr/requirements.in`, and the production install uses the generated, hash-pinned `requirements/paddle-ocr/requirements.lock` required by the PRD. No unbounded `pip install -U` command is used.

Non-model dependencies such as MarkItDown `[xlsx]`, PyMuPDF, Pillow/image plugins, `python-magic`, and safe SVG support are added to the backend dependency set, not the OCR lock.

### Pipeline Configuration

`config/paddleocr-vl-thor.yaml` is exported from the installed PaddleOCR/PaddleX 3.6.x version, then minimally edited. It is not authored from an assumed schema.

Required semantics:

- Pipeline version v1.6;
- Transformers engine for every actual model;
- CUDA device 0 for every model, with no CPU or PaddlePaddle fallback;
- BF16/SDPA for `PaddleOCR-VL-1.6-0.9B` recognition;
- FP32/eager compatibility exceptions for `PP-DocLayoutV3`,
  `PP-LCNet_x1_0_doc_ori`, and `UVDoc`;
- deterministic generation;
- layout detection, chart recognition, OCR for image blocks, block formatting, layout merge, and internal queues enabled;
- seal recognition disabled by default and request-controlled;
- default unwarping disabled and retry-controlled;
- header/footer/number ignored, but footnote and aside text retained;
- service visualization and Markdown image payloads disabled;
- local, revision-pinned model directories.

Specifying the Transformers engine is not enough by itself. P0 must prove that no submodel resolves to PaddlePaddle or silently falls back to CPU.

The FP32/eager exceptions are required by the installed PaddleX 3.6.1
Transformers paths, not performance tuning. `PP-DocLayoutV3` produced `Got
unsupported ScalarType BFloat16` when its official postprocessing converted a
BF16 tensor to NumPy. The orientation and unwarping modules have equivalent
NumPy-facing preprocessing/postprocessing boundaries, so they remain CUDA
FP32/eager. The generative VLM remains CUDA BF16/SDPA. Raising precision or
changing these three modules back to BF16 requires a new hardware canary; it
must not be done by assumption.

PaddleX 3.6.1 also couples UVDoc construction to
`DocPreprocessor.use_doc_unwarping`: setting it false means the model is not
loaded and a later `useDocUnwarping=true` request is rejected. The service
configuration therefore initializes UVDoc with `use_doc_unwarping: true`.
Project behavior remains default-false: every project-owned normal request and
normal canary must explicitly send `useDocUnwarping=false`; only a distorted
retry sends true. Any project request builder that omits this field is invalid.
This is an unavoidable service-representation difference, not a change to the
PRD retry policy.

## Configuration Surface

`config-server.yaml` receives OCR service connectivity:

- host, default `127.0.0.1`;
- port, default `8118`;
- base URL;
- connect/read/request timeouts;
- pipeline configuration path.

`config-misc.yaml` receives `read_file` behavior:

- allowed roots;
- supported formats;
- source, normalized, page, frame, pixel, and archive limits;
- PDF chunk pages/bytes/overlap;
- LibreOffice timeout;
- Base64 threshold and object token TTL;
- OCR concurrency and queue limit;
- main LLM memory reserve;
- retry limits;
- cache path and TTLs.

`backend/config.py` adds typed `OCRServiceConfig` and `ReadFileConfig` fields to `AppConfig`. Important operational values are not duplicated as shell constants.

## Startup and Lifecycle

### OCR Script

`scripts/2b_start_OCR_LM.sh`:

1. resolves the shared workspace root;
2. verifies `.paddle-ocr` and the locked environment;
3. verifies `platform.machine() == "aarch64"`;
4. verifies Torch CUDA availability and reports the GPU name;
5. verifies package versions and absence of forbidden inference engines;
6. verifies the exported config selects Transformers/CUDA/BF16;
7. checks port 8118 without killing anything;
8. reuses a service only when a fresh atomic attestation matches the config
   hash, model-manifest hash, PID start identity, executable/cmdline identity,
   and listener socket owner; otherwise it rejects the listener;
9. launches `paddlex --serve` on `127.0.0.1:8118` with the custom Pipeline, Transformers engine, and `gpu:0`;
10. waits for the API contract and runs content-aware image, one-page PDF,
    rotated-page, and photographed/warped-page canaries;
11. treats engine, device, or canary failure as fatal;
12. serializes launch with `flock`, traps termination, and gives only its owned
    service process a bounded TERM-only shutdown interval.

The script may write logs under the existing ignored `logs/` directory. It must not install or upgrade dependencies during normal startup.

### Backend Script

`scripts/3_start_backend.sh` probes the configured OCR endpoint before launching FastAPI:

- absent port: warn that document OCR is unavailable and show `scripts/2b_start_OCR_LM.sh`, then continue;
- valid PaddleX layout-parsing service: report available;
- occupied port with an incompatible API: emit a stronger warning, then continue;
- never start, terminate, or clean up the process on port 8118.

Existing cleanup of the backend's own port remains separate.

## Official API Contract

Small normalized files at or below 16 MB use Base64. Larger files use a loopback-only one-time URL with an opaque token and ten-minute maximum TTL.

The client calls `POST /layout-parsing` with the PRD controls, including:

- explicit `fileType`;
- orientation, layout, chart, seal, image-block OCR, formatting, and merge controls;
- `restructurePages`, `mergeTables`, and `relevelTitles`;
- `returnMarkdownImages=false`;
- `visualize=false`.

The client validates HTTP status, top-level `errorCode`, expected page count, result ordering, and each page's `markdown.text`. Failed cache-first or provider fallback concepts do not apply here; this is a document service, not an LLM prompt path.

The P0 photographed/warped canary uses PaddleX's official
`doc_test_rotated.jpg` demo asset, pinned by source URL and SHA-256. It sends
the identical project request once with unwarping false and once with true,
requires the true path to recognize the expected key phrases, and rejects a
true result that loses expected phrases recognized by the false baseline.
`prunedResult.model_settings` and
`doc_preprocessor_res.model_settings/angle` are required execution evidence.

## Models and Output

The internal result follows the PRD's `ReadFileResult`, `PageResult`, `SheetResult`, embedded-image, and parse-error structures. Markdown includes stable provenance comments for source page, slide, sheet, format, engine, model, and Pipeline version.

Binary images and temporary paths are never returned to the Agent. Meaningful visual blocks retain concise model descriptions; decorative blocks use a simple placeholder.

## Cache

The cache is a local SQLite database in WAL mode under `~/.cache/llm-deployment/read_file/` by default. It stores serialized structured results, not just final text.

The key hashes:

- source bytes;
- parser version;
- model revision;
- Pipeline config hash;
- output-affecting request options.

Passwords are excluded as plaintext. Successful, partial, and failed results use 30-day, 7-day, and 1-hour TTLs. Writes are transactional. Expired or schema-incompatible rows are ignored and lazily removed.

## Resource Control and Cancellation

- one active OCR task;
- at most two queued OCR tasks;
- one LibreOffice conversion;
- two image normalization workers;
- 16 GB default system-available UMA floor in addition to SGLang's already
  resident static main-LLM pool;
- 16 GB YAML-configurable PyTorch CUDA allocator ceiling for PaddleOCR;
- pre-admission and pre-chunk available-memory checks;
- no unlimited swap strategy;
- abort checks while queued and between validation, conversion, chunk, retry, and assembly stages.

An abort prevents new work from starting and cleans temporary files. A blocking in-flight PaddleX request is bounded by configured HTTP timeouts; the design does not claim provider-style targeted cancellation that the official PaddleX API does not expose.

## Error Semantics

The implementation uses the PRD error codes. The facade catches expected errors and returns a non-empty, readable, machine-identifiable string rather than raising into the Agent loop.

Partial page failure keeps `success=true`, records warnings and errors, emits a page placeholder, and continues. Whole-file failure occurs only for validation/security failure, unreadable/encrypted input without a valid password, Office conversion failure, unavailable OCR service, all pages failing, or unsupported format.

`.xls` and `.ods` use an explicit placeholder variant of `UNSUPPORTED_FORMAT` that states why the format is recognized but unavailable in this release.

## Security

Required controls include:

- resolved path allowlist and symlink containment;
- extension/MIME/magic agreement;
- archive and image decompression limits;
- isolated temporary directories and cleanup;
- isolated LibreOffice profiles, macro and external-link restrictions, hard timeout, and process-group termination;
- safe SVG rasterization without scripts, network, or local file reads;
- loopback-only OCR and temporary object services;
- unpredictable single-use object tokens;
- fixed model revisions and audited local model files;
- no password, Base64, full body, or full sensitive path logging.

Production acceptance additionally requires a dedicated low-privilege OCR account, a read-only project/model mount where applicable, and blocked outbound network access. The convenience shell script is a local single-user launcher and does not by itself satisfy account-level production isolation.

## Observability and Health

Metrics follow the PRD: request ID, format, sizes, page/sheet/media counts, cache state, stage timings, retries, failed pages, process/system memory peaks, and result size.

The OCR startup preflight writes an atomic runtime attestation containing
Pipeline name/version, config and immutable model-manifest hashes, engine,
model device, architecture, CUDA/SBSA ELF evidence, canary state, PID start
identity, listener socket identity, and timestamp. Backend `/health` combines
a live API probe, this non-stale attestation, and `resource_guard` queue depth
into a nested `ocr` object matching the PRD health fields. Missing OCR degrades
only that nested object and does not make the otherwise usable LLM backend
unhealthy. The attestation is necessary because the unmodified official
PaddleX service does not provide the custom PRD health payload.

## Verification Strategy

### Unit and Contract Tests

- preserve all current text encoding and line-range tests;
- default missing line bounds return the complete result;
- tool schema exposes document options and hides `check_abort`;
- canonical `read_file` tool-call/observation history is not rewritten;
- route every supported and placeholder extension correctly;
- validate MIME mismatches, symlink escapes, archive bombs, image bombs, and malicious SVG;
- test Office subprocess arguments, timeout, process-group cleanup, and PDF validation;
- test PDF overlap mapping, adaptive retry, deduplication, and failed-page continuation;
- test PaddleX request fields and response parsing with recorded fixtures;
- test `.xlsx` sheet order, relationships, anchors, image SHA deduplication, and OCR insertion;
- test cache key invalidation, TTLs, transaction safety, and password exclusion;
- test resource queue limits, memory refusal, and abort boundaries;
- test startup probes and the no-kill policy for port 8118.

### Hardware Integration Tests

Hardware tests are explicitly marked and require Thor, LibreOffice, the locked OCR environment, and local model files. P0 covers content-aware image/PDF/rotation plus the pinned official photographed/warped comparison canary. Later phases cover the PRD's PDF, Word, PowerPoint, XLSX, and image corpus. `.xls` and `.ods` verify only the placeholder response.

### Performance and Soak Tests

Record cold/warm startup, first result, total duration, peak UMA, GPU/CPU utilization, temporary disk, output size, and retry count for the PRD benchmark matrix. Production acceptance requires no OOM, duplicate model load, silent CPU fallback, missing pages, reserve violation, or sustained growth after 20 requests.

FlashAttention 2 and other performance changes are deferred until correctness, AArch64 build, memory, and regression tests pass. SDPA is the production baseline.

## Implementation Phases

### P0: Thor Feasibility Gate

Create and lock the isolated environment, export the official config, start the full official service, verify every submodel uses Transformers on CUDA, run all content-aware P0 canaries, and record UMA. Reproduce a fresh environment from the lock alone with the emitted CUDA index and explicitly verify the NVIDIA SBSA wheel contains an AArch64 ELF. Failure stops implementation and produces a blocker report. It does not trigger a fallback architecture.

### P1: Unified Visual Main Path

Implement typed contracts, validation, routing, Word/PowerPoint rendering, PDF chunking, image normalization, the PaddleX client, result assembly, and the `file_ops.read_file` facade.

### P2: XLSX

Implement MarkItDown, OOXML media/anchor extraction, deduplicated OCR, and Markdown insertion. Add only `.xls/.ods` placeholder handling.

### P3: Reliability and Security

Add cache, resource admission, bounded queueing, abort handling, temporary object serving, observability, security hardening, long-document fallback, and production deployment checks.

### P4: Thor Performance Tuning

Run the benchmark matrix and tune only evidence-backed parameters. Do not enable FlashAttention 2 or raise concurrency by assumption.

## Non-Goals

In addition to the PRD non-goals, this implementation does not include:

- `.xls` or `.ods` conversion;
- a custom OCR FastAPI gateway;
- direct calls to the standalone PaddleOCR-VL model;
- PaddlePaddle inference on Thor;
- OCR through the project's SGLang or vLLM services;
- automatic backend startup or shutdown of OCR;
- automatic truncation of full `read_file` output.

## Design Acceptance

The user approved:

- official PaddleX sidecar plus backend orchestration;
- Python 3.12 and model-stack-only isolation;
- configurable home/project/temp path allowlist;
- explicit `.xls/.ods` placeholder errors;
- full output when line bounds are omitted;
- the environment, script, reliability, security, testing, and phased-gate design above.
