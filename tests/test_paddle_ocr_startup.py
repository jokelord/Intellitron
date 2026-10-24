from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import copy
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_IN = PROJECT_ROOT / "requirements/paddle-ocr/requirements.in"
REQUIREMENTS_LOCK = PROJECT_ROOT / "requirements/paddle-ocr/requirements.lock"
MODEL_MANIFEST = PROJECT_ROOT / "config/paddleocr-vl-thor-model-manifest.json"
PHOTO_FIXTURE = PROJECT_ROOT / "tests/fixtures/paddleocr/doc_test_rotated.jpg"
PREFLIGHT_PATH = PROJECT_ROOT / "scripts/paddle_ocr_preflight.py"
START_SCRIPT = PROJECT_ROOT / "scripts/2b_start_OCR_LM.sh"
BACKEND_START_SCRIPT = PROJECT_ROOT / "scripts/3_start_backend.sh"
WORKSPACE_HELPERS = PROJECT_ROOT / "scripts/_workspace_paths.sh"
EXPECTED_MODEL_IDENTITIES = {
    "LayoutDetection": (
        "PaddlePaddle/PP-DocLayoutV3_safetensors",
        "f8bd2e7f6bb8773617356c59592a9beea51a9a6f",
    ),
    "VLRecognition": (
        "PaddlePaddle/PaddleOCR-VL-1.6",
        "d911116c363676c602c4786ad0b9667b1aee055f",
    ),
    "DocOrientationClassify": (
        "PaddlePaddle/PP-LCNet_x1_0_doc_ori_safetensors",
        "04cd0dee25d2300e63442f6989e8984b1f3dae77",
    ),
    "DocUnwarping": (
        "PaddlePaddle/UVDoc_safetensors",
        "505cd57c9915ac2656bee70ae4204aa12201aa1f",
    ),
}


def _cuda_memory_report(pid: int, limit_gb: int = 16) -> dict[str, object]:
    total_bytes = 128 * 1024**3
    limit_bytes = limit_gb * 1024**3
    return {
        "schemaVersion": 1,
        "pid": pid,
        "cudaMemoryLimitGiB": limit_gb,
        "cudaMemoryLimitBytes": limit_bytes,
        "deviceTotalMemoryBytes": total_bytes,
        "allocatorFraction": limit_bytes / total_bytes,
    }


