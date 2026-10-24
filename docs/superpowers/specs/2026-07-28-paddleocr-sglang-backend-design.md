# PaddleOCR-VL SGLang Backend MVP PRD

Date: 2026-07-28

Status: Draft for written user review; section-level design approved

## 1. Summary

Add an opt-in startup path that keeps the official complete PaddleOCR-VL-1.6
page-level Pipeline in `.paddle-ocr`, but delegates only its region-level
PaddleOCR-VL-1.6-0.9B recognition stage to SGLang 0.5.15 running from the
existing `.sglang` environment.

The primary deliverable is:

```text
scripts/2_start_OCR_LM_with_SGLang_backend.sh
```

The deployment has three distinct local services:

```text
127.0.0.1:30000  Existing main-model SGLang service
127.0.0.1:30002  Dedicated PaddleOCR-VL-1.6-0.9B SGLang VLM service
127.0.0.1:8118   Complete PaddleX /layout-parsing API
```

The backend continues to call only the complete PaddleX API. It never calls
the region-recognition SGLang service directly.

This phase establishes and functionally validates the accelerated architecture.
It does not run an A/B performance benchmark and must not report a measured
speedup or describe the initial tuning values as proven optimal.

## 2. Problem Statement

The current deployment uses the official complete page-level
PaddleOCR-VL-1.6 Pipeline, but its VLM recognition model runs locally through
Transformers. In PaddleX 3.6.1, `VLRecognition.batch_size: -1` resolves to a
local batch size of one. Layout regions are therefore recognized through
individual local `model.generate()` calls.

PaddleX already supports a remote `sglang-server` recognition backend. Its
remote predictor submits independent crop requests asynchronously, allowing
SGLang to apply continuous batching and optimized scheduling while PaddleX
retains layout detection, cropping, reading-order recovery, and page-level
Markdown assembly.

The current `.paddle-ocr` environment cannot safely become the SGLang server
environment. Installing only `sglang==0.5.15` with `--no-deps` and the local
kernel wheel would leave many direct runtime dependencies absent and would
introduce conflicts in Transformers, OpenAI, FlashAttention, and PaddleX's
published SGLang plugin contract. The existing `.sglang` environment already
contains the requested SGLang and kernel versions and the associated runtime
stack.

## 3. Goals

- Add `scripts/2_start_OCR_LM_with_SGLang_backend.sh` as an explicit opt-in
  launch path.
- Keep the complete PaddleOCR-VL-1.6 Pipeline and `/layout-parsing` API.
- Run PP-DocLayoutV3, document orientation classification, and UVDoc from
  `.paddle-ocr`.
- Run PaddleOCR-VL-1.6-0.9B from `.sglang` on `127.0.0.1:30002`.
- Move the complete PaddleX API from `30001` to `8118` for both the existing
  Transformers launcher and the new SGLang-backed launcher.
- Preserve `127.0.0.1:30000` for the existing main-model SGLang service.
- Keep `.paddle-ocr` and `.sglang` dependency sets unchanged.
- Replace unsafe implicit remote batching defaults with a conservative,
  explicitly documented MVP profile.
- Show both services' prefixed output in the launching terminal while retaining
  separate component logs.
- Guarantee cleanup of every service started by the current launcher or by an
  implementation/validation task.
- Add a simple terminal debug client for reading a user-selected PDF or image
  through the new SGLang-backed topology.

## 4. Non-Goals

- Do not merge or cross-load the two virtual environments.
- Do not install SGLang, SGLang kernel, FlashInfer, or FlashAttention into
  `.paddle-ocr`.
- Do not install, upgrade, or downgrade packages in `.sglang`.
- Do not use `.paddle-ocr/bin/paddleocr genai_server`; the current environment
  does not expose a usable SGLang engine plugin for this stack.
- Do not replace the complete Pipeline with direct SGLang image requests.
- Do not modify PaddleX, PaddleOCR, Transformers, SGLang, or other dependency
  source files.
- Do not change the main-model SGLang process or its configuration.
- Do not add concurrent document requests; project OCR request concurrency
  remains one.
