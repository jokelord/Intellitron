from __future__ import annotations

import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT_ROOT / "scripts/2_start_OCR_LM_with_SGLang_backend.sh"
WORKSPACE_HELPERS = PROJECT_ROOT / "scripts/_workspace_paths.sh"


def _unused_ports(count: int = 2) -> list[int]:
    reservations: list[socket.socket] = []
    try:
        while len(reservations) < count:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            if sock.getsockname()[1] == 30001:
                sock.close()
                continue
            reservations.append(sock)
        return [int(sock.getsockname()[1]) for sock in reservations]
    finally:
        for sock in reservations:
            sock.close()


def _wait_for_lines(path: Path, values: set[str], timeout: float = 5) -> list[str]:
    deadline = time.monotonic() + timeout
    lines: list[str] = []
    while time.monotonic() < deadline:
        if path.exists():
            lines = path.read_text().splitlines()
            if values.issubset(set(lines)):
                return lines
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {values}; observed {lines}")


def _port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _write_fake_workspace(tmp_path: Path) -> tuple[Path, dict[str, str], int, int]:
    project = tmp_path / "project"
    scripts = project / "scripts"
    config = project / "config"
    patch_dir = project / "patch"
    sglang_bin = project / ".sglang/bin"
    paddle_bin = project / ".paddle-ocr/bin"
    for directory in (scripts, config, patch_dir, sglang_bin, paddle_bin):
        directory.mkdir(parents=True, exist_ok=True)

    ptxas_wrapper = patch_dir / "ptxas_wrapper.sh"
    ptxas_wrapper.write_text("#!/bin/bash\nexit 0\n")
    ptxas_wrapper.chmod(0o755)

    paddlex_port, sglang_port = _unused_ports()
    launcher = scripts / LAUNCHER.name
    launcher.write_text(
        LAUNCHER.read_text()
        .replace("8118", str(paddlex_port))
        .replace("30002", str(sglang_port))
    )
    launcher.chmod(0o755)
    (scripts / "_workspace_paths.sh").write_text(WORKSPACE_HELPERS.read_text())
    (scripts / "paddle_ocr_service.py").write_text("# fake wrapper marker\n")
    model_dir = project / "models/PaddleOCR-VL-1.6"
    model_dir.mkdir(parents=True)
    chat_template = model_dir / "chat_template.jinja"
    chat_template.write_text("{{ messages }}")
    pipeline = config / "paddleocr-vl-thor-sglang.yaml"
    pipeline.write_text(
        f"""pipeline_name: PaddleOCR-VL-1.6
SubModules:
  VLRecognition:
    batch_size: 8
    genai_config:
      backend: sglang-server
      server_url: http://127.0.0.1:{sglang_port}/v1
      max_concurrency: 4
LLMDeploymentSGLangBackend:
  host: 127.0.0.1
  port: {sglang_port}
  model_dir: {model_dir}
  served_model_name: PaddleOCR-VL-1.6-0.9B
  chat_template: {chat_template}
  trust_remote_code: true
  context_length: 16384
  max_running_requests: 4
  max_prefill_tokens: 8192
  mem_fraction_static: 0.10
  enable_metrics: true
  log_requests: true
  log_requests_level: 0
  readiness_timeout_seconds: 5
  shutdown_timeout_seconds: 1
"""
    )
    (config / "paddleocr-vl-thor-model-manifest.json").write_text("{}")
    (config / "config-overall.yaml").write_text(
        "server: config/config-server.yaml\nmisc: config/config-misc.yaml\n"
    )
    (config / "config-server.yaml").write_text("ocr:\n  cuda_memory_limit_gb: 16\n")
    (config / "config-misc.yaml").write_text(
        "read_file:\n  main_llm_memory_reserve_gb: 16\n"
    )

    event_log = tmp_path / "events.log"
    component_program = scripts / "fake_component.py"
    component_program.write_text(
        """import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

args = sys.argv[1:]
if args and args[0] == "--fake-descendant":
    component = args[1]
    port = int(args[2])
    events = Path(os.environ["FAKE_EVENT_LOG"])
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen()
    with events.open("a") as stream:
        stream.write(f"start:{component}-descendant:{os.getpid()}\\n")
    running = True
    def stop_descendant(signum, _frame):
        global running
        with events.open("a") as stream:
            stream.write(f"signal:{component}-descendant:{signal.Signals(signum).name}\\n")
        if os.environ.get(f"FAKE_{component.upper()}_DESCENDANT_IGNORE_TERM") != "1":
            running = False
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop_descendant)
    deadline = time.monotonic() + float(
        os.environ.get(f"FAKE_{component.upper()}_DESCENDANT_EXIT_AFTER", "2")
    )
    while running and time.monotonic() < deadline:
        time.sleep(0.02)
    listener.close()
    with events.open("a") as stream:
        stream.write(f"stop:{component}-descendant\\n")
    raise SystemExit(0)

component = "sglang" if "sglang.launch_server" in args else "paddlex"
port = int(args[args.index("--port") + 1])
events = Path(os.environ["FAKE_EVENT_LOG"])
if component == "sglang" and os.environ.get("FAKE_REQUIRE_THOR_TRITON_ENV") == "1":
    expected_wrapper = os.environ["FAKE_EXPECTED_PTXAS_WRAPPER"]
    observed = {
        "TRITON_PTXAS_PATH": os.environ.get("TRITON_PTXAS_PATH"),
        "TRITON_PTXAS_BLACKWELL_PATH": os.environ.get("TRITON_PTXAS_BLACKWELL_PATH"),
        "TORCH_CUDA_ARCH_LIST": os.environ.get("TORCH_CUDA_ARCH_LIST"),
    }
    if observed != {
        "TRITON_PTXAS_PATH": expected_wrapper,
        "TRITON_PTXAS_BLACKWELL_PATH": expected_wrapper,
        "TORCH_CUDA_ARCH_LIST": "11.0a",
    }:
        with events.open("a") as stream:
            stream.write(f"triton-env:{observed!r}\\n")
        raise SystemExit(89)
if component == "paddlex" and os.environ.get("FAKE_REQUIRE_PROXY_ENV_CLEARED") == "1":
    active_proxy_names = [
        name
        for name in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy")
        if os.environ.get(name)
    ]
    if active_proxy_names:
        with events.open("a") as stream:
            stream.write(f"proxy-env:{','.join(active_proxy_names)}\\n")
        raise SystemExit(88)
with events.open("a") as stream:
    stream.write(f"start:{component}\\n")
if os.environ.get(f"FAKE_{component.upper()}_EXIT") == "1":
    raise SystemExit(23)
time.sleep(float(os.environ.get(f"FAKE_{component.upper()}_LISTEN_DELAY", "0")))
if os.environ.get(f"FAKE_{component.upper()}_SPAWN_DESCENDANT") == "1":
    subprocess.Popen(
        [sys.executable, __file__, "--fake-descendant", component, str(port)]
    )
    time.sleep(float(os.environ.get(f"FAKE_{component.upper()}_LEADER_EXIT_DELAY", "0.1")))
    if os.environ.get(f"FAKE_{component.upper()}_LEADER_EXIT_AFTER_SPAWN") == "1":
        with events.open("a") as stream:
            stream.write(f"leader-exit:{component}\\n")
        raise SystemExit(0)
    listener = None
else:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen()
print(f"{component} ready on {port}", flush=True)
running = True
def stop(signum, _frame):
    global running
    with events.open("a") as stream:
        stream.write(f"signal:{component}:{signal.Signals(signum).name}\\n")
    if os.environ.get(f"FAKE_{component.upper()}_IGNORE_TERM") != "1":
        running = False
for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, stop)
exit_after = float(os.environ.get(f"FAKE_{component.upper()}_EXIT_AFTER", "0"))
deadline = time.monotonic() + exit_after if exit_after else None
while running and (deadline is None or time.monotonic() < deadline):
    time.sleep(0.02)
if listener is not None:
    listener.close()
with events.open("a") as stream:
    stream.write(f"stop:{component}\\n")
"""
    )

    fake_python = scripts / "fake_python.sh"
    fake_python.write_text(
        """#!/bin/bash
set -euo pipefail
if [ "${1:-}" = "-" ] || [[ "${1:-}" == *paddle_ocr_preflight.py ]]; then
    exec "$REAL_PYTHON" "$@"
fi
exec "$REAL_PYTHON" "$FAKE_COMPONENT_PROGRAM" "$@"
"""
    )
    fake_python.chmod(0o755)
    for destination in (sglang_bin / "python", paddle_bin / "python"):
        destination.symlink_to(fake_python)

    preflight = scripts / "paddle_ocr_preflight.py"
    preflight.write_text(
        """import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

command = sys.argv[1]
with Path(os.environ["FAKE_EVENT_LOG"]).open("a") as stream:
    stream.write(f"preflight:{command}\\n")
required = {
    "check-sglang-env": {"--python", "--config"},
    "check-memory-reserve": {"--minimum-gb"},
    "probe-sglang": {"--url", "--served-model-name", "--timeout"},
    "probe": {"--url", "--timeout"},
    "canary-sglang": {
        "--paddlex-url", "--sglang-url", "--served-model-name", "--attestation",
        "--pipeline-pid", "--sglang-pid", "--config", "--memory-report",
        "--cuda-memory-limit-gb", "--sglang-log", "--sglang-log-offset", "--timeout",
        "--sglang-python", "--main-llm-memory-reserve-gb",
    },
}[command]
if not required.issubset(set(sys.argv[2:])):
    raise SystemExit(97)
if {"--manifest", "--service-pid", "--sglang-service-pid"} & set(sys.argv[2:]):
    raise SystemExit(98)
if command == "check-sglang-env":
    raise SystemExit(int(os.environ.get("FAKE_DEPENDENCY_STATUS", "0")))
if command == "check-memory-reserve":
    raise SystemExit(int(os.environ.get("FAKE_MEMORY_STATUS", "0")))
if command == "canary-sglang":
    time.sleep(float(os.environ.get("FAKE_CANARY_DELAY", "0")))
    raise SystemExit(int(os.environ.get("FAKE_CANARY_STATUS", "0")))
url = sys.argv[sys.argv.index("--url") + 1]
parsed = urlparse(url)
with socket.socket() as sock:
    if sock.connect_ex((parsed.hostname, parsed.port)) == 0:
        raise SystemExit(0)
print("expected transient probe failure", file=sys.stderr)
raise SystemExit(1)
"""
    )

    env = {
        **os.environ,
        "REAL_PYTHON": sys.executable,
        "FAKE_COMPONENT_PROGRAM": str(component_program),
        "FAKE_EVENT_LOG": str(event_log),
        "OCR_PREFLIGHT": str(preflight),
        "OCR_PIPELINE_CONFIG": str(pipeline),
        "OCR_SERVICE_WRAPPER": str(scripts / "paddle_ocr_service.py"),
        "OCR_LOG_DIR": str(tmp_path / "logs"),
        "OCR_ATTESTATION_PATH": str(tmp_path / "attestation.json"),
        "OCR_MEMORY_REPORT": str(tmp_path / "memory.json"),
        "OCR_TOPOLOGY_LOCK": str(tmp_path / "topology.lock"),
        "OCR_CUDA_MEMORY_LIMIT_GB": "16",
        "OCR_READINESS_TIMEOUT": "3",
        "OCR_SHUTDOWN_TIMEOUT": "1",
        "OCR_EXIT_AFTER_CANARY": "1",
    }
    return launcher, env, paddlex_port, sglang_port


