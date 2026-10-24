"""Exercise speculative YAML options through the real launcher without a GPU."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _launch_with_argv_recorder(
    tmp_path: Path, sglang: dict, *, model_path: str = "/fake/model", home: Path | None = None
) -> dict:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("1_A_start_sglang.sh", "_workspace_paths.sh"):
        shutil.copy2(PROJECT_ROOT / "scripts" / name, scripts / name)
    config = tmp_path / "config"
    config.mkdir()
    (config / "config-overall.yaml").write_text("infer: config/test-config-infer.yaml\n")
    (config / "test-config-infer.yaml").write_text(
        yaml.safe_dump({"model": {"model_path": model_path}, "sglang": sglang})
    )
    activate = tmp_path / ".sglang" / "bin" / "activate"
    activate.parent.mkdir(parents=True)
    activate.write_text(":\n")
    fake_package = tmp_path / "sglang"
    fake_package.mkdir()
    (fake_package / "__init__.py").write_text("")
    (fake_package / "launch_server.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['SGLANG_TEST_ARGV']).write_text(json.dumps({\n"
        "    'argv': sys.argv[1:],\n"
        "    'external_model_package': os.environ.get('SGLANG_EXTERNAL_MODEL_PACKAGE'),\n"
        "    'pythonpath': os.environ.get('PYTHONPATH', '').split(os.pathsep),\n"
        "    'uniform_q_len_compat': os.environ.get('LLM_DEPLOYMENT_FLASHINFER_PREFILL_UNIFORM_Q_LEN_COMPAT'),\n"
        "}))\n"
    )
    argv_file = tmp_path / "argv.json"
    env = dict(os.environ)
    if home is not None:
        env["HOME"] = str(home)
    env.pop("SGLANG_EXTERNAL_MODEL_PACKAGE", None)
    env.update(
        PATH=f"{Path(sys.executable).parent}:{env.get('PATH', '')}",
        PYTHONPATH=str(tmp_path),
        SGLANG_TEST_ARGV=str(argv_file),
        SGLANG_COMPILE_CACHE_BASE=str(tmp_path / "cache"),
        LLM_DEPLOYMENT_FLASHINFER_PREFILL_UNIFORM_Q_LEN_COMPAT="1",
    )
    for name in (
        "FLASHINFER_WORKSPACE_BASE", "SGLANG_CACHE_DIR", "TORCH_EXTENSIONS_DIR",
        "TRITON_CACHE_DIR", "CUDA_CACHE_PATH", "SGLANG_DG_CACHE_DIR",
    ):
        env.pop(name, None)
    result = subprocess.run(
        ["bash", str(scripts / "1_A_start_sglang.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert argv_file.exists(), result.stdout + result.stderr
    return json.loads(argv_file.read_text())


def test_launcher_passes_eagle_replayssm_options_from_yaml(tmp_path: Path):
    argv = _launch_with_argv_recorder(
        tmp_path,
        {
            "speculative_algorithm": "EAGLE",
            "speculative_num_steps": 3,
            "speculative_eagle_topk": 1,
            "speculative_num_draft_tokens": 4,
            "enable_linear_replayssm_spec": True,
        },
    )

    assert argv["argv"] == [
        "--model-path", "/fake/model",
        "--speculative-algorithm", "EAGLE",
        "--speculative-num-steps", "3",
        "--speculative-eagle-topk", "1",
        "--speculative-num-draft-tokens", "4",
        "--enable-linear-replayssm-spec",
    ]


@pytest.mark.parametrize("sglang", [{}, {
    "speculative_num_steps": None,
    "speculative_eagle_topk": None,
    "speculative_num_draft_tokens": None,
    "enable_linear_replayssm_spec": False,
}])
def test_launcher_omits_unconfigured_or_disabled_speculation(tmp_path: Path, sglang: dict):
    payload = _launch_with_argv_recorder(tmp_path, sglang)
    assert payload["argv"] == ["--model-path", "/fake/model"]
    assert payload["external_model_package"] is None


def test_launcher_exposes_configured_external_model_package(tmp_path: Path):
    payload = _launch_with_argv_recorder(
        tmp_path, {"external_model_package": "sglang_extensions"}
    )
    assert payload["external_model_package"] == "sglang_extensions"
    assert payload["pythonpath"][0] == str(tmp_path)


def test_launcher_enables_uniform_q_len_compat_only_when_selected(tmp_path: Path):
    payload = _launch_with_argv_recorder(
        tmp_path, {"flashinfer_prefill_uniform_q_len_compat": True}
    )
    assert payload["uniform_q_len_compat"] == "1"


@pytest.mark.parametrize("sglang", [
    {}, {"flashinfer_prefill_uniform_q_len_compat": None},
    {"flashinfer_prefill_uniform_q_len_compat": False},
])
def test_launcher_clears_inherited_uniform_q_len_compat_for_other_models(tmp_path: Path, sglang: dict):
    assert _launch_with_argv_recorder(tmp_path, sglang)["uniform_q_len_compat"] is None


@pytest.mark.parametrize("home_name", ["another-user", "home with spaces"])
def test_launcher_expands_model_and_draft_paths_for_runtime_user(tmp_path: Path, home_name: str):
    home = tmp_path / home_name
    home.mkdir()
    (home / "chat.jinja").write_text("{{ messages }}")
    payload = _launch_with_argv_recorder(
        tmp_path,
        {"speculative_draft_model_path": "~/models/draft",
         "speculative_algorithm": "EAGLE", "chat_template": "~/chat.jinja"},
        model_path="~/models/main",
        home=home,
    )
    argv = payload["argv"]
    assert argv[argv.index("--model-path") + 1] == str(home / "models/main")
    assert argv[argv.index("--speculative-draft-model-path") + 1] == str(home / "models/draft")
    assert argv[argv.index("--speculative-algorithm") + 1] == "EAGLE"
    assert argv[argv.index("--chat-template") + 1] == str(home / "chat.jinja")