- Do not run an A/B speed benchmark in this phase.
- Do not probe, classify, or clean a listener on port `30001` during startup.
- Do not claim that the selected batch or memory values are benchmark-optimal.

## 5. Architecture

### 5.1 Data Flow

```text
Backend read_file
  -> POST http://127.0.0.1:8118/layout-parsing
  -> PaddleX complete Pipeline in .paddle-ocr
     -> page decode and optional orientation classification
     -> PP-DocLayoutV3
     -> layout filtering, merging, and cropping
     -> independent crop requests to http://127.0.0.1:30002/v1
        -> SGLang 0.5.15 in .sglang
        -> PaddleOCR-VL-1.6-0.9B
     -> reading-order reconstruction and page-level Markdown
  -> backend read_file result
```

### 5.2 Environment Boundary

The process boundary is also the dependency boundary:

| Component | Environment | Responsibility |
|---|---|---|
| Main SGLang | `.sglang` | Existing main-model inference on `30000` |
| OCR SGLang | `.sglang` | PaddleOCR-VL-1.6-0.9B recognition on `30002` |
| PaddleX API | `.paddle-ocr` | Complete page-level Pipeline on `8118` |
| Backend | `.sglang` | Calls only PaddleX `/layout-parsing` |

Reusing `.sglang` means reusing its package environment, not reusing the
already running model process on `30000`. A second SGLang process is required
because the existing process has loaded a different model.

The following isolation techniques are prohibited:

- Cross-environment `PYTHONPATH`.
- `.pth` files or import hooks that expose one environment to the other.
- Symlinking one environment's site-packages into the other.
- Installing both `flash-attn` and `flash-attn-4` into one prefix.
- Installing SGLang with `--no-deps` into `.paddle-ocr` and assuming missing
  runtime dependencies are optional.

### 5.3 SGLang Launch Method

The new launcher starts the OCR VLM directly from the existing SGLang runtime:

```text
.sglang/bin/python -m sglang.launch_server
```

It supplies the reviewed local model directory, exact served model name,
loopback host, port, chat template, context length, scheduler limits, prefill
budget, metrics setting, and static memory fraction from the unified YAML.

The served model name is exactly:

```text
PaddleOCR-VL-1.6-0.9B
```

The model artifact remains the reviewed local PaddleOCR-VL-1.6 snapshot in the
project manifest.

## 6. Port Contract

| Port | Owner | Protocol |
|---:|---|---|
| `30000` | Existing main-model SGLang | OpenAI-compatible API |
| `30002` | OCR SGLang | OpenAI-compatible API |
| `8118` | Complete PaddleX Pipeline | `POST /layout-parsing` |

The complete PaddleX API port changes to `8118` in at least:

- `config/config-server.yaml`
- `scripts/2b_start_OCR_LM.sh`
- `scripts/2_start_OCR_LM_with_SGLang_backend.sh`
- Runtime probes, tests, and current operational documentation

`OCRServiceConfig` keeps `pipeline_config_path` for the local Transformers
rollback YAML and adds `sglang_pipeline_config_path` for the single new remote
YAML. Backend health selects the expected config hash from attestation `mode`;
it must not force both modes to match one file.

Port `8118` is a project convention for the complete API. It must not be
described as PaddleX's universal default; PaddleX basic serving commonly
defaults to `8080`, while official examples also use `8118` for VLM services.

The new launcher only checks and manages `8118` and `30002`. It performs no
runtime test against `30001`. Active backend routing must no longer target
`30001`, but the launcher does not care whether some unrelated listener exists
there.

## 7. Unified Configuration

Only one new configuration file is added:

```text
config/paddleocr-vl-thor-sglang.yaml
```

The file remains a valid PaddleX 3.6.1 Pipeline configuration and contains one
project-specific top-level section for launching SGLang:

```yaml
# PaddleX 官方完整 Pipeline 配置。下面的 batch_size 按文档页分组，
# 不是 VLM 生成批大小；增大可提高多页吞吐，但会增加解码图片和中间结果内存。
pipeline_name: PaddleOCR-VL-1.6
device: gpu:0
batch_size: 8

# 队列重叠输入加载、文档 CV/layout 和 VLM 阶段，主要优化多页吞吐。
# 单页可能承担线程和最多约 0.5 秒的聚合等待，本阶段不做延迟 benchmark。
use_queues: true

SubModules:
  # SubModules 是父 Pipeline 直接调用的独立模型组件。
  LayoutDetection:
    module_name: layout_detection
    model_name: PP-DocLayoutV3
    engine: transformers
    # 版面检测 batch 按页面计数；该模型使用 FP32，增大时会提高 UMA 峰值。
    batch_size: 4

  VLRecognition:
    module_name: vl_recognition
    model_name: PaddleOCR-VL-1.6-0.9B
    # 远程 VLM batch 表示一轮提交的裁剪区域数量，不是一个 tensor batch。
    batch_size: 8
    genai_config:
      backend: sglang-server
      server_url: http://127.0.0.1:30002/v1
      # 同时在途的 HTTP 区域请求数，应与 SGLang max_running_requests 对齐。
      max_concurrency: 4

SubPipelines:
  # SubPipelines 是有自己处理流程的嵌套 Pipeline。DocPreprocessor
  # 负责页面预处理，并继续包含方向分类和按需 UVDoc 矫正模块。
  DocPreprocessor:
    pipeline_name: doc_preprocessor
    # 文档预处理 batch 按页面计数；启用 layout 队列时还受 layout batch 限制。
    batch_size: 4
    SubModules:
      DocOrientationClassify:
        # 方向分类模型的页面 batch。
        batch_size: 4
      DocUnwarping:
        # UVDoc batch；正常请求不启用，仅失真模式或质量失败重试使用。
        batch_size: 1

# 本项目启动脚本专用配置。PaddleX 3.6.1 忽略这个未知顶层键；
# 测试必须锁定该兼容行为，防止未来 PaddleX 升级为严格 schema 后静默回归。
LLMDeploymentSGLangBackend:
  host: 127.0.0.1
  port: 30002
  model_dir: /home/zk/.paddlex/official_models/PaddleOCR-VL-1.6
  served_model_name: PaddleOCR-VL-1.6-0.9B
  chat_template: /home/zk/.paddlex/official_models/PaddleOCR-VL-1.6/chat_template.jinja
  trust_remote_code: true
  context_length: 16384
  # SGLang 同时运行的区域请求上限；提高吞吐也会增加 KV、UMA 和主模型竞争。
  max_running_requests: 4
  # 单次 prefill 调度 token 预算，不是上下文长度或输出 token 上限。
  max_prefill_tokens: 8192
  # SGLang 模型/KV 静态池初始比例，不是进程硬内存上限。
  mem_fraction_static: 0.10
  enable_metrics: true
  # 只记录请求元数据，不记录图片、prompt 或输出；用于证明 canary 经过 30002。
  log_requests: true
  log_requests_level: 0
  readiness_timeout_seconds: 900
  shutdown_timeout_seconds: 30
```

The complete production YAML retains every existing model directory, revision,
engine setting, layout option, serving option, and document preprocessing
option omitted from this abbreviated example.

PaddleX 3.6.1 currently accesses known Pipeline keys and reads serving options
only from `Serving`. A regression test must prove that
`LLMDeploymentSGLangBackend` remains harmless to Pipeline construction and the
serving schema.

The new launcher does not pass global `--engine transformers`. Layout,
orientation, and UVDoc select Transformers explicitly inside their component
configurations. `VLRecognition` has no local `engine` or local `engine_config`;
its `genai_config.backend` selects `sglang-server`.

## 8. Batch And Queue Profile

### 8.1 Initial MVP Values

