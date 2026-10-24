from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest
import httpx
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config/paddleocr-vl-thor-sglang.yaml"
LOCAL_CONFIG_PATH = PROJECT_ROOT / "config/paddleocr-vl-thor.yaml"
PREFLIGHT_PATH = PROJECT_ROOT / "scripts/paddle_ocr_preflight.py"
REQUIRED_DISTRIBUTIONS = (
    "sglang",
    "sglang-kernel",
    "torch",
    "transformers",
    "triton",
    "flashinfer-python",
    "flashinfer-cubin",
)


def _load_preflight():
    spec = importlib.util.spec_from_file_location("paddle_ocr_preflight_sglang", PREFLIGHT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_config() -> dict[str, object]:
    value = yaml.safe_load(CONFIG_PATH.read_text())
    assert isinstance(value, dict)
    return value


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


class _SGLangHandler(_QuietHandler):
    model_name = "PaddleOCR-VL-1.6-0.9B"
    request_count = 0
    log_path: Path
    log_mode = "metadata"
    payload_to_log = ""

    def do_GET(self) -> None:
        if self.path == "/health":
            body = b"ok"
            content_type = "text/plain"
        elif self.path == "/v1/models":
            body = json.dumps({"data": [{"id": type(self).model_name}]}).encode()
            content_type = "application/json"
        elif self.path == "/metrics":
            body = (
                'sglang:http_requests_total{method="POST",endpoint="/v1/chat/completions"} '
                f"{type(self).request_count}.0\n"
            ).encode()
            content_type = "text/plain"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).request_count += 1
        if type(self).log_mode == "metadata":
            appended = "Receive: rid=canary\nFinish: rid=canary\n"
        elif type(self).log_mode == "delayed":
            appended = "Receive: rid=canary\n"
        elif type(self).log_mode == "mismatched":
            appended = "Receive: rid=one\nFinish: rid=two\n"
        elif type(self).log_mode == "whitespace":
            appended = "\n"
        elif type(self).log_mode == "payload":
            appended = type(self).payload_to_log
        else:
            appended = ""
        if appended:
            with type(self).log_path.open("a", encoding="utf-8") as stream:
                stream.write(appended)
                stream.flush()
        if type(self).log_mode == "delayed":
            def append_finish() -> None:
                time.sleep(0.1)
                with type(self).log_path.open("a", encoding="utf-8") as stream:
                    stream.write("Finish: rid=canary\n")
                    stream.flush()

            threading.Thread(target=append_finish, daemon=True).start()
        body = json.dumps(
            {
                "id": "canary",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _PaddleXCanaryHandler(_QuietHandler):
    requests: list[dict[str, object]] = []
    sglang_url: str

    def do_GET(self) -> None:
        if self.path != "/openapi.json":
            self.send_error(404)
            return
        body = json.dumps({"paths": {"/layout-parsing": {"post": {}}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/layout-parsing":
            self.send_error(404)
            return
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(payload)
        _SGLangHandler.payload_to_log = str(payload["file"])
        with httpx.Client(trust_env=False, timeout=2) as client:
            response = client.post(
                f"{type(self).sglang_url}/v1/chat/completions",
                json={"model": _SGLangHandler.model_name, "messages": []},
            )
            response.raise_for_status()
        body = json.dumps(
            {
                "errorCode": 0,
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {"text": "THOR SGLANG CANARY 2026"},
                            "prunedResult": {
                                "model_settings": {"use_doc_preprocessor": True},
                                "doc_preprocessor_res": {
                                    "model_settings": {
                                        "use_doc_orientation_classify": True,
                                        "use_doc_unwarping": False,
                                    },
                                    "angle": 0,
                                },
                            },
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


def test_unified_remote_config_has_approved_values_and_relationships() -> None:
    cfg = _load_config()
    assert cfg["pipeline_name"] == "PaddleOCR-VL-1.6"
    assert cfg["batch_size"] == 8
    assert cfg["use_queues"] is True
    submodules = cfg["SubModules"]
    assert submodules["LayoutDetection"]["batch_size"] == 4
    vlm = submodules["VLRecognition"]
    assert "engine" not in vlm
    assert "engine_config" not in vlm
    assert vlm["batch_size"] == 8
    assert vlm["genai_config"] == {
        "backend": "sglang-server",
        "server_url": "http://127.0.0.1:30002/v1",
        "max_concurrency": 8,
    }
    backend = cfg["LLMDeploymentSGLangBackend"]
    assert backend["port"] == 30002
    assert backend["served_model_name"] == "PaddleOCR-VL-1.6-0.9B"
    assert backend["max_running_requests"] == 8
    assert backend["max_prefill_tokens"] == 16384
    assert backend["context_length"] == 32768
    assert backend["mem_fraction_static"] == 0.10
    assert backend["log_requests"] is True
    assert backend["log_requests_level"] == 0
    assert vlm["batch_size"] >= vlm["genai_config"]["max_concurrency"]
    assert vlm["genai_config"]["max_concurrency"] == backend["max_running_requests"]


def test_unified_remote_config_retains_local_pipeline_semantics() -> None:
    local = yaml.safe_load(LOCAL_CONFIG_PATH.read_text())
    remote = _load_config()
    for key in (
        "use_doc_preprocessor",
        "use_layout_detection",
        "use_chart_recognition",
        "use_seal_recognition",
        "use_ocr_for_image_block",
        "format_block_content",
        "merge_layout_blocks",
        "markdown_ignore_labels",
        "Serving",
    ):
        assert remote[key] == local[key]
    for module in ("LayoutDetection",):
        expected = copy.deepcopy(local["SubModules"][module])
        actual = copy.deepcopy(remote["SubModules"][module])
        expected.pop("batch_size")
        actual.pop("batch_size")
        assert actual == expected
    for module in ("DocOrientationClassify", "DocUnwarping"):
        expected = copy.deepcopy(local["SubPipelines"]["DocPreprocessor"]["SubModules"][module])
        actual = copy.deepcopy(remote["SubPipelines"]["DocPreprocessor"]["SubModules"][module])
        expected.pop("batch_size", None)
        actual.pop("batch_size", None)
        assert actual == expected


def test_yaml_contains_required_chinese_batch_and_backend_explanations() -> None:
    raw = CONFIG_PATH.read_text()
    local_raw = LOCAL_CONFIG_PATH.read_text()
    for phrase in (
        "batch_size 按文档页分组",
        "SubModules 是父 Pipeline",
        "SubPipelines 是有自己处理流程",
        "队列",
        "同时在途",
        "同时运行",
        "prefill 调度 token 预算",
        "静态池初始比例",
    ):
        assert phrase in raw
    for phrase in ("batch_size", "SubModules", "SubPipelines", "队列"):
        assert phrase in local_raw


def test_custom_section_is_ignored_by_paddlex_app_config() -> None:
    program = """
import json
import sys
from paddlex.inference.pipelines import load_pipeline_config
from paddlex.inference.serving.infra.config import create_app_config

loaded = load_pipeline_config(sys.argv[1])
app_config = create_app_config(loaded)
print(json.dumps({
    "customSectionLoaded": "LLMDeploymentSGLangBackend" in loaded,
    "appConfig": app_config.model_dump(),
}))
"""
    completed = subprocess.run(
        [str(PROJECT_ROOT / ".paddle-ocr/bin/python"), "-c", program, str(CONFIG_PATH)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )
    result = json.loads(completed.stdout)
    assert result["customSectionLoaded"] is True
    assert "LLMDeploymentSGLangBackend" not in result["appConfig"]
    assert result["appConfig"]["visualize"] is False
    assert result["appConfig"]["extra"] == {"max_num_input_imgs": 32}


def _write_mutated_config(tmp_path: Path, mutation) -> Path:
    cfg = _load_config()
    mutation(cfg)
    path = tmp_path / "pipeline.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return path


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda cfg: cfg["LLMDeploymentSGLangBackend"].update(host="0.0.0.0"), "loopback"),
        (lambda cfg: cfg["LLMDeploymentSGLangBackend"].update(port=30003), "30002"),
        (
            lambda cfg: cfg["SubModules"]["VLRecognition"]["genai_config"].update(
                server_url="http://127.0.0.1:30003/v1"
            ),
            "server_url",
        ),
        (
            lambda cfg: cfg["LLMDeploymentSGLangBackend"].update(served_model_name="wrong"),
            "served model",
        ),
        (
            lambda cfg: cfg["LLMDeploymentSGLangBackend"].update(model_dir="/tmp/wrong"),
            "model directory",
        ),
        (
            lambda cfg: cfg["LLMDeploymentSGLangBackend"].update(chat_template="/tmp/chat.jinja"),
            "chat template",
        ),
        (
            lambda cfg: cfg["SubModules"]["VLRecognition"]["genai_config"].update(
                max_concurrency=0
            ),
            "max_concurrency",
        ),
        (
            lambda cfg: cfg["LLMDeploymentSGLangBackend"].update(max_running_requests=0),
            "max_running_requests",
        ),
        (lambda cfg: cfg["SubModules"]["VLRecognition"].update(batch_size=3), "batch_size"),
        (
            lambda cfg: cfg["LLMDeploymentSGLangBackend"].update(max_running_requests=3),
            "must equal",
        ),
        (
            lambda cfg: cfg["LLMDeploymentSGLangBackend"].update(max_prefill_tokens=0),
            "max_prefill_tokens",
        ),
        (
            lambda cfg: cfg["LLMDeploymentSGLangBackend"].update(context_length=0),
            "context_length",
        ),
        (
            lambda cfg: cfg["LLMDeploymentSGLangBackend"].update(mem_fraction_static=1),
            "mem_fraction_static",
        ),
        (
            lambda cfg: cfg["LLMDeploymentSGLangBackend"].update(log_requests_level=1),
            "log_requests_level",
        ),
        (
            lambda cfg: cfg["SubModules"]["VLRecognition"].update(engine="transformers"),
            "local engine",
        ),
        (
            lambda cfg: cfg["SubModules"]["VLRecognition"].update(engine_config={}),
            "engine_config",
        ),
    ],
)
def test_remote_config_validation_rejects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation, message: str
) -> None:
    preflight = _load_preflight()
    monkeypatch.setattr(
        preflight,
        "_validate_model_manifest",
        lambda modules, path: ({name: {} for name in modules}, "manifest-sha"),
    )
    path = _write_mutated_config(tmp_path, mutation)
    with pytest.raises(preflight.PreflightError, match=message):
        preflight.validate_sglang_pipeline_config(path)


def test_remote_config_validation_returns_mode_and_scheduler_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = _load_preflight()
    monkeypatch.setattr(
        preflight,
        "_validate_model_manifest",
        lambda modules, path: ({name: {} for name in modules}, "manifest-sha"),
    )
    result = preflight.validate_sglang_pipeline_config(CONFIG_PATH)
    assert result["mode"] == "remote_sglang"
    assert result["vlmBackend"] == "sglang-server"
    assert result["sglangBackend"] == {
        "host": "127.0.0.1",
        "port": 30002,
        "url": "http://127.0.0.1:30002",
        "servedModelName": "PaddleOCR-VL-1.6-0.9B",
        "modelDir": "~/.paddlex/official_models/PaddleOCR-VL-1.6",
        "chatTemplate": "~/.paddlex/official_models/PaddleOCR-VL-1.6/chat_template.jinja",
        "contextLength": 32768,
        "maxRunningRequests": 8,
        "maxPrefillTokens": 16384,
        "effectiveMaxPrefillTokens": 32768,
        "memFractionStatic": 0.10,
        "enableMetrics": True,
        "logRequests": True,
        "logRequestsLevel": 0,
        "readinessTimeoutSeconds": 900,
        "shutdownTimeoutSeconds": 30,
    }
    assert result["batchAndConcurrencySettings"] == {
        "pipelineBatchSize": 8,
        "layoutBatchSize": 4,
        "vlmBatchSize": 8,
        "vlmMaxConcurrency": 8,
        "docPreprocessorBatchSize": 4,
        "orientationBatchSize": 4,
        "unwarpingBatchSize": 1,
        "maxRunningRequests": 8,
    }


def test_sglang_dependency_check_gates_only_on_exact_name_presence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = _load_preflight()
    versions = {name: f"unexpected-{index}" for index, name in enumerate(REQUIRED_DISTRIBUTIONS)}

    def fake_run(command, **kwargs):
        assert command[0] == "/workspace/.sglang/bin/python"
        assert command[1] == "-c"
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(command, 0, json.dumps(versions), "")

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    result = preflight.check_sglang_dependency_names(
        Path("/workspace/.sglang/bin/python")
    )
    assert tuple(result["requiredDistributions"]) == REQUIRED_DISTRIBUTIONS
    assert result["versions"] == versions


def test_sglang_dependency_check_rejects_each_missing_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = _load_preflight()
    for missing in REQUIRED_DISTRIBUTIONS:
        versions = {name: "1" for name in REQUIRED_DISTRIBUTIONS}
        versions[missing] = None
        monkeypatch.setattr(
            preflight.subprocess,
            "run",
            lambda command, **kwargs: subprocess.CompletedProcess(
                command, 0, json.dumps(versions), ""
            ),
        )
        with pytest.raises(preflight.PreflightError, match=missing):
            preflight.check_sglang_dependency_names(Path("/safe/python"))


def test_probe_sglang_requires_health_and_exact_model() -> None:
    preflight = _load_preflight()
    _SGLangHandler.model_name = "PaddleOCR-VL-1.6-0.9B"
    with _serve(_SGLangHandler) as url:
        result = preflight.probe_sglang_service(url, _SGLangHandler.model_name)
        assert result == {
            "url": url,
            "servedModelName": _SGLangHandler.model_name,
            "modelReady": True,
        }
        with pytest.raises(preflight.PreflightError, match="served model"):
            preflight.probe_sglang_service(url, "wrong-model")


def test_sglang_metrics_counter_reads_exact_chat_completion_endpoint() -> None:
    preflight = _load_preflight()
    _SGLangHandler.request_count = 3
    with _serve(_SGLangHandler) as url:
        assert preflight._read_sglang_chat_completion_count(url, 2) == 3.0


def _patch_canary_host_dependencies(preflight, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight,
        "validate_sglang_pipeline_config",
        lambda path: {
            "configSha256": "config-sha",
            "manifestSha256": "manifest-sha",
            "batchAndConcurrencySettings": {"maxRunningRequests": 4},
        },
    )
    monkeypatch.setattr(
        preflight,
        "collect_environment_attestation",
        lambda: {"architecture": "aarch64", "gpuName": "NVIDIA Thor"},
    )
    monkeypatch.setattr(
        preflight,
        "collect_process_identity",
        lambda pid, url: {"pid": pid, "url": url},
    )
    monkeypatch.setattr(
        preflight,
        "check_sglang_dependency_names",
        lambda path: {
            "requiredDistributions": list(REQUIRED_DISTRIBUTIONS),
            "versions": {name: "installed" for name in REQUIRED_DISTRIBUTIONS},
        },
    )
    monkeypatch.setattr(
        preflight,
        "validate_available_memory_floor",
        lambda minimum_gb: {
            "minimumAvailableGiB": minimum_gb,
            "memAvailableKiB": 64 * 1024**2,
        },
        raising=False,
    )


@pytest.mark.parametrize("sglang_log_mode", ["metadata", "delayed"])
def test_remote_canary_posts_one_image_and_writes_schema_2_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sglang_log_mode: str
) -> None:
    preflight = _load_preflight()
    _patch_canary_host_dependencies(preflight, monkeypatch)
    _PaddleXCanaryHandler.requests = []
    log_path = tmp_path / "sglang.log"
    log_path.write_text("startup complete\n")
    _SGLangHandler.request_count = 0
    _SGLangHandler.log_mode = sglang_log_mode
    _SGLangHandler.log_path = log_path
    offset = log_path.stat().st_size
    report = tmp_path / "memory.json"
    report.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "pid": 101,
                "cudaMemoryLimitGiB": 16,
                "cudaMemoryLimitBytes": 16 * 1024**3,
                "deviceTotalMemoryBytes": 128 * 1024**3,
                "allocatorFraction": 0.125,
            }
        )
    )
    config = tmp_path / "pipeline.yaml"
    config.write_text("pipeline_name: PaddleOCR-VL-1.6\n")
    attestation_path = tmp_path / "runtime/attestation.json"

    with _serve(_SGLangHandler) as sglang_url:
        _PaddleXCanaryHandler.sglang_url = sglang_url
        with _serve(_PaddleXCanaryHandler) as paddlex_url:
            attestation = preflight.run_sglang_canary(
                paddlex_url,
                sglang_url,
                _SGLangHandler.model_name,
                attestation_path,
                pipeline_pid=101,
                sglang_pid=202,
                config_path=config,
                memory_report_path=report,
                cuda_memory_limit_gb=16,
                sglang_log_path=log_path,
                sglang_log_offset=offset,
                sglang_python_path=Path("/workspace/.sglang/bin/python"),
                main_llm_memory_reserve_gb=16,
                timeout=2,
            )

    assert len(_PaddleXCanaryHandler.requests) == 1
    assert _PaddleXCanaryHandler.requests[0]["fileType"] == 1
    assert attestation["schemaVersion"] == 2
    assert attestation["mode"] == "remote_sglang"
    assert attestation["vlmBackend"] == "sglang-server"
    assert attestation["processIdentity"] == {"pid": 101, "url": paddlex_url}
    assert attestation["pipelineProcessIdentity"] == attestation["processIdentity"]
    assert attestation["sglangProcessIdentity"] == {"pid": 202, "url": sglang_url}
    assert attestation["fullApi"] == f"{paddlex_url}/layout-parsing"
    assert attestation["servedModelName"] == _SGLangHandler.model_name
    assert attestation["actualPackageVersions"]["sglang"] == "installed"
    assert attestation["canaryEvidence"]["requestCount"] == 1
    assert attestation["canaryEvidence"]["sglangChatCompletionsDelta"] == 1
    assert attestation["canaryEvidence"]["matchedRequestLogPairs"] == 1
    assert attestation["canaryEvidence"]["sglangLogBytesAppended"] > 0
    assert attestation["memoryReserve"]["minimumAvailableGiB"] == 16
    assert json.loads(attestation_path.read_text()) == attestation


