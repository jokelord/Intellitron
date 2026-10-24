# vLLM Container Configuration Forwarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Launch both NGC vLLM container variants from the active YAML model and vLLM configuration without hardcoded vLLM API-server values.

**Architecture:** Each script resolves the selected configuration, reads scalar and JSON values once with Python, then appends each supported vLLM flag through explicit Bash `if` branches. Snapshot and ordinary model mount resolution remain separate because their host path semantics differ. A fake Docker executable tests command construction and compatibility validation without launching vLLM.

**Tech Stack:** Bash, Python 3 with PyYAML, Docker CLI, pytest.

## Global Constraints

- Target image is exactly `nvcr.io/nvidia/vllm:26.06-py3`.
- Do not use local source/uv vLLM as a deployment target; Jetson Thor SM110a uses the NVIDIA NGC container runtime.
- `config-overall.yaml` selects the model and infer files; `vllm` is the server configuration source.
- Use explicit `if ...; then ARGS+=(...); fi` argument branches, never one-line short-circuit appends.
- Do not hardcode model-specific `vllm.entrypoints.openai.api_server` values in Shell.
- Print the actual Docker invocation using `printf '%q'` before execution.
- Do not perform an end-to-end vLLM inference-server launch during verification.
- Do not run Git commands or create commits without explicit user permission.

---

### Task 1: Establish a no-launch command-construction regression test

**Files:**
- Create: `tests/test_vllm_container_scripts.py`
- Modify: `scripts/1b_start_vllm_container_with_snapshots.sh`
- Modify: `scripts/1c_start_vllm_container.sh`

**Interfaces:**
- Consumes: `CONFIG_OVERALL_PATH` optional environment override and `VLLM_DOCKER_BIN` optional Docker executable override.
- Produces: each launcher prints `实际启动命令` and calls the Docker executable only for image-help validation and the final constructed invocation.

- [ ] **Step 1: Write failing tests with a fake Docker executable**

```python
def test_snapshot_launcher_uses_config_values_and_prints_final_command(tmp_path):
    completed = run_launcher("1b_start_vllm_container_with_snapshots.sh", tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert "--quantization compressed-tensors" in completed.stdout
    assert "--max-num-seqs 7" in completed.stdout
    assert "--max-num-batched-tokens 8192" in completed.stdout
    assert "--model /model-cache/snapshots/revision-a" in completed.stdout


def test_snapshot_launcher_rejects_ambiguous_cache_root(tmp_path):
    completed = run_launcher("1b_start_vllm_container_with_snapshots.sh", tmp_path, snapshots=2)
    assert completed.returncode != 0
    assert "多个 snapshot" in completed.stderr


def test_normal_launcher_uses_direct_model_directory(tmp_path):
        completed = run_launcher("1c_start_vllm_container.sh", tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert "--model /model" in completed.stdout
    assert "--no-enable-prefix-caching" in completed.stdout
```

The fixture writes a temporary overall/model/infer YAML set, a normal model directory, a Hugging Face cache with `blobs` and `snapshots/revision-a`, and a fake Docker executable. The fake writes `--help` output containing the tested flags and exits successfully without running an engine.

- [ ] **Step 2: Run the focused test file and confirm it fails before implementation**

Run: `pytest tests/test_vllm_container_scripts.py -v`

Expected: FAIL because the current scripts ignore the test configuration override and hardcode model/API-server settings.

- [ ] **Step 3: Add test-only injection seams without changing production defaults**

Add these assignments near each script's project/config initialization:

```bash
YAML_OVERALL="${CONFIG_OVERALL_PATH:-$PROJECT_ROOT/config/config-overall.yaml}"
DOCKER_BIN="${VLLM_DOCKER_BIN:-docker}"
```

Replace direct `docker` calls with `"$DOCKER_BIN"`. The normal production path remains the repository overall YAML and Docker CLI.

- [ ] **Step 4: Re-run the focused test file**

Run: `pytest tests/test_vllm_container_scripts.py -v`

Expected: tests still fail on hardcoded launch arguments, but the scripts reach the fake Docker executable and resolve the test YAML.

