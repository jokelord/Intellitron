# PaddleOCR CUDA Memory Limit Design

## Problem

The Read File admission setting `main_llm_memory_reserve_gb` only rejects work
when system-available UMA is low. It does not allocate or cap PaddleOCR memory.
Lowering that setting cannot guarantee an OCR model memory ceiling.

The PaddleX CLI exposes no CUDA memory-limit option. Jetson Thor cgroup v2 also
does not account CUDA allocations: a diagnostic service with `MemoryMax=1G`
successfully allocated and touched 2 GiB of CUDA memory while systemd reported a
1.3 MiB memory peak. A cgroup-only design would therefore be a false limit.

The running official PaddleOCR-VL-1.6 service currently peaks at approximately
6.1 GiB reported GPU memory and 3.1 GiB process RSS on the four-page distorted
PDF canary. The required default CUDA allocation ceiling is 16 GiB and must be
YAML-configurable.

## Goals

- Enforce a 16 GiB default ceiling on PyTorch CUDA allocations in the PaddleX
  Transformers process before any OCR model is imported or loaded.
- Keep the ceiling configurable through project YAML.
- Prove the active process applied the expected ceiling before writing the
  runtime attestation or reusing the service.
- Lower the independent document-work admission floor to 16 GiB available UMA.
- Preserve the official complete PaddleOCR-VL-1.6 Pipeline, Transformers/CUDA,
  loopback endpoint, PID ownership, canaries, and TERM-only shutdown.

## Non-Goals

- Modifying PaddleX, PaddleOCR, Transformers, or PyTorch package source.
- Treating Linux cgroup memory accounting as a CUDA allocation limit.
- Capping ordinary CPU RSS inside the PaddleX process. Source, decoded-image,
  concurrency, and available-UMA guards continue to control host pressure.
- Restarting SGLang or changing its memory configuration.

## Configuration

`config/config-server.yaml` adds:

```yaml
ocr:
  cuda_memory_limit_gb: 16
```

`OCRServiceConfig.cuda_memory_limit_gb` is a positive integer. The OCR launcher
reads this value through the `config-overall.yaml -> server YAML` reference.
`OCR_CUDA_MEMORY_LIMIT_GB` may override it for explicit operations and tests.

`config/config-misc.yaml` sets:

```yaml
read_file:
  main_llm_memory_reserve_gb: 16
```

This second value is an admission floor: Read File work is rejected if less
than 16 GiB remains system-available. It is not the OCR CUDA ceiling.

## Memory-Limited Launcher

A project-owned `scripts/paddle_ocr_service.py` runs under the locked
`.paddle-ocr` Python environment. It accepts the configured limit, a report
path, and the unmodified PaddleX CLI arguments.

Before importing PaddleX it:

1. imports Torch and requires CUDA availability;
2. converts the configured GiB ceiling to bytes;
3. reads `torch.cuda.get_device_properties(0).total_memory`;
4. rejects a non-positive limit or a limit greater than device-visible total;
5. calls `torch.cuda.set_per_process_memory_fraction(limit_bytes / total_bytes,
   device=0)`;
6. atomically writes a JSON report containing schema version, PID, limit GiB,
   limit bytes, device total bytes, and allocator fraction;
7. rewrites `sys.argv` and calls `paddlex.__main__.console_entry()`.

The wrapper sets a ceiling; it does not preallocate 16 GiB. Allocation failures
above the ceiling surface as OCR startup/request failures rather than consuming
the main LLM's remaining UMA.

`scripts/2b_start_OCR_LM.sh` launches this wrapper instead of the generated
`paddlex` entry-point script. The wrapper invokes the same official PaddleX CLI
inside the same process, so the service PID remains directly owned and existing
signal/wait behavior remains valid.

## Runtime Proof and Reuse

The wrapper report is not sufficient by itself unless it is bound to the live
listener. `paddle_ocr_preflight.py canary` receives the report path and expected
limit, validates its schema and values, and requires its PID to equal the
attested listener service PID. The validated memory-limit object is stored in
the atomic runtime attestation.

`paddle_ocr_preflight.py reuse` receives the same expected limit and report
path. Reuse succeeds only when:

- the existing process identity and listener checks pass;
- the attestation contains the expected CUDA limit;
- the live wrapper report has the same expected limit and service PID.

An old attestation without this evidence is incompatible. The launcher remains
non-destructive and reports the mismatch; operational restart explicitly stops
the old owned service before starting the limited service.

## Error Handling

- Invalid YAML or environment limit: fail before starting PaddleX.
- Limit greater than device-visible total: wrapper exits before PaddleX import.
- CUDA unavailable or allocator-cap call fails: wrapper exits before writing a
  valid report.
- Missing, stale, malformed, wrong-PID, or wrong-limit report: canary/reuse
  fails and no attestation is accepted.
- Allocator ceiling exceeded during inference: the request fails normally;
  Read File does not retry by launching another model copy.

## Testing

- Config tests lock both 16 GiB defaults and reject non-positive limits.
- Wrapper tests use a fake Torch module and fake PaddleX entry point to prove
  fraction calculation, import/call ordering, argument forwarding, atomic
  report shape, and failure before PaddleX on invalid limits.
- Preflight tests validate correct reports and reject missing, malformed,
  wrong-PID, and wrong-limit reports.
- Startup tests prove YAML/env value forwarding into wrapper, canary, and reuse;
  no direct `paddlex --serve` path may remain.
- Existing launcher concurrency, attestation, canary, and shutdown regressions
  remain green.
- Hardware verification restarts only OCR and FastAPI, confirms the attestation
  says 16 GiB, checks OCR GPU memory remains below the ceiling, and reads the
  original four-page PDF through the Agent tool.

## Acceptance Criteria

- The active OCR attestation is bound to a live process that applied a 16 GiB
  PyTorch CUDA allocator ceiling before PaddleX import.
- Changing `ocr.cuda_memory_limit_gb` in YAML changes the enforced ceiling after
  OCR restart; an old differently limited service is not reused.
- The document-processing admission floor is 16 GiB available UMA.
- The original PDF succeeds through `read_file` after OCR and backend restart.
- SGLang remains running and unchanged.