def _run_launcher(launcher: Path, env: dict[str, str], timeout: float = 10):
    return subprocess.run(
        ["bash", launcher], env=env, capture_output=True, text=True, timeout=timeout
    )


def test_launcher_static_contract() -> None:
    text = LAUNCHER.read_text()
    assert "get_shared_sglang_python" in text
    assert "get_shared_paddle_ocr_python" in text
    assert "config/paddleocr-vl-thor-sglang.yaml" in text
    assert "LLMDeploymentSGLangBackend" in text
    assert '"$SGLANG_PYTHON" -m sglang.launch_server' in text
    assert "check-sglang-env" in text and '--python "$SGLANG_PYTHON"' in text
    assert "check-memory-reserve" in text
    assert "probe-sglang" in text
    assert "canary-sglang" in text
    assert '--paddlex-url "$PADDLEX_URL"' in text
    assert '--pipeline-pid "$PADDLEX_PID"' in text
    assert '--sglang-pid "$SGLANG_PID"' in text
    assert '--sglang-python "$SGLANG_PYTHON"' in text
    assert '--main-llm-memory-reserve-gb "$MAIN_LLM_MEMORY_RESERVE_GB"' in text
    assert 'LOCAL_NO_PROXY="127.0.0.1,localhost"' in text
    assert 'NO_PROXY="${NO_PROXY:+$NO_PROXY,}$LOCAL_NO_PROXY"' in text
    assert 'no_proxy="${no_proxy:+$no_proxy,}$LOCAL_NO_PROXY"' in text
    assert 'TRITON_PTXAS_PATH="$TRITON_PTXAS_WRAPPER"' in text
    assert 'TRITON_PTXAS_BLACKWELL_PATH="$TRITON_PTXAS_WRAPPER"' in text
    assert 'TORCH_CUDA_ARCH_LIST="$OCR_TORCH_CUDA_ARCH_LIST"' in text
    assert "--log-requests-target" in text
    assert "OCR_EXIT_AFTER_CANARY" in text
    assert "OCR_LAUNCHER_TEST_MODE" not in text
    assert "OCR_TEST_PADDLEX_PORT" not in text
    assert "OCR_TEST_SGLANG_PORT" not in text
    assert "OCR_MODEL_MANIFEST" not in text
    assert "--engine transformers" not in text
    for forbidden in ("uv pip install", "pkill", "killall", "fuser -k", "30001"):
        assert forbidden not in text


