from pathlib import Path
import os
import subprocess
import sys

import pytest

from scripts.path_utils import resolve_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_resolve_path_expands_home_and_preserves_absolute_and_relative_bases(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    project_root = tmp_path / "project"
    config_dir = project_root / "config"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    assert resolve_path("~/models/qwen", project_root) == home / "models/qwen"
    assert (
        resolve_path("/mnt/models/qwen", project_root)
        == Path("/mnt/models/qwen")
    )
    assert (
        resolve_path("config/config-infer.yaml", project_root)
        == project_root / "config/config-infer.yaml"
    )
    assert (
        resolve_path("templates/chat.jinja", config_dir)
        == config_dir / "templates/chat.jinja"
    )
    assert (
        resolve_path("templates/chat.jinja", "~/project/config")
        == home / "project/config/templates/chat.jinja"
    )


def test_shell_path_helpers_expand_tilde_before_project_root_resolution(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project_root = tmp_path / "project"
    helper = PROJECT_ROOT / "scripts" / "_workspace_paths.sh"
    script = (
        'source "$1"; '
        'printf "%s\\n%s\\n%s\\n%s\\n" '
        '"$(resolve_project_path "$2" "$3")" '
        '"$(resolve_project_path "$4" "$3")" '
        '"$(resolve_project_path "$5" "$3")" '
        '"$(resolve_project_path cache "$6")"'
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            script,
            "bash",
            str(helper),
            "config/infer.yaml",
            str(project_root),
            "~/cache",
            "/mnt/models/qwen",
            "~/project",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
    )

    assert result.stdout.splitlines() == [
        str(project_root / "config/infer.yaml"),
        str(home / "cache"),
        "/mnt/models/qwen",
        str(home / "project/cache"),
    ]


def test_local_memory_preparer_imports_path_helper_from_another_working_directory(
    tmp_path: Path,
) -> None:
    import_probe = "\n".join(
        [
            "import importlib, sys, types",
            "backend = types.ModuleType('backend'); backend.__path__ = []",
            "retrieval = types.ModuleType('backend.retrieval'); retrieval.__path__ = []",
            "config = types.ModuleType('backend.retrieval.config')",
            "config.load_local_memory_config = lambda *args: None",
            "embedding = types.ModuleType('backend.retrieval.embedding')",
            "embedding.write_artifact_manifest = lambda *args: None",
            "embedding.artifact_digest = lambda *args, **kwargs: None",
            "sys.modules.update({",
            "    'backend': backend, 'backend.retrieval': retrieval,",
            "    'backend.retrieval.config': config,",
            "    'backend.retrieval.embedding': embedding,",
            "})",
            "module = importlib.import_module('scripts.prepare_local_memory_model')",
            "assert module.resolve_path.__module__ == 'scripts.path_utils'",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", import_probe],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_asr_preflight_expands_config_and_model_paths_before_reading(
    tmp_path: Path,
) -> None:
    probe = "\n".join(
        [
            "import sys, types",
            "from pathlib import Path",
            "from types import SimpleNamespace",
            "backend = types.ModuleType('backend'); backend.__path__ = []",
            "asr = types.ModuleType('backend.asr'); asr.__path__ = []",
            "config = types.ModuleType('backend.asr.config')",
            "config.EXPECTED_ASR_ARCHITECTURE = 'FakeASR'",
            "config.AsrConfig = type('AsrConfig', (), {})",
            "seen = []",
            "settings = SimpleNamespace(enabled=False, sglang=SimpleNamespace(model_path='~/models/asr', port=0, host='127.0.0.1'))",
            "config.load_asr_config = lambda path, **kwargs: (seen.append(path), settings)[1]",
            "config.validate_asr_model_artifacts = lambda path: None",
            "sys.modules.update({'backend': backend, 'backend.asr': asr, 'backend.asr.config': config})",
            "from scripts.asr_preflight import validate_asr_model",
            "report = validate_asr_model(Path('~/asr.yaml'), check_port=False, check_registry=False)",
            "home = Path.home()",
            "assert seen == [(home / 'asr.yaml').resolve()]",
            "assert report.model_path == home / 'models/asr'",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(tmp_path / "runtime-home"), "PYTHONPATH": str(PROJECT_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_paddle_ocr_preflight_expands_path_arguments_before_validation(
    tmp_path: Path,
) -> None:
    probe = "\n".join(
        [
            "import sys, types",
            "httpx = types.ModuleType('httpx')",
            "yaml = types.ModuleType('yaml')",
            "packaging = types.ModuleType('packaging'); packaging.__path__ = []",
            "version = types.ModuleType('packaging.version'); version.Version = lambda value: value",
            "sys.modules.update({'httpx': httpx, 'yaml': yaml, 'packaging': packaging, 'packaging.version': version})",
            "import scripts.paddle_ocr_preflight as preflight",
            "from pathlib import Path",
            "seen = {}",
            "preflight.check_sglang_dependency_names = lambda path: (seen.update(python=path), {'requiredDistributions': [], 'versions': {}})[1]",
            "preflight.validate_sglang_pipeline_config = lambda path: (seen.update(config=path), {'sglangBackend': 'fake'})[1]",
            "assert preflight.main(['check-sglang-env', '--python', '~/venv/bin/python', '--config', '~/pipeline.yaml']) == 0",
            "home = Path.home()",
            "assert seen == {'python': home / 'venv/bin/python', 'config': home / 'pipeline.yaml'}",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(tmp_path / "runtime-home"), "PYTHONPATH": str(PROJECT_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_paddle_ocr_service_expands_memory_report_path_before_use(
    tmp_path: Path,
) -> None:
    probe = "\n".join(
        [
            "import sys, types",
            "from contextlib import contextmanager",
            "from pathlib import Path",
            "sys.modules['yaml'] = types.ModuleType('yaml')",
            "paddlex = types.ModuleType('paddlex'); paddlex.__path__ = []",
            "paddlex_main = types.ModuleType('paddlex.__main__'); paddlex_main.console_entry = lambda: 0",
            "sys.modules.update({'paddlex': paddlex, 'paddlex.__main__': paddlex_main})",
            "import scripts.paddle_ocr_service as service",
            "seen = []",
            "service.apply_cuda_memory_limit = lambda limit, path: seen.append((limit, path))",
            "@contextmanager",
            "def passthrough(arguments): yield arguments",
            "service.expanded_pipeline_arguments = passthrough",
            "assert service.main(['--cuda-memory-limit-gb', '24', '--memory-report', '~/memory.json', '--predictor', 'fake']) == 0",
            "assert seen == [(24, Path.home() / 'memory.json')]",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(tmp_path / "runtime-home"), "PYTHONPATH": str(PROJECT_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_asr_launcher_expands_tilde_config_and_exits_for_typed_disabled_config(
    tmp_path: Path,
) -> None:
    real_python = PROJECT_ROOT / ".sglang" / "bin" / "python"
    if not real_python.is_file():
        pytest.skip("the project .sglang Python environment is required for typed ASR config")

    project = tmp_path / "workspace"
    scripts = project / "scripts"
    config_dir = project / "config"
    fake_python_dir = project / ".sglang" / "bin"
    home = tmp_path / "runtime-home"
    for directory in (scripts, config_dir, fake_python_dir, home):
        directory.mkdir(parents=True, exist_ok=True)

    for name in (
        "3_optional_start_ASR_sglang.sh",
        "_workspace_paths.sh",
        "asr_preflight.py",
    ):
        (scripts / name).write_bytes((PROJECT_ROOT / "scripts" / name).read_bytes())
    for name in ("path_utils.py",):
        (scripts / name).write_bytes((PROJECT_ROOT / "scripts" / name).read_bytes())
    for name in ("__init__.py",):
        (project / "backend" / name).parent.mkdir(parents=True, exist_ok=True)
        (project / "backend" / name).write_bytes((PROJECT_ROOT / "backend" / name).read_bytes())
        (project / "backend" / "asr" / name).parent.mkdir(parents=True, exist_ok=True)
        (project / "backend" / "asr" / name).write_bytes((PROJECT_ROOT / "backend" / "asr" / name).read_bytes())
    (project / "backend" / "asr" / "config.py").write_bytes(
        (PROJECT_ROOT / "backend" / "asr" / "config.py").read_bytes()
    )
    (fake_python_dir / "activate").write_text(":\n", encoding="utf-8")
    argv_log = tmp_path / "asr-python-argv.txt"
    fake_python = fake_python_dir / "python"
    fake_python.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "printf '%s\\n' \"$@\" > \"$ASR_TEST_ARGV\"\n"
        "exec \"$ASR_TEST_REAL_PYTHON\" \"$@\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    (config_dir / "config-overall.yaml").write_text(
        "asr: config/config-asr.yaml\n", encoding="utf-8"
    )
    disabled_config = (PROJECT_ROOT / "config" / "config-asr.yaml").read_text(
        encoding="utf-8"
    ).replace("enabled: true", "enabled: false", 1)
    (home / "asr.yaml").write_text(disabled_config, encoding="utf-8")

    result = subprocess.run(
        ["bash", str(scripts / "3_optional_start_ASR_sglang.sh")],
        cwd=tmp_path,
        env={
            **os.environ,
            "HOME": str(home),
            "ASR_CONFIG_PATH": "~/asr.yaml",
            "ASR_LOG_DIR": "~/launcher-logs",
            "ASR_TEST_ARGV": str(argv_log),
            "ASR_TEST_REAL_PYTHON": str(real_python),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ASR is disabled in the typed configuration" in result.stdout
    assert argv_log.read_text(encoding="utf-8").splitlines() == [
        str(scripts / "asr_preflight.py"),
        "--config",
        str(home / "asr.yaml"),
        "--shell",
    ]
    assert not (home / "launcher-logs").exists()
