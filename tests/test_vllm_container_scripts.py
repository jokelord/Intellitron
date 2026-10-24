import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]

API_SERVER_HELP = """
--model --host --port --tensor-parallel-size --pipeline-parallel-size
--distributed-executor-backend --max-model-len --gpu-memory-utilization
--dtype --quantization --tokenizer --revision --tokenizer-revision
--load-format --download-dir --trust-remote-code --no-trust-remote-code
--enforce-eager --no-enforce-eager --max-num-seqs --max-num-batched-tokens
--block-size --enable-prefix-caching --no-enable-prefix-caching
--kv-cache-dtype --cpu-offload-gb --attention-backend --attention-config
--enable-chunked-prefill --no-enable-chunked-prefill --disable-sliding-window
--no-disable-sliding-window --disable-log-stats --enable-log-requests
--no-enable-log-requests --max-logprobs --disable-custom-all-reduce
--no-disable-custom-all-reduce --reasoning-parser --chat-template
--tool-call-parser --tool-parser-plugin --enable-auto-tool-choice --no-enable-auto-tool-choice
--served-model-name --enable-lora --no-enable-lora --max-loras
--max-lora-rank --max-cpu-loras --fully-sharded-loras --no-fully-sharded-loras
--hf-overrides --limit-mm-per-prompt --skip-mm-profiling --no-skip-mm-profiling
--model-loader-extra-config --ignore-patterns --max-parallel-loading-workers
"""