def test_exit_after_canary_starts_in_order_logs_and_cleans(tmp_path: Path) -> None:
    launcher, env, paddlex_port, sglang_port = _write_fake_workspace(tmp_path)

    result = _run_launcher(launcher, env)

    assert result.returncode == 0, (result.stdout, result.stderr)
    events = Path(env["FAKE_EVENT_LOG"]).read_text().splitlines()
    assert events.index("preflight:check-sglang-env") < events.index("start:sglang")
    assert events.index("preflight:check-memory-reserve") < events.index("start:sglang")
    assert events.index("start:sglang") < events.index("preflight:probe-sglang")
    assert events.index("preflight:probe-sglang") < events.index("start:paddlex")
    assert events.index("start:paddlex") < events.index("preflight:probe")
    assert events.index("preflight:probe") < events.index("preflight:canary-sglang")
    assert events.index("signal:paddlex:SIGTERM") < events.index("signal:sglang:SIGTERM")
    assert "[OCR-SGLANG] sglang ready" in result.stdout
    assert "[PADDLEX] paddlex ready" in result.stdout
    logs = list((tmp_path / "logs").glob("*"))
    assert any("sglang" in path.name and "ready" in path.read_text() for path in logs)
    assert any("paddlex" in path.name and "ready" in path.read_text() for path in logs)
    supervisor = next(path for path in logs if "supervisor" in path.name).read_text()
    for marker in (
        "SGLang identity",
        "SGLang readiness passed",
        "PaddleX identity",
        "PaddleX readiness passed",
        "remote canary passed",
        "Stopping task-owned paddlex",
        "Stopping task-owned sglang",
    ):
        assert marker in supervisor
    assert _port_is_free(paddlex_port)
    assert _port_is_free(sglang_port)
    assert "30001" not in Path(env["FAKE_EVENT_LOG"]).read_text()


