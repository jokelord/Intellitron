# PaddleOCR-VL SGLang Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan. Each coding worker must use superpowers:test-driven-development for its own scope. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a foreground-supervised, SGLang-backed PaddleOCR-VL launch path with complete PaddleX page parsing on `8118`, dedicated OCR SGLang recognition on `30002`, isolated environments, conservative batch settings, and a terminal debug client.

**Architecture:** `.paddle-ocr` runs the complete PaddleX Pipeline and calls an OpenAI-compatible PaddleOCR-VL-1.6-0.9B server launched from `.sglang`. One unified Pipeline YAML contains both PaddleX settings and a project-only SGLang launch section. The new shell launcher owns both child process groups, starts SGLang before PaddleX, mirrors prefixed logs, runs a minimal remote-path canary, and cleans only processes it created.

**Tech Stack:** Bash, Python 3.12, PaddleOCR 3.6.0, PaddleX 3.6.1, SGLang 0.5.15, PyTorch CUDA 13, FastAPI/httpx, YAML, pytest, Linux `/proc`, `flock`, `setsid`.

## Global Constraints

- Follow `docs/superpowers/specs/2026-07-28-paddleocr-sglang-backend-design.md` exactly.
- Do not run Git commands; the project requires explicit user permission and none was granted.
- Do not install, remove, upgrade, or downgrade packages in `.paddle-ocr` or `.sglang`.
- Do not modify dependency package source.
- Do not change or restart the existing main-model SGLang service on `127.0.0.1:30000`.
- Complete PaddleX API: `127.0.0.1:8118`; OCR SGLang API: `127.0.0.1:30002`.
- Do not probe, classify, signal, or clean port `30001` during startup or tests.
- Startup checks required SGLang dependency names, not exact versions.
- New experimental launcher refuses existing listeners on `8118` or `30002`; it never reuses or kills them.
- Every task-owned live service must be stopped immediately after the last required check, including failure paths.
- Do not run A/B timing benchmarks or claim a measured speedup.
- Keep project OCR document request concurrency at one.
- Use ASCII in code; Chinese is allowed and required for YAML explanatory comments.

## File Ownership And Parallel Decomposition

Coding workers must not cross these ownership boundaries.

### Agent A: Configuration, Preflight, Debug Client

Owns only:

- Create `config/paddleocr-vl-thor-sglang.yaml`
- Modify `config/paddleocr-vl-thor.yaml` comments only
- Modify `scripts/paddle_ocr_preflight.py`
- Create `scripts/debug_OCR_with_SGLang_backend.py`
- Create `tests/test_paddle_ocr_sglang_config.py`
- Create `tests/test_paddle_ocr_sglang_debug.py`

Agent A must not edit launcher scripts, backend files, server config, or existing test files.

### Agent B: Ports, Backend Health, Launcher Lifecycle

Owns only:

- Modify `scripts/_workspace_paths.sh`
- Modify `scripts/2_start_OCR_LM.sh`
- Create `scripts/2_start_OCR_LM_with_SGLang_backend.sh`
- Modify `scripts/3_start_backend.sh`
- Modify `config/config-server.yaml`
- Modify `backend/config.py`
- Modify `backend/main.py`
- Modify `tests/test_paddle_ocr_startup.py`
- Modify `tests/test_runtime_config_limits.py`
- Modify `tests/test_main_lifespan_wiring.py`
- Modify `tests/test_paddle_ocr_memory_limiter.py` only for port-only expectations
- Create `tests/test_paddle_ocr_sglang_launcher.py`

Agent B must not edit the unified YAML, preflight implementation, debug client, or Agent A's new tests.

### Shared Interface Contract

Agent A produces these CLI commands for Agent B:

```text
.paddle-ocr/bin/python scripts/paddle_ocr_preflight.py check-sglang-env \
  --config config/paddleocr-vl-thor-sglang.yaml \
  --sglang-python .sglang/bin/python

.paddle-ocr/bin/python scripts/paddle_ocr_preflight.py probe-sglang \
  --url http://127.0.0.1:30002 \
  --served-model-name PaddleOCR-VL-1.6-0.9B

.paddle-ocr/bin/python scripts/paddle_ocr_preflight.py canary-sglang \
  --url http://127.0.0.1:8118 \
  --sglang-url http://127.0.0.1:30002 \
  --served-model-name PaddleOCR-VL-1.6-0.9B \
  --attestation logs/paddleocr-runtime-attestation.json \
  --config config/paddleocr-vl-thor-sglang.yaml \
  --pipeline-pid PID \
  --sglang-pid PID \
  --memory-report logs/paddleocr-cuda-memory-limit.json \
  --cuda-memory-limit-gb 16 \
  --sglang-log logs/paddleocr-sglang-service.log \
  --sglang-log-offset BYTE_OFFSET
```

`check-sglang-env` prints a JSON object with `pipeline`, `sglangBackend`, `dependencies`, and `actualVersions`. `probe-sglang` prints `url`, `servedModelName`, and `modelReady`. `canary-sglang` writes schema-version-2 attestation and prints it.

---

### Task 1: Unified Remote Pipeline Configuration And Mode-Aware Preflight

**Owner:** Agent A

**Files:**
- Create: `config/paddleocr-vl-thor-sglang.yaml`
- Modify: `config/paddleocr-vl-thor.yaml`
- Modify: `scripts/paddle_ocr_preflight.py`
- Create: `tests/test_paddle_ocr_sglang_config.py`

**Interfaces:**
- Consumes: Existing model manifest and local Pipeline config.
- Produces: `validate_sglang_pipeline_config`, `check_sglang_dependency_names`, `probe_sglang_service`, `run_sglang_canary`, and the three CLI commands in the shared interface contract.

- [ ] **Step 1: Write failing unified-config tests**

Create tests that load `config/paddleocr-vl-thor-sglang.yaml` and assert:

```python
assert cfg["pipeline_name"] == "PaddleOCR-VL-1.6"
assert cfg["batch_size"] == 8
assert cfg["use_queues"] is True
assert cfg["SubModules"]["LayoutDetection"]["batch_size"] == 4
vlm = cfg["SubModules"]["VLRecognition"]
assert "engine" not in vlm
assert "engine_config" not in vlm
assert vlm["batch_size"] == 8
assert vlm["genai_config"] == {
    "backend": "sglang-server",
    "server_url": "http://127.0.0.1:30002/v1",
    "max_concurrency": 4,
}
backend = cfg["LLMDeploymentSGLangBackend"]
assert backend["port"] == 30002
assert backend["served_model_name"] == "PaddleOCR-VL-1.6-0.9B"
assert backend["max_running_requests"] == 4
assert backend["max_prefill_tokens"] == 8192
assert backend["context_length"] == 16384
assert backend["mem_fraction_static"] == 0.10
assert backend["log_requests"] is True
assert backend["log_requests_level"] == 0
assert vlm["batch_size"] >= vlm["genai_config"]["max_concurrency"]
assert vlm["genai_config"]["max_concurrency"] == backend["max_running_requests"]
```

Also read the raw YAML and assert it contains Chinese explanations for `batch_size`, `SubModules`, `SubPipelines`, queues, remote concurrency, `max_running_requests`, `max_prefill_tokens`, and `mem_fraction_static`.

- [ ] **Step 2: Write failing PaddleX compatibility and drift tests**

Use `.paddle-ocr` to call `paddlex.inference.pipelines.load_pipeline_config` and `paddlex.inference.serving.infra.config.create_app_config`. Assert the custom top-level section survives YAML parsing but does not enter `AppConfig`.

Add mutation cases that reject:

```text
non-loopback host
port other than 30002
remote URL not matching host/port
served model not matching VLRecognition.model_name
model directory not matching manifest
chat template outside the model directory or missing from manifest
max_concurrency <= 0
max_running_requests <= 0
VLRecognition.batch_size < max_concurrency
max_concurrency != max_running_requests
non-positive max_prefill_tokens or context_length
mem_fraction_static outside (0, 1)
log_requests_level other than 0
local engine or engine_config on VLRecognition
```

- [ ] **Step 3: Run RED config tests**

Run:

```bash
.paddle-ocr/bin/python -m pytest tests/test_paddle_ocr_sglang_config.py -q
```

Expected: failures because the new YAML and validation functions do not exist.

- [ ] **Step 4: Implement the unified YAML**

Copy every reviewed model path, revision-sensitive setting, switch, layout option, DocPreprocessor option, and Serving option from `config/paddleocr-vl-thor.yaml`. Apply only the approved remote differences:

```yaml
batch_size: 8
use_queues: true

SubModules:
  LayoutDetection:
    batch_size: 4
  VLRecognition:
    batch_size: 8
    genai_config:
      backend: sglang-server
      server_url: http://127.0.0.1:30002/v1
      max_concurrency: 4

SubPipelines:
  DocPreprocessor:
    batch_size: 4
    SubModules:
      DocOrientationClassify:
        batch_size: 4
      DocUnwarping:
        batch_size: 1

LLMDeploymentSGLangBackend:
  host: 127.0.0.1
  port: 30002
  model_dir: ~/.paddlex/official_models/PaddleOCR-VL-1.6
  served_model_name: PaddleOCR-VL-1.6-0.9B
  chat_template: ~/.paddlex/official_models/PaddleOCR-VL-1.6/chat_template.jinja
  trust_remote_code: true
  context_length: 16384
  max_running_requests: 4
  max_prefill_tokens: 8192
  mem_fraction_static: 0.10
  enable_metrics: true
  log_requests: true
  log_requests_level: 0
  readiness_timeout_seconds: 900
  shutdown_timeout_seconds: 30
```

Do not include a top-level `engine` or `engine_config`, and do not include a local engine under `VLRecognition`. Retain explicit Transformers engines for layout, orientation, and UVDoc. Add concise Chinese comments to the existing local YAML without changing its values or engine behavior.

- [ ] **Step 5: Implement mode-specific validation**

Keep `validate_pipeline_config()` unchanged for local mode and add:

```python
def validate_sglang_pipeline_config(
    config_path: Path,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
) -> dict[str, Any]:
    """Validate the complete Pipeline with remote SGLang VL recognition."""
```

Return the existing Pipeline identity fields plus:

```python
{
    "mode": "remote_sglang",
    "vlmBackend": "sglang-server",
    "sglangBackend": {
        "host": "127.0.0.1",
        "port": 30002,
        "url": "http://127.0.0.1:30002",
        "servedModelName": "PaddleOCR-VL-1.6-0.9B",
        "modelDir": "...",
        "chatTemplate": "...",
        "contextLength": 16384,
        "maxRunningRequests": 4,
        "maxPrefillTokens": 8192,
        "memFractionStatic": 0.10,
        "enableMetrics": True,
        "logRequests": True,
        "logRequestsLevel": 0,
        "readinessTimeoutSeconds": 900,
        "shutdownTimeoutSeconds": 30,
    },
    "batchAndConcurrencySettings": {...},
}
```

- [ ] **Step 6: Write and implement dependency-name tests**

Test a fake Python executable or monkeypatched subprocess response so presence passes regardless of returned versions. Missing any one of the exact names must fail:

```python
SGLANG_REQUIRED_DISTRIBUTIONS = (
    "sglang",
    "sglang-kernel",
    "torch",
    "transformers",
    "triton",
    "flashinfer-python",
    "flashinfer-cubin",
)
```

Implement:

```python
def check_sglang_dependency_names(python_path: Path) -> dict[str, Any]:
    """Check distribution presence in another interpreter without version gating."""
```

The subprocess prints installed versions for diagnostics, but the function gates only on missing names.

