# PaddleOCR CUDA Memory Limit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce a YAML-configurable 16 GiB PyTorch CUDA allocation ceiling for the official PaddleOCR-VL service, attest it, restart OCR/FastAPI, and verify the original PDF through the Agent API.

**Architecture:** A project-owned Python wrapper applies `torch.cuda.set_per_process_memory_fraction()` before importing PaddleX, writes atomic runtime evidence, then calls the official PaddleX CLI in the same process. The existing preflight binds that evidence to the service PID and expected YAML value for canary and reuse; Read File separately retains a 16 GiB available-UMA admission floor.

**Tech Stack:** Python 3.12, PyTorch CUDA allocator, PaddleX CLI, Bash, YAML, pytest, FastAPI Agent API.

## Global Constraints

- Default `ocr.cuda_memory_limit_gb` is exactly `16` and remains YAML-configurable.
- Default `read_file.main_llm_memory_reserve_gb` is exactly `16`; this is an admission floor, not an allocation cap.
- Preserve official PaddleOCR-VL-1.6 full Pipeline, Transformers, CUDA GPU 0, BF16 VLM, loopback `127.0.0.1:30001`, one service process, and TERM-only ownership.
- Do not modify third-party dependency source.
- Do not restart or modify SGLang.
- Do not run Git commands or dispatch sub-agents.

---

### Task 1: Typed Configuration and CUDA-Limited PaddleX Wrapper

**Files:**
- Create: `scripts/paddle_ocr_service.py`
- Create: `tests/test_paddle_ocr_memory_limiter.py`
- Modify: `backend/config.py:134-149,1012-1044`
- Modify: `config/config-server.yaml:31-42`
- Modify: `config/config-misc.yaml:33-38`
- Modify: `tests/test_runtime_config_limits.py:11-25`

**Interfaces:**
- Produces: `OCRServiceConfig.cuda_memory_limit_gb: int`.
- Produces: `apply_cuda_memory_limit(limit_gb: int, report_path: Path, torch_module=None) -> dict[str, object]`.
- Produces: `main(argv: list[str] | None = None) -> int`, forwarding remaining arguments to `paddlex.__main__.console_entry()` only after the cap succeeds.

- [x] **Step 1: Write failing config and wrapper tests**

Test that active config loads both values as 16 and `OCRServiceConfig(cuda_memory_limit_gb=0)` raises `ValueError`. In `tests/test_paddle_ocr_memory_limiter.py`, load the wrapper module by path and use a fake Torch object whose CUDA surface records `set_per_process_memory_fraction`. Assert a 16 GiB limit on a 128 GiB device calls the allocator with `0.125`, writes this exact report, and rejects zero or a limit greater than total before writing:

```python
{
    "schemaVersion": 1,
    "pid": os.getpid(),
    "cudaMemoryLimitGiB": 16,
    "cudaMemoryLimitBytes": 16 * 1024**3,
    "deviceTotalMemoryBytes": 128 * 1024**3,
    "allocatorFraction": 0.125,
}
```

Also inject a fake `paddlex.__main__.console_entry`, call `main(["--cuda-memory-limit-gb", "16", "--memory-report", path, "--serve", "--port", "30001"])`, and assert PaddleX sees exactly `paddlex --serve --port 30001` after the report exists.

- [x] **Step 2: Run RED tests**

Run:

```bash
.sglang/bin/python -m pytest tests/test_paddle_ocr_memory_limiter.py tests/test_runtime_config_limits.py::test_read_file_v7_defaults_are_loaded -q
```

Expected: fail because the wrapper and OCR config field do not exist and the admission floor is still 32.

- [x] **Step 3: Implement configuration and wrapper**

Add `cuda_memory_limit_gb: int = 16` and a positive-value `__post_init__` check to `OCRServiceConfig`; parse it in `ConfigLoader._parse_ocr_config`. Add YAML value `ocr.cuda_memory_limit_gb: 16` and lower `read_file.main_llm_memory_reserve_gb` to 16.