def test_launcher_retries_one_shot_probes_until_delayed_services_are_ready(
    tmp_path: Path,
) -> None:
    launcher, env, paddlex_port, sglang_port = _write_fake_workspace(tmp_path)
    env["FAKE_SGLANG_LISTEN_DELAY"] = "0.25"
    env["FAKE_PADDLEX_LISTEN_DELAY"] = "0.25"

    result = _run_launcher(launcher, env)

    assert result.returncode == 0, (result.stdout, result.stderr)
    events = Path(env["FAKE_EVENT_LOG"]).read_text().splitlines()
    assert events.count("preflight:probe-sglang") > 1
    assert events.count("preflight:probe") > 1
    assert "expected transient probe failure" not in result.stdout + result.stderr
    assert _port_is_free(paddlex_port)
    assert _port_is_free(sglang_port)


def test_paddlex_child_does_not_inherit_proxy_transport_environment(
    tmp_path: Path,
) -> None:
    launcher, env, paddlex_port, sglang_port = _write_fake_workspace(tmp_path)
    env.update(
        {
            "ALL_PROXY": "socks5h://127.0.0.1:1",
            "HTTP_PROXY": "http://127.0.0.1:2",
            "HTTPS_PROXY": "http://127.0.0.1:3",
            "FAKE_REQUIRE_PROXY_ENV_CLEARED": "1",
        }
    )

    result = _run_launcher(launcher, env)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "proxy-env:" not in Path(env["FAKE_EVENT_LOG"]).read_text()
    assert _port_is_free(paddlex_port)
    assert _port_is_free(sglang_port)