- [ ] **Step 7: Write and implement SGLang probe tests**

Use a fake HTTP server. Require `/health` success and a `/v1/models` response whose `data[*].id` contains the exact served model. Use `httpx.Client(trust_env=False)`.

Implement:

```python
def probe_sglang_service(
    url: str,
    served_model_name: str,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Require live SGLang health and exact served-model identity."""
```

- [ ] **Step 8: Write and implement minimal remote canary tests**

The remote canary must perform exactly one generated image request, not the existing five-case local canary suite. It must:

- Probe SGLang and PaddleX.
- Validate the Pipeline CUDA memory report for the PaddleX PID.
- Record the SGLang log byte offset before the request.
- Submit one image canary through `/layout-parsing`.
- Require expected Markdown and DocPreprocessor evidence.
- Wait boundedly for SGLang request-metadata logging after the offset.
- Reject an unchanged log, empty appended log, or a log that contains image Base64/canary payload text.
- Write schema-version-2 attestation atomically.
- Keep top-level `processIdentity` for the PaddleX listener so backend health remains compatible.
- Add `mode: remote_sglang`, `vlmBackend: sglang-server`, `sglangProcessIdentity`, actual SGLang versions, exact endpoints, batch settings, and canary evidence.

Implement:

```python
def run_sglang_canary(
    paddlex_url: str,
    sglang_url: str,
    served_model_name: str,
    attestation_path: Path,
    *,
    pipeline_pid: int,
    sglang_pid: int,
    config_path: Path,
    memory_report_path: Path,
    cuda_memory_limit_gb: int,
    sglang_log_path: Path,
    sglang_log_offset: int,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Run one end-to-end canary and attest the remote SGLang topology."""
```

- [ ] **Step 9: Add the three CLI subcommands**

Add argparse commands exactly matching the shared interface contract. Existing `check-env`, `probe`, `reuse`, and `canary` behavior must remain backward compatible.

- [ ] **Step 10: Run GREEN config/preflight tests**

Run:

```bash
.paddle-ocr/bin/python -m pytest tests/test_paddle_ocr_sglang_config.py -q
```

Expected: all tests pass; no real OCR/SGLang service starts.

### Task 2: Terminal OCR Debug Client

**Owner:** Agent A

**Files:**
- Create: `scripts/debug_OCR_with_SGLang_backend.py`
- Create: `tests/test_paddle_ocr_sglang_debug.py`

**Interfaces:**
- Consumes: Live `30002 /v1/models` and `8118 /layout-parsing` contracts.
- Produces: `main(argv: list[str] | None = None) -> int` and terminal page Markdown.

- [ ] **Step 1: Write failing debug-client tests**

Load the script with `importlib.util.spec_from_file_location`. Use two fake loopback HTTP servers or one handler exposing the required paths. Cover:

- Editable `DOCUMENT_PATH`, `PADDLEX_API_URL`, and `OCR_SGLANG_URL` constants.
- Optional positional path override.
- PDF `fileType=0` and common image `fileType=1`.
- Exact SGLang model check.
- PaddleX OpenAPI check.
- Exact request switches matching `PaddleOCRClient._payload` for normal mode.
- Ordered multi-page `markdown.text` terminal output.
- `trust_env=False` under fake `ALL_PROXY=socks5h://127.0.0.1:1`.
- Missing file, unsupported extension, wrong model, HTTP 500, malformed JSON,
  PaddleX nonzero `errorCode`, missing pages, and empty Markdown return nonzero.
- Captured stdout/stderr never contains the Base64 payload.

- [ ] **Step 2: Run RED debug tests**

Run:

```bash
.paddle-ocr/bin/python -m pytest tests/test_paddle_ocr_sglang_debug.py -q
```

Expected: fail because the script does not exist.

- [ ] **Step 3: Implement the minimal client**

Use:

```python
from pathlib import Path

DOCUMENT_PATH = str(Path("~/Documents/example.pdf").expanduser())
PADDLEX_API_URL = "http://127.0.0.1:8118"
OCR_SGLANG_URL = "http://127.0.0.1:30002"
SERVED_MODEL_NAME = "PaddleOCR-VL-1.6-0.9B"
```

Expose:

```python
def build_payload(path: Path) -> dict[str, Any]: ...
def verify_sglang(client: httpx.Client) -> None: ...
def verify_paddlex(client: httpx.Client) -> None: ...
def parse_markdown_pages(body: Any) -> list[str]: ...
def main(argv: list[str] | None = None) -> int: ...
```

Encode the selected local file as Base64, print:

```text
===== Page 1 =====
<markdown>

===== Page 2 =====
<markdown>
```

Do not start services, write cache entries, print timing, or import backend Agent/LLM modules.

- [ ] **Step 4: Run GREEN debug tests**

Run:

```bash
.paddle-ocr/bin/python -m pytest tests/test_paddle_ocr_sglang_debug.py -q
.paddle-ocr/bin/python -m compileall -q scripts/debug_OCR_with_SGLang_backend.py
```

Expected: all tests pass and compileall exits zero.

### Task 3: Port Migration And Mode-Aware Backend Health

**Owner:** Agent B

**Files:**
- Modify: `scripts/_workspace_paths.sh`
- Modify: `scripts/2_start_OCR_LM.sh`
- Modify: `scripts/3_start_backend.sh`
- Modify: `config/config-server.yaml`
- Modify: `backend/config.py`
- Modify: `backend/main.py`
- Modify: `tests/test_paddle_ocr_startup.py`
- Modify: `tests/test_runtime_config_limits.py`
- Modify: `tests/test_main_lifespan_wiring.py`
- Modify: `tests/test_paddle_ocr_memory_limiter.py`

**Interfaces:**
- Consumes: Schema-1 local attestation and schema-2 remote attestation from Task 1.
- Produces: Unified complete API routing to `8118`, `OCRServiceConfig.sglang_pipeline_config_path`, and mode-aware health diagnostics.

- [ ] **Step 1: Write failing port/default tests**

Update expected active defaults to:

```python
assert cfg.ocr.port == 8118
assert cfg.ocr.base_url == "http://127.0.0.1:8118"
assert cfg.ocr.pipeline_config_path == "config/paddleocr-vl-thor.yaml"
assert cfg.ocr.sglang_pipeline_config_path == "config/paddleocr-vl-thor-sglang.yaml"
```

Assert the old launcher contains `OCR_PORT="${OCR_PORT:-8118}"` and backend startup fallback uses `http://127.0.0.1:8118`.

- [ ] **Step 2: Write failing mode-aware health tests**

Keep schema-1 local tests. Add schema-2 remote tests with:

```python
{
    "schemaVersion": 2,
    "mode": "remote_sglang",
    "engine": "transformers",
    "vlmBackend": "sglang-server",
    "processIdentity": pipeline_identity,
    "sglangProcessIdentity": sglang_identity,
    "pipeline": remote_pipeline_summary,
    "canaryPassed": True,
}
```

Assert health hashes `sglang_pipeline_config_path` for remote mode and
`pipeline_config_path` for local mode. Reject missing remote identity, dead
remote PID, wrong mode, wrong config hash, wrong endpoint, or schema mismatch.
Expose `vlmBackend` in OCR diagnostics without changing the frontend protocol.

- [ ] **Step 3: Run RED port/health tests**

Run:

```bash
.sglang/bin/python -m pytest \
  tests/test_runtime_config_limits.py \
  tests/test_main_lifespan_wiring.py \
  tests/test_paddle_ocr_startup.py::test_workspace_helpers_resolve_shared_paddle_ocr_paths \
  tests/test_paddle_ocr_startup.py::test_start_script_is_loopback_non_destructive_and_does_not_install \
  tests/test_paddle_ocr_startup.py::test_backend_script_probes_but_never_owns_ocr_service -q
```