| Layer | Value | Unit and scope |
|---|---:|---|
| Pipeline `batch_size` | `8` | Document pages per top-level input group |
| `LayoutDetection.batch_size` | `4` | Pages per PP-DocLayoutV3 call |
| `VLRecognition.batch_size` | `8` | Crops per remote submission wave |
| `genai_config.max_concurrency` | `4` | Simultaneous crop HTTP requests |
| `DocPreprocessor.batch_size` | `4` | Pages per preprocessing group |
| `DocOrientationClassify.batch_size` | `4` | Pages per orientation model call |
| `DocUnwarping.batch_size` | `1` | Pages per UVDoc call |
| SGLang `max_running_requests` | `4` | Scheduler-running crop requests |
| SGLang `max_prefill_tokens` | `8192` | Prefill scheduling token budget |
| SGLang `context_length` | `16384` | Per-request model context limit |
| SGLang `mem_fraction_static` | `0.10` | Fraction of Thor UMA for static pool policy |

### 8.2 Optimization Intent

These values improve the configuration design without claiming measured
optimality:

- Replace local `-1 -> 1` recognition behavior with explicit remote submission
  and concurrency controls.
- Avoid PaddleX's remote defaults of an extremely large submission wave and up
  to hundreds of concurrent requests.
- Align client concurrency and SGLang running requests at four.
- Reduce the top-level page group from 64 to 8, lowering decoded-image and
  intermediate-result retention.
- Keep queues enabled so page loading, local CV/layout work, and remote VLM work
  can overlap.
- Keep UVDoc at one because it is a conditional quality-recovery path rather
  than the normal throughput path.

The following relationship is mandatory:

```text
VLRecognition.batch_size >= genai_config.max_concurrency
genai_config.max_concurrency == max_running_requests
```

Changing these values later requires coordinated review. A higher value may
increase crop throughput but also raises visual-prefill bursts, KV/cache use,
tail latency, and contention with the main model on the same Thor GPU.

### 8.3 Existing Transformers Configuration

`config/paddleocr-vl-thor.yaml` remains the local Transformers rollback profile.
It receives concise Chinese comments explaining top-level batch, SubModules,
SubPipelines, layout batch, local VLM batch, preprocessing batch, and queues,
but this task does not silently convert it to the remote backend.

## 9. Resource Policy

- The existing main-model SGLang configuration and process remain unchanged.
- The PaddleX process on `8118` remains under the existing
  `ocr.cuda_memory_limit_gb: 16` PyTorch allocator ceiling.
- The SGLang process on `30002` does not automatically inherit that allocator
  ceiling. Its initial scheduler/static-pool policy is configured separately.
- `mem_fraction_static: 0.10` is not described or enforced as a hard 12.8 GiB
  process cap.
- Startup and post-canary checks retain the project's
  `main_llm_memory_reserve_gb: 16` system-available UMA floor.
- The combination of the two OCR processes must not change the main model's
  memory settings.
- No attention or multimodal attention backend is hardcoded in the MVP. SGLang
  0.5.15 selects its supported SM110 path; a later tuning phase may pin a
  backend only after compatibility evidence exists.

## 10. Dependency Validation

Startup checks dependency presence by distribution or import name, not exact
versions. The launch gate checks exactly this focused SGLang-side name list:

```text
sglang
sglang-kernel
torch
transformers
triton
flashinfer-python
flashinfer-cubin
```

The launcher:

- Does not compare these packages to specific version strings.
- Does not reject startup merely because an installed version differs from a
  documented value.
- Does not install or repair a missing package.
- Fails before spawning a service when a required name is absent.
- May record actual versions for diagnostics after the presence check.

Native import, model, CUDA, or kernel incompatibility is allowed to surface in
the bounded readiness phase. Any such failure triggers the same task-owned
cleanup contract as other startup failures.

## 11. Startup Lifecycle

### 11.1 Ordered Startup