def test_remote_canary_uses_the_same_text_for_image_and_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preflight = _load_preflight()
    created_text: list[str | None] = []
    validated_text: list[str] = []
    log_path = tmp_path / "sglang.log"
    log_path.write_text("")
    config = tmp_path / "pipeline.yaml"
    config.write_text("pipeline_name: PaddleOCR-VL-1.6\n")

    monkeypatch.setattr(preflight, "probe_sglang_service", lambda *args, **kwargs: {})
    monkeypatch.setattr(preflight, "probe_service", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        preflight,
        "validate_sglang_pipeline_config",
        lambda path: {
            "configSha256": "config-sha",
            "manifestSha256": "manifest-sha",
            "sglangBackend": {"servedModelName": _SGLangHandler.model_name},
            "batchAndConcurrencySettings": {},
        },
    )
    monkeypatch.setattr(preflight, "validate_cuda_memory_limit_report", lambda *args, **kwargs: {})
    monkeypatch.setattr(preflight, "collect_environment_attestation", lambda: {})
    monkeypatch.setattr(
        preflight,
        "check_sglang_dependency_names",
        lambda path: {"versions": {}, "requiredDistributions": []},
    )
    monkeypatch.setattr(
        preflight,
        "collect_process_identity",
        lambda pid, url: {"pid": pid, "url": url},
    )
    request_counts = iter((1.0, 2.0))
    monkeypatch.setattr(
        preflight,
        "_read_sglang_chat_completion_count",
        lambda *args, **kwargs: next(request_counts, 2.0),
    )
    monkeypatch.setattr(
        preflight,
        "validate_available_memory_floor",
        lambda minimum_gb: {"minimumAvailableGiB": minimum_gb},
    )

    def fake_create(directory: Path, *, image_text: str | None = None):
        created_text.append(image_text)
        image = directory / "image.png"
        image.write_bytes(b"image")
        return {"image": image}

    def fake_post(url, payload, *, expected_text: str, **kwargs):
        validated_text.append(expected_text)
        with log_path.open("a") as stream:
            stream.write("Receive: rid=canary\nFinish: rid=canary\n")
        return {"markdownCharacters": len(expected_text), "docPreprocessor": {}}

    monkeypatch.setattr(preflight, "_create_canary_files", fake_create)
    monkeypatch.setattr(preflight, "_post_canary", fake_post)

    preflight.run_sglang_canary(
        "http://127.0.0.1:8118",
        "http://127.0.0.1:30002",
        _SGLangHandler.model_name,
        tmp_path / "attestation.json",
        pipeline_pid=101,
        sglang_pid=202,
        config_path=config,
        memory_report_path=tmp_path / "memory.json",
        cuda_memory_limit_gb=16,
        sglang_log_path=log_path,
        sglang_log_offset=0,
        sglang_python_path=Path("/workspace/.sglang/bin/python"),
        main_llm_memory_reserve_gb=16,
        timeout=0.1,
    )

    assert created_text == validated_text


