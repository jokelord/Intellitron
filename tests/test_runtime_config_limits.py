"""Tests for runtime-safe config values exposed to the frontend."""

from dataclasses import replace

import pytest

from fastapi.testclient import TestClient

from backend.main import app
from backend.config import ConfigLoader, ReadFileConfig


def test_read_file_v7_defaults_are_loaded():
    cfg = ConfigLoader().load(reload=True)

    assert cfg.server.session_hub_heartbeat_seconds == 15
    assert cfg.ocr.host == "127.0.0.1"
    assert cfg.ocr.port == 8118
    assert cfg.ocr.base_url == "http://127.0.0.1:8118"
    assert cfg.ocr.pipeline_config_path == "config/paddleocr-vl-thor.yaml"
    assert (
        cfg.ocr.sglang_pipeline_config_path
        == "config/paddleocr-vl-thor-sglang.yaml"
    )
    assert cfg.read_file.pdf_chunk_pages == 24
    assert cfg.read_file.pdf_chunk_overlap_pages == 1
    assert cfg.ocr.cuda_memory_limit_gb == 16
    assert cfg.read_file.main_llm_memory_reserve_gb == 16
    assert cfg.read_file.ocr_queue_limit == 2
    assert cfg.read_file.normal_request_use_doc_unwarping is False
    assert cfg.read_file.max_pdf_pages == 1000
    assert cfg.read_file.max_image_frames == 200
    assert cfg.read_file.max_xlsx_sheets > 0
    assert cfg.read_file.max_xlsx_cells > 0


def test_ocr_config_rejects_non_positive_cuda_memory_limit():
    with pytest.raises(ValueError, match="cuda_memory_limit_gb"):
        from backend.config import OCRServiceConfig

        OCRServiceConfig(cuda_memory_limit_gb=0)


@pytest.mark.parametrize("overlap", [-1, 24, 25])
def test_read_file_config_rejects_overlap_not_less_than_chunk_pages(overlap):
    with pytest.raises(ValueError, match="pdf_chunk_overlap_pages"):
        ReadFileConfig(pdf_chunk_pages=24, pdf_chunk_overlap_pages=overlap)


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "8192", None])
def test_read_file_config_rejects_invalid_ocr_max_new_tokens(value):
    with pytest.raises(ValueError, match="ocr_max_new_tokens"):
        ReadFileConfig(ocr_max_new_tokens=value)


def test_config_loader_rejects_yaml_bool_for_ocr_max_new_tokens(
    tmp_path, monkeypatch
):
    overall = tmp_path / "config-overall.yaml"
    agent_operation = tmp_path / "config-agent-operation.yaml"
    overall.write_text("agent_operation: config-agent-operation.yaml\n")
    agent_operation.write_text("read_file:\n  ocr_max_new_tokens: true\n")
    monkeypatch.setattr("backend.config.PROJECT_ROOT", tmp_path)

    with pytest.raises(ValueError, match="ocr_max_new_tokens"):
        ConfigLoader(overall).load()


def test_config_endpoint_uses_active_backend_context_and_safe_max_tokens(monkeypatch):
    class FakeService:
        active_backend = "vllm"
        is_ready = True

    current = ConfigLoader().load(reload=True)
    replacement = replace(
        current,
        model=replace(current.model, max_context_length=262144),
        vllm=replace(current.vllm, max_model_len=160000),
        inference=replace(current.inference, max_tokens=262144, max_new_tokens=262144),
    )

    monkeypatch.setattr("backend.main.reload_config", lambda: replacement)
    monkeypatch.setattr("backend.main.get_llm_service", lambda: FakeService())

    client = TestClient(app)
    response = client.get("/config")

    assert response.status_code == 200
    data = response.json()
    assert data["model"]["max_context_length"] == 160000
    assert data["inference"]["max_tokens"] == 160000