1. Resolve the workspace-owned `.paddle-ocr` and `.sglang` interpreters.
2. Parse and validate the unified YAML and model manifest.
3. Check required dependency names.
4. Acquire one topology-level startup lock covering `8118` and `30002`.
5. Check `8118` and `30002` inside the lock.
6. If either port already has a listener, fail closed without signalling it.
7. Start OCR SGLang from `.sglang` in a dedicated process group.
8. Wait for `30002 /health`.
9. Verify `30002 /v1/models` contains the exact served OCR model.
10. Start PaddleX from `.paddle-ocr` in a separate process group.
11. Wait for `8118 /openapi.json` and verify `POST /layout-parsing`.
12. Run the end-to-end functional canary and prove traffic reached SGLang.
13. Write the successful runtime attestation atomically.
14. Release the startup lock and remain as the foreground supervisor.

No startup step probes port `30001`.

### 11.2 Existing Listeners

The experimental launcher does not reuse listeners. A listener already present
on either `8118` or `30002` causes a clear failure and no process changes.

The launcher must never terminate a listener merely because it is compatible,
uses a project command line, or has a historical attestation. This preserves
user ownership and guarantees that a successful experimental launch shows the
complete startup output in the current terminal.

### 11.3 Foreground Supervision

The launcher remains in the foreground while either task-owned service is
running. If one component exits unexpectedly, the supervisor stops the other
task-owned component and exits nonzero.

It handles normal completion, startup failure, canary failure, `INT`, `TERM`,
`HUP`, and shell `EXIT` through one idempotent cleanup path.

## 12. Ownership And Cleanup

### 12.1 Identity

A process is task-owned only if the current launcher created it. For each
component, the launcher records enough identity to prevent PID-reuse mistakes:

- Run ID and component name.
- PID and process-group ID.
- Boot ID and `/proc` start time ticks.
- Executable path and command-line hash.
- Host, port, and creation timestamp.

Identity is revalidated immediately before every signal.

### 12.2 Shutdown Order

1. Mark the run as stopping.
2. Stop the task-owned PaddleX process group first so it cannot create new VLM
   requests.
3. Stop the task-owned OCR SGLang process group second.
4. Send `TERM` to each revalidated process group.
5. Wait for the configured grace interval and listener disappearance.
6. If a group remains, revalidate its identity and send `KILL` only to that
   task-owned group.
7. Confirm that task-owned listeners on `8118` and `30002` disappeared before
   returning.

Broad process operations such as `pkill`, `killall`, `fuser -k`, or killing the
current owner of a port are prohibited.

### 12.3 Automation Rule

Any implementation agent, sub-agent, test, or validation task that starts this
topology must stop its task-owned services immediately after the last required
check. Cleanup remains mandatory after failures and timeouts.

`OCR_EXIT_AFTER_CANARY=1` provides an automation mode that runs readiness and
the functional canary, then shuts down both task-owned services before exiting.

Normal user mode remains in the foreground until interrupted, then performs the
same cleanup.

## 13. Logging

The supervisor writes separate component logs and mirrors both streams to the
current terminal:

```text
[OCR-SGLANG] ...
[PADDLEX] ...
```

Requirements:

- Preserve separate SGLang, PaddleX, and supervisor diagnostic files.
- Include run boundaries or per-run filenames.
- Record startup state, process identity, readiness transitions, canary result,
  received signals, escalation, and listener disappearance.
- Do not log document Base64 payloads, API secrets, or full request bodies.
- SGLang request logging is restricted to metadata level zero so the canary can
  prove remote traffic without recording images, prompts, sampling parameters,
  or generated text.
- Preserve the child process exit status and report cleanup failures explicitly.

## 14. Debug Client

Add:

```text
scripts/debug_OCR_with_SGLang_backend.py
```

The script is a client only. It assumes the new foreground launcher is already
running in another terminal and never starts or stops a service.

### 14.1 User Interface

The file contains easy-to-edit constants near the top:

```python
DOCUMENT_PATH = "/home/zk/Documents/example.pdf"
PADDLEX_API_URL = "http://127.0.0.1:8118"
OCR_SGLANG_URL = "http://127.0.0.1:30002"
```

An optional positional path overrides `DOCUMENT_PATH`:

```bash
.paddle-ocr/bin/python scripts/debug_OCR_with_SGLang_backend.py /path/to/document.pdf
```

### 14.2 Behavior

