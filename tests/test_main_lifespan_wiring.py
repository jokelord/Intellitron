"""Regression: backend.main lifespan must wire compact_orchestrator into AgentRunner.

Bug (2026-06-07): the wiring block was placed BEFORE the
compact_orchestrator was initialized, so `app.state.compact_orchestrator`
was still None when the wiring ran. The `if ... and ... is not None:`
guard silently skipped the wiring, leaving `agent_runner.compact_orchestrator`
as None for the rest of the process lifetime.

Symptom: manual /compact still worked (it uses
`app.state.compact_orchestrator` directly via /v1/compact/run), but
auto-compact in agent mode never fired — `_should_auto_compact_session`
returned False on the very first line (`if self.compact_orchestrator is None:
return False`) for every session, regardless of token count. The
agent-mode dispatch card kept appearing round after round.

This test pins the contract: after the lifespan startup runs, the
AgentRunner must have its compact_orchestrator wired. It would have
caught the original bug.
"""
from __future__ import annotations

import json
import hashlib
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import logging
from types import SimpleNamespace

from backend.config import OCRServiceConfig
from backend.tools.read_file.paddleocr_client import OCRServiceStatus


@pytest.mark.asyncio
async def test_main_preserves_one_injected_session_hub_runtime_across_lifespan(
    monkeypatch,
):
    import backend.main as main_module
    from backend.api.session_hub import router as session_hub_router
    from backend.session_hub_runtime import SessionHubRuntime

    injected = SessionHubRuntime()
    previous = main_module.app.state.session_hub_runtime
    main_module.app.state.session_hub_runtime = injected

    class _Service:
        def initialize(self):
            return False

        def shutdown(self):
            return None

    class _AgentRunner:
        def __init__(self, *args, **kwargs):
            self.compact_orchestrator = None

        def cleanup_expired_sessions(self, _ttl_minutes):
            return ()

    class _AsrService:
        def __init__(self, _config):
            pass

        def validate_vad(self):
            pass

        async def shutdown(self):
            pass

    monkeypatch.setattr(main_module, "_validate_ocr_worker_count", lambda *_: None)
    monkeypatch.setattr(main_module, "get_llm_service", lambda: _Service())
    monkeypatch.setattr("backend.compact.tokenizer.TokenCounter.get", lambda **_: None)
    monkeypatch.setattr("backend.agent.agent_runner.AgentRunner", _AgentRunner)
    monkeypatch.setattr("backend.cycle.camera.CameraService.__init__", lambda self: None)
    # Keep the real, hardware-free executor initialization so shutdown uses
    # the same registry ownership as production.
    monkeypatch.setattr("backend.asr.service.AsrService", _AsrService)

    try:
        async with main_module.lifespan(main_module.app):
            assert main_module.app.state.session_hub_runtime is injected
            assert any(
                getattr(route, "original_router", None) is session_hub_router
                for route in main_module.app.routes
            )
    finally:
        main_module.app.state.session_hub_runtime = previous


def test_asr_debug_audio_startup_logs_privacy_warning(caplog):
    from backend.main import _log_asr_debug_audio_privacy_warning

    asr_config = SimpleNamespace(
        observability=SimpleNamespace(save_debug_audio=True)
    )
    with caplog.at_level(logging.WARNING, logger="backend.main"):
        _log_asr_debug_audio_privacy_warning(asr_config)

    assert "privacy" in caplog.text.lower()
    assert "debug audio" in caplog.text.lower()