def test_config_endpoint_exposes_frontend_stream_idle_timeouts(monkeypatch):
    class FakeService:
        active_backend = "sglang"
        is_ready = True

    current = ConfigLoader().load(reload=True)
    replacement = replace(
        current,
        frontend=replace(
            current.frontend,
            chat_stream_idle_timeout_seconds=123,
            agent_stream_idle_timeout_seconds=456,
            cycle_stream_idle_timeout_seconds=789,
        ),
    )

    monkeypatch.setattr("backend.main.reload_config", lambda: replacement)
    monkeypatch.setattr("backend.main.get_llm_service", lambda: FakeService())

    client = TestClient(app)
    response = client.get("/config")

    assert response.status_code == 200
    data = response.json()
    assert data["frontend"]["chat_stream_idle_timeout_seconds"] == 123
    assert data["frontend"]["agent_stream_idle_timeout_seconds"] == 456
    assert data["frontend"]["cycle_stream_idle_timeout_seconds"] == 789


@pytest.mark.asyncio
async def test_config_endpoint_exposes_cycle_scheduled_track_defaults(monkeypatch):
    class FakeService:
        active_backend = "sglang"
        is_ready = True

    current = ConfigLoader().load(reload=True)
    replacement = replace(
        current,
        cycle=replace(
            current.cycle,
            timeout_policy="exempt_cycle_run",
            scheduled_track_max_duration_seconds=172_800,
            max_scheduled_message_days=7,
            event_journal_retention_seconds=3_600,
            event_journal_max_records=123,
        ),
    )
    monkeypatch.setattr("backend.main.reload_config", lambda: replacement)
    monkeypatch.setattr("backend.main.get_llm_service", lambda: FakeService())

    from backend.main import get_current_config

    cycle = (await get_current_config())["cycle"]
    assert cycle["timeout_policy"] == "exempt_cycle_run"
    assert cycle["scheduled_track_max_duration_seconds"] == 172_800
    assert cycle["max_scheduled_message_days"] == 7
    assert cycle["event_journal_retention_seconds"] == 3_600
    assert cycle["event_journal_max_records"] == 123
    assert cycle["default_scheduled_messages"] == []


def test_config_endpoint_exposes_runtime_time_context_flag(monkeypatch):
    class FakeService:
        active_backend = "sglang"
        is_ready = True

    current = ConfigLoader().load(reload=True)
    replacement = replace(
        current,
        runtime_prompts=replace(
            current.runtime_prompts, enable_runtime_time_context=True
        ),
    )
    monkeypatch.setattr("backend.main.reload_config", lambda: replacement)
    monkeypatch.setattr("backend.main.get_llm_service", lambda: FakeService())

    client = TestClient(app)
    response = client.get("/config")

    assert response.status_code == 200
    assert response.json()["runtime_prompts"]["enable_runtime_time_context"] is True


def test_config_endpoint_exposes_backend_validated_bubble_character_limit(monkeypatch):
    class FakeService:
        active_backend = "sglang"
        is_ready = True

    current = ConfigLoader().load(reload=True)
    replacement = replace(
        current,
        web_ui=replace(
            current.web_ui,
            content=replace(current.web_ui.content, bubble_character_limit=31_337),
        ),
    )
    monkeypatch.setattr("backend.main.reload_config", lambda: replacement)
    monkeypatch.setattr("backend.main.get_llm_service", lambda: FakeService())

    response = TestClient(app).get("/config")

    assert response.status_code == 200
    assert response.json()["webUI"]["content"] == {
        "bubble_character_limit": 31_337
    }


@pytest.mark.asyncio
async def test_config_endpoint_exposes_backend_validated_message_templates(monkeypatch):
    import backend.main as main

    class FakeService:
        active_backend = "sglang"
        is_ready = True

    current = ConfigLoader().load(reload=True)
    replacement = replace(
        current,
        web_ui=replace(
            current.web_ui,
            message_templates=["first", "second\nline"],
        ),
    )
    monkeypatch.setattr("backend.main.reload_config", lambda: replacement)
    monkeypatch.setattr("backend.main.get_llm_service", lambda: FakeService())

    response = await main.get_current_config()

    assert response["webUI"]["components"]["message_templates"] == {
        "items": ["first", "second\nline"],
    }