- Accept supported PDF and image formats for the SGLang-backed OCR path.
- Validate that the selected path exists and is a regular file.
- Probe `30002 /v1/models` for the exact OCR model.
- Probe `8118 /openapi.json` for `POST /layout-parsing`.
- Encode the file using the official PaddleX request protocol.
- Use the same quality and page-restructuring request switches as the backend
  PaddleOCR client where applicable.
- Use `trust_env=False` for all loopback HTTP calls.
- Validate HTTP status, JSON shape, PaddleX `errorCode`, page list, and Markdown
  text.
- Print each page's `markdown.text` to the terminal with a clear page separator.
- Return nonzero with an actionable error for an unavailable service, wrong
  model, unsupported file, malformed response, empty page, or PaddleX error.
- Never print Base64 payloads or large internal response structures.
- Do not display timing data or implement a benchmark.
- Do not use the backend Agent, LLM, read-file cache, or chat protocol.

The debug client is covered by local fake-HTTP-server tests and does not require
a real model for its unit test suite.

### 14.3 PDFs Over The Service Page Limit

The PaddleX service keeps `Serving.extra.max_num_input_imgs=32` as a bounded
input-memory guard. The debug client must not treat a truncated 32-page response
as success when the source PDF contains more pages.

For a PDF over 32 pages, the debug client:

1. Requires the installed Poppler `pdfseparate` and `pdfunite` executables and
   creates all intermediate files under an automatically cleaned temporary
   directory.
2. Splits the source into ordered chunks of at most 32 pages without invoking
   shell parsing or interpolating the path into a command string.
3. Sends each chunk to `POST /layout-parsing` with `restructurePages=false` and
   validates `dataInfo.numPages` plus the returned result count against the
   chunk's known page count.
4. Collects only the ordered `prunedResult` and Markdown image mapping required
   by the official PaddleX restructuring protocol. It does not log or persist
   document payloads or internal responses.
5. Sends the complete ordered page list once to `POST /restructure-pages` with
   `mergeTables=true`, `relevelTitles=true`, `concatenatePages=false`, and the
   same Markdown formatting switches as the normal request.
6. Requires the restructured response count to equal the source page count,
   then applies the normal Markdown validation and terminal rendering.

This two-stage flow preserves table merging across chunk boundaries. Independent
per-chunk restructuring is forbidden because a table spanning pages 32 and 33
would otherwise lose its cross-page merge context. PDFs with at most 32 pages
and all supported images retain the existing single-request path.

An empty `markdown.text` remains an error unless PaddleX supplies explicit
cross-page table-merge provenance: every non-ignored content block is an empty
`table` block whose integer `global_group_id` points to an earlier
`global_block_id`. The debug output shows a short merge notice for such a page;
all other empty or malformed pages remain terminal response errors.

## 15. Runtime Attestation

The attestation JSON is project-owned diagnostic evidence. It is not an
official PaddleOCR document format, OCR text format, or model input. PaddleOCR
and PaddleX do not consume it to parse documents.

The canonical file remains:

```text
logs/paddleocr-runtime-attestation.json
```

The schema becomes mode-aware:

```text
mode: local_transformers | remote_sglang
fullApi
vlmBackend
pipelineProcessIdentity
sglangProcessIdentity
pipelineConfigSha256
modelManifestSha256
actualPackageVersions
servedModelName
batchAndConcurrencySettings
canaryEvidence
timestamp
```

For the new experimental launcher:

- Attestation is not used to reuse an existing service.
- Attestation does not authorize signalling or killing a process.
- Package versions are recorded but are not startup version gates.
- A successful file is written only after the end-to-end canary proves the
  remote SGLang path.
- The live `/v1/models`, `/openapi.json`, process identity, and endpoint checks
  remain authoritative for current runtime status.
- Backend health validates `pipeline_config_path` for `local_transformers` and
  `sglang_pipeline_config_path` for `remote_sglang`.

The existing launcher writes `mode: local_transformers`; the new launcher
writes `mode: remote_sglang`.