@pytest.mark.asyncio
async def test_lifespan_wires_compact_orchestrator_into_agent_runner(monkeypatch):
    """Run backend.main lifespan startup and verify the wiring is non-None.

    We patch out the heavy bits (LLM service init, SGLang connection) so
    the test doesn't need a running SGLang instance. The compact service
    IS the system under test here, so we let it initialize normally.
    """
    from backend.main import app, lifespan
    from backend.compact.orchestrator import CompactOrchestrator

    # Stub LLM service init so we don't actually connect to SGLang.
    from backend.llm_service import LLMService

    def _noop_init(self):
        # Mark as "initialized" without doing real work
        self.is_ready = True
        self._initialized = True
        return True

    monkeypatch.setattr(LLMService, "initialize", _noop_init)

    # Stub camera initialization; CycleExecutor itself needs no hardware and
    # must initialize its registry for the lifespan shutdown path.
    from backend.cycle.camera import CameraService

    monkeypatch.setattr(CameraService, "__init__", lambda self: None)

    # Stub AgentRunner so we don't actually build a smolagents agent.
    # We only need to verify that main.py assigns compact_orchestrator
    # onto the runner instance — the runner construction itself is covered
    # by other tests.
    wiring_target = {"compact_orchestrator": None}

    class _StubSessionStore:
        def cleanup_expired(self, _ttl_minutes: int) -> None:
            pass

    class _StubAgentRunner:
        def __init__(self, *args, **kwargs):
            self.sessions = _StubSessionStore()

        def cleanup_expired_sessions(self, ttl_minutes: int) -> None:
            self.sessions.cleanup_expired(ttl_minutes)

        @property
        def compact_orchestrator(self):
            return wiring_target["compact_orchestrator"]

        @compact_orchestrator.setter
        def compact_orchestrator(self, value):
            wiring_target["compact_orchestrator"] = value

    monkeypatch.setattr(
        "backend.agent.agent_runner.AgentRunner", _StubAgentRunner
    )

    async with lifespan(app):
        # After lifespan startup, the wiring MUST be in place.
        # Before the fix, wiring_target["compact_orchestrator"] stayed None
        # because the wiring block ran BEFORE app.state.compact_orchestrator
        # was initialized.
        assert wiring_target["compact_orchestrator"] is not None, (
            "AgentRunner.compact_orchestrator is None after lifespan startup. "
            "This is the pre-2026-06-07 wiring-ordering bug — the wiring "
            "block ran BEFORE compact_orchestrator was initialized, so "
            "the `if ... is not None` guard silently skipped it. "
            "Auto-compact in agent mode will never fire in this state."
        )
        # And the wired value is the actual CompactOrchestrator instance.
        assert isinstance(
            wiring_target["compact_orchestrator"], CompactOrchestrator
        ), (
            f"expected CompactOrchestrator, got "
            f"{type(wiring_target['compact_orchestrator']).__name__}"
        )
        # Also sanity-check the app.state mirror matches the runner wiring.
        assert (
            app.state.compact_orchestrator
            is wiring_target["compact_orchestrator"]
        )

    assert app.state.asr_service is None


def test_ocr_health_requires_live_probe_fresh_attestation_and_reports_queue(tmp_path, monkeypatch):
    from backend.main import get_ocr_health_diagnostics

    pipeline_config = tmp_path / "pipeline.yaml"
    model_manifest = tmp_path / "manifest.json"
    pipeline_config.write_bytes(b"pipeline")
    model_manifest.write_bytes(b"manifest")
    attestation = tmp_path / "attestation.json"
    attestation.write_text(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "schemaVersion": 1,
                "architecture": "aarch64",
                "cudaAvailable": True,
                "gpuName": "NVIDIA Thor",
                "engine": "transformers",
                "device": "cuda:0",
                "packages": {"transformers": "5.14.1"},
                "canaryPassed": True,
                "pipelineApi": "http://127.0.0.1:8118/layout-parsing",
                "pipeline": {
                    "pipelineName": "PaddleOCR-VL-1.6",
                    "pipelineVersion": "v1.6",
                    "configSha256": hashlib.sha256(b"pipeline").hexdigest(),
                    "manifestSha256": hashlib.sha256(b"manifest").hexdigest(),
                },
                "processIdentity": {
                    "pid": os.getpid(),
                    "listenerSocketInodes": ["12345"],
                },
            }
        )
    )
    config = OCRServiceConfig(
        pipeline_config_path=str(pipeline_config),
        model_manifest_path=str(model_manifest),
        attestation_path=str(attestation),
        attestation_max_age_seconds=60,
    )
    monkeypatch.setattr(
        "backend.main._current_ocr_listener_inodes",
        lambda pid, host, port: {"12345"},
    )
    guard = type("Guard", (), {"queue_depth": 2})()
    result = get_ocr_health_diagnostics(
        config,
        guard,
        probe=lambda _: OCRServiceStatus(True, True, "healthy"),
    )
    assert result == {
        "status": "healthy",
        "pipeline": "PaddleOCR-VL-1.6",
        "pipelineVersion": "v1.6",
        "engine": "transformers",
        "vlmBackend": None,
        "modelDevice": "cuda:0",
        "architecture": "aarch64",
        "cudaAvailable": True,
        "canaryPassed": True,
        "queueDepth": 2,
    }
    assert "markdown" not in result

    payload = json.loads(attestation.read_text())
    payload["pipelineApi"] = "http://127.0.0.1:39999/layout-parsing"
    attestation.write_text(json.dumps(payload))
    mismatched = get_ocr_health_diagnostics(
        config,
        guard,
        probe=lambda _: OCRServiceStatus(True, True, "healthy"),
    )
    assert mismatched["status"] == "degraded"


