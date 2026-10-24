from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "scripts/asr_preflight.py"
LAUNCHER = ROOT / "scripts/2c_start_asr_sglang.sh"
TLS_GENERATOR = ROOT / "scripts/generate_asr_tls_cert.sh"
README = ROOT / "README.md"


def test_generated_asr_private_data_is_ignored_without_ignoring_all_data():
    rules = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "data/asr-tls/" in rules
    assert "data/debug-audio/" in rules
    assert "data/" not in rules


def _fake_model(tmp_path: Path, *, missing_second_shard: bool = False) -> Path:
    model = tmp_path / "Qwen3-ASR-1.7B"
    model.mkdir()
    (model / "config.json").write_text(
        json.dumps({"architectures": ["Qwen3ASRForConditionalGeneration"]})
    )
    (model / "tokenizer_config.json").write_text("{}")
    (model / "vocab.json").write_text("{}")
    (model / "merges.txt").write_text("#version: 0.2\n")
    (model / "model.safetensors.index.json").write_text(
        json.dumps(
            {
                "weight_map": {
                    "a": "model-00001-of-00002.safetensors",
                    "b": "model-00002-of-00002.safetensors",
                }
            }
        )
    )
    (model / "model-00001-of-00002.safetensors").write_bytes(b"one")
    if not missing_second_shard:
        (model / "model-00002-of-00002.safetensors").write_bytes(b"two")
    return model


def _config_for(tmp_path: Path, model: Path) -> Path:
    data = yaml.safe_load((ROOT / "config/config-asr.yaml").read_text())
    data["sglang"]["model_path"] = str(model)
    data["sglang"]["port"] = 39997
    data["sglang"]["startup_timeout_seconds"] = 300.0
    path = tmp_path / "config-asr.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_asr_preflight_accepts_complete_qwen_model(tmp_path: Path):
    from scripts.asr_preflight import validate_asr_model

    report = validate_asr_model(_config_for(tmp_path, _fake_model(tmp_path)))
    assert report.architecture == "Qwen3ASRForConditionalGeneration"
    assert report.shard_count == 2
    assert report.port == 39997


def test_asr_preflight_rejects_missing_index_shard(tmp_path: Path):
    from scripts.asr_preflight import ModelPreflightError, validate_asr_model

    config = _config_for(
        tmp_path, _fake_model(tmp_path, missing_second_shard=True)
    )
    with pytest.raises(ModelPreflightError, match="model-00002-of-00002"):
        validate_asr_model(config)


def test_asr_preflight_reuses_central_tokenizer_vocabulary_validation(
    tmp_path: Path,
):
    from scripts.asr_preflight import ModelPreflightError, validate_asr_model

    model = _fake_model(tmp_path)
    (model / "vocab.json").unlink()
    (model / "merges.txt").unlink()

    with pytest.raises(ModelPreflightError, match="tokenizer vocabulary assets"):
        validate_asr_model(_config_for(tmp_path, model))


def test_asr_launcher_uses_preflight_yaml_values_without_model_fallback():
    text = LAUNCHER.read_text()
    assert "asr_preflight.py" in text
    assert 'ARGS=(python3 -m sglang.launch_server' in text
    assert '--port "$ASR_PORT"' in text
    assert '--model-path "$MODEL_PATH"' in text
    assert "MODEL_PATH:-" not in text
    assert "~/Project/LLM-weight/Qwen3-ASR-1.7B" not in text
    assert "pkill" not in text
    assert "lsof" not in text


def test_asr_preflight_serializes_typed_startup_timeout(tmp_path: Path):
    from scripts.asr_preflight import _shell_assignments, validate_asr_model

    config_path = _config_for(tmp_path, _fake_model(tmp_path))
    data = yaml.safe_load(config_path.read_text())
    data["sglang"]["startup_timeout_seconds"] = 321.5
    config_path.write_text(yaml.safe_dump(data))

    assignments = _shell_assignments(validate_asr_model(config_path))

    assert "STARTUP_TIMEOUT_SECONDS=321.5" in assignments