Implement the wrapper with no top-level PaddleX import. `apply_cuda_memory_limit` validates CUDA, integer limit, and device total, calls the allocator, then writes the report with `tempfile.mkstemp`, `fsync`, and `os.replace`. `main` parses only the two wrapper options, applies the limit, sets `sys.argv = ["paddlex", *paddlex_args]`, imports `console_entry`, and returns `int(console_entry() or 0)`.

- [x] **Step 4: Run GREEN tests**

Run the Step 2 command. Expected: all pass.

---

### Task 2: Bind Allocator Evidence to Canary and Reuse

**Files:**
- Modify: `scripts/paddle_ocr_preflight.py:585-648,858-992,1001-1048`
- Modify: `tests/test_paddle_ocr_startup.py:616-884`

**Interfaces:**
- Produces: `validate_cuda_memory_limit_report(path: Path, expected_limit_gb: int, expected_pid: int) -> dict[str, object]`.
- Extends: `run_canaries(..., memory_report_path: Path | None = None, cuda_memory_limit_gb: int | None = None)`.
- Extends: `validate_reusable_service(..., memory_report_path: Path | None = None, cuda_memory_limit_gb: int | None = None)`.

- [x] **Step 1: Write failing report, canary, and reuse tests**

Add a valid report fixture with the Task 1 shape. Assert validation accepts it and rejects schema mismatch, wrong PID, wrong limit, non-finite/wrong fraction, and limit above total. Update the main canary test to pass report path plus 16 and assert `attestation["cudaMemoryLimit"]["cudaMemoryLimitGiB"] == 16`. Update the successful reuse test to include the same object in attestation, pass report path/16, and assert the reuse result reports 16. Add an old-attestation test proving expected limit 16 rejects missing evidence.

- [x] **Step 2: Run RED tests**

Run the named new report tests plus the existing canary/reuse success tests. Expected: fail because memory evidence is not accepted or attested.

- [x] **Step 3: Implement report validation and CLI plumbing**

Validate exact integer fields, expected PID/limit/bytes, positive device total, and `math.isclose(allocatorFraction, limitBytes / totalBytes, rel_tol=1e-12)`. If either optional memory argument is supplied, require both. Store the validated report as `cudaMemoryLimit` in canary attestation. During reuse require the attested object to equal the current validated report. Add required production CLI options `--memory-report` and `--cuda-memory-limit-gb` to `reuse` and `canary`, forwarding them to the functions.

- [x] **Step 4: Run GREEN tests**

Run the Step 2 tests. Expected: all pass.

---

### Task 3: Wire YAML Through the OCR Launcher

**Files:**
- Modify: `scripts/2_start_OCR_LM.sh`
- Modify: `tests/test_paddle_ocr_startup.py:259-590`
- Modify: `docs/read_file-tool-全面升级-V7.MD`
- Modify: `docs/superpowers/specs/2026-07-26-read-file-v7-paddleocr-design.md`

**Interfaces:**
- Consumes: `OCR_CUDA_MEMORY_LIMIT_GB` environment override or `config-overall.yaml -> server -> ocr.cuda_memory_limit_gb`.
- Consumes: `OCR_SERVICE_WRAPPER`, default `scripts/paddle_ocr_service.py`.
- Produces: `OCR_MEMORY_REPORT`, default `logs/paddleocr-cuda-memory-limit.json`.

- [x] **Step 1: Write failing launcher-chain tests**

Assert the script contains and forwards `--cuda-memory-limit-gb` and `--memory-report` to wrapper, canary, and both reuse checks; assert the PaddleX process command starts through `OCR_PYTHON + OCR_SERVICE_WRAPPER`, not the generated `paddlex` executable. Update fake launcher environments with `OCR_CUDA_MEMORY_LIMIT_GB=16`, `OCR_MEMORY_REPORT=<tmp>`, and `OCR_SERVICE_WRAPPER=<fake paddlex>` so concurrency tests exercise the new argument chain without CUDA.

- [x] **Step 2: Run RED launcher tests**

Run:

```bash
.sglang/bin/python -m pytest tests/test_paddle_ocr_startup.py -q
```

Expected: new forwarding assertions fail.

- [x] **Step 3: Implement launcher plumbing**