def run_launcher(
    script_name: str,
    tmp_path: Path,
    *,
    snapshots: int = 1,
    infer_overrides: dict[str, object] | None = None,
    api_server_help: str = API_SERVER_HELP,
    api_server_help_stderr: str = "",
    model_path_value: Path | None = None,
    model_name: str = "test-model",
    relative_selections: bool = False,
    invalid_snapshot_link: bool = False,
    cache_root_host: str | None = None,
):
    model_path = tmp_path / "direct-model"
    model_path.mkdir()

    cache_root = tmp_path / "model-cache"
    (cache_root / "blobs").mkdir(parents=True)
    blob = cache_root / "blobs" / "config.json"
    blob.write_text("{}", encoding="utf-8")
    for index in range(snapshots):
        snapshot = cache_root / "snapshots" / f"revision-{chr(ord('a') + index)}"
        snapshot.mkdir(parents=True)
        target = (
            tmp_path / "outside-blob.json"
            if invalid_snapshot_link and index == 0
            else Path("../../blobs/config.json")
        )
        if invalid_snapshot_link and index == 0:
            target.write_text("{}", encoding="utf-8")
        (snapshot / "config.json").symlink_to(target)

    configured_model_path = model_path_value or (
        cache_root
        if script_name == "1_B_start_vllm_container_with_snapshots.sh"
        else model_path
    )

    model_config = tmp_path / "model.yaml"
    model_config.write_text(
        yaml.safe_dump(
            {"model": {"model_path": str(configured_model_path), "model_name": model_name}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    infer_config = tmp_path / "infer.yaml"
    infer_config_data = {
        "vllm": {
            "host": "127.0.0.1",
            "port": 8000,
            "max_model_len": 12345,
            "gpu_memory_utilization": 0.42,
            "quantization": "compressed-tensors",
            "max_num_seqs": 7,
            "max_num_batched_tokens": 8192,
            "enable_prefix_caching": False,
            "trust_remote_code": False,
            "enforce_eager": False,
            "pipeline_parallel_size": 1,
            "load_format": "auto",
            "disable_sliding_window": False,
            "disable_log_stats": False,
            "disable_log_requests": False,
            "disable_custom_all_reduce": False,
            "enable_auto_tool_choice": False,
            "enable_lora": False,
            "fully_sharded_loras": False,
            "skip_mm_profiling": False,
            "flash_attn_version": 4,
            "hf_overrides": {"max_pixels": 42},
            "limit_mm_per_prompt": {"image": 2},
        },
        "container": {"api_url": "http://localhost:9123/v1"},
    }
    infer_config_data["vllm"].update(infer_overrides or {})
    infer_config.write_text(
        yaml.safe_dump(infer_config_data, sort_keys=False), encoding="utf-8"
    )
    overall_config = tmp_path / "overall.yaml"
    selected_model_path = (
        os.path.relpath(model_config, PROJECT_ROOT) if relative_selections else model_config
    )
    selected_infer_path = (
        os.path.relpath(infer_config, PROJECT_ROOT) if relative_selections else infer_config
    )
    overall_config.write_text(
        f"model: {selected_model_path}\ninfer: {selected_infer_path}\n", encoding="utf-8"
    )

    fake_docker_log = tmp_path / "fake-docker.log"
    fake_docker = tmp_path / "fake-docker"
    fake_docker.write_text(
        f"""#!/bin/sh
printf 'CALL:' >> "$FAKE_DOCKER_LOG"
printf ' <%s>' "$@" >> "$FAKE_DOCKER_LOG"
printf '\\n' >> "$FAKE_DOCKER_LOG"
for argument in "$@"; do
    if [ "$argument" = "--help" ]; then
        cat <<'EOF'
{api_server_help}
EOF
        cat >&2 <<'EOF'
{api_server_help_stderr}
EOF
    fi
done
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    guard_dir = tmp_path / "guard-bin"
    guard_dir.mkdir()
    docker_guard = guard_dir / "docker"
    docker_guard.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'real Docker is forbidden in this test' >&2\nexit 97\n",
        encoding="utf-8",
    )
    docker_guard.chmod(0o755)

    environment = os.environ | {
        "CONFIG_OVERALL_PATH": str(overall_config),
        "VLLM_DOCKER_BIN": str(fake_docker),
        "FAKE_DOCKER_LOG": str(fake_docker_log),
        "VLLM_CACHE_ROOT_HOST": cache_root_host or str(tmp_path / "vllm-cache"),
        "PATH": f"{guard_dir}:{os.environ['PATH']}",
    }
    completed = subprocess.run(
        ["bash", str(PROJECT_ROOT / "scripts" / script_name)],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed, fake_docker_log, cache_root


def final_docker_call(fake_docker_log: Path) -> str:
    calls = fake_docker_log.read_text(encoding="utf-8").splitlines()
    return next(call for call in reversed(calls) if call.startswith("CALL: <run>"))


def assert_final_call_disables_persistent_compile_cache(final_call: str):
    assert "<--gpus> <all>" in final_call
    assert "<--ipc=host>" in final_call
    assert ":/root/.cache/vllm>" not in final_call
    assert ":/root/.triton>" not in final_call
    assert ":/tmp/torchinductor_root>" not in final_call
    assert "VLLM_CACHE_ROOT=/root/.cache/vllm" not in final_call
    assert "TRITON_CACHE_DIR=/root/.triton/cache" not in final_call
    assert "TORCHINDUCTOR_CACHE_DIR=/tmp/torchinductor_root" not in final_call


@pytest.mark.parametrize(
    "script_name",
    ["1_C_start_vllm_container.sh", "1_B_start_vllm_container_with_snapshots.sh"],
)
def test_launcher_serializes_mapping_model_loader_extra_config_as_one_compact_json_argument(
    tmp_path, script_name
):
    config = {"load_config": {"use_safetensors": True}, "revision": 7}
    completed, fake_docker_log, _ = run_launcher(
        script_name,
        tmp_path,
        infer_overrides={"model_loader_extra_config": config},
    )

    assert completed.returncode == 0, completed.stderr
    final_call = final_docker_call(fake_docker_log)
    match = re.search(r"<--model-loader-extra-config> <([^>]*)>", final_call)
    assert match
    assert match.group(1) == json.dumps(config, separators=(",", ":"))
    assert json.loads(match.group(1)) == config


def test_snapshot_launcher_mounts_configured_cache_root_and_prints_final_command(tmp_path):
    cache_root = tmp_path / "model-cache"
    completed, fake_docker_log, _ = run_launcher(
        "1_B_start_vllm_container_with_snapshots.sh",
        tmp_path,
        model_path_value=cache_root,
        model_name="Qwen/Test Model.v1",
    )

    assert completed.returncode == 0, completed.stderr
    assert fake_docker_log.exists()
    final_call = final_docker_call(fake_docker_log)
    assert "实际启动命令:" in completed.stdout
    assert "--quantization compressed-tensors" in completed.stdout
    assert "--max-num-seqs 7" in completed.stdout
    assert "--max-num-batched-tokens 8192" in completed.stdout
    assert "--model /model/snapshots/revision-a" in completed.stdout
    assert f"<{cache_root}:/model>" in final_call
    assert "<--network> <host>" in final_call
    assert "<-p>" not in final_call
    assert "<--name> <qwen-test-model.v1>" in final_call
    assert "<--model> </model/snapshots/revision-a>" in final_call
    assert "VLLM_TEST_FORCE_FP8_MARLIN" not in final_call


def test_snapshot_launcher_accepts_a_direct_snapshot_path(tmp_path):
    snapshot_path = tmp_path / "model-cache" / "snapshots" / "revision-a"
    completed, fake_docker_log, _ = run_launcher(
        "1_B_start_vllm_container_with_snapshots.sh",
        tmp_path,
        model_path_value=snapshot_path,
    )

    assert completed.returncode == 0, completed.stderr
    final_call = final_docker_call(fake_docker_log)
    assert f"<{tmp_path / 'model-cache'}:/model>" in final_call
    assert "<--model> </model/snapshots/revision-a>" in final_call


def test_snapshot_launcher_rejects_ambiguous_cache_root(tmp_path):
    cache_root = tmp_path / "model-cache"
    completed, fake_docker_log, _ = run_launcher(
        "1_B_start_vllm_container_with_snapshots.sh",
        tmp_path,
        snapshots=2,
        model_path_value=cache_root,
    )

    assert completed.returncode != 0
    assert "多个 snapshot" in completed.stderr
    assert not fake_docker_log.exists()


def test_snapshot_launcher_rejects_snapshot_symlinks_outside_blobs(tmp_path):
    cache_root = tmp_path / "model-cache"
    completed, fake_docker_log, _ = run_launcher(
        "1_B_start_vllm_container_with_snapshots.sh",
        tmp_path,
        model_path_value=cache_root,
        invalid_snapshot_link=True,
    )

    assert completed.returncode != 0
    assert "blobs" in completed.stderr
    assert not fake_docker_log.exists()


def test_normal_launcher_uses_direct_model_directory(tmp_path):
    completed, fake_docker_log, _ = run_launcher("1_C_start_vllm_container.sh", tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert fake_docker_log.exists()
    final_call = final_docker_call(fake_docker_log)
    assert "实际启动命令" in completed.stdout
    assert "--model /model" in completed.stdout
    assert "--host 127.0.0.1" in completed.stdout
    assert "--port 8000" in completed.stdout
    assert "--pipeline-parallel-size 1" in completed.stdout
    assert "--load-format auto" in completed.stdout
    assert "--no-trust-remote-code" in completed.stdout
    assert "--no-enforce-eager" in completed.stdout
    assert "--no-enable-prefix-caching" in completed.stdout
    assert "--no-disable-sliding-window" in completed.stdout
    assert "--enable-log-requests" in completed.stdout
    assert "--no-disable-custom-all-reduce" in completed.stdout
    assert "--no-enable-auto-tool-choice" in completed.stdout
    assert "--no-enable-lora" in completed.stdout
    assert "--no-fully-sharded-loras" in completed.stdout
    assert '<--attention-config> <{"flash_attn_version":4}>' in final_call
    assert '<--hf-overrides> <{"max_pixels":42}>' in final_call
    assert '<--limit-mm-per-prompt> <{"image":2}>' in final_call
    assert "--no-skip-mm-profiling" in completed.stdout
    assert "<--entrypoint> <python3>" in fake_docker_log.read_text(encoding="utf-8")
    assert f"<{tmp_path / 'direct-model'}:/model>" in final_call
    assert "<--network> <host>" in final_call
    assert "<-p>" not in final_call
    assert "<--name> <test-model>" in final_call
    assert "<--model> </model>" in final_call


@pytest.mark.parametrize(
    "script_name",
    ["1_C_start_vllm_container.sh", "1_B_start_vllm_container_with_snapshots.sh"],
)
def test_launcher_omits_limit_mm_per_prompt_when_config_is_absent(
    tmp_path, script_name
):
    completed, fake_docker_log, _ = run_launcher(
        script_name,
        tmp_path,
        infer_overrides={"limit_mm_per_prompt": None},
    )

    assert completed.returncode == 0, completed.stderr
    final_call = final_docker_call(fake_docker_log)
    assert "--limit-mm-per-prompt" not in completed.stdout
    assert "<--limit-mm-per-prompt>" not in final_call


def test_active_nvidia_nvfp4_config_does_not_set_single_image_limit():
    infer_path = PROJECT_ROOT / "config/qwen3__6-NVIDIA-NVFP4-config-infer.yaml"
    infer_config = yaml.safe_load(infer_path.read_text(encoding="utf-8"))

    assert "limit_mm_per_prompt" not in infer_config["vllm"]


@pytest.mark.parametrize(
    "script_name",
    ["1_C_start_vllm_container.sh", "1_B_start_vllm_container_with_snapshots.sh"],
)
def test_final_launch_preserves_gpu_ipc_and_disables_persistent_compile_cache(
    tmp_path, script_name
):
    completed, fake_docker_log, _ = run_launcher(script_name, tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert_final_call_disables_persistent_compile_cache(final_docker_call(fake_docker_log))


@pytest.mark.parametrize(
    "script_name, model_path_kind",
    [
        ("1_C_start_vllm_container.sh", "direct"),
        ("1_B_start_vllm_container_with_snapshots.sh", "cache"),
    ],
)
def test_launcher_resolves_relative_model_and_infer_selections(
    tmp_path, script_name, model_path_kind
):
    model_path_value = (
        tmp_path / "direct-model"
        if model_path_kind == "direct"
        else tmp_path / "model-cache"
    )
    completed, fake_docker_log, _ = run_launcher(
        script_name,
        tmp_path,
        model_path_value=model_path_value,
        relative_selections=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert final_docker_call(fake_docker_log)


def test_launcher_rejects_a_set_legacy_vllm_option_without_invoking_docker(tmp_path):
    completed, fake_docker_log, _ = run_launcher(
        "1_C_start_vllm_container.sh",
        tmp_path,
        infer_overrides={"preemption_mode": "swap"},
    )

    assert completed.returncode != 0
    assert "vllm.preemption_mode" in completed.stderr
    assert "vLLM 26.06" in completed.stderr
    assert not fake_docker_log.exists()


@pytest.mark.parametrize(
    "script_name",
    ["1_C_start_vllm_container.sh", "1_B_start_vllm_container_with_snapshots.sh"],
)
@pytest.mark.parametrize(
    "legacy_option",
    [
        "disable_flash_attn",
        "enable_reasoning",
        "enable_multi_modal",
        "disable_mm_preprocessor_cache",
        "enable_prompt_logprobs",
    ],
)
@pytest.mark.parametrize("legacy_value", [True, False])
def test_launcher_rejects_every_set_legacy_boolean_before_docker(
    tmp_path, script_name, legacy_option, legacy_value
):
    completed, fake_docker_log, _ = run_launcher(
        script_name,
        tmp_path,
        infer_overrides={legacy_option: legacy_value},
    )

    assert completed.returncode != 0
    assert f"vllm.{legacy_option}" in completed.stderr
    assert "vLLM 26.06" in completed.stderr
    assert not fake_docker_log.exists()


def test_normal_launcher_ignores_relative_cache_root_when_persistence_is_disabled(tmp_path):
    completed, fake_docker_log, _ = run_launcher(
        "1_C_start_vllm_container.sh",
        tmp_path,
        cache_root_host="relative-cache-root",
    )

    assert completed.returncode == 0, completed.stderr
    assert fake_docker_log.exists()
    assert "relative-cache-root" not in final_docker_call(fake_docker_log)


@pytest.mark.parametrize(
    "script_name",
    ["1_C_start_vllm_container.sh", "1_B_start_vllm_container_with_snapshots.sh"],
)
def test_launcher_rejects_unsupported_image_flag_before_removing_or_running_container(
    tmp_path, script_name
):
    completed, fake_docker_log, _ = run_launcher(
        script_name,
        tmp_path,
        api_server_help=API_SERVER_HELP.replace(" --no-enable-prefix-caching", ""),
    )

    assert completed.returncode != 0
    assert "--no-enable-prefix-caching" in completed.stderr
    calls = fake_docker_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1
    assert "<--help>" in calls[0]


@pytest.mark.parametrize(
    "script_name, model_path_kind",
    [
        ("1_C_start_vllm_container.sh", "direct"),
        ("1_B_start_vllm_container_with_snapshots.sh", "cache"),
    ],
)
def test_launcher_hides_successful_image_help_stderr(
    tmp_path, script_name, model_path_kind
):
    model_path_value = (
        tmp_path / "direct-model"
        if model_path_kind == "direct"
        else tmp_path / "model-cache"
    )
    completed, fake_docker_log, _ = run_launcher(
        script_name,
        tmp_path,
        model_path_value=model_path_value,
        api_server_help_stderr="torch cleanup traceback",
    )

    assert completed.returncode == 0, completed.stderr
    assert "torch cleanup traceback" not in completed.stderr
    assert final_docker_call(fake_docker_log)


def test_active_nvfp4_config_enables_auto_tool_choice():
    infer_path = PROJECT_ROOT / "config/qwen3__6-herotic-NVFP4-config-infer.yaml"
    infer_config = yaml.safe_load(infer_path.read_text(encoding="utf-8"))

    assert infer_config["vllm"]["enable_auto_tool_choice"] is True

@pytest.mark.parametrize('script_name', ['1_B_start_vllm_container_with_snapshots.sh', '1_C_start_vllm_container.sh'])
def test_repository_native_template_mounted_into_ngc(script_name, tmp_path):
    relative = 'config/chat-templates/qwen-native-cache-stable.jinja'
    completed, log, _ = run_launcher(script_name, tmp_path, infer_overrides={'chat_template':relative})
    assert completed.returncode == 0, completed.stderr + completed.stdout
    call = final_docker_call(log)
    assert f'<{PROJECT_ROOT / relative}:/opt/llm-chat-template.jinja:ro>' in call
    assert '<--chat-template> </opt/llm-chat-template.jinja>' in call

@pytest.mark.parametrize('script_name', ['1_B_start_vllm_container_with_snapshots.sh', '1_C_start_vllm_container.sh'])
@pytest.mark.parametrize('infer_path', sorted((PROJECT_ROOT / 'config').glob('qwen*config-infer.yaml')))
def test_selected_native_tool_parser_config_flows_to_launcher(script_name, infer_path, tmp_path):
    config = yaml.safe_load(infer_path.read_text())
    assert config['inference']['capture_raw_tool_calls'] is True
    selected = config['vllm']
    assert selected['tool_call_parser'] == 'qwen3_coder_raw_passthrough'
    assert selected['enable_auto_tool_choice'] is True
    completed, log, _ = run_launcher(script_name, tmp_path, infer_overrides={key:selected[key] for key in ('tool_call_parser','tool_parser_plugin','enable_auto_tool_choice','chat_template')})
    assert completed.returncode == 0, completed.stderr + completed.stdout
    call = final_docker_call(log)
    assert '<--tool-call-parser> <qwen3_coder_raw_passthrough>' in call
    assert f'<{PROJECT_ROOT / selected["tool_parser_plugin"]}:/opt/llm-tool-parser-plugin.py:ro>' in call
    assert '<--tool-parser-plugin> </opt/llm-tool-parser-plugin.py>' in call
    assert '<--enable-auto-tool-choice>' in call