### Task 2: Normalize the active NVFP4 vLLM configuration

**Files:**
- Modify: `config/qwen3__6-herotic-NVFP4-config-infer.yaml:114-280`
- Test: `tests/test_vllm_container_scripts.py`

**Interfaces:**
- Consumes: vLLM 26.06 API-server options verified from `python3 -m vllm.entrypoints.openai.api_server --help` in the target image.
- Produces: a `vllm` mapping whose non-null fields can be transferred or explicitly rejected by the launchers.

- [ ] **Step 1: Extend the failing test to assert legacy-field rejection**

```python
def test_launcher_rejects_a_set_legacy_vllm_option(tmp_path):
    completed = run_launcher(
        "1c_start_vllm_container.sh",
        tmp_path,
        infer_overrides={"preemption_mode": "swap"},
    )
    assert completed.returncode != 0
    assert "preemption_mode" in completed.stderr
    assert "vLLM 26.06" in completed.stderr
```

- [ ] **Step 2: Run the focused test and confirm the rejection assertion fails**

Run: `pytest tests/test_vllm_container_scripts.py::test_launcher_rejects_a_set_legacy_vllm_option -v`

Expected: FAIL because the existing launcher silently omits this configuration.

- [ ] **Step 3: Correct the active `vllm` values from the known-good snapshot invocation**

Set these YAML keys to the explicit values previously contained in `1b`:

```yaml
gpu_memory_utilization: 0.65
dtype: auto
quantization: compressed-tensors
trust_remote_code: true
attention_backend: flashinfer
enable_chunked_prefill: true
enable_prefix_caching: true
reasoning_parser: qwen3
tool_call_parser: qwen3_coder
```

Keep `max_model_len`, `max_num_seqs`, and `max_num_batched_tokens` as YAML-controlled values. Keep unsupported legacy fields null and amend their comments to name vLLM 26.06's replacement or state that no direct equivalent exists.

- [ ] **Step 4: Re-run YAML parsing and the focused legacy test**

Run: `python3 -c "import yaml; yaml.safe_load(open('config/qwen3__6-herotic-NVFP4-config-infer.yaml'))" && pytest tests/test_vllm_container_scripts.py::test_launcher_rejects_a_set_legacy_vllm_option -v`

Expected: YAML parsing succeeds; the focused test remains red until Task 3 implements rejection.

### Task 3: Implement configuration extraction, flag forwarding, and image compatibility checks

**Files:**
- Modify: `scripts/1b_start_vllm_container_with_snapshots.sh`
- Modify: `scripts/1c_start_vllm_container.sh`
- Test: `tests/test_vllm_container_scripts.py`

**Interfaces:**
- Consumes: resolved model path, selected infer YAML, and `vllm` mapping.
- Produces: `VLLM_ARGS` Bash array beginning with `python3 -m vllm.entrypoints.openai.api_server --model <container-model-path>`.

- [ ] **Step 1: Replace duplicated partial YAML readers with one safe extraction block per script**

The embedded Python loads the overall YAML, resolves relative model/infer paths against the repository root, validates both files, and emits shell-quoted variables for:

```text
MODEL_PATH, VLLM_HOST, VLLM_PORT, TENSOR_PARALLEL_SIZE,
PIPELINE_PARALLEL_SIZE, DISTRIBUTED_EXECUTOR_BACKEND, MAX_MODEL_LEN,
GPU_MEMORY_UTILIZATION, DTYPE, QUANTIZATION, TOKENIZER, REVISION,
TOKENIZER_REVISION, LOAD_FORMAT, DOWNLOAD_DIR, TRUST_REMOTE_CODE,
ENFORCE_EAGER, MAX_NUM_SEQS, MAX_NUM_BATCHED_TOKENS, BLOCK_SIZE,
ENABLE_PREFIX_CACHING, KV_CACHE_DTYPE, CPU_OFFLOAD_GB,
ATTENTION_BACKEND, FLASH_ATTN_VERSION, ENABLE_CHUNKED_PREFILL,
DISABLE_SLIDING_WINDOW, DISABLE_LOG_STATS, DISABLE_LOG_REQUESTS,
MAX_LOGPROBS, DISABLE_CUSTOM_ALL_REDUCE, REASONING_PARSER,
CHAT_TEMPLATE, TOOL_CALL_PARSER, ENABLE_AUTO_TOOL_CHOICE,
SERVED_MODEL_NAME, ENABLE_LORA, MAX_LORAS, MAX_LORA_RANK,
MAX_CPU_LORAS, FULLY_SHARDED_LORAS, HF_OVERRIDES,
LIMIT_MM_PER_PROMPT, SKIP_MM_PROFILING, MODEL_LOADER_EXTRA_CONFIG,
IGNORE_PATTERNS, MAX_PARALLEL_LOADING_WORKERS
```

