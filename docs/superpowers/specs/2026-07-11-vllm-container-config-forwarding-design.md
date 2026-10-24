# vLLM Container Configuration Forwarding Design

## Goal

Make `scripts/1b_start_vllm_container_with_snapshots.sh` and
`scripts/1c_start_vllm_container.sh` launches the NVIDIA NGC vLLM image using the
active `config-overall.yaml` model and inference configuration. The scripts
must not hardcode `vllm.entrypoints.openai.api_server` configuration values.

The local source/uv vLLM path is intentionally out of scope. It has not been
a reliable deployment target for Jetson Thor SM110a, so container deployment
uses the NVIDIA NGC runtime instead.

## Configuration Source

Both scripts read the model and infer paths selected by `config-overall.yaml`.
`model.model_path` selects the host model. The `vllm` section of the selected
infer file is the source of all vLLM server configuration.

The active NVFP4 infer configuration will be corrected so values that were
previously hardcoded by the snapshot script are represented in `vllm`:

- compressed-tensors quantization
- auto dtype
- 0.65 GPU-memory utilization
- remote-code trust
- FlashInfer attention backend
- chunked prefill and prefix caching
- Qwen tool-call and reasoning parsers

Fields with no valid equivalent in vLLM 26.06 are not silently ignored. A
non-null legacy value produces an actionable startup error. Null legacy fields
remain valid for compatibility with the backend configuration reader.

## Command Construction

The scripts use Bash arrays. Each setting is appended with an explicit
`if ...; then ARGS+=(...); fi` branch. This is preferred to the short-circuit
form because it is readable while handling string, JSON, and bidirectional
boolean arguments safely.

All values are parsed once from YAML. Booleans map to the target image's
`--flag` or `--no-flag` form. JSON values retain their original boundaries as
single argv values. The scripts query the exact NGC image's
`api_server --help` output before startup and reject unsupported final flags.

Before execution, each script prints the complete `docker run` command using
`printf '%q'`, matching the observability pattern used by the SGLang launcher.

## Model Mount Resolution

`1c_start_vllm_container.sh` resolves `model.model_path` as a normal model
directory, validates it, mounts it, and derives the model's container path.

The snapshot launcher supports both forms:

- A direct `.../snapshots/<revision>` path. It resolves symlinks, validates
  the cache root and sibling `blobs` directory, then mounts the cache root.
- A Hugging Face cache root containing `snapshots/` and `blobs/`. It selects
  the only snapshot when exactly one exists. With multiple snapshots it fails
  and directs the user to set `model.model_path` to one explicit revision.

This recognizes the distinct model-path semantics without selecting an
ambiguous checkpoint.

## Container Networking And Runtime

The container uses host networking so `vllm.host` and `vllm.port` are passed
unchanged. This avoids the current bridge-networking conflict where a server
bound to `127.0.0.1` inside the container cannot be reached through a published
host port.

The persistent vLLM, Triton, and TorchInductor cache mounts remain in place,
with cache roots isolated by NGC image tag. The fixed image is
`nvcr.io/nvidia/vllm:26.06-py3`; the scripts retain no model-specific vLLM CLI
values.

## Verification

- Parse both scripts with `bash -n`.
- Parse the updated YAML with Python/PyYAML.
- Exercise extraction and path-resolution branches using temporary model
  layouts without launching an inference engine.
- Inspect the final command construction and run the NGC image help check.