def _load_preflight():
    assert PREFLIGHT_PATH.is_file(), "PaddleOCR preflight program is missing"
    spec = importlib.util.spec_from_file_location("paddle_ocr_preflight", PREFLIGHT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


class _PaddleXContractHandler(_QuietHandler):
    requests: list[dict[str, object]] = []
    include_preprocessor_evidence = True
    scramble_photographed_phrases = False

    def do_GET(self) -> None:
        if self.path != "/openapi.json":
            self.send_error(404)
            return
        body = json.dumps(
            {"paths": {"/layout-parsing": {"post": {"operationId": "layout_parsing"}}}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/layout-parsing":
            self.send_error(404)
            return
        size = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(size))
        type(self).requests.append(payload)
        file_type = payload.get("fileType")
        kind = str(payload.get("logId", "unknown")).removeprefix("paddleocr-p0-")
        marker = (
            (
                "The photographed page has disappearing words, then sum. "
                "Veronica appears separately from choices."
                if type(self).scramble_photographed_phrases
                else "The disappearing sum. Veronica's choices."
            )
            if kind.startswith("distorted-")
            else f"THOR {kind.upper()} CANARY 2026"
        )
        pruned_result = {}
        if type(self).include_preprocessor_evidence:
            use_unwarping = payload.get("useDocUnwarping")
            pruned_result = {
                "model_settings": {"use_doc_preprocessor": True},
                "doc_preprocessor_res": {
                    "model_settings": {
                        "use_doc_orientation_classify": payload.get(
                            "useDocOrientationClassify"
                        ),
                        "use_doc_unwarping": use_unwarping,
                    },
                    "angle": (
                        180
                        if kind.startswith("distorted-")
                        else 90 if kind == "rotated" else 0
                    ),
                },
            }
        body = json.dumps(
            {
                "errorCode": 0,
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {"text": f"## {marker} fileType={file_type}"},
                            "prunedResult": pruned_result,
                        }
                    ]
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_ocr_requirements_forbid_conflicting_engines() -> None:
    text = REQUIREMENTS_IN.read_text()
    assert "paddleocr[doc-parser]>=3.6.0,<3.7.0" in text
    assert "paddlex[serving]>=3.6.0,<3.7.0" in text
    assert "transformers>=5.10.0,<6.0" in text
    assert "torch==2.11.0+cu130" in text
    assert "torchvision==0.26.0+cu130" in text
    assert "chardet>=5.0,<6.0" in text
    assert "paddlepaddle" not in text.lower()
    assert "sglang" not in text.lower()
    assert "vllm" not in text.lower()


def test_backend_start_rejects_multiple_workers_for_process_local_ocr_admission() -> None:
    text = BACKEND_START_SCRIPT.read_text()
    guard = 'if [ "$BACKEND_WORKERS" -ne 1 ]'
    assert guard in text
    assert text.index(guard) < text.index("python3 -m uvicorn")


def test_preflight_enforces_transformers_upper_bound_and_thor_gpu_identity() -> None:
    preflight = _load_preflight()
    assert preflight._transformers_version_supported("5.10.0") is True
    assert preflight._transformers_version_supported("5.99.0") is True
    assert preflight._transformers_version_supported("6.0.0") is False
    assert preflight._is_thor_gpu_name("NVIDIA Jetson Thor") is True
    assert preflight._is_thor_gpu_name("NVIDIA H100") is False


def test_direct_backend_startup_rejects_multiworker_process_local_ocr(monkeypatch) -> None:
    from backend.config import OCRServiceConfig
    from backend.main import _validate_ocr_worker_count

    enabled = OCRServiceConfig(enabled=True)
    with pytest.raises(RuntimeError, match="worker"):
        _validate_ocr_worker_count(enabled, 2, {})
    with pytest.raises(RuntimeError, match="worker"):
        _validate_ocr_worker_count(enabled, 1, {"WEB_CONCURRENCY": "2"})
    _validate_ocr_worker_count(OCRServiceConfig(enabled=False), 4, {"WEB_CONCURRENCY": "8"})


def test_ocr_lock_is_hash_pinned_and_forbids_conflicting_engines() -> None:
    text = REQUIREMENTS_LOCK.read_text()
    lowered = text.lower()
    assert "--hash=sha256:" in text
    assert "paddlepaddle==" not in lowered
    assert "sglang==" not in lowered
    assert "vllm==" not in lowered
    assert "torch==2.11.0+cu130" in lowered
    assert "torchvision==0.26.0+cu130" in lowered
    assert "--extra-index-url https://download.pytorch.org/whl/cu130" in lowered


def test_sbsa_gate_requires_sbsa_wheel_tag_and_aarch64_elf(tmp_path: Path) -> None:
    preflight = _load_preflight()
    wheel = tmp_path / "WHEEL"
    library = tmp_path / "libcusparseLt.so.0"
    wheel.write_text("Wheel-Version: 1.0\nTag: py3-none-manylinux2014_sbsa\n")
    elf_header = bytearray(64)
    elf_header[:6] = b"\x7fELF\x02\x01"
    elf_header[18:20] = (183).to_bytes(2, "little")
    library.write_bytes(elf_header)

    assert preflight.validate_sbsa_cuda_artifact(wheel, library) == {
        "wheelTag": "py3-none-manylinux2014_sbsa",
        "elfClass": 64,
        "elfMachine": "AArch64",
    }

    wheel.write_text("Wheel-Version: 1.0\nTag: py3-none-manylinux2014_x86_64\n")
    with pytest.raises(preflight.PreflightError, match="SBSA"):
        preflight.validate_sbsa_cuda_artifact(wheel, library)

    wheel.write_text("Wheel-Version: 1.0\nTag: py3-none-manylinux2014_sbsa\n")
    elf_header[18:20] = (62).to_bytes(2, "little")
    library.write_bytes(elf_header)
    with pytest.raises(preflight.PreflightError, match="AArch64"):
        preflight.validate_sbsa_cuda_artifact(wheel, library)


def test_official_photographed_fixture_has_pinned_provenance() -> None:
    assert PHOTO_FIXTURE.is_file()
    assert hashlib.sha256(PHOTO_FIXTURE.read_bytes()).hexdigest() == (
        "6fb6b8ea8836a0acb46762a85d2bcf97100e33f0c4b49dcfc0b97f5955f4e534"
    )

def test_workspace_helpers_resolve_shared_paddle_ocr_paths(tmp_path: Path) -> None:
    command = f"""
set -euo pipefail
    source {WORKSPACE_HELPERS!s}
    get_shared_sglang_python {tmp_path!s}
    get_shared_paddle_ocr_python {tmp_path!s}
    get_shared_paddle_ocr_activate {tmp_path!s}
"""
    result = subprocess.run(
        ["bash", "-c", command], check=True, capture_output=True, text=True
    )
    assert result.stdout.splitlines() == [
        str(tmp_path / ".sglang/bin/python"),
        str(tmp_path / ".paddle-ocr/bin/python"),
        str(tmp_path / ".paddle-ocr/bin/activate"),
    ]


def test_start_script_is_loopback_non_destructive_and_does_not_install() -> None:
    text = START_SCRIPT.read_text()
    assert 'OCR_HOST="${OCR_HOST:-127.0.0.1}"' in text
    assert 'OCR_PORT="${OCR_PORT:-8118}"' in text
    assert "--engine" in text and "transformers" in text
    assert "--device" in text and "gpu:0" in text
    assert "kill -KILL" not in text
    assert "pkill" not in text
    assert "uv pip install" not in text
    assert "uv pip sync" not in text
    assert 'OCR_EXIT_AFTER_CANARY="${OCR_EXIT_AFTER_CANARY:-0}"' in text
    assert "flock" in text
    assert "9>&-" in text
    assert "OCR_SHUTDOWN_TIMEOUT" in text
    assert "reuse" in text
    assert 'OCR_CUDA_MEMORY_LIMIT_GB="${OCR_CUDA_MEMORY_LIMIT_GB:-' in text
    assert '"$OCR_PYTHON" "$OCR_SERVICE_WRAPPER"' in text
    assert text.count('--cuda-memory-limit-gb "$OCR_CUDA_MEMORY_LIMIT_GB"') >= 3
    assert text.count('--memory-report "$OCR_MEMORY_REPORT"') >= 3
    subprocess.run(["bash", "-n", START_SCRIPT], check=True)


def _write_fake_launcher_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    scripts = project / "scripts"
    ocr_bin = project / ".paddle-ocr/bin"
    scripts.mkdir(parents=True)
    ocr_bin.mkdir(parents=True)
    (project / "config").mkdir()
    (project / "config/paddleocr-vl-thor.yaml").write_text(
        "pipeline_name: PaddleOCR-VL-1.6\n"
    )
    launcher = scripts / "2b_start_OCR_LM.sh"
    launcher.write_text(START_SCRIPT.read_text())
    (scripts / "_workspace_paths.sh").write_text(WORKSPACE_HELPERS.read_text())
    os.symlink(sys.executable, ocr_bin / "python")
    (ocr_bin / "activate").write_text("")
    (scripts / "paddle_ocr_preflight.py").write_text(
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

command = sys.argv[1]
if command == "reuse":
    Path(os.environ["FAKE_REUSE_MARKER"]).write_text("reused")
    raise SystemExit(int(os.environ.get("FAKE_REUSE_STATUS", "0")))
if command == "canary":
    Path(os.environ["FAKE_CANARY_MARKER"]).write_text("ready")
raise SystemExit(0)
"""
    )
    paddlex = ocr_bin / "paddlex"
    paddlex.write_text(
        """#!/usr/bin/env python3
import os
import signal
import time
from pathlib import Path

Path(os.environ["FAKE_LAUNCH_MARKER"]).write_text("launched")
running = True

def stop(_signum, _frame):
    global running
    running = False

signal.signal(signal.SIGTERM, stop)
while running:
    time.sleep(0.05)
"""
    )
    paddlex.chmod(0o755)
    return launcher


def _fake_launcher_env(tmp_path: Path, lock_path: Path, port: int) -> dict[str, str]:
    return {
        **os.environ,
        "OCR_PORT": str(port),
        "OCR_READINESS_TIMEOUT": "3",
        "OCR_SHUTDOWN_TIMEOUT": "3",
        "OCR_START_LOCK_TIMEOUT": "1",
        "OCR_START_LOCK": str(lock_path),
        "OCR_LOG": str(tmp_path / "service.log"),
        "OCR_ATTESTATION_PATH": str(tmp_path / "attestation.json"),
        "OCR_CUDA_MEMORY_LIMIT_GB": "16",
        "OCR_MEMORY_REPORT": str(tmp_path / "memory-limit.json"),
        "OCR_SERVICE_WRAPPER": str(
            tmp_path / "project/.paddle-ocr/bin/paddlex"
        ),
        "FAKE_REUSE_MARKER": str(tmp_path / "reuse-called"),
        "FAKE_CANARY_MARKER": str(tmp_path / "canary-called"),
        "FAKE_LAUNCH_MARKER": str(tmp_path / "launch-called"),
    }


def _unused_loopback_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


def _start_lock_holder(lock_path: Path) -> subprocess.Popen[str]:
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys, time; "
                "handle = open(sys.argv[1], 'w'); "
                "fcntl.flock(handle, fcntl.LOCK_EX); "
                "print('ready', flush=True); "
                "time.sleep(30)"
            ),
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline() == "ready\n"
    return holder


def test_healthy_service_bypasses_legacy_lifetime_start_lock(tmp_path: Path) -> None:
    launcher = _write_fake_launcher_project(tmp_path)
    lock_path = tmp_path / "startup.lock"
    holder = _start_lock_holder(lock_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        port = listener.getsockname()[1]
        env = _fake_launcher_env(tmp_path, lock_path, port)
        result = subprocess.run(
            ["bash", launcher],
            env=env,
            capture_output=True,
            text=True,
            timeout=3,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert "Reusing the attested" in result.stdout
        assert Path(env["FAKE_REUSE_MARKER"]).exists()
        assert not Path(env["FAKE_LAUNCH_MARKER"]).exists()
    finally:
        listener.close()
        holder.terminate()
        holder.wait(timeout=5)


def test_lock_waiter_rechecks_reuse_before_launching(tmp_path: Path) -> None:
    launcher = _write_fake_launcher_project(tmp_path)
    lock_path = tmp_path / "startup.lock"
    holder = _start_lock_holder(lock_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    env = _fake_launcher_env(tmp_path, lock_path, port)
    contender = subprocess.Popen(
        ["bash", launcher],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.2)
        listener.listen()
        holder.terminate()
        holder.wait(timeout=5)
        stdout, stderr = contender.communicate(timeout=5)
        assert contender.returncode == 0, (stdout, stderr)
        assert Path(env["FAKE_REUSE_MARKER"]).exists()
        assert not Path(env["FAKE_LAUNCH_MARKER"]).exists()
    finally:
        listener.close()
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)
        if contender.poll() is None:
            contender.terminate()
            contender.wait(timeout=5)


def test_launcher_releases_start_lock_after_canary(tmp_path: Path) -> None:
    launcher = _write_fake_launcher_project(tmp_path)
    lock_path = tmp_path / "startup.lock"
    env = _fake_launcher_env(tmp_path, lock_path, _unused_loopback_port())
    owner = subprocess.Popen(
        ["bash", launcher],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        canary = Path(env["FAKE_CANARY_MARKER"])
        while not canary.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert canary.exists()
        assert owner.poll() is None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            probe = subprocess.run(["flock", "-n", str(lock_path), "true"])
            if probe.returncode == 0:
                break
            time.sleep(0.02)
        assert probe.returncode == 0
    finally:
        owner.terminate()
        owner.communicate(timeout=5)


def test_backend_script_probes_but_never_owns_ocr_service() -> None:
    text = BACKEND_START_SCRIPT.read_text()
    expected_warning = (
        'echo "WARNING: PaddleOCR-VL is unavailable at ${OCR_SERVICE_URL}. '
        'Start it with: bash scripts/2b_start_OCR_LM.sh"'
    )
    stale_warning = (
        'echo "WARNING: PaddleOCR-VL is unavailable at ${OCR_SERVICE_URL}. '
        'Start it with: bash scripts/2_start_OCR_LM.sh"'
    )
    assert "OCR_SERVICE_URL" in text
    assert 'ocr.get("base_url", "http://127.0.0.1:8118")' in text
    assert expected_warning in text
    assert stale_warning not in text
    assert "/openapi.json" in text
    assert "POST /layout-parsing" in text or "layout-parsing" in text
    ocr_block = text[text.index("OCR_SERVICE_URL"):text.index("cleanup()")]
    assert "check_and_kill_port 30001" not in ocr_block
    assert "check_and_kill_port \"$OCR_PORT\"" not in ocr_block
    subprocess.run(["bash", "-n", BACKEND_START_SCRIPT], check=True)


def test_two_launchers_do_not_reuse_child_being_stopped(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scripts = project / "scripts"
    ocr_bin = project / ".paddle-ocr/bin"
    scripts.mkdir(parents=True)
    ocr_bin.mkdir(parents=True)
    (project / "config").mkdir()
    (project / "config/paddleocr-vl-thor.yaml").write_text(
        "pipeline_name: PaddleOCR-VL-1.6\n"
    )
    (scripts / "2b_start_OCR_LM.sh").write_text(START_SCRIPT.read_text())
    (scripts / "_workspace_paths.sh").write_text(WORKSPACE_HELPERS.read_text())
    os.symlink(sys.executable, ocr_bin / "python")
    (ocr_bin / "activate").write_text("")

    fake_preflight = scripts / "paddle_ocr_preflight.py"
    fake_preflight.write_text(
        """#!/usr/bin/env python3
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

command = sys.argv[1]
if command == "reuse":
    Path(os.environ["FAKE_REUSE_MARKER"]).write_text("unsafe reuse")
    raise SystemExit(0)
if command == "probe":
    url = sys.argv[sys.argv.index("--url") + 1]
    parsed = urlparse(url)
    with socket.socket() as sock:
        sock.settimeout(0.2)
        raise SystemExit(0 if sock.connect_ex((parsed.hostname, parsed.port)) == 0 else 1)
raise SystemExit(0)
"""
    )

    fake_paddlex = ocr_bin / "paddlex"
    fake_paddlex.write_text(
        """#!/usr/bin/env python3
import os
import signal
import socket
import sys
import time
from pathlib import Path

port = int(sys.argv[sys.argv.index("--port") + 1])
running = True
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen()
server.settimeout(0.1)

def stop(_signum, _frame):
    global running
    Path(os.environ["FAKE_TERM_MARKER"]).write_text("term started")
    time.sleep(1.5)
    running = False

signal.signal(signal.SIGTERM, stop)
while running:
    try:
        connection, _ = server.accept()
    except TimeoutError:
        continue
    connection.close()
server.close()
"""
    )
    fake_paddlex.chmod(0o755)

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]

    term_marker = tmp_path / "term-started"
    reuse_marker = tmp_path / "reuse-called"
    env = {
        **os.environ,
        "OCR_PORT": str(port),
        "OCR_EXIT_AFTER_CANARY": "1",
        "OCR_READINESS_TIMEOUT": "10",
        "OCR_SHUTDOWN_TIMEOUT": "5",
        "OCR_START_LOCK_TIMEOUT": "5",
        "OCR_START_LOCK": str(tmp_path / "startup.lock"),
        "OCR_LOG": str(tmp_path / "service.log"),
        "OCR_ATTESTATION_PATH": str(tmp_path / "attestation.json"),
        "OCR_CUDA_MEMORY_LIMIT_GB": "16",
        "OCR_MEMORY_REPORT": str(tmp_path / "memory-limit.json"),
        "OCR_SERVICE_WRAPPER": str(ocr_bin / "paddlex"),
        "FAKE_TERM_MARKER": str(term_marker),
        "FAKE_REUSE_MARKER": str(reuse_marker),
    }
    first = subprocess.Popen(
        ["bash", scripts / "2b_start_OCR_LM.sh"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not term_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert term_marker.exists(), first.communicate(timeout=5)

    second = subprocess.run(
        ["bash", scripts / "2b_start_OCR_LM.sh"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    first_stdout, first_stderr = first.communicate(timeout=10)

    assert first.returncode == 0, (first_stdout, first_stderr)
    assert second.returncode == 0, (second.stdout, second.stderr)
    assert not reuse_marker.exists(), (second.stdout, second.stderr)
    assert second.stdout.count("Starting official PaddleOCR-VL full Pipeline") == 1


def test_probe_requires_layout_parsing_post_operation() -> None:
    preflight = _load_preflight()
    with _serve(_PaddleXContractHandler) as url:
        result = preflight.probe_service(url)
    assert result["layout_parsing_post"] is True


def test_probe_rejects_unrelated_http_listener() -> None:
    preflight = _load_preflight()

    class UnrelatedHandler(_QuietHandler):
        def do_GET(self) -> None:
            body = b'{"paths":{"/other":{"get":{}}}}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with _serve(UnrelatedHandler) as url:
        with pytest.raises(preflight.PreflightError, match="layout-parsing"):
            preflight.probe_service(url)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ({"schemaVersion": 0}, "schema"),
        ({"pid": os.getpid() + 1}, "PID|pid"),
        ({"cudaMemoryLimitGiB": 15}, "limit"),
        ({"allocatorFraction": 0.5}, "fraction"),
        ({"deviceTotalMemoryBytes": 8 * 1024**3}, "total|device"),
    ],
)
def test_cuda_memory_limit_report_rejects_mismatched_runtime_evidence(
    tmp_path: Path, mutation: dict[str, object], error: str
) -> None:
    preflight = _load_preflight()
    report = _cuda_memory_report(os.getpid())
    report.update(mutation)
    report_path = tmp_path / "memory.json"
    report_path.write_text(json.dumps(report))

    with pytest.raises(preflight.PreflightError, match=error):
        preflight.validate_cuda_memory_limit_report(
            report_path, expected_limit_gb=16, expected_pid=os.getpid()
        )


def test_cuda_memory_limit_report_accepts_expected_runtime_evidence(
    tmp_path: Path,
) -> None:
    preflight = _load_preflight()
    report = _cuda_memory_report(os.getpid())
    report_path = tmp_path / "memory.json"
    report_path.write_text(json.dumps(report))

    assert preflight.validate_cuda_memory_limit_report(
        report_path, expected_limit_gb=16, expected_pid=os.getpid()
    ) == report


def test_reuse_requires_fresh_config_bound_listener_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preflight = _load_preflight()
    config_path = tmp_path / "pipeline.yaml"
    config_path.write_text("pipeline_name: PaddleOCR-VL-1.6\n")
    pipeline = {
        "pipelineName": "PaddleOCR-VL-1.6",
        "pipelineVersion": "v1.6",
        "configSha256": "config-sha",
        "manifestSha256": "manifest-sha",
    }
    monkeypatch.setattr(preflight, "validate_pipeline_config", lambda path: pipeline)
    attestation_path = tmp_path / "attestation.json"
    memory_report_path = tmp_path / "memory.json"

    with _serve(_PaddleXContractHandler) as url:
        identity = preflight.collect_process_identity(os.getpid(), url)
        memory_report = _cuda_memory_report(os.getpid())
        memory_report_path.write_text(json.dumps(memory_report))
        attestation_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "architecture": "aarch64",
                    "cudaAvailable": True,
                    "gpuName": "NVIDIA Thor",
                    "engine": "transformers",
                    "device": "cuda:0",
                    "packages": {"transformers": "5.14.1"},
                    "canaryPassed": True,
                    "pipelineApi": f"{url}/layout-parsing",
                    "pipeline": pipeline,
                    "cudaMemoryLimit": memory_report,
                    "processIdentity": identity,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
        result = preflight.validate_reusable_service(
            url,
            attestation_path,
            config_path,
            max_age_seconds=60,
            memory_report_path=memory_report_path,
            cuda_memory_limit_gb=16,
        )

    assert result["reuse"] is True
    assert result["servicePid"] == os.getpid()
    assert result["configSha256"] == "config-sha"
    assert result["manifestSha256"] == "manifest-sha"
    assert result["cudaMemoryLimitGiB"] == 16


def test_reuse_rejects_attestation_without_expected_memory_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preflight = _load_preflight()
    config_path = tmp_path / "pipeline.yaml"
    config_path.write_text("pipeline_name: PaddleOCR-VL-1.6\n")
    pipeline = {
        "pipelineName": "PaddleOCR-VL-1.6",
        "pipelineVersion": "v1.6",
        "configSha256": "config-sha",
        "manifestSha256": "manifest-sha",
    }
    monkeypatch.setattr(preflight, "validate_pipeline_config", lambda path: pipeline)
    attestation_path = tmp_path / "attestation.json"
    memory_report_path = tmp_path / "memory.json"

    with _serve(_PaddleXContractHandler) as url:
        identity = preflight.collect_process_identity(os.getpid(), url)
        memory_report_path.write_text(json.dumps(_cuda_memory_report(os.getpid())))
        attestation_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "architecture": "aarch64",
                    "cudaAvailable": True,
                    "gpuName": "NVIDIA Thor",
                    "engine": "transformers",
                    "device": "cuda:0",
                    "packages": {"transformers": "5.14.1"},
                    "canaryPassed": True,
                    "pipelineApi": f"{url}/layout-parsing",
                    "pipeline": pipeline,
                    "processIdentity": identity,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
        with pytest.raises(preflight.PreflightError, match="memory|limit|attestation"):
            preflight.validate_reusable_service(
                url,
                attestation_path,
                config_path,
                max_age_seconds=60,
                memory_report_path=memory_report_path,
                cuda_memory_limit_gb=16,
            )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ({"schemaVersion": 0}, "schema"),
        ({"gpuName": "NVIDIA H100"}, "Thor|GPU"),
        ({"transformers": "6.0.0"}, "Transformers|transformers"),
    ],
)
def test_reuse_rejects_old_schema_wrong_gpu_and_transformers_6(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: dict[str, object],
    error: str,
) -> None:
    preflight = _load_preflight()
    config_path = tmp_path / "pipeline.yaml"
    config_path.write_text("pipeline_name: PaddleOCR-VL-1.6\n")
    pipeline = {
        "pipelineName": "PaddleOCR-VL-1.6",
        "pipelineVersion": "v1.6",
        "configSha256": "config-sha",
        "manifestSha256": "manifest-sha",
    }
    monkeypatch.setattr(preflight, "validate_pipeline_config", lambda path: pipeline)
    attestation_path = tmp_path / "attestation.json"
    with _serve(_PaddleXContractHandler) as url:
        identity = preflight.collect_process_identity(os.getpid(), url)
        payload = {
            "schemaVersion": mutation.get("schemaVersion", 1),
            "architecture": "aarch64",
            "cudaAvailable": True,
            "gpuName": mutation.get("gpuName", "NVIDIA Thor"),
            "engine": "transformers",
            "device": "cuda:0",
            "packages": {
                "transformers": mutation.get("transformers", "5.14.1")
            },
            "canaryPassed": True,
            "pipelineApi": f"{url}/layout-parsing",
            "pipeline": pipeline,
            "processIdentity": identity,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        attestation_path.write_text(json.dumps(payload))
        with pytest.raises(preflight.PreflightError, match=error):
            preflight.validate_reusable_service(
                url, attestation_path, config_path, max_age_seconds=60
            )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ({"timestamp": "stale"}, "stale|timestamp"),
        ({"configSha256": "wrong"}, "config"),
        ({"manifestSha256": "wrong"}, "manifest"),
        ({"startTimeTicks": "wrong"}, "identity|start"),
        ({"listenerSocketInodes": []}, "listener|identity"),
    ],
)
def test_reuse_rejects_stale_or_mismatched_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: dict[str, object],
    error: str,
) -> None:
    preflight = _load_preflight()
    config_path = tmp_path / "pipeline.yaml"
    config_path.write_text("pipeline_name: PaddleOCR-VL-1.6\n")
    pipeline = {
        "pipelineName": "PaddleOCR-VL-1.6",
        "pipelineVersion": "v1.6",
        "configSha256": "config-sha",
        "manifestSha256": "manifest-sha",
    }
    monkeypatch.setattr(preflight, "validate_pipeline_config", lambda path: pipeline)
    attestation_path = tmp_path / "attestation.json"

    with _serve(_PaddleXContractHandler) as url:
        identity = preflight.collect_process_identity(os.getpid(), url)
        timestamp = datetime.now(timezone.utc).isoformat()
        if mutation.get("timestamp") == "stale":
            timestamp = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        attested_pipeline = dict(pipeline)
        attested_identity = dict(identity)
        for key, value in mutation.items():
            if key in attested_pipeline:
                attested_pipeline[key] = value
            elif key in attested_identity:
                attested_identity[key] = value
        attestation_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "architecture": "aarch64",
                    "cudaAvailable": True,
                    "gpuName": "NVIDIA Thor",
                    "engine": "transformers",
                    "device": "cuda:0",
                    "packages": {"transformers": "5.14.1"},
                    "canaryPassed": True,
                    "pipelineApi": f"{url}/layout-parsing",
                    "pipeline": attested_pipeline,
                    "processIdentity": attested_identity,
                    "timestamp": timestamp,
                }
            )
        )
        with pytest.raises(preflight.PreflightError, match=error):
            preflight.validate_reusable_service(
                url, attestation_path, config_path, max_age_seconds=60
            )


def test_canary_posts_content_aware_preprocessing_matrix_and_writes_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preflight = _load_preflight()
    _PaddleXContractHandler.requests = []
    _PaddleXContractHandler.include_preprocessor_evidence = True
    _PaddleXContractHandler.scramble_photographed_phrases = False
    monkeypatch.setattr(
        preflight,
        "collect_environment_attestation",
        lambda: {
            "architecture": "aarch64",
            "cudaAvailable": True,
            "gpuName": "NVIDIA Thor",
            "engine": "transformers",
            "device": "cuda:0",
        },
    )
    monkeypatch.setattr(
        preflight,
        "validate_pipeline_config",
        lambda path: {"pipelineName": "PaddleOCR-VL-1.6", "pipelineVersion": "v1.6"},
    )
    attestation_path = tmp_path / "runtime/ocr-attestation.json"
    memory_report_path = tmp_path / "runtime/ocr-memory.json"
    config_path = tmp_path / "pipeline.yaml"
    config_path.write_text("pipeline_name: PaddleOCR-VL-1.6\n")
    memory_report_path.parent.mkdir(parents=True, exist_ok=True)
    memory_report_path.write_text(json.dumps(_cuda_memory_report(os.getpid())))

    with _serve(_PaddleXContractHandler) as url:
        attestation = preflight.run_canaries(
            url,
            attestation_path,
            service_pid=os.getpid(),
            config_path=config_path,
            memory_report_path=memory_report_path,
            cuda_memory_limit_gb=16,
        )

    assert [item["fileType"] for item in _PaddleXContractHandler.requests] == [
        1,
        0,
        1,
        1,
        1,
    ]
    request_by_kind = {
        str(item["logId"]).removeprefix("paddleocr-p0-"): item
        for item in _PaddleXContractHandler.requests
    }
    assert set(request_by_kind) == {
        "image",
        "pdf",
        "rotated",
        "distorted-baseline",
        "distorted-retry",
    }
    expected_switches = {
        "useDocOrientationClassify": True,
        "useLayoutDetection": True,
        "useChartRecognition": True,
        "useSealRecognition": False,
        "useOcrForImageBlock": True,
        "formatBlockContent": True,
        "mergeLayoutBlocks": True,
        "layoutShapeMode": "auto",
        "prettifyMarkdown": True,
        "showFormulaNumber": True,
        "restructurePages": True,
        "mergeTables": True,
        "relevelTitles": True,
        "returnMarkdownImages": False,
        "visualize": False,
    }
    for kind, payload in request_by_kind.items():
        assert payload["file"]
        for key, value in expected_switches.items():
            assert payload[key] == value
        assert "useDocUnwarping" in payload
        assert payload["useDocUnwarping"] is (kind == "distorted-retry")
    assert attestation["canaryPassed"] is True
    for kind in ("image", "pdf", "rotated"):
        result = attestation["canaries"][kind]
        expected_text = f"THOR {kind.upper()} CANARY 2026"
        assert expected_text in result["markdown"]
        assert result["docPreprocessor"]["orientationEnabled"] is True
        assert result["docPreprocessor"]["unwarpingEnabled"] is False
    distorted = attestation["canaries"]["distorted"]
    assert distorted["passed"] is True
    assert distorted["fixture"] == {
        "provenance": (
            "PaddleX official doc_preprocessor demo asset: "
            "https://paddle-model-ecology.bj.bcebos.com/paddlex/demo_image/"
            "doc_test_rotated.jpg"
        ),
        "sha256": "6fb6b8ea8836a0acb46762a85d2bcf97100e33f0c4b49dcfc0b97f5955f4e534",
        "expectedPhrases": ["THE DISAPPEARING SUM", "VERONICA", "CHOICES"],
    }
    assert distorted["baseline"]["docPreprocessor"]["unwarpingEnabled"] is False
    assert distorted["retry"]["docPreprocessor"]["unwarpingEnabled"] is True
    assert distorted["retry"]["matchedExpectedPhrases"] == 3
    assert distorted["retry"]["matchedExpectedPhrases"] >= distorted["baseline"][
        "matchedExpectedPhrases"
    ]
    assert attestation["canaries"]["rotated"]["docPreprocessor"]["angle"] in {
        90,
        270,
    }
    assert json.loads(attestation_path.read_text()) == attestation
    assert attestation["memory"]["servicePid"] == os.getpid()
    assert attestation["memory"]["serviceVmHwmKiB"] > 0
    assert attestation["memory"]["serviceVmRssKiB"] > 0
    assert attestation["pipeline"]["pipelineVersion"] == "v1.6"
    assert attestation["cudaMemoryLimit"]["cudaMemoryLimitGiB"] == 16
    assert not list(attestation_path.parent.glob("*.tmp"))


def test_canary_rejects_missing_doc_preprocessor_execution_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preflight = _load_preflight()
    _PaddleXContractHandler.requests = []
    monkeypatch.setattr(_PaddleXContractHandler, "include_preprocessor_evidence", False)
    monkeypatch.setattr(
        preflight,
        "collect_environment_attestation",
        lambda: {"architecture": "aarch64", "cudaAvailable": True},
    )

    with _serve(_PaddleXContractHandler) as url:
        with pytest.raises(preflight.PreflightError, match="DocPreprocessor|preprocessor"):
            preflight.run_canaries(url, tmp_path / "attestation.json")


def test_canary_requires_all_expected_phrases_from_distorted_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preflight = _load_preflight()
    _PaddleXContractHandler.requests = []
    monkeypatch.setattr(_PaddleXContractHandler, "include_preprocessor_evidence", True)
    monkeypatch.setattr(_PaddleXContractHandler, "scramble_photographed_phrases", True)
    monkeypatch.setattr(
        preflight,
        "collect_environment_attestation",
        lambda: {"architecture": "aarch64", "cudaAvailable": True},
    )

    with _serve(_PaddleXContractHandler) as url:
        with pytest.raises(preflight.PreflightError, match="expected phrases"):
            preflight.run_canaries(url, tmp_path / "attestation.json")


def test_config_validation_rejects_cpu_or_non_transformers_models(
    tmp_path: Path,
) -> None:
    preflight = _load_preflight()
    config_path = tmp_path / "pipeline.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "pipeline_name": "PaddleOCR-VL-1.6",
                "device": "gpu:0",
                "engine": "transformers",
                "engine_config": {
                    "dtype": "bfloat16",
                    "device_type": "gpu",
                    "device_id": 0,
                },
                "SubModules": {
                    "LayoutDetection": {
                        "model_name": "layout-model",
                        "engine": "paddle",
                        "device": "cpu",
                    },
                    "VLRecognition": {
                        "model_name": "PaddleOCR-VL-1.6-0.9B",
                    },
                },
            }
        )
    )

    with pytest.raises(preflight.PreflightError, match="Transformers|CUDA"):
        preflight.validate_pipeline_config(config_path)


def test_exported_v16_config_has_reviewed_transformers_overrides() -> None:
    config = yaml.safe_load(
        (PROJECT_ROOT / "config/paddleocr-vl-thor.yaml").read_text()
    )
    assert config["pipeline_name"] == "PaddleOCR-VL-1.6"
    assert config["engine"] == "transformers"
    assert config["device"] == "gpu:0"
    assert config["engine_config"] == {
        "dtype": "bfloat16",
        "device_type": "gpu",
        "device_id": 0,
    }
    assert config["use_queues"] is True
    assert config["use_doc_preprocessor"] is True
    assert config["use_layout_detection"] is True
    assert config["use_chart_recognition"] is True
    assert config["use_seal_recognition"] is False
    assert config["use_ocr_for_image_block"] is True
    assert config["format_block_content"] is True
    assert config["merge_layout_blocks"] is True
    assert set(config["markdown_ignore_labels"]) == {
        "number",
        "header",
        "header_image",
        "footer",
        "footer_image",
    }
    assert config["SubModules"]["LayoutDetection"]["model_name"] == "PP-DocLayoutV3"
    assert config["SubModules"]["LayoutDetection"]["engine"] == "transformers"
    assert config["SubModules"]["LayoutDetection"]["engine_config"] == {
        "dtype": "float32",
        "device_type": "gpu",
        "device_id": 0,
        "attn_implementation": "eager",
    }
    assert (
        config["SubModules"]["VLRecognition"]["model_name"]
        == "PaddleOCR-VL-1.6-0.9B"
    )
    assert config["SubModules"]["VLRecognition"]["engine"] == "transformers"
    assert config["SubModules"]["VLRecognition"]["engine_config"] == {
        "dtype": "bfloat16",
        "device_type": "gpu",
        "device_id": 0,
        "attn_implementation": "sdpa",
        "generation_config": {"do_sample": False},
    }
    assert config["SubModules"]["LayoutDetection"]["model_dir"] == (
        "~/.paddlex/official_models/PP-DocLayoutV3_safetensors"
    )
    assert config["SubModules"]["VLRecognition"]["model_dir"] == (
        "~/.paddlex/official_models/PaddleOCR-VL-1.6"
    )
    preprocessor = config["SubPipelines"]["DocPreprocessor"]
    assert preprocessor["use_doc_orientation_classify"] is True
    # PaddleX 3.6.1 only constructs UVDoc when this initialization flag is true.
    # Project-owned normal requests still explicitly send useDocUnwarping=false.
    assert preprocessor["use_doc_unwarping"] is True
    orientation = preprocessor["SubModules"]["DocOrientationClassify"]
    assert orientation["engine"] == "transformers"
    assert orientation["model_dir"] == (
        "~/.paddlex/official_models/PP-LCNet_x1_0_doc_ori_safetensors"
    )
    assert orientation["engine_config"]["device_type"] == "gpu"
    assert orientation["engine_config"]["device_id"] == 0
    unwarping = preprocessor["SubModules"]["DocUnwarping"]
    assert unwarping["engine"] == "transformers"
    assert unwarping["model_dir"] == (
        "~/.paddlex/official_models/UVDoc_safetensors"
    )
    assert unwarping["engine_config"]["device_type"] == "gpu"
    assert unwarping["engine_config"]["device_id"] == 0
    assert config["Serving"]["visualize"] is False
    assert config["Serving"]["return_urls"] is False
    assert config["Serving"]["extra"]["max_num_input_imgs"] == 32


def test_model_manifest_covers_every_local_model_artifact() -> None:
    manifest = json.loads(MODEL_MANIFEST.read_text())
    models = manifest["models"]
    assert set(models) == {
        "LayoutDetection",
        "VLRecognition",
        "DocOrientationClassify",
        "DocUnwarping",
    }
    assert {
        "configuration_paddleocr_vl.py",
        "modeling_paddleocr_vl.py",
        "image_processing_paddleocr_vl.py",
        "processing_paddleocr_vl.py",
        "chat_template.jinja",
        "tokenizer.json",
        "tokenizer.model",
        "model.safetensors",
    } <= set(models["VLRecognition"]["files"])
    for model in models.values():
        assert len(model["revision"]) == 40
        assert Path(model["model_dir"]).expanduser().is_absolute()
        assert model["files"]
        for metadata in model["files"].values():
            assert len(metadata["sha256"]) == 64
            assert metadata["bytes"] > 0


@pytest.mark.parametrize("module_name", EXPECTED_MODEL_IDENTITIES)
@pytest.mark.parametrize(
    ("field", "mutated_value", "error"),
    [
        ("model_id", "PaddlePaddle/unexpected-model", "model ID"),
        ("revision", "0" * 40, "revision"),
    ],
)
def test_model_manifest_rejects_exact_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    field: str,
    mutated_value: str,
    error: str,
) -> None:
    preflight = _load_preflight()
    required_files = {name: {"artifact.bin"} for name in EXPECTED_MODEL_IDENTITIES}
    monkeypatch.setattr(preflight, "REQUIRED_MODEL_FILES", required_files)
    manifest = {"schema_version": 1, "models": {}}
    module_configs = {}
    for name, (model_id, revision) in EXPECTED_MODEL_IDENTITIES.items():
        model_dir = tmp_path / name
        model_dir.mkdir()
        artifact = model_dir / "artifact.bin"
        artifact.write_bytes(name.encode())
        model_name = f"{name}-model"
        manifest["models"][name] = {
            "model_id": model_id,
            "model_name": model_name,
            "model_dir": str(model_dir),
            "revision": revision,
            "files": {
                "artifact.bin": {
                    "bytes": artifact.stat().st_size,
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                }
            },
        }
        module_configs[name] = {
            "model_name": model_name,
            "model_dir": str(model_dir),
        }

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    preflight._validate_model_manifest(module_configs, manifest_path)

    manifest["models"][module_name][field] = mutated_value
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(preflight.PreflightError, match=error):
        preflight._validate_model_manifest(module_configs, manifest_path)


@pytest.mark.parametrize("home_name", ["alice", "bob"])
def test_model_manifest_expands_current_home_and_preserves_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    home_name: str,
) -> None:
    preflight = _load_preflight()
    home = tmp_path / home_name
    monkeypatch.setenv("HOME", str(home))
    manifest = {"schema_version": 1, "models": {}}
    module_configs = {}
    for name, required_files in preflight.REQUIRED_MODEL_FILES.items():
        model_id, revision = EXPECTED_MODEL_IDENTITIES[name]
        model_dir = home / "models" / name
        model_dir.mkdir(parents=True)
        files = {}
        for relative_path in required_files:
            artifact = model_dir / relative_path
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(b"reviewed-test-artifact")
            files[relative_path] = {
                "bytes": artifact.stat().st_size,
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        configured_dir = f"~/models/{name}"
        manifest["models"][name] = {
            "model_id": model_id,
            "model_name": name,
            "model_dir": configured_dir,
            "revision": revision,
            "files": files,
        }
        module_configs[name] = {"model_name": name, "model_dir": configured_dir}

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    summaries, _ = preflight._validate_model_manifest(module_configs, manifest_path)
    assert set(summaries) == set(EXPECTED_MODEL_IDENTITIES)
    assert summaries["LayoutDetection"]["modelDir"] == "~/models/LayoutDetection"

    entry = manifest["models"]["LayoutDetection"]
    original_dir = entry["model_dir"]
    for invalid_dir in ("models/LayoutDetection", "~/missing-model"):
        entry["model_dir"] = invalid_dir
        module_configs["LayoutDetection"]["model_dir"] = invalid_dir
        manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(preflight.PreflightError, match="existing absolute model directory"):
            preflight._validate_model_manifest(module_configs, manifest_path)

    entry["model_dir"] = original_dir
    manifest_path.write_text(json.dumps(manifest))
    module_configs["LayoutDetection"]["model_dir"] = str(home / "models" / "LayoutDetection")
    with pytest.raises(preflight.PreflightError, match="directory does not match config"):
        preflight._validate_model_manifest(module_configs, manifest_path)

    module_configs["LayoutDetection"]["model_dir"] = original_dir
    relative_path = next(iter(entry["files"]))
    (home / "models" / "LayoutDetection" / relative_path).write_bytes(b"tampered-test-artifact")
    with pytest.raises(preflight.PreflightError, match="checksum or size"):
        preflight._validate_model_manifest(module_configs, manifest_path)


def test_config_validation_rejects_tampered_local_model_artifacts(
    tmp_path: Path,
) -> None:
    preflight = _load_preflight()
    config = yaml.safe_load(
        (PROJECT_ROOT / "config/paddleocr-vl-thor.yaml").read_text()
    )
    manifest = json.loads(MODEL_MANIFEST.read_text())
    model_dir = tmp_path / "LayoutDetection"
    model_dir.mkdir()
    for relative_path in manifest["models"]["LayoutDetection"]["files"]:
        artifact = model_dir / relative_path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"tampered")
    config["SubModules"]["LayoutDetection"]["model_dir"] = str(model_dir)
    manifest["models"]["LayoutDetection"]["model_dir"] = str(model_dir)
    config_path = tmp_path / "pipeline.yaml"
    config_path.write_text(yaml.safe_dump(config))
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(preflight.PreflightError, match="checksum"):
        preflight.validate_pipeline_config(config_path, manifest_path)


def test_config_validation_rejects_every_prd_switch_drift(tmp_path: Path) -> None:
    preflight = _load_preflight()
    baseline = yaml.safe_load(
        (PROJECT_ROOT / "config/paddleocr-vl-thor.yaml").read_text()
    )
    mutations = [
        ("use_queues", False),
        ("use_doc_preprocessor", False),
        ("use_layout_detection", False),
        ("use_chart_recognition", False),
        ("use_seal_recognition", True),
        ("use_ocr_for_image_block", False),
        ("format_block_content", False),
        ("merge_layout_blocks", False),
    ]
    for key, value in mutations:
        config = copy.deepcopy(baseline)
        config[key] = value
        path = tmp_path / f"{key}.yaml"
        path.write_text(yaml.safe_dump(config))
        with pytest.raises(preflight.PreflightError, match="config|switch|must"):
            preflight.validate_pipeline_config(path, MODEL_MANIFEST)

    for key, value in (
        ("use_doc_orientation_classify", False),
        ("use_doc_unwarping", False),
    ):
        config = copy.deepcopy(baseline)
        config["SubPipelines"]["DocPreprocessor"][key] = value
        path = tmp_path / f"{key}.yaml"
        path.write_text(yaml.safe_dump(config))
        with pytest.raises(preflight.PreflightError, match="config|switch|must"):
            preflight.validate_pipeline_config(path, MODEL_MANIFEST)

    config = copy.deepcopy(baseline)
    config["Serving"]["extra"]["max_num_input_imgs"] = None
    path = tmp_path / "serving.yaml"
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(preflight.PreflightError, match="32"):
        preflight.validate_pipeline_config(path, MODEL_MANIFEST)


def test_paddle_ocr_environment_is_ignored() -> None:
    entries = (PROJECT_ROOT / ".gitignore").read_text().splitlines()
    assert ".paddle-ocr/" in entries