@pytest.mark.parametrize("log_mode", ["unchanged", "whitespace", "payload", "mismatched"])
def test_remote_canary_rejects_missing_or_payload_bearing_sglang_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    log_mode: str,
) -> None:
    preflight = _load_preflight()
    _patch_canary_host_dependencies(preflight, monkeypatch)
    _PaddleXCanaryHandler.requests = []
    log_path = tmp_path / "sglang.log"
    log_path.write_text("ready\n")
    _SGLangHandler.request_count = 0
    _SGLangHandler.log_mode = log_mode
    _SGLangHandler.log_path = log_path
    report = tmp_path / "memory.json"
    report.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "pid": 101,
                "cudaMemoryLimitGiB": 16,
                "cudaMemoryLimitBytes": 16 * 1024**3,
                "deviceTotalMemoryBytes": 128 * 1024**3,
                "allocatorFraction": 0.125,
            }
        )
    )
    config = tmp_path / "pipeline.yaml"
    config.write_text("pipeline_name: PaddleOCR-VL-1.6\n")
    with _serve(_SGLangHandler) as sglang_url:
        _PaddleXCanaryHandler.sglang_url = sglang_url
        with _serve(_PaddleXCanaryHandler) as paddlex_url:
            with pytest.raises(
                preflight.PreflightError,
                match="SGLang.*log|payload|request metadata",
            ):
                preflight.run_sglang_canary(
                    paddlex_url,
                    sglang_url,
                    _SGLangHandler.model_name,
                    tmp_path / "attestation.json",
                    pipeline_pid=101,
                    sglang_pid=202,
                    config_path=config,
                    memory_report_path=report,
                    cuda_memory_limit_gb=16,
                    sglang_log_path=log_path,
                    sglang_log_offset=log_path.stat().st_size,
                    sglang_python_path=Path("/workspace/.sglang/bin/python"),
                    main_llm_memory_reserve_gb=16,
                    timeout=0.05,
                )