def test_sglang_child_uses_thor_blackwell_ptxas_wrapper_environment(
    tmp_path: Path,
) -> None:
    launcher, env, paddlex_port, sglang_port = _write_fake_workspace(tmp_path)
    expected_wrapper = str(launcher.parent.parent / "patch/ptxas_wrapper.sh")
    env.update(
        {
            "TRITON_PTXAS_PATH": "/wrong/generic-ptxas",
            "TRITON_PTXAS_BLACKWELL_PATH": "/wrong/blackwell-ptxas",
            "TORCH_CUDA_ARCH_LIST": "wrong",
            "FAKE_REQUIRE_THOR_TRITON_ENV": "1",
            "FAKE_EXPECTED_PTXAS_WRAPPER": expected_wrapper,
        }
    )

    result = _run_launcher(launcher, env)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "triton-env:" not in Path(env["FAKE_EVENT_LOG"]).read_text()
    assert _port_is_free(paddlex_port)
    assert _port_is_free(sglang_port)


@pytest.mark.parametrize("occupied_component", ["paddlex", "sglang"])
def test_existing_listener_fails_closed_without_start_or_signal(
    tmp_path: Path, occupied_component: str
) -> None:
    launcher, env, paddlex_port, sglang_port = _write_fake_workspace(tmp_path)
    port = paddlex_port if occupied_component == "paddlex" else sglang_port
    listener = socket.socket()
    listener.bind(("127.0.0.1", port))
    listener.listen()
    try:
        result = _run_launcher(launcher, env)
        assert result.returncode != 0
        assert listener.getsockname()[1] == port
        events = Path(env["FAKE_EVENT_LOG"])
        assert not events.exists() or "start:" not in events.read_text()
    finally:
        listener.close()


def test_missing_dependency_fails_before_children_start(tmp_path: Path) -> None:
    launcher, env, _, _ = _write_fake_workspace(tmp_path)
    env["FAKE_DEPENDENCY_STATUS"] = "1"

    result = _run_launcher(launcher, env)

    assert result.returncode != 0
    assert Path(env["FAKE_EVENT_LOG"]).read_text().splitlines() == [
        "preflight:check-sglang-env"
    ]


def test_invalid_config_fails_cleanly_before_preflight_or_children(tmp_path: Path) -> None:
    launcher, env, _, _ = _write_fake_workspace(tmp_path)
    config = Path(env["OCR_PIPELINE_CONFIG"])
    config.write_text(config.read_text().replace("trust_remote_code: true", "trust_remote_code: maybe"))

    result = _run_launcher(launcher, env)

    assert result.returncode != 0
    assert "trust_remote_code must be a boolean" in result.stderr
    assert "unbound variable" not in result.stderr
    assert not Path(env["FAKE_EVENT_LOG"]).exists()


def test_inconsistent_concurrency_fails_without_unbound_variable(tmp_path: Path) -> None:
    launcher, env, _, _ = _write_fake_workspace(tmp_path)
    env.pop("OCR_CUDA_MEMORY_LIMIT_GB")
    config = Path(env["OCR_PIPELINE_CONFIG"])
    config.write_text(config.read_text().replace("max_concurrency: 4", "max_concurrency: 3"))

    result = _run_launcher(launcher, env)

    assert result.returncode != 0
    assert "VLRecognition concurrency is inconsistent" in result.stderr
    assert "unbound variable" not in result.stderr
    assert "未绑定的变量" not in result.stderr
    assert "CONFIG_OCR_CUDA_MEMORY_LIMIT_GB" not in result.stderr
    assert not Path(env["FAKE_EVENT_LOG"]).exists()