Resolve server YAML from `config-overall.yaml` with the locked OCR Python and PyYAML when no environment override exists. Reject a non-integer or non-positive limit. Require the wrapper file. Pass memory report and expected limit into pre-lock reuse, locked reuse, and canary. Remove a stale report only immediately before starting a new owned service. Launch:

```bash
"$OCR_PYTHON" "$OCR_SERVICE_WRAPPER" \
  --cuda-memory-limit-gb "$OCR_CUDA_MEMORY_LIMIT_GB" \
  --memory-report "$OCR_MEMORY_REPORT" \
  --serve --pipeline "$PIPELINE_CONFIG" --engine transformers \
  --device gpu:0 --host "$OCR_HOST" --port "$OCR_PORT"
```

Keep lock, PID, log, signal, and shutdown behavior unchanged.

- [x] **Step 4: Run GREEN launcher and related tests**

Run:

```bash
.sglang/bin/python -m pytest tests/test_paddle_ocr_memory_limiter.py tests/test_paddle_ocr_startup.py tests/test_runtime_config_limits.py tests/test_read_file_cache_resource.py -q
bash -n scripts/2_start_OCR_LM.sh
.sglang/bin/python -m compileall -q scripts/paddle_ocr_service.py scripts/paddle_ocr_preflight.py backend/config.py
```

Expected: all tests pass; syntax/compile commands exit 0.

---

### Task 4: Restart Services and Run Hardware/Agent E2E

**Files:**
- Runtime output: `logs/paddleocr-cuda-memory-limit.json`
- Runtime output: `logs/paddleocr-runtime-attestation.json`

**Interfaces:**
- Consumes: existing OCR owner PID and FastAPI PID.
- Produces: limited OCR at `127.0.0.1:30001`, restarted backend at `8090`, unchanged SGLang at `30000`.

- [x] **Step 1: Gracefully stop the old OCR and backend owners**

Send TERM to their launcher/owned processes, wait for ports 30001 and 8090 to close, and confirm SGLang port 30000 remains healthy. Do not use KILL unless an existing bounded project launcher escalates according to its own policy; the OCR launcher itself remains TERM-only.

- [x] **Step 2: Start OCR and wait for canary/attestation**

Start `scripts/2_start_OCR_LM.sh` detached with its existing file logging, poll `openapi.json`, and require attestation plus report PID/limit agreement at 16 GiB. Query `nvidia-smi --query-compute-apps=pid,used_gpu_memory` and require OCR usage below 16384 MiB.

- [x] **Step 3: Start FastAPI and verify active config**

Start `scripts/3_start_backend.sh` detached, poll `/health`, and verify a fresh Python config load reports both values as 16.

- [x] **Step 4: Run direct tool and Agent API E2E**

First call `backend.tools.file_ops.read_file` for the original PDF and require content rather than `MEMORY_RESERVE`. Then POST non-streaming JSON to `/v1/agent/chat`:

```json
{
  "message": "读取 `/home/zk/Documents/Jetson_Thor_PCN213501_Update_of_the_Jetson_thor_OrderingPN.pdf`",
  "stream": false,
  "mode": "tool_calling",
  "enable_thinking": false
}
```

Require HTTP 200, a non-empty answer grounded in the PDF, and no `MEMORY_RESERVE`, `INVALID_DOCUMENT_MODE`, malformed-tool transcript, or tool failure in the response/log evidence.

- [x] **Step 5: Final verification**

Rerun the Task 3 regression command, validate OCR/backend/SGLang health, confirm the active OCR PID equals report/attestation PID, and confirm the configured allocator ceiling remains 16 GiB.

### E2E Blocker Resolved During Execution

- [x] Added `tests/test_llm_backend_selection.py::test_sglang_local_health_client_ignores_environment_proxies` after the restarted backend inherited `ALL_PROXY=socks5h://...` and failed before contacting loopback SGLang.
- [x] Set `trust_env=False` on SGLang, vLLM, container-provider, and SGLang abort `httpx.Client` instances in `backend/llm_service.py`, matching the existing PaddleOCR loopback client policy.
- [x] Restarted FastAPI and verified `/config` reports `active_backend=sglang` before rerunning Agent E2E.