Expected: failures for old `30001` defaults, absent remote config field, and unsupported schema 2.

- [ ] **Step 4: Implement the port migration**

Change active defaults only:

```yaml
ocr:
  port: 8118
  base_url: http://127.0.0.1:8118
  pipeline_config_path: config/paddleocr-vl-thor.yaml
  sglang_pipeline_config_path: config/paddleocr-vl-thor-sglang.yaml
```

Update `OCRServiceConfig`, `ConfigLoader._parse_ocr_config`, old OCR launcher default, and backend startup fallback. Do not add any `30001` listener probe.

Add to `_workspace_paths.sh`:

```bash
get_shared_sglang_python() {
    local project_root="${1:-$(get_project_root)}"
    local shared_root
    shared_root="$(get_shared_workspace_root "$project_root")"
    printf '%s/.sglang/bin/python\n' "$shared_root"
}
```

- [ ] **Step 5: Implement mode-aware health**

Keep the top-level PaddleX `processIdentity` contract. Select expected config:

```python
mode = attestation.get("mode", "local_transformers")
if mode == "local_transformers":
    expected_schema = 1
    config_path_value = ocr_config.pipeline_config_path
elif mode == "remote_sglang":
    expected_schema = 2
    config_path_value = ocr_config.sglang_pipeline_config_path
else:
    raise ValueError("unsupported OCR attestation mode")
```

For remote mode, require `vlmBackend == "sglang-server"`, a live
`sglangProcessIdentity.pid`, and valid process start ticks. Do not require the
SGLang PID to own `8118`; the existing PaddleX identity still owns that port.

- [ ] **Step 6: Run GREEN port/health tests**

Run:

```bash
.sglang/bin/python -m pytest \
  tests/test_runtime_config_limits.py \
  tests/test_main_lifespan_wiring.py \
  tests/test_paddle_ocr_startup.py::test_workspace_helpers_resolve_shared_paddle_ocr_paths \
  tests/test_paddle_ocr_startup.py::test_start_script_is_loopback_non_destructive_and_does_not_install \
  tests/test_paddle_ocr_startup.py::test_backend_script_probes_but_never_owns_ocr_service \
  tests/test_paddle_ocr_memory_limiter.py -q
bash -n scripts/2_start_OCR_LM.sh scripts/3_start_backend.sh scripts/_workspace_paths.sh
```

Expected: all tests pass and shell syntax checks exit zero.

### Task 4: Dual-Service Foreground Launcher

**Owner:** Agent B

**Files:**
- Create: `scripts/2_start_OCR_LM_with_SGLang_backend.sh`
- Create: `tests/test_paddle_ocr_sglang_launcher.py`

**Interfaces:**
- Consumes: Unified YAML and Task 1 preflight CLI contract.
- Produces: Foreground-supervised task-owned services on `30002` and `8118`.

- [ ] **Step 1: Write failing static launcher tests**

Assert the script:

- Uses `get_shared_sglang_python` and `get_shared_paddle_ocr_python`.
- Defaults the Pipeline config to `config/paddleocr-vl-thor-sglang.yaml`.
- Parses `LLMDeploymentSGLangBackend` instead of hardcoding batch/scheduler values.
- Invokes `.sglang` with `-m sglang.launch_server`.
- Starts SGLang before PaddleX.
- Does not pass global `--engine transformers` to PaddleX.
- Binds only loopback `30002` and `8118`.
- Uses the three preflight commands from Task 1.
- Contains no `uv pip install`, `pkill`, `killall`, or `fuser -k`.
- Contains no probe or cleanup for `30001`.
- Enables metadata-only SGLang request logging.
- Supports `OCR_EXIT_AFTER_CANARY=1`.

- [ ] **Step 2: Write failing fake-process lifecycle tests**

Create fake `.sglang` and `.paddle-ocr` interpreters plus fake preflight behavior
in a temporary shared workspace. Cover:

- `30002` starts before `8118`.
- PaddleX is not started until SGLang probe succeeds.
- Existing listener on either owned port fails with no signal.
- Missing dependency-name check fails before children start.
- SGLang startup failure leaves no child.
- PaddleX startup failure terminates the task-owned SGLang process group.
- Canary failure terminates both process groups.
- `INT`, `TERM`, and `HUP` stop PaddleX before SGLang.
- One child exiting causes sibling cleanup.
- A child that ignores `TERM` receives scoped `KILL` after the configured timeout.
- A stale or replaced PID identity is never signalled.
- `OCR_EXIT_AFTER_CANARY=1` exits zero and leaves both fake ports free.
- A fake unrelated listener on `30001` is never opened, queried, or signalled.

- [ ] **Step 3: Run RED launcher tests**

Run:

```bash
.sglang/bin/python -m pytest tests/test_paddle_ocr_sglang_launcher.py -q
```

Expected: fail because the launcher does not exist.

- [ ] **Step 4: Implement config loading and startup lock**

The script must use `set -euo pipefail`, resolve the shared environments, and
load only validated scalar values from the unified YAML. Reject non-loopback
hosts, unexpected ports, malformed booleans, non-positive integers, or unsafe
paths before spawning children.

Acquire a dedicated topology lock, then check only `8118` and `30002` inside
the lock. Any existing listener fails closed.

- [ ] **Step 5: Implement component logging**

Start each component with line-preserving output mirrored to terminal and its
own file:

```text
[OCR-SGLANG] child line
[PADDLEX] child line
```

The SGLang command must include values from YAML equivalent to:

```text
--model-path MODEL_DIR
--served-model-name PaddleOCR-VL-1.6-0.9B
--host 127.0.0.1
--port 30002
--chat-template CHAT_TEMPLATE
--trust-remote-code
--context-length 16384
--max-running-requests 4
--max-prefill-tokens 8192
--mem-fraction-static 0.10
--enable-metrics
--log-requests
--log-requests-level 0
--log-requests-target stdout
```

The PaddleX command continues through `scripts/paddle_ocr_service.py`, applies
the existing 16 GiB allocator report, and serves the unified YAML on `8118`.

- [ ] **Step 6: Implement ownership and cleanup**

Start each service in its own session/process group. Record PID, PGID, boot ID,
start ticks, executable, and command hash immediately after spawn. Before each
signal, re-read and compare identity.

One idempotent cleanup function must:

1. Stop task-owned PaddleX first.
2. Stop task-owned SGLang second.
3. Send `TERM` to verified process groups.
4. Wait boundedly for group/listener disappearance.
5. Revalidate and send scoped `KILL` only after timeout.
6. Confirm owned listeners disappeared.
7. Preserve the triggering failure status unless cleanup itself fails.

- [ ] **Step 7: Implement readiness, canary, and supervision**

Call `check-sglang-env`, launch SGLang, call `probe-sglang`, launch PaddleX,
call existing PaddleX `probe`, capture the SGLang log byte offset, and call
`canary-sglang`.

After success:

- Release the startup lock.
- If `OCR_EXIT_AFTER_CANARY=1`, clean both services and exit zero.
- Otherwise remain foreground and monitor both PIDs.
- If either exits, clean the sibling and return nonzero.

- [ ] **Step 8: Run GREEN launcher tests**

Run:

```bash
.sglang/bin/python -m pytest tests/test_paddle_ocr_sglang_launcher.py -q
bash -n scripts/2_start_OCR_LM_with_SGLang_backend.sh
```

Expected: all tests pass and shell syntax exits zero.

### Task 5: Integration, Independent Audit, And Thor Functional Smoke

**Owner:** Main orchestrator and independent auditor; coding agents do not perform this task.

**Files:**
- Review all files from Tasks 1-4
- Modify only confirmed audit defects, preserving file ownership where practical

- [ ] **Step 1: Run combined static and unit verification**

Run:

```bash
.paddle-ocr/bin/python -m pytest \
  tests/test_paddle_ocr_sglang_config.py \
  tests/test_paddle_ocr_sglang_debug.py \
  tests/test_paddle_ocr_startup.py \
  tests/test_paddle_ocr_memory_limiter.py -q

.sglang/bin/python -m pytest \
  tests/test_paddle_ocr_sglang_launcher.py \
  tests/test_runtime_config_limits.py \
  tests/test_main_lifespan_wiring.py \
  tests/test_read_file_paddleocr_client.py -q

bash -n \
  scripts/2_start_OCR_LM.sh \
  scripts/2_start_OCR_LM_with_SGLang_backend.sh \
  scripts/3_start_backend.sh \
  scripts/_workspace_paths.sh

.sglang/bin/python -m compileall -q \
  backend/config.py backend/main.py \
  scripts/paddle_ocr_preflight.py \
  scripts/debug_OCR_with_SGLang_backend.py
```

Expected: zero failures and zero syntax/compile errors.

- [ ] **Step 2: Dispatch an independent read-only auditor**

The auditor must not edit files. It must inspect:

- Shell ownership, PID/PGID reuse, trap recursion, races, and cleanup escalation.
- Whether a failed startup can leave child/grandchild processes or listeners.
- Whether user-owned listeners can be signalled.
- Variable quoting, command injection, unsafe `eval`, unbound variables, and log-pipeline PID mistakes.
- Python resource lifecycle: HTTP clients, temp files, subprocesses, file handles, and exception paths.
- Batch/concurrency consistency and UMA/static-pool semantics.
- Attestation schema compatibility and backend health behavior.
- Dead code, duplicated logic, unused variables, and scope beyond the PRD.
- Silent local Transformers fallback or incorrect SGLang model routing.
- Debug-client payload safety and malformed-response handling.

The auditor returns findings first, ordered by severity with file/line references, followed by residual risks. No praise-only review is acceptable.

- [ ] **Step 3: Resolve every confirmed audit finding with TDD**

For each confirmed defect, add or tighten one focused failing regression, run it
RED, make the smallest correction, then run it GREEN. Do not make speculative
refactors or modify dependency source.

- [ ] **Step 4: Re-run combined verification**

Repeat Step 1 exactly. Expected: zero failures.

- [ ] **Step 5: Verify clean-port preconditions without touching 30001**

Check only `8118` and `30002`. If either is occupied, stop and report the
pre-existing listener; do not signal it. Do not query `30001`.

- [ ] **Step 6: Run the real Thor functional smoke with automatic cleanup**

Run:

```bash
OCR_EXIT_AFTER_CANARY=1 scripts/2_start_OCR_LM_with_SGLang_backend.sh
```

Expected:

- SGLang loads PaddleOCR-VL-1.6-0.9B on `30002`.
- PaddleX exposes `/layout-parsing` on `8118`.
- One minimal image canary succeeds through the remote backend.
- Schema-version-2 `remote_sglang` attestation is written.
- The command exits zero after stopping both task-owned services.
- Task-owned listeners on `8118` and `30002` are absent afterward.
- The existing main-model service on `30000` was not restarted or reconfigured.

- [ ] **Step 7: Run one debug-client functional read only if the foreground topology is intentionally started**

If a separate debug-client live check is needed, start the new launcher in a
controlled background terminal/session, run:

```bash
.paddle-ocr/bin/python scripts/debug_OCR_with_SGLang_backend.py \
  tests/fixtures/paddleocr/doc_test_rotated.jpg
```

Then terminate the launcher through its normal signal path and confirm `8118`
and `30002` are closed. Do not leave the service running after this check.

- [ ] **Step 8: Report evidence without speed claims**

Report test counts, shell/compile status, live canary result, attestation mode,
exact endpoints, cleanup evidence, and auditor findings/resolutions. Explicitly
state that no A/B timing benchmark was run and no measured speedup is claimed.