def test_remote_canary_rejects_log_noise_without_request_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preflight = _load_preflight()
    _patch_canary_host_dependencies(preflight, monkeypatch)
    monkeypatch.setattr(preflight, "probe_sglang_service", lambda *args, **kwargs: {})
    monkeypatch.setattr(preflight, "probe_service", lambda *args, **kwargs: {})
    request_counts = iter((4.0, 5.0))
    monkeypatch.setattr(
        preflight,
        "_read_sglang_chat_completion_count",
        lambda *args, **kwargs: next(request_counts, 5.0),
    )
    log_path = tmp_path / "sglang.log"
    log_path.write_text("ready\n")
    report = tmp_path / "memory.json"
    report.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "pid": 101,
                "cudaMemoryLimitGiB": 16,
                "cudaMemoryLimitBytes": 16 * 1024**3,
                "deviceTotalMemoryBytes": 128 * 1024**3,
                "allocatorFraction": 0.125,
            }
        )
    )
    config = tmp_path / "pipeline.yaml"
    config.write_text("pipeline_name: PaddleOCR-VL-1.6\n")

    def fake_post(*args, **kwargs):
        with log_path.open("a") as stream:
            stream.write("delayed startup diagnostics\n")
        return {"markdownCharacters": 20, "docPreprocessor": {}}

    monkeypatch.setattr(preflight, "_post_canary", fake_post)
    with pytest.raises(preflight.PreflightError, match="request metadata"):
        preflight.run_sglang_canary(
            "http://127.0.0.1:8118",
            "http://127.0.0.1:30002",
            _SGLangHandler.model_name,
            tmp_path / "attestation.json",
            pipeline_pid=101,
            sglang_pid=202,
            config_path=config,
            memory_report_path=report,
            cuda_memory_limit_gb=16,
            sglang_log_path=log_path,
            sglang_log_offset=log_path.stat().st_size,
            sglang_python_path=Path("/workspace/.sglang/bin/python"),
            main_llm_memory_reserve_gb=16,
            timeout=0.05,
        )