@pytest.mark.parametrize(
    ("failure_env", "expected_starts", "expected_status"),
    [
        ("FAKE_SGLANG_EXIT", ["start:sglang"], 23),
        ("FAKE_PADDLEX_EXIT", ["start:sglang", "start:paddlex"], 23),
        ("FAKE_CANARY_STATUS", ["start:sglang", "start:paddlex"], 1),
    ],
)
def test_startup_failures_clean_only_started_children(
    tmp_path: Path,
    failure_env: str,
    expected_starts: list[str],
    expected_status: int,
) -> None:
    launcher, env, paddlex_port, sglang_port = _write_fake_workspace(tmp_path)
    env[failure_env] = "1"

    result = _run_launcher(launcher, env)

    assert result.returncode == expected_status, (result.stdout, result.stderr)
    events = Path(env["FAKE_EVENT_LOG"]).read_text().splitlines()
    assert [line for line in events if line.startswith("start:")] == expected_starts
    if "start:paddlex" in events and "signal:paddlex:SIGTERM" in events:
        assert events.index("signal:paddlex:SIGTERM") < events.index("signal:sglang:SIGTERM")
    assert _port_is_free(paddlex_port)
    assert _port_is_free(sglang_port)


@pytest.mark.parametrize("launcher_signal", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_signals_stop_paddlex_before_sglang(
    tmp_path: Path, launcher_signal: signal.Signals
) -> None:
    launcher, env, paddlex_port, sglang_port = _write_fake_workspace(tmp_path)
    env["OCR_EXIT_AFTER_CANARY"] = "0"
    process = subprocess.Popen(
        ["bash", launcher], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        _wait_for_lines(Path(env["FAKE_EVENT_LOG"]), {"preflight:canary-sglang"})
        process.send_signal(launcher_signal)
        process.communicate(timeout=8)
        assert process.returncode == {
            signal.SIGINT: 130,
            signal.SIGTERM: 143,
            signal.SIGHUP: 129,
        }[launcher_signal]
        events = Path(env["FAKE_EVENT_LOG"]).read_text().splitlines()
        assert events.index("signal:paddlex:SIGTERM") < events.index("signal:sglang:SIGTERM")
        assert _port_is_free(paddlex_port)
        assert _port_is_free(sglang_port)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_child_exit_stops_sibling_and_returns_nonzero(tmp_path: Path) -> None:
    launcher, env, paddlex_port, sglang_port = _write_fake_workspace(tmp_path)
    env["OCR_EXIT_AFTER_CANARY"] = "0"
    env["FAKE_SGLANG_EXIT_AFTER"] = "0.3"

    result = _run_launcher(launcher, env)

    assert result.returncode != 0
    supervisor = next((tmp_path / "logs").glob("*supervisor*")).read_text()
    assert "exited unexpectedly with status 0" in supervisor
    events = Path(env["FAKE_EVENT_LOG"]).read_text().splitlines()
    assert "signal:paddlex:SIGTERM" in events
    assert _port_is_free(paddlex_port)
    assert _port_is_free(sglang_port)


def test_unresponsive_owned_child_receives_scoped_kill(tmp_path: Path) -> None:
    launcher, env, _, sglang_port = _write_fake_workspace(tmp_path)
    env["FAKE_SGLANG_IGNORE_TERM"] = "1"

    result = _run_launcher(launcher, env)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "KILL" in result.stdout + result.stderr
    assert _port_is_free(sglang_port)


def test_cleanup_retains_group_identity_after_leader_exits_before_cleanup(
    tmp_path: Path,
) -> None:
    launcher, env, _, sglang_port = _write_fake_workspace(tmp_path)
    env["FAKE_SGLANG_SPAWN_DESCENDANT"] = "1"
    env["FAKE_SGLANG_LEADER_EXIT_AFTER_SPAWN"] = "1"
    env["FAKE_SGLANG_DESCENDANT_EXIT_AFTER"] = "2"
    env["FAKE_CANARY_DELAY"] = "0.3"

    result = _run_launcher(launcher, env)

    assert result.returncode == 0, (result.stdout, result.stderr)
    events = Path(env["FAKE_EVENT_LOG"]).read_text().splitlines()
    assert "leader-exit:sglang" in events
    assert "signal:sglang-descendant:SIGTERM" in events
    assert _port_is_free(sglang_port)


def test_early_leader_exit_before_identity_capture_still_cleans_descendant(
    tmp_path: Path,
) -> None:
    launcher, env, _, sglang_port = _write_fake_workspace(tmp_path)
    env["FAKE_SGLANG_SPAWN_DESCENDANT"] = "1"
    env["FAKE_SGLANG_LEADER_EXIT_AFTER_SPAWN"] = "1"
    env["FAKE_SGLANG_LEADER_EXIT_DELAY"] = "0"
    env["FAKE_SGLANG_DESCENDANT_EXIT_AFTER"] = "2"

    result = _run_launcher(launcher, env)

    assert result.returncode != 0
    events = Path(env["FAKE_EVENT_LOG"]).read_text().splitlines()
    assert "leader-exit:sglang" in events
    assert "signal:sglang-descendant:SIGTERM" in events
    assert _port_is_free(sglang_port)


def test_cleanup_kills_descendant_when_leader_exits_after_group_term(
    tmp_path: Path,
) -> None:
    launcher, env, _, sglang_port = _write_fake_workspace(tmp_path)
    env["FAKE_SGLANG_SPAWN_DESCENDANT"] = "1"
    env["FAKE_SGLANG_DESCENDANT_IGNORE_TERM"] = "1"
    env["FAKE_SGLANG_DESCENDANT_EXIT_AFTER"] = "3"

    result = _run_launcher(launcher, env)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "sending scoped KILL" in result.stdout
    events = Path(env["FAKE_EVENT_LOG"]).read_text().splitlines()
    assert "signal:sglang:SIGTERM" in events
    assert "signal:sglang-descendant:SIGTERM" in events
    assert _port_is_free(sglang_port)


def test_identity_mismatch_refuses_to_signal_replacement_process(tmp_path: Path) -> None:
    replacement = subprocess.Popen(["setsid", "sleep", "30"])
    try:
        command = f"""
set -euo pipefail
source {shlex.quote(str(LAUNCHER))}
SGLANG_PID={replacement.pid}
SGLANG_PGID={replacement.pid}
SGLANG_BOOT_ID=wrong
SGLANG_START_TICKS=0
SGLANG_EXE=/wrong/executable
SGLANG_CMD_HASH=wrong
OCR_SHUTDOWN_TIMEOUT=1
OCR_PYTHON={shlex.quote(sys.executable)}
if stop_component sglang 12345; then
    exit 42
fi
kill -0 {replacement.pid}
"""
        result = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True, timeout=2
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
    finally:
        replacement.terminate()
        replacement.wait(timeout=5)


def test_absent_leader_fallback_refuses_unobserved_replacement_group(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    leader_program = tmp_path / "leader.py"
    leader_program.write_text(
        """import subprocess
import sys
from pathlib import Path

child = subprocess.Popen(["sleep", "30"])
Path(sys.argv[1]).write_text(str(child.pid))
"""
    )
    leader = subprocess.Popen(
        ["setsid", sys.executable, str(leader_program), str(child_pid_path)]
    )
    leader.wait(timeout=5)
    child_pid = int(child_pid_path.read_text())
    try:
        command = f"""
set -euo pipefail
source {shlex.quote(str(LAUNCHER))}
SGLANG_PID={leader.pid}
SGLANG_PGID={leader.pid}
SGLANG_SID={leader.pid}
SGLANG_GROUP_GONE=0
SGLANG_MEMBER_IDENTITIES=""
if signal_verified_component sglang TERM; then
    exit 42
fi
kill -0 {child_pid}
"""
        result = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True, timeout=2
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        os.kill(child_pid, 0)
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_live_leader_identity_capture_failure_uses_observed_member_for_cleanup(
    tmp_path: Path,
) -> None:
    process = subprocess.Popen(["setsid", "sleep", "30"])
    try:
        command = f"""
set -euo pipefail
source {shlex.quote(str(LAUNCHER))}
record_component_identity() {{ return 1; }}
if record_or_report_early_exit sglang {process.pid}; then
    exit 41
fi
OCR_SHUTDOWN_TIMEOUT=1
OCR_PYTHON={shlex.quote(sys.executable)}
stop_component sglang 12345
"""
        result = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True, timeout=4
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert process.wait(timeout=2) == -signal.SIGTERM
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OCR_READINESS_TIMEOUT", "not-a-number"),
        ("OCR_SHUTDOWN_TIMEOUT", "0"),
        ("OCR_START_LOCK_TIMEOUT", "-1"),
        ("OCR_PROBE_TIMEOUT", "1.5"),
    ],
)
def test_invalid_effective_timeout_fails_before_children_start(
    tmp_path: Path, name: str, value: str
) -> None:
    launcher, env, _, _ = _write_fake_workspace(tmp_path)
    env[name] = value

    result = _run_launcher(launcher, env)

    assert result.returncode != 0
    events = Path(env["FAKE_EVENT_LOG"])
    assert not events.exists() or "start:" not in events.read_text()