Serialize `hf_overrides` and `limit_mm_per_prompt` compactly with `json.dumps`; render null as an empty string and booleans as lowercase strings.

- [ ] **Step 2: Implement shell value helpers and explicit argument branches**

```bash
is_set_value() {
    local value="${1:-}"
    [[ -n "$value" && "$value" != "null" && "$value" != "None" ]]
}

append_boolean_arg() {
    local value="$1"
    local positive_flag="$2"
    if [ "$value" = "true" ]; then
        VLLM_ARGS+=("$positive_flag")
    elif [ "$value" = "false" ]; then
        VLLM_ARGS+=("--no-${positive_flag#--}")
    fi
}
```

Use explicit `if` branches for every non-boolean value and call the boolean helper only for vLLM 26.06 options that expose both forms. Pass `flash_attn_version` as a single JSON `--attention-config` value. Do not drop configured default values such as `load_format: auto`, `pipeline_parallel_size: 1`, or false booleans.

- [ ] **Step 3: Reject configured unsupported legacy fields**

```bash
reject_legacy_value() {
    local config_key="$1"
    local value="$2"
    if is_set_value "$value"; then
        fail "vllm.${config_key} 在 vLLM 26.06 中没有可安全映射的 CLI 参数；请移除该值或改用 vLLM 26.06 的对应配置。"
    fi
}
```

Call this for `disable_flash_attn`, `enable_reasoning`, `enable_multi_modal`, `disable_mm_preprocessor_cache`, `guided_decoding_backend`, `preemption_mode`, `scheduler_delay_factor`, and all legacy speculative fields. Do not reject `false` values for declarative legacy booleans; reject only truthy values that would request behavior.

- [ ] **Step 4: Validate final flags against the exact image help output**

```bash
API_SERVER_HELP="$("$DOCKER_BIN" run --rm --entrypoint python3 "$CONTAINER_TAG" \
    -m vllm.entrypoints.openai.api_server --help)"
for argument in "${VLLM_ARGS[@]}"; do
    if [[ "$argument" == --* ]] && ! grep -Fq -- "$argument" <<<"$API_SERVER_HELP"; then
        fail "目标镜像不支持 vLLM 参数: $argument"
    fi
done
```

Skip `--no-*` compatibility false positives by accepting the positive form only when the help text lists the exact paired `--no-` form. The fake Docker fixture must provide the resulting help forms.

- [ ] **Step 5: Run the focused command-construction and legacy tests**

Run: `pytest tests/test_vllm_container_scripts.py -v`

Expected: PASS with fake Docker only; no vLLM engine is started.

### Task 4: Implement robust normal and snapshot mount resolution plus command output

**Files:**
- Modify: `scripts/1b_start_vllm_container_with_snapshots.sh`
- Modify: `scripts/1c_start_vllm_container.sh`
- Test: `tests/test_vllm_container_scripts.py`

**Interfaces:**
- Consumes: absolute resolved `MODEL_PATH` from Task 3.
- Produces: `MODEL_MOUNT_HOST`, `MODEL_MOUNT_CONTAINER`, and `CONTAINER_MODEL_PATH` used in Docker and `VLLM_ARGS`.

- [ ] **Step 1: Implement ordinary model-path resolution**