def test_ocr_health_rejects_stale_attestation_without_degrading_llm(tmp_path, monkeypatch):
    from backend.main import app, get_ocr_health_diagnostics
    from fastapi.testclient import TestClient

    attestation = tmp_path / "attestation.json"
    attestation.write_text(
        json.dumps(
            {
                "timestamp": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                "architecture": "aarch64",
                "cudaAvailable": True,
                "engine": "transformers",
                "device": "cuda:0",
                "canaryPassed": True,
                "pipeline": {"pipelineName": "PaddleOCR-VL-1.6", "pipelineVersion": "v1.6"},
                "processIdentity": {"pid": 99999999},
            }
        )
    )
    config = OCRServiceConfig(attestation_path=str(attestation), attestation_max_age_seconds=10)
    result = get_ocr_health_diagnostics(
        config,
        type("Guard", (), {"queue_depth": 0})(),
        probe=lambda _: OCRServiceStatus(True, True, "healthy"),
    )
    assert result["status"] == "degraded"
    assert result["canaryPassed"] is False

    class LLM:
        is_ready = True

    monkeypatch.setattr("backend.main.get_llm_service", lambda: LLM())
    monkeypatch.setattr("backend.main.get_ocr_health_diagnostics", lambda *args, **kwargs: result)
    response = TestClient(app).get("/health")
    assert response.json()["status"] == "healthy"
    assert response.json()["ocr"]["status"] == "degraded"


def test_ocr_health_revalidates_listener_and_pipeline_hashes(tmp_path, monkeypatch):
    from backend.main import get_ocr_health_diagnostics
    from backend.config import OCRServiceConfig
    from backend.tools.read_file.paddleocr_client import OCRServiceStatus

    pipeline_config = tmp_path / "pipeline.yaml"
    model_manifest = tmp_path / "manifest.json"
    pipeline_config.write_bytes(b"pipeline")
    model_manifest.write_bytes(b"manifest")
    identity = {
        "pid": os.getpid(),
        "listenerSocketInodes": ["12345"],
    }
    attestation = tmp_path / "attestation.json"
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "schemaVersion": 1,
        "architecture": "aarch64",
        "cudaAvailable": True,
        "gpuName": "NVIDIA Thor",
        "engine": "transformers",
        "device": "cuda:0",
        "packages": {"transformers": "5.14.1"},
        "canaryPassed": True,
        "fullApi": "http://127.0.0.1:8118/layout-parsing",
        "pipelineApi": "http://127.0.0.1:8118/layout-parsing",
        "pipeline": {
            "pipelineName": "PaddleOCR-VL-1.6",
            "pipelineVersion": "v1.6",
            "configSha256": hashlib.sha256(b"pipeline").hexdigest(),
            "manifestSha256": hashlib.sha256(b"manifest").hexdigest(),
        },
        "processIdentity": identity,
    }
    attestation.write_text(json.dumps(payload))
    ocr_config = OCRServiceConfig(
        pipeline_config_path=str(pipeline_config),
        model_manifest_path=str(model_manifest),
        attestation_path=str(attestation),
        attestation_max_age_seconds=60,
    )
    guard = type("Guard", (), {"queue_depth": 0})()
    probe = lambda _: OCRServiceStatus(True, True, "healthy")
    monkeypatch.setattr(
        "backend.main._current_ocr_listener_inodes",
        lambda pid, host, port: {"12345"},
        raising=False,
    )
    assert get_ocr_health_diagnostics(ocr_config, guard, probe=probe)["status"] == "healthy"

    payload["processIdentity"]["listenerSocketInodes"] = ["stale"]
    attestation.write_text(json.dumps(payload))
    assert get_ocr_health_diagnostics(ocr_config, guard, probe=probe)["status"] == "degraded"

    payload["processIdentity"]["listenerSocketInodes"] = ["12345"]
    payload["pipeline"]["configSha256"] = "stale"
    attestation.write_text(json.dumps(payload))
    assert get_ocr_health_diagnostics(ocr_config, guard, probe=probe)["status"] == "degraded"