def test_asr_preflight_serializes_typed_token_limits(tmp_path: Path):
    from scripts.asr_preflight import _shell_assignments, validate_asr_model

    config_path = _config_for(tmp_path, _fake_model(tmp_path))
    data = yaml.safe_load(config_path.read_text())
    data["sglang"]["context_length"] = 12288
    data["sglang"]["max_total_tokens"] = 16384
    data["sglang"]["max_running_requests"] = 1
    config_path.write_text(yaml.safe_dump(data))

    assignments = _shell_assignments(validate_asr_model(config_path))

    assert "CONTEXT_LENGTH=12288" in assignments
    assert "MAX_TOTAL_TOKENS=16384" in assignments
    assert "MAX_RUNNING_REQUESTS=1" in assignments


def test_asr_preflight_serializes_shutdown_grace(tmp_path: Path):
    from scripts.asr_preflight import _shell_assignments, validate_asr_model

    config_path = _config_for(tmp_path, _fake_model(tmp_path))
    data = yaml.safe_load(config_path.read_text())
    data["runtime"]["shutdown_timeout_seconds"] = 17
    config_path.write_text(yaml.safe_dump(data))

    assignments = _shell_assignments(validate_asr_model(config_path))

    assert "SHUTDOWN_TIMEOUT_SECONDS=17" in assignments


def test_asr_preflight_detects_bound_port_without_listener():
    from scripts.asr_preflight import _port_is_free

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = int(occupied.getsockname()[1])

        assert _port_is_free("127.0.0.1", port) is False


@pytest.mark.parametrize("host", ["::", "::1"])
def test_asr_preflight_uses_ipv6_family_for_free_and_bound_ports(host: str):
    from scripts.asr_preflight import _port_is_free

    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind((host, 0))
            port = int(probe.getsockname()[1])
        assert _port_is_free(host, port) is True
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as occupied:
            occupied.bind((host, port))
            assert _port_is_free(host, port) is False
    except OSError as exc:
        pytest.skip(f"platform cannot bind IPv6 host {host}: {exc}")


def test_asr_preflight_checks_all_hostname_address_families():
    from scripts.asr_preflight import _port_is_free

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = int(occupied.getsockname()[1])

        assert _port_is_free("localhost", port) is False


