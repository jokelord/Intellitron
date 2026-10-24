from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIMITER_PATH = PROJECT_ROOT / "scripts/paddle_ocr_service.py"


def _load_limiter():
    assert LIMITER_PATH.is_file(), "PaddleOCR memory-limited launcher is missing"
    spec = importlib.util.spec_from_file_location("paddle_ocr_service", LIMITER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeCuda:
    def __init__(self, total_memory: int = 128 * 1024**3, *, available: bool = True):
        self.total_memory = total_memory
        self.available = available
        self.calls: list[tuple[float, int]] = []

    def is_available(self) -> bool:
        return self.available

    def get_device_properties(self, device: int):
        assert device == 0
        return SimpleNamespace(total_memory=self.total_memory)

    def set_per_process_memory_fraction(self, fraction: float, device: int) -> None:
        self.calls.append((fraction, device))


def test_applies_cuda_allocator_limit_and_writes_atomic_report(tmp_path: Path) -> None:
    limiter = _load_limiter()
    cuda = _FakeCuda()
    report_path = tmp_path / "runtime/memory.json"

    report = limiter.apply_cuda_memory_limit(
        16, report_path, torch_module=SimpleNamespace(cuda=cuda)
    )

    assert cuda.calls == [(0.125, 0)]
    assert report == {
        "schemaVersion": 1,
        "pid": os.getpid(),
        "cudaMemoryLimitGiB": 16,
        "cudaMemoryLimitBytes": 16 * 1024**3,
        "deviceTotalMemoryBytes": 128 * 1024**3,
        "allocatorFraction": 0.125,
    }
    assert json.loads(report_path.read_text()) == report
    assert not list(report_path.parent.glob("*.tmp"))


@pytest.mark.parametrize("limit_gb", [0, -1, 129])
def test_rejects_invalid_limit_before_writing_report(
    tmp_path: Path, limit_gb: int
) -> None:
    limiter = _load_limiter()
    cuda = _FakeCuda()
    report_path = tmp_path / "memory.json"

    with pytest.raises(ValueError, match="CUDA memory limit"):
        limiter.apply_cuda_memory_limit(
            limit_gb, report_path, torch_module=SimpleNamespace(cuda=cuda)
        )

    assert cuda.calls == []
    assert not report_path.exists()


def test_rejects_unavailable_cuda_before_paddlex_import(tmp_path: Path) -> None:
    limiter = _load_limiter()
    report_path = tmp_path / "memory.json"

    with pytest.raises(RuntimeError, match="CUDA"):
        limiter.apply_cuda_memory_limit(
            16,
            report_path,
            torch_module=SimpleNamespace(cuda=_FakeCuda(available=False)),
        )

    assert not report_path.exists()


def test_main_forwards_paddlex_arguments_only_after_limit_is_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter = _load_limiter()
    report_path = tmp_path / "memory.json"
    events: list[object] = []

    def fake_apply(limit_gb, path):
        events.append(("limit", limit_gb, path))
        path.write_text("{}")
        return {}

    def fake_console_entry():
        events.append(("paddlex", list(sys.argv), report_path.exists()))
        return 0

    package = ModuleType("paddlex")
    entrypoint = ModuleType("paddlex.__main__")
    entrypoint.console_entry = fake_console_entry
    monkeypatch.setitem(sys.modules, "paddlex", package)
    monkeypatch.setitem(sys.modules, "paddlex.__main__", entrypoint)
    monkeypatch.setattr(limiter, "apply_cuda_memory_limit", fake_apply)

    status = limiter.main(
        [
            "--cuda-memory-limit-gb",
            "16",
            "--memory-report",
            str(report_path),
            "--serve",
            "--port",
            "8118",
        ]
    )

    assert status == 0
    assert events == [
        ("limit", 16, report_path),
        ("paddlex", ["paddlex", "--serve", "--port", "8118"], True),
    ]


@pytest.mark.parametrize("home_name", ["another-user", "home with spaces"])
@pytest.mark.parametrize("cli_raises", [False, True])
def test_main_expands_pipeline_paths_without_changing_attested_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home_name: str, cli_raises: bool
) -> None:
    limiter = _load_limiter()
    home = tmp_path / home_name
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config = home / "pipeline.yaml"
    payload = {
        "pipeline_name": "PaddleOCR-VL-1.6",
        "SubModules": {"LayoutDetection": {"model_dir": "~/models/layout"}},
        "SubPipelines": {"Nested": {"SubModules": {
            "VLM": {"model_dir": "~/models/vlm"},
        }}},
        "LLMDeploymentSGLangBackend": {"chat_template": "~/models/vlm/chat_template.jinja"},
        "label": "~/ordinary-text",
        "server_url": "http://localhost:30002/v1",
        "batch_size": 8,
    }
    config.write_text(yaml.safe_dump(payload))
    original_bytes = config.read_bytes()
    observed_paths = []

    def fake_console_entry():
        pipeline_path = Path(sys.argv[sys.argv.index("--pipeline") + 1])
        observed_paths.append(pipeline_path)
        loaded = yaml.safe_load(pipeline_path.read_text())
        assert loaded["SubModules"]["LayoutDetection"]["model_dir"] == str(home / "models/layout")
        assert loaded["SubPipelines"]["Nested"]["SubModules"]["VLM"]["model_dir"] == str(home / "models/vlm")
        assert loaded["LLMDeploymentSGLangBackend"]["chat_template"] == str(home / "models/vlm/chat_template.jinja")
        assert loaded["label"] == "~/ordinary-text"
        assert loaded["server_url"] == payload["server_url"]
        assert loaded["batch_size"] == 8
        assert config.read_bytes() == original_bytes
        if cli_raises:
            raise RuntimeError("PaddleX failed")
        return 0

    package = ModuleType("paddlex")
    entrypoint = ModuleType("paddlex.__main__")
    entrypoint.console_entry = fake_console_entry
    monkeypatch.setitem(sys.modules, "paddlex", package)
    monkeypatch.setitem(sys.modules, "paddlex.__main__", entrypoint)
    monkeypatch.setattr(limiter, "apply_cuda_memory_limit", lambda *args: {})
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    arguments = ["--cuda-memory-limit-gb", "16", "--memory-report", str(tmp_path / "memory.json"),
                 "--serve", "--pipeline", "~/pipeline.yaml"]
    if cli_raises:
        with pytest.raises(RuntimeError, match="PaddleX failed"):
            limiter.main(arguments)
    else:
        assert limiter.main(arguments) == 0
    assert observed_paths
    assert observed_paths[0] != config
    assert not observed_paths[0].exists()
    assert config.read_bytes() == original_bytes