@pytest.mark.parametrize(
    "mutation",
    [
        {"schemaVersion": 0},
        {"gpuName": "NVIDIA H100"},
        {"packages": {"transformers": "6.0.0"}},
    ],
)
def test_ocr_health_rejects_old_schema_wrong_gpu_and_transformers_6(
    tmp_path, monkeypatch, mutation
):
    from backend.main import get_ocr_health_diagnostics

    pipeline_config = tmp_path / "pipeline.yaml"
    model_manifest = tmp_path / "manifest.json"
    pipeline_config.write_bytes(b"pipeline")
    model_manifest.write_bytes(b"manifest")
    attestation = tmp_path / "attestation.json"
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "schemaVersion": 1,
        "architecture": "aarch64",
        "cudaAvailable": True,
        "gpuName": "NVIDIA Thor",
        "engine": "transformers",
        "device": "cuda:0",
        "packages": {"transformers": "5.14.1"},
        "canaryPassed": True,
        "fullApi": "http://127.0.0.1:8118/layout-parsing",
        "pipelineApi": "http://127.0.0.1:8118/layout-parsing",
        "pipeline": {
            "pipelineName": "PaddleOCR-VL-1.6",
            "pipelineVersion": "v1.6",
            "configSha256": hashlib.sha256(b"pipeline").hexdigest(),
            "manifestSha256": hashlib.sha256(b"manifest").hexdigest(),
        },
        "processIdentity": {
            "pid": os.getpid(),
            "listenerSocketInodes": ["12345"],
        },
    }
    payload.update(mutation)
    attestation.write_text(json.dumps(payload))
    config = OCRServiceConfig(
        pipeline_config_path=str(pipeline_config),
        model_manifest_path=str(model_manifest),
        attestation_path=str(attestation),
        attestation_max_age_seconds=60,
    )
    monkeypatch.setattr(
        "backend.main._current_ocr_listener_inodes",
        lambda pid, host, port: {"12345"},
    )
    result = get_ocr_health_diagnostics(
        config,
        type("Guard", (), {"queue_depth": 0})(),
        probe=lambda _: OCRServiceStatus(True, True, "healthy"),
    )
    assert result["status"] == "degraded"


def _process_start_ticks(pid: int) -> int:
    stat_text = Path(f"/proc/{pid}/stat").read_text()
    return int(stat_text[stat_text.rfind(")") + 2 :].split()[19])


def _remote_ocr_health_fixture(tmp_path):
    local_pipeline = tmp_path / "local-pipeline.yaml"
    remote_pipeline = tmp_path / "remote-pipeline.yaml"
    model_manifest = tmp_path / "manifest.json"
    local_pipeline.write_bytes(b"local pipeline")
    remote_pipeline.write_bytes(b"remote pipeline")
    model_manifest.write_bytes(b"manifest")
    identity = {
        "pid": os.getpid(),
        "startTimeTicks": _process_start_ticks(os.getpid()),
        "listenerSocketInodes": ["12345"],
    }
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "schemaVersion": 2,
        "mode": "remote_sglang",
        "architecture": "aarch64",
        "cudaAvailable": True,
        "gpuName": "NVIDIA Thor",
        "engine": "transformers",
        "vlmBackend": "sglang-server",
        "sglangApi": "http://127.0.0.1:30002/v1",
        "servedModelName": "PaddleOCR-VL-1.6-0.9B",
        "device": "cuda:0",
        "packages": {"transformers": "5.14.1"},
        "canaryPassed": True,
        "fullApi": "http://127.0.0.1:8118/layout-parsing",
        "pipelineApi": "http://127.0.0.1:8118/layout-parsing",
        "pipeline": {
            "pipelineName": "PaddleOCR-VL-1.6",
            "pipelineVersion": "v1.6",
            "configSha256": hashlib.sha256(b"remote pipeline").hexdigest(),
            "manifestSha256": hashlib.sha256(b"manifest").hexdigest(),
        },
        "processIdentity": identity,
        "pipelineConfigSha256": hashlib.sha256(b"remote pipeline").hexdigest(),
        "modelManifestSha256": hashlib.sha256(b"manifest").hexdigest(),
        "sglangProcessIdentity": {
            "pid": os.getpid(),
            "startTimeTicks": _process_start_ticks(os.getpid()),
            "bootId": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "executable": os.readlink(f"/proc/{os.getpid()}/exe"),
            "cmdlineSha256": hashlib.sha256(
                Path(f"/proc/{os.getpid()}/cmdline").read_bytes()
            ).hexdigest(),
            "listenerSocketInodes": ["67890"],
        },
    }
    attestation = tmp_path / "attestation.json"
    attestation.write_text(json.dumps(payload))
    config = OCRServiceConfig(
        pipeline_config_path=str(local_pipeline),
        sglang_pipeline_config_path=str(remote_pipeline),
        model_manifest_path=str(model_manifest),
        attestation_path=str(attestation),
        attestation_max_age_seconds=60,
    )
    return config, attestation, payload