def test_disabled_preflight_reports_status_before_model_and_bound_port_checks(
    tmp_path: Path,
):
    config_path = _config_for(tmp_path, _fake_model(tmp_path))
    data = yaml.safe_load(config_path.read_text())
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        data["enabled"] = False
        data["sglang"]["model_path"] = str(tmp_path / "missing-disabled-model")
        data["sglang"]["port"] = int(occupied.getsockname()[1])
        config_path.write_text(yaml.safe_dump(data))

        result = subprocess.run(
            [
                sys.executable,
                str(PREFLIGHT),
                "--config",
                str(config_path),
                "--check-only",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert "disabled" in result.stdout.lower()


def test_asr_preflight_supports_direct_script_execution():
    result = subprocess.run(
        [sys.executable, str(PREFLIGHT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_tls_generator_and_launchers_use_configured_trust_without_global_bypass():
    generator = TLS_GENERATOR.read_text()
    backend = (ROOT / "scripts/3_start_backend.sh").read_text()
    frontend = (ROOT / "scripts/4_start_frontend.sh").read_text()
    assert "subject_alt_names" in generator
    assert 'chmod 600 "$CA_KEY" "$SERVER_KEY"' in generator
    assert "--ssl-keyfile" in backend and "--ssl-certfile" in backend
    assert "--experimental-https-key" in frontend
    assert "--experimental-https-cert" in frontend
    assert "--experimental-https-ca" in frontend
    assert "NODE_EXTRA_CA_CERTS" in frontend
    assert "NODE_TLS_REJECT_UNAUTHORIZED=0" not in backend + frontend


def test_backend_resolves_all_tls_paths_before_health_probe():
    backend = (ROOT / "scripts/3_start_backend.sh").read_text()
    assert '"ASR_CA_CERTIFICATE": resolve_path(' in backend
    assert '"ASR_CERTIFICATE": resolve_path(' in backend
    assert '"ASR_PRIVATE_KEY": resolve_path(' in backend
    assert 'curl_tls=(--cacert "$ASR_CA_CERTIFICATE")' in backend
    assert '[ -f "$ASR_CA_CERTIFICATE" ]' in backend
    assert '"$PROJECT_ROOT/$ASR_CA_CERTIFICATE"' not in backend


def test_https_launchers_report_secure_urls_and_android_ca_path():
    backend = (ROOT / "scripts/3_start_backend.sh").read_text()
    frontend = (ROOT / "scripts/4_start_frontend.sh").read_text()
    assert 'BACKEND_SCHEME="https"' in backend
    assert '${BACKEND_SCHEME}://${BACKEND_HOST}:${BACKEND_PORT}' in backend
    assert '${BACKEND_SCHEME}://127.0.0.1:${BACKEND_PORT}' in backend
    assert 'https://${LAN_HOST}:${FRONTEND_DEV_PORT}' in frontend
    assert 'Android CA: $ASR_CA_CERTIFICATE' in frontend


def test_backend_and_frontend_launchers_use_shared_typed_asr_validation():
    backend = (ROOT / "scripts/3_start_backend.sh").read_text()
    frontend = (ROOT / "scripts/4_start_frontend.sh").read_text()

    for launcher in (backend, frontend):
        assert "get_shared_sglang_python" in launcher
        assert "load_asr_config" in launcher
        assert 'if ! ASR_CONFIG_ASSIGNMENTS=' in launcher
        assert 'eval "$ASR_CONFIG_ASSIGNMENTS"' in launcher


def test_readme_documents_complete_asr_android_https_workflow():
    readme = README.read_text()

    for required in (
        "silero-vad==6.2.0",
        "scripts/asr_preflight.py",
        "scripts/2c_start_asr_sglang.sh",
        "scripts/generate_asr_tls_cert.sh",
        "https_enabled: true",
        "scripts/3_start_backend.sh",
        "scripts/4_start_frontend.sh",
        "Android",
        "Voice",
        "麦克风",
    ):
        assert required in readme
    assert "LAN HTTP" in readme
    assert "键盘文本" in readme
    assert "bash scripts/2_start_backend.sh" not in readme
    assert "bash scripts/3_start_frontend.sh" not in readme


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_fake_sglang(tmp_path: Path) -> Path:
    package_root = tmp_path / "fake-python"
    registry = package_root / "sglang/srt/models"
    registry.mkdir(parents=True)
    for package in [
        package_root / "sglang",
        package_root / "sglang/srt",
        package_root / "sglang/srt/models",
    ]:
        (package / "__init__.py").write_text("")
    (registry / "registry.py").write_text(
        "class ModelRegistry:\n"
        "    @staticmethod\n"
        "    def get_supported_archs():\n"
        "        return {'Qwen3ASRForConditionalGeneration'}\n"
    )
    (package_root / "sglang/launch_server.py").write_text(
        """
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument('--model-path')
parser.add_argument('--host')
parser.add_argument('--port', type=int)
parser.add_argument('--served-model-name')
parser.add_argument('--dtype')
parser.add_argument('--mem-fraction-static')
parser.add_argument('--context-length', type=int)
parser.add_argument('--max-total-tokens', type=int)
parser.add_argument('--max-running-requests')
parser.add_argument('--trust-remote-code', action='store_true')
args = parser.parse_args()

mode = os.environ.get('FAKE_ASR_MODE', 'ready')
if mode == 'exit':
    raise SystemExit(23)
delay = float(os.environ.get('FAKE_ASR_DELAY', '0'))
time.sleep(delay)
pid_file = os.environ.get('FAKE_ASR_PID_FILE')
if pid_file:
    open(pid_file, 'w').write(str(os.getpid()))
args_file = os.environ.get('FAKE_ASR_ARGS_FILE')
if args_file:
    open(args_file, 'w').write(json.dumps({
        'context_length': args.context_length,
        'max_total_tokens': args.max_total_tokens,
        'max_running_requests': int(args.max_running_requests),
    }))
if mode in {'stubborn', 'leader-exit-before-ready', 'leader-exit-after-ready'}:
    descendant = subprocess.Popen([
        sys.executable,
        '-c',
        'import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        'signal.signal(signal.SIGINT, signal.SIG_IGN); '
        'signal.signal(signal.SIGHUP, signal.SIG_IGN); '
        'open(os.environ["FAKE_ASR_DESCENDANT_PID_FILE"], "w").write(str(os.getpid())); '
        'time.sleep(60)',
    ])
if mode == 'leader-exit-before-ready':
    time.sleep(0.25)
    raise SystemExit(24)

class Handler(BaseHTTPRequestHandler):
    health_requests = 0

    def do_GET(self):
        if self.path == '/health':
            type(self).health_requests += 1
            if mode == 'slow-health':
                time.sleep(2)
            if mode == 'health-400':
                self.send_response(400)
                self.end_headers()
                return
            if mode == 'startup-503' and type(self).health_requests <= 2:
                self.send_response(503)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ok')
            return
        if self.path == '/v1/models':
            model = args.served_model_name if mode != 'mismatch' else 'wrong-model'
            body = json.dumps({'data': [{'id': model}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass

server = ThreadingHTTPServer((args.host, args.port), Handler)
if mode == 'leader-exit-after-ready':
    def exit_leader():
        time.sleep(0.5)
        os._exit(24)
    threading.Thread(target=exit_leader, daemon=True).start()
def stop(signum, frame):
    if mode == 'stubborn':
        return
    open(os.environ['FAKE_ASR_SIGNAL_FILE'], 'w').write(str(signum))
    raise SystemExit(0)
for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(sig, stop)
server.serve_forever()
""".lstrip()
    )
    return package_root


def _launcher_config(
    tmp_path: Path,
    *,
    timeout: float = 3.0,
    enabled: bool = True,
    shutdown_timeout: int = 15,
) -> tuple[Path, int]:
    model = _fake_model(tmp_path)
    config = _config_for(tmp_path, model)
    data = yaml.safe_load(config.read_text())
    port = _free_port()
    data["sglang"]["port"] = port
    data["sglang"]["startup_timeout_seconds"] = timeout
    data["runtime"]["shutdown_timeout_seconds"] = shutdown_timeout
    data["enabled"] = enabled
    if not enabled:
        data["sglang"]["model_path"] = str(tmp_path / "disabled-model-does-not-exist")
    config.write_text(yaml.safe_dump(data))
    return config, port


def _start_launcher(
    tmp_path: Path,
    *,
    mode: str,
    delay: float = 0.0,
    timeout: float = 3.0,
    enabled: bool = True,
    shutdown_timeout: int = 15,
) -> tuple[subprocess.Popen, Path, Path]:
    config, _ = _launcher_config(
        tmp_path,
        timeout=timeout,
        enabled=enabled,
        shutdown_timeout=shutdown_timeout,
    )
    fake_python = _write_fake_sglang(tmp_path)
    output = tmp_path / "launcher.log"
    signal_file = tmp_path / "signal.txt"
    pid_file = tmp_path / "child.pid"
    env = os.environ.copy()
    env.update(
        {
            "ASR_CONFIG_PATH": str(config),
            "ASR_LOG_DIR": str(tmp_path / "logs"),
            "ASR_STARTUP_POLL_INTERVAL_SECONDS": "0.05",
            "FAKE_ASR_MODE": mode,
            "FAKE_ASR_DELAY": str(delay),
            "FAKE_ASR_SIGNAL_FILE": str(signal_file),
            "FAKE_ASR_PID_FILE": str(pid_file),
            "FAKE_ASR_ARGS_FILE": str(tmp_path / "args.json"),
            "FAKE_ASR_DESCENDANT_PID_FILE": str(tmp_path / "descendant.pid"),
            "PYTHONPATH": str(fake_python),
        }
    )
    output_handle = output.open("w")
    process = subprocess.Popen(
        ["bash", str(LAUNCHER)],
        cwd=ROOT,
        env=env,
        stdout=output_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output_handle.close()
    return process, output, signal_file


def _wait_for_output(process: subprocess.Popen, output: Path, text: str) -> str:
    deadline = time.monotonic() + 8
    contents = ""
    while time.monotonic() < deadline:
        contents = output.read_text() if output.exists() else ""
        if text in contents:
            return contents
        if process.poll() is not None:
            break
        time.sleep(0.05)
    pytest.fail(
        f"launcher did not print {text!r}; rc={process.poll()}, output={contents!r}"
    )


def _wait_for_nonempty_file(process: subprocess.Popen, path: Path) -> str:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        contents = path.read_text() if path.exists() else ""
        if contents:
            return contents
        if process.poll() is not None:
            break
        time.sleep(0.05)
    pytest.fail(f"launcher did not create nonempty file {path}; rc={process.poll()}")


def test_launcher_waits_for_delayed_readiness_and_stays_attached(tmp_path: Path):
    process, output, signal_file = _start_launcher(
        tmp_path, mode="ready", delay=0.2
    )
    try:
        contents = _wait_for_output(process, output, "ASR SGLang is ready")
        assert "Qwen3-ASR-1.7B" in contents
        assert process.poll() is None
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)
    assert signal_file.read_text() == str(signal.SIGTERM)


def test_launcher_passes_configured_token_limits(tmp_path: Path):
    process, output, _ = _start_launcher(tmp_path, mode="ready")
    try:
        _wait_for_output(process, output, "ASR SGLang is ready")
        args = json.loads((tmp_path / "args.json").read_text())
        configured = yaml.safe_load((ROOT / "config/config-asr.yaml").read_text())[
            "sglang"
        ]
        assert args == {
            "context_length": configured["context_length"],
            "max_total_tokens": configured["max_total_tokens"],
            "max_running_requests": configured["max_running_requests"],
        }
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)


def test_launcher_retries_health_503_until_server_is_ready(tmp_path: Path):
    process, output, _ = _start_launcher(
        tmp_path,
        mode="startup-503",
        timeout=3.0,
    )
    try:
        contents = _wait_for_output(process, output, "ASR SGLang is ready")
        assert contents.count("HTTP status 503") >= 2
        assert process.poll() is None
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)


def test_launcher_fails_promptly_on_non_transient_health_status(tmp_path: Path):
    process, output, signal_file = _start_launcher(
        tmp_path,
        mode="health-400",
        timeout=30.0,
    )
    try:
        assert process.wait(timeout=5) != 0
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=5)

    contents = output.read_text().lower()
    assert "unhealthy http status 400" in contents
    assert signal_file.exists()


def test_disabled_launcher_exits_zero_without_spawning(tmp_path: Path):
    process, output, _ = _start_launcher(
        tmp_path, mode="ready", enabled=False
    )

    assert process.wait(timeout=5) == 0
    assert "disabled" in output.read_text().lower()
    assert not (tmp_path / "child.pid").exists()


def test_launcher_fails_and_cleans_up_on_model_mismatch(tmp_path: Path):
    process, output, signal_file = _start_launcher(tmp_path, mode="mismatch")

    assert process.wait(timeout=8) != 0
    contents = output.read_text().lower()
    assert "model mismatch" in contents
    assert "wrong-model" in contents
    assert "asr_sglang_" in contents and ".log" in contents
    assert signal_file.exists()


def test_launcher_reports_early_child_exit(tmp_path: Path):
    process, output, _ = _start_launcher(tmp_path, mode="exit", timeout=30.0)

    assert process.wait(timeout=5) != 0
    assert "exited before readiness" in output.read_text().lower()


def test_launcher_timeout_terminates_only_owned_child(tmp_path: Path):
    process, output, signal_file = _start_launcher(
        tmp_path, mode="ready", delay=5.0, timeout=0.2
    )

    assert process.wait(timeout=8) != 0
    assert "timed out" in output.read_text().lower()
    assert signal_file.exists() is False
    pid_file = tmp_path / "child.pid"
    if pid_file.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)


def test_launcher_timeout_caps_a_slow_health_probe(tmp_path: Path):
    started = time.monotonic()
    process, output, signal_file = _start_launcher(
        tmp_path, mode="slow-health", timeout=0.2
    )

    assert process.wait(timeout=8) != 0
    elapsed = time.monotonic() - started
    assert elapsed < 1.5
    assert "timed out" in output.read_text().lower()
    assert signal_file.exists()


def _process_is_running(pid: int) -> bool:
    stat = Path(f"/proc/{pid}/stat")
    if not stat.exists():
        return False
    fields = stat.read_text().split()
    return len(fields) > 2 and fields[2] != "Z"


def _wait_until_process_stops(pid: int) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not _process_is_running(pid):
            return
        time.sleep(0.05)
    pytest.fail(f"process {pid} remained alive")


def test_launcher_escalates_only_owned_stubborn_process_group(tmp_path: Path):
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    process, output, _ = _start_launcher(
        tmp_path, mode="stubborn", shutdown_timeout=1
    )
    try:
        _wait_for_output(process, output, "ASR SGLang is ready")
        _wait_for_nonempty_file(process, tmp_path / "descendant.pid")
        leader_pid = int((tmp_path / "child.pid").read_text())
        descendant_pid = int((tmp_path / "descendant.pid").read_text())

        started = time.monotonic()
        process.send_signal(signal.SIGTERM)

        assert process.wait(timeout=4) != 0
        assert time.monotonic() - started < 3
        _wait_until_process_stops(leader_pid)
        _wait_until_process_stops(descendant_pid)
        assert sentinel.poll() is None
        contents = output.read_text()
        assert "KILL" in contents
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        leader_file = tmp_path / "child.pid"
        if leader_file.exists():
            try:
                os.killpg(int(leader_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        sentinel.terminate()
        sentinel.wait(timeout=3)


@pytest.mark.parametrize(
    "mode", ["leader-exit-before-ready", "leader-exit-after-ready"]
)
def test_launcher_cleans_stubborn_descendant_when_owned_leader_exits(
    tmp_path: Path, mode: str
):
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    process, output, _ = _start_launcher(
        tmp_path, mode=mode, shutdown_timeout=1
    )
    try:
        descendant_pid = int(_wait_for_nonempty_file(process, tmp_path / "descendant.pid"))

        assert process.wait(timeout=5) != 0
        _wait_until_process_stops(descendant_pid)
        assert sentinel.poll() is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        leader_file = tmp_path / "child.pid"
        if leader_file.exists():
            try:
                os.killpg(int(leader_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        sentinel.terminate()
        sentinel.wait(timeout=3)


def _generate_launcher_certificates(
    tmp_path: Path, sans: list[str]
) -> tuple[Path, Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required for launcher TLS validation tests")
    ca_key = tmp_path / "ca.key"
    ca_cert = tmp_path / "ca.crt"
    server_key = tmp_path / "server.key"
    request = tmp_path / "server.csr"
    server_cert = tmp_path / "server.crt"
    extensions = tmp_path / "extensions.cnf"
    extensions.write_text("[server]\nsubjectAltName=" + ",".join(sans) + "\n")
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(ca_key), "-out", str(ca_cert), "-subj", "/CN=Launcher Test CA", "-days", "1"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [openssl, "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(server_key), "-out", str(request), "-subj", "/CN=localhost"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [openssl, "x509", "-req", "-in", str(request), "-CA", str(ca_cert), "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(server_cert), "-days", "1", "-sha256", "-extfile", str(extensions), "-extensions", "server"],
        check=True,
        capture_output=True,
    )
    return ca_cert, server_cert, server_key


def _isolated_https_launcher_root(tmp_path: Path, launcher_name: str) -> tuple[Path, Path]:
    root = tmp_path / launcher_name.replace(".sh", "")
    (root / "scripts").mkdir(parents=True)
    (root / "backend/asr").mkdir(parents=True)
    (root / "config").mkdir()
    (root / ".sglang/bin").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / launcher_name, root / "scripts" / launcher_name)
    shutil.copy2(ROOT / "scripts/_workspace_paths.sh", root / "scripts/_workspace_paths.sh")
    shutil.copy2(ROOT / "backend/asr/config.py", root / "backend/asr/config.py")
    (root / "backend/__init__.py").write_text("")
    (root / "backend/asr/__init__.py").write_text("")
    (root / ".sglang/bin/activate").write_text("")
    isolated_python = root / ".sglang/bin/python"
    isolated_python.write_text(f"#!/bin/bash\nexec {sys.executable} \"$@\"\n")
    isolated_python.chmod(0o755)

    model = _fake_model(root)
    ca_cert, server_cert, server_key = _generate_launcher_certificates(
        root, ["DNS:localhost", "IP:127.0.0.1", "DNS:unexpected.local"]
    )
    asr = yaml.safe_load((ROOT / "config/config-asr.yaml").read_text())
    asr["sglang"]["model_path"] = str(model)
    asr["browser_security"].update(
        {
            "https_enabled": True,
            "ca_certificate_path": str(ca_cert),
            "certificate_path": str(server_cert),
            "private_key_path": str(server_key),
            "subject_alt_names": ["localhost", "127.0.0.1"],
        }
    )
    (root / "config/config-asr.yaml").write_text(yaml.safe_dump(asr))
    (root / "config/config-overall.yaml").write_text(
        "server: config/config-server.yaml\ninfer: config/config-infer.yaml\nasr: config/config-asr.yaml\n"
    )
    (root / "config/config-server.yaml").write_text(
        "server:\n  host: 127.0.0.1\n  port: 39991\n  workers: 1\nfrontend:\n  dev_port: 39992\n  api_base_url: http://127.0.0.1:39991\nocr:\n  enabled: false\n"
    )
    (root / "config/config-infer.yaml").write_text("{}\n")

    marker = root / "spawned"
    fake_bin = root / "fake-bin"
    fake_bin.mkdir()
    for command in ("python3", "npm"):
        executable = fake_bin / command
        executable.write_text(
            f"#!/bin/bash\nprintf spawned > {marker!s}\nexit 97\n"
        )
        executable.chmod(0o755)
    return root, marker


@pytest.mark.parametrize("launcher_name", ["3_start_backend.sh", "4_start_frontend.sh"])
def test_https_launcher_rejects_san_mismatch_before_server_spawn(
    tmp_path: Path, launcher_name: str
):
    root, marker = _isolated_https_launcher_root(tmp_path, launcher_name)
    env = os.environ.copy()
    env["PATH"] = f"{root / 'fake-bin'}:{env['PATH']}"

    result = subprocess.run(
        ["bash", str(root / "scripts" / launcher_name)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "typed/TLS config validation failed" in result.stdout
    assert "unexpected SANs" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("received_signal", [signal.SIGINT, signal.SIGHUP])
def test_launcher_uses_term_cleanup_for_signals_and_waits_for_child(
    tmp_path: Path, received_signal: signal.Signals
):
    process, output, signal_file = _start_launcher(tmp_path, mode="ready")
    _wait_for_output(process, output, "ASR SGLang is ready")

    process.send_signal(received_signal)

    assert process.wait(timeout=5) != 0
    assert signal_file.read_text() == str(signal.SIGTERM)