def test_remote_canary_requires_sglang_chat_completion_counter_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preflight = _load_preflight()
    _patch_canary_host_dependencies(preflight, monkeypatch)
    monkeypatch.setattr(preflight, "probe_sglang_service", lambda *args, **kwargs: {})
    monkeypatch.setattr(preflight, "probe_service", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        preflight,
        "_read_sglang_chat_completion_count",
        lambda *args, **kwargs: 7.0,
        raising=False,
    )
    log_path = tmp_path / "sglang.log"
    log_path.write_text("")
    report = tmp_path / "memory.json"
    report.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "pid": 101,
                "cudaMemoryLimitGiB": 16,
                "cudaMemoryLimitBytes": 16 * 1024**3,
                "deviceTotalMemoryBytes": 128 * 1024**3,
                "allocatorFraction": 0.125,
            }
        )
    )
    config = tmp_path / "pipeline.yaml"
    config.write_text("pipeline_name: PaddleOCR-VL-1.6\n")

    def fake_post(*args, **kwargs):
        with log_path.open("a") as stream:
            stream.write("Receive: rid=canary\nFinish: rid=canary\n")
        return {"markdownCharacters": 20, "docPreprocessor": {}}

    monkeypatch.setattr(preflight, "_post_canary", fake_post)
    with pytest.raises(preflight.PreflightError, match="chat completion counter"):
        preflight.run_sglang_canary(
            "http://127.0.0.1:8118",
            "http://127.0.0.1:30002",
            _SGLangHandler.model_name,
            tmp_path / "attestation.json",
            pipeline_pid=101,
            sglang_pid=202,
            config_path=config,
            memory_report_path=report,
            cuda_memory_limit_gb=16,
            sglang_log_path=log_path,
            sglang_log_offset=0,
            sglang_python_path=Path("/workspace/.sglang/bin/python"),
            main_llm_memory_reserve_gb=16,
            timeout=0.05,
        )