## 16. Error Handling

| Failure | Required behavior |
|---|---|
| Required dependency name missing | Fail before spawning either service |
| `8118` or `30002` occupied | Fail without reuse or signalling |
| SGLang startup failure | Do not start PaddleX; clean task-owned SGLang group |
| SGLang health timeout | Clean task-owned SGLang group |
| `/v1/models` mismatch | Do not start PaddleX; clean SGLang group |
| PaddleX startup failure | Stop task-owned PaddleX and SGLang groups |
| PaddleX OpenAPI mismatch | Stop both task-owned groups |
| Canary failure | Stop both groups; do not write success attestation |
| SGLang exits while supervised | Stop task-owned PaddleX; exit nonzero |
| PaddleX exits while supervised | Stop task-owned SGLang; exit nonzero |
| `TERM` grace period expires | Revalidate identity, then scoped `KILL` |
| Cleanup cannot remove owned listener | Report PID/port and exit nonzero |

## 17. Testing Strategy

### 17.1 Static Configuration Tests

- The unified YAML remains loadable by PaddleX 3.6.1.
- `LLMDeploymentSGLangBackend` does not alter the Pipeline or Serving schema.
- The remote URL, host, port, model directory, served model, and chat template
  are mutually consistent.
- Client concurrency equals scheduler running-request concurrency.
- `VLRecognition.batch_size` is not less than client concurrency.
- Required Chinese explanatory comments are present.
- The new Pipeline has no local Transformers engine for `VLRecognition`.
- Layout, orientation, and UVDoc retain explicit Transformers/CUDA engines.

### 17.2 Launcher Unit Tests

Use fake child processes and local test listeners to verify:

- Topology lock and lock-internal port checks.
- SGLang starts before PaddleX.
- PaddleX never starts before the exact SGLang model is ready.
- Existing `8118` or `30002` listeners receive no signal.
- PID/PGID identity prevents stale-PID and PID-reuse signalling.
- All traps converge on one idempotent cleanup path.
- Shutdown order is PaddleX then SGLang.
- Scoped escalation removes a deliberately unresponsive task-owned child.
- Child failure stops the sibling component.
- Terminal streams contain component prefixes and separate log files.
- `OCR_EXIT_AFTER_CANARY=1` leaves no task-owned listener.
- No test probes or kills port `30001`.

### 17.3 Dependency Isolation Tests

- `.paddle-ocr` remains free of SGLang, SGLang kernel, and FlashInfer.
- The OCR SGLang command uses only `.sglang/bin/python`.
- Startup checks names but does not assert version equality.
- The task does not invoke `uv pip install` in either environment.

### 17.4 Debug Client Tests

With a fake local HTTP server, verify:

- Editable default path and positional override.
- PDF/image file-type mapping.
- Correct `/v1/models`, `/openapi.json`, and `/layout-parsing` calls.
- Loopback clients ignore environment proxies.
- Page Markdown is printed in order.
- Invalid model, HTTP error, malformed JSON, PaddleX error, and empty page return
  nonzero without dumping payloads.

### 17.5 Thor Functional Smoke

Run only after unit/static tests pass:

1. Ensure `8118` and `30002` are free.
2. Launch with `OCR_EXIT_AFTER_CANARY=1`.
3. Confirm SGLang health and exact model identity.
4. Confirm PaddleX exposes `/layout-parsing`.
5. Submit the existing small fixed OCR canary.
6. Confirm expected phrases and valid page-level Markdown.
7. Confirm SGLang metrics/request count or run-correlated access logs changed.
8. Confirm PaddleX did not silently instantiate the local VLM.
9. Confirm the main model on `30000` was not changed.
10. Confirm task-owned `8118` and `30002` listeners disappeared after the run.

The smoke test records no A/B timing comparison and produces no speedup claim.

## 18. Semantic Invariants

The SGLang-backed path must not change:

- Model artifact revisions.
- Layout threshold, NMS, unclip, and merge policy.
- Chart, image-block OCR, seal, formatting, and Markdown behavior.
- Image pixel budgets.
- Deterministic generation.
- Page ordering and restructuring.
- Cross-page table handling.
- Normal-request unwarping policy.
- Retry behavior.
- Project-level OCR request concurrency.
- Prefix-cache-sensitive main LLM behavior or configuration.

## 19. Acceptance Criteria

- The new launcher starts the isolated SGLang VLM on `30002` and complete
  PaddleX API on `8118` from clean ports.
- The old Transformers launcher starts its complete API on `8118` as a rollback
  path.
- Backend OCR routing points to `8118/layout-parsing`.
- PaddleX crop requests demonstrably reach `30002`; there is no silent local
  VLM fallback.
- Client concurrency and SGLang running-request concurrency are both four.
- The minimum functional canary returns expected page-level Markdown.
- The debug client prints OCR text for a selected supported PDF or image.
- The new YAML contains clear Chinese explanations for Pipeline batch,
  SubModules, SubPipelines, every component batch, queues, client concurrency,
  SGLang concurrency, prefill budget, and memory fraction.
- No package is installed, removed, upgraded, or downgraded in either virtual
  environment.
- No main-model process or configuration is changed.
- Startup does not inspect port `30001`.
- Normal shutdown and all tested failures leave no task-owned listener on
  `8118` or `30002`.
- Existing user-owned listeners are never signalled.
- No A/B benchmark is run and no measured speedup is claimed.

## 20. Rollback

Rollback does not require dependency changes:

1. Stop the foreground SGLang-backed launcher.
2. Confirm its task-owned `8118` and `30002` listeners are gone.
3. Start `scripts/2b_start_OCR_LM.sh`.
4. The existing local Transformers Pipeline serves the same complete API on
   `8118`.

The backend endpoint remains unchanged between the two modes.

## 21. Implementation Scope

Expected additions:

- `scripts/2_start_OCR_LM_with_SGLang_backend.sh`
- `scripts/debug_OCR_with_SGLang_backend.py`
- `config/paddleocr-vl-thor-sglang.yaml`
- Focused launcher, configuration, debug-client, and integration tests

Expected modifications:

- `config/config-server.yaml`
- `backend/config.py`
- `backend/main.py`
- `scripts/2b_start_OCR_LM.sh`
- `scripts/paddle_ocr_preflight.py`
- `config/paddleocr-vl-thor.yaml` comments only for batch concepts
- Runtime attestation handling
- Current tests and operational documentation that assume the complete API is
  on `30001`

Dependency package source and both virtual-environment package sets are outside
the modification scope.

## 22. Evidence And References

Local evidence:

- `docs/两个虚拟环境共有依赖包的不同版本对比情况.md`
- `config/paddleocr-vl-thor.yaml`
- `scripts/2b_start_OCR_LM.sh`
- `scripts/paddle_ocr_preflight.py`
- `backend/tools/read_file/paddleocr_client.py`
- `.paddle-ocr/.../paddlex/inference/pipelines/paddleocr_vl/pipeline.py`
- `.paddle-ocr/.../paddlex/inference/models/doc_vlm/predictor.py`
- `.paddle-ocr/.../paddlex/inference/models/doc_vlm/constants.py`
- `.paddle-ocr/.../paddlex/inference/genai/backends/sglang.py`
- `.sglang/.../sglang-0.5.15.dist-info/METADATA`
- `.sglang/.../sglang/srt/models/paddleocr_vl.py`
- `whl/sglang_kernel-0.4.4+cu130-cp310-abi3-manylinux2014_aarch64.whl`

External references:

- PaddleOCR-VL usage guide:
  `https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PaddleOCR-VL.html`
- NVIDIA Blackwell PaddleOCR-VL guide:
  `https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PaddleOCR-VL-NVIDIA-Blackwell.html`
- SGLang PaddleOCR-VL support:
  `https://github.com/sgl-project/sglang/pull/12953`
- NVIDIA SGLang 26.06 release notes:
  `https://docs.nvidia.com/deeplearning/frameworks/sglang-release-notes/rel-26-06.html`