```bash
HOST_MODEL_PATH="$(realpath -e "$MODEL_PATH")"
require_dir "$HOST_MODEL_PATH"
MODEL_MOUNT_HOST="$HOST_MODEL_PATH"
MODEL_MOUNT_CONTAINER="/model"
CONTAINER_MODEL_PATH="$MODEL_MOUNT_CONTAINER"
```

Use the resolved container path as the model argument generated in Task 3.

- [ ] **Step 2: Implement snapshot-aware resolution**

```bash
resolve_snapshot_model_path() {
    local configured_path
    configured_path="$(realpath -e "$1")" || fail "模型路径不存在: $1"
    if [ "$(basename "$(dirname "$configured_path")")" = "snapshots" ]; then
        SNAPSHOT_HOST_PATH="$configured_path"
        HF_CACHE_ROOT="$(dirname "$(dirname "$configured_path")")"
    elif [ -d "$configured_path/snapshots" ] && [ -d "$configured_path/blobs" ]; then
        HF_CACHE_ROOT="$configured_path"
        SNAPSHOT_HOST_PATH="$(resolve_unique_snapshot "$HF_CACHE_ROOT/snapshots")"
    else
        fail "snapshot 启动脚本要求 model_path 指向 snapshots/<revision> 或包含 snapshots 与 blobs 的缓存根目录"
    fi
    require_dir "$HF_CACHE_ROOT/blobs"
}
```

Derive the container snapshot path from the selected snapshot basename after mounting `HF_CACHE_ROOT` at `/model-cache`.

- [ ] **Step 3: Use host networking and preserve cache persistence**

Build the Docker array with `--network host`, GPU access, IPC sharing, cache mounts and required NGC runtime environment variables. Remove port publishing so YAML `host` and `port` reach vLLM unchanged. Keep cache locations isolated by image tag and configurable through `VLLM_CACHE_ROOT_HOST`.

- [ ] **Step 4: Print and execute the final array**

```bash
FINAL_ARGS=("$DOCKER_BIN" run "${DOCKER_ARGS[@]}" "$CONTAINER_TAG" "${VLLM_ARGS[@]}")
echo "🚀 实际启动命令："
printf '  %q' "${FINAL_ARGS[@]}"
echo
"${FINAL_ARGS[@]}"
```

Continue removing a same-name container before the final run, but derive the name from `model.model_name` after validating Docker-safe characters rather than using a model-specific constant.

- [ ] **Step 5: Run syntax, YAML, and all launcher tests**

Run: `bash -n scripts/1b_start_vllm_container_with_snapshots.sh && bash -n scripts/1c_start_vllm_container.sh && python3 -c "import yaml; yaml.safe_load(open('config/qwen3__6-herotic-NVFP4-config-infer.yaml'))" && pytest tests/test_vllm_container_scripts.py -v`

Expected: all commands pass. The fake Docker test confirms final argv without starting a vLLM engine.

### Task 5: Review the final behavior and documentation

**Files:**
- Modify: `docs/superpowers/specs/2026-07-11-vllm-container-config-forwarding-design.md`
- Modify: `docs/superpowers/plans/2026-07-11-vllm-container-config-forwarding.md`

**Interfaces:**
- Consumes: final scripts and passing no-launch tests.
- Produces: implementation documentation accurately describing the behavior and verification boundary.

- [ ] **Step 1: Inspect the printed fake-Docker invocation in test output**

Run: `pytest tests/test_vllm_container_scripts.py -v -s`

Expected: output includes the full escaped command and no real image engine process is launched.

- [ ] **Step 2: Update the design/plan only if implementation differs from approved behavior**

Document any image-help option whose exact vLLM 26.06 behavior required a different safe mapping. Do not document unverified runtime performance claims.

- [ ] **Step 3: Re-run all static and unit verification**

Run: `bash -n scripts/1b_start_vllm_container_with_snapshots.sh && bash -n scripts/1c_start_vllm_container.sh && pytest tests/test_vllm_container_scripts.py -v`

Expected: PASS; no end-to-end launch performed.