def test_available_memory_floor_rejects_less_than_configured_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflight = _load_preflight()
    monkeypatch.setattr(
        preflight,
        "_read_meminfo",
        lambda: {"MemTotal": 128 * 1024**2, "MemAvailable": 15 * 1024**2},
    )

    with pytest.raises(preflight.PreflightError, match="16 GiB"):
        preflight.validate_available_memory_floor(16)


def test_new_cli_commands_use_the_shared_argument_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    preflight = _load_preflight()
    calls: list[tuple[str, object]] = []
    config_path = tmp_path / "c.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "LLMDeploymentSGLangBackend": {
                    "served_model_name": "PaddleOCR-VL-1.6-0.9B"
                }
            }
        )
    )
    monkeypatch.setattr(
        preflight,
        "check_sglang_dependency_names",
        lambda path: calls.append(("check", path))
        or {
            "requiredDistributions": list(REQUIRED_DISTRIBUTIONS),
            "versions": {name: "diagnostic" for name in REQUIRED_DISTRIBUTIONS},
        },
    )
    monkeypatch.setattr(
        preflight,
        "validate_sglang_pipeline_config",
        lambda path: calls.append(("config", path))
        or {"mode": "remote_sglang", "sglangBackend": {"port": 30002}},
    )
    monkeypatch.setattr(
        preflight,
        "probe_sglang_service",
        lambda url, model, timeout: calls.append(("probe", (url, model, timeout))) or {},
    )
    monkeypatch.setattr(
        preflight,
        "run_sglang_canary",
        lambda *args, **kwargs: calls.append(("canary", (args, kwargs))) or {},
    )
    assert preflight.main(
        [
            "check-sglang-env",
            "--python",
            "/safe/python",
            "--config",
            str(config_path),
        ]
    ) == 0
    check_output = json.loads(capsys.readouterr().out)
    assert set(check_output) == {
        "pipeline",
        "sglangBackend",
        "dependencies",
        "actualVersions",
    }
    assert preflight.main(
        [
            "probe-sglang",
            "--url",
            "http://127.0.0.1:30002",
            "--served-model-name",
            "PaddleOCR-VL-1.6-0.9B",
            "--timeout",
            "7",
        ]
    ) == 0
    assert preflight.main(
        [
            "canary-sglang",
            "--paddlex-url",
            "http://127.0.0.1:8118",
            "--sglang-url",
            "http://127.0.0.1:30002",
            "--served-model-name",
            "PaddleOCR-VL-1.6-0.9B",
            "--attestation",
            str(tmp_path / "a.json"),
            "--pipeline-pid",
            "101",
            "--sglang-pid",
            "202",
            "--config",
            str(config_path),
            "--memory-report",
            str(tmp_path / "m.json"),
            "--cuda-memory-limit-gb",
            "16",
            "--sglang-log",
            str(tmp_path / "s.log"),
            "--sglang-log-offset",
            "42",
            "--sglang-python",
            "/workspace/.sglang/bin/python",
            "--main-llm-memory-reserve-gb",
            "16",
            "--timeout",
            "9",
        ]
    ) == 0
    assert calls[0] == ("check", Path("/safe/python"))
    assert calls[1] == ("config", config_path)
    assert calls[2] == (
        "probe",
        ("http://127.0.0.1:30002", "PaddleOCR-VL-1.6-0.9B", 7.0),
    )
    canary_args, canary_kwargs = calls[3][1]
    assert canary_args[:4] == (
        "http://127.0.0.1:8118",
        "http://127.0.0.1:30002",
        "PaddleOCR-VL-1.6-0.9B",
        tmp_path / "a.json",
    )
    assert canary_kwargs["pipeline_pid"] == 101
    assert canary_kwargs["sglang_pid"] == 202
    assert canary_kwargs["sglang_log_offset"] == 42
    assert canary_kwargs["sglang_python_path"] == Path("/workspace/.sglang/bin/python")
    assert canary_kwargs["main_llm_memory_reserve_gb"] == 16
    capsys.readouterr()