def test_ocr_health_accepts_remote_sglang_attestation_and_uses_remote_config_hash(
    tmp_path, monkeypatch
):
    from backend.main import get_ocr_health_diagnostics

    config, _, _ = _remote_ocr_health_fixture(tmp_path)
    monkeypatch.setattr(
        "backend.main._current_ocr_listener_inodes",
        lambda pid, host, port: {"12345"} if port == 8118 else {"67890"},
    )
    monkeypatch.setattr(
        "backend.main._probe_remote_sglang",
        lambda url, model, *args: True,
        raising=False,
    )

    result = get_ocr_health_diagnostics(
        config,
        type("Guard", (), {"queue_depth": 1})(),
        probe=lambda _: OCRServiceStatus(True, True, "healthy"),
    )

    assert result == {
        "status": "healthy",
        "pipeline": "PaddleOCR-VL-1.6",
        "pipelineVersion": "v1.6",
        "engine": "transformers",
        "vlmBackend": "sglang-server",
        "modelDevice": "cuda:0",
        "architecture": "aarch64",
        "cudaAvailable": True,
        "canaryPassed": True,
        "queueDepth": 1,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"schemaVersion": 1},
        {"mode": "unknown"},
        {"vlmBackend": "transformers"},
        {"sglangProcessIdentity": None},
        {"sglangProcessIdentity": {"pid": 99999999, "startTimeTicks": 1}},
        {"pipelineApi": "http://127.0.0.1:30002/layout-parsing"},
        {"fullApi": "http://127.0.0.1:30002/layout-parsing"},
        {"sglangApi": "http://127.0.0.1:30003/v1"},
        {"servedModelName": "wrong-model"},
        {"pipelineConfigSha256": "wrong"},
        {"modelManifestSha256": "wrong"},
        {"pipeline": {"configSha256": "wrong"}},
    ],
)
def test_ocr_health_rejects_invalid_remote_sglang_attestation(
    tmp_path, monkeypatch, mutation
):
    from backend.main import get_ocr_health_diagnostics

    config, attestation, payload = _remote_ocr_health_fixture(tmp_path)
    if "pipeline" in mutation:
        payload["pipeline"].update(mutation["pipeline"])
    else:
        payload.update(mutation)
    attestation.write_text(json.dumps(payload))
    monkeypatch.setattr(
        "backend.main._current_ocr_listener_inodes",
        lambda pid, host, port: {"12345"} if port == 8118 else {"67890"},
    )
    monkeypatch.setattr(
        "backend.main._probe_remote_sglang",
        lambda url, model, *args: True,
        raising=False,
    )

    result = get_ocr_health_diagnostics(
        config,
        type("Guard", (), {"queue_depth": 0})(),
        probe=lambda _: OCRServiceStatus(True, True, "healthy"),
    )

    assert result["status"] == "degraded"


def test_ocr_health_rejects_remote_sglang_when_live_probe_fails(
    tmp_path, monkeypatch
):
    from backend.main import get_ocr_health_diagnostics

    config, _, _ = _remote_ocr_health_fixture(tmp_path)
    monkeypatch.setattr(
        "backend.main._current_ocr_listener_inodes",
        lambda pid, host, port: {"12345"} if port == 8118 else {"67890"},
    )
    monkeypatch.setattr(
        "backend.main._probe_remote_sglang",
        lambda url, model, *args: False,
        raising=False,
    )

    result = get_ocr_health_diagnostics(
        config,
        type("Guard", (), {"queue_depth": 0})(),
        probe=lambda _: OCRServiceStatus(True, True, "healthy"),
    )

    assert result["status"] == "degraded"


def test_ocr_health_rejects_remote_sglang_listener_identity_loss(
    tmp_path, monkeypatch
):
    from backend.main import get_ocr_health_diagnostics

    config, _, _ = _remote_ocr_health_fixture(tmp_path)
    monkeypatch.setattr(
        "backend.main._current_ocr_listener_inodes",
        lambda pid, host, port: {"12345"} if port == 8118 else set(),
    )
    monkeypatch.setattr(
        "backend.main._probe_remote_sglang",
        lambda url, model, *args: True,
        raising=False,
    )

    result = get_ocr_health_diagnostics(
        config,
        type("Guard", (), {"queue_depth": 0})(),
        probe=lambda _: OCRServiceStatus(True, True, "healthy"),
    )

    assert result["status"] == "degraded"
