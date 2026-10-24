from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.api.agent import AgentChatRequest, agent_chat
from backend.agent.agent_runner import AgentRunner
from backend.agent.tool_configuration import AgentToolConfigurationRequest
from backend.agent.tool_configuration import AgentToolConfigurationError
from backend.api.agent import agent_chat
from backend.config import get_config


def test_admission_stages_exact_id_snapshot_and_rolls_back_only_that_id():
    runner = AgentRunner(MagicMock(), get_config())
    session_id = "123e4567-e89b-12d3-a456-426614174000"
    candidate = AgentToolConfigurationRequest(
        schema_version=1,
        catalog_revision=runner.tool_catalog.catalog_revision,
        manager_agent_tools=[],
        basic_agent_tools=[],
    )
    stage = runner.stage_agent_request_admission(
        session_id, None, None, 0, tool_configuration=candidate
    )
    assert stage.session_id == session_id
    assert stage.tool_configuration_snapshot is not None
    assert stage.tool_configuration_snapshot.manager_agent.tool_names == ()
    runner.rollback_agent_request_stage(stage)
    assert runner.sessions.get(session_id) is None
    assert session_id not in runner._pending_agent_admissions


def test_pending_exact_id_rejects_different_candidate_as_locked_not_generic_busy():
    runner = AgentRunner(MagicMock(), get_config())
    session_id = "123e4567-e89b-12d3-a456-426614174001"
    candidate_a = AgentToolConfigurationRequest(
        schema_version=1,
        catalog_revision=runner.tool_catalog.catalog_revision,
        manager_agent_tools=[],
        basic_agent_tools=[],
    )
    candidate_b = AgentToolConfigurationRequest(
        schema_version=1,
        catalog_revision=runner.tool_catalog.catalog_revision,
        manager_agent_tools=["read_file"],
        basic_agent_tools=[],
    )
    stage = runner.stage_agent_request_admission(
        session_id, None, None, 0, tool_configuration=candidate_a
    )
    try:
        runner.stage_agent_request_admission(
            session_id, None, None, 0, tool_configuration=candidate_b
        )
    except AgentToolConfigurationError as exc:
        assert exc.code == "tool_configuration_locked"
        assert exc.status_code == 409
    else:
        raise AssertionError("different same-ID candidate was not rejected as locked")
    runner.rollback_agent_request_stage(stage)


@pytest.mark.asyncio
async def test_standalone_agent_chat_uses_candidate_stage_without_lifecycle_registry():
    catalog = AgentRunner(MagicMock(), get_config()).tool_catalog
    candidate = AgentToolConfigurationRequest(
        schema_version=1,
        catalog_revision=catalog.catalog_revision,
        manager_agent_tools=[],
        basic_agent_tools=[],
    )
    stage = SimpleNamespace(session_id="123e4567-e89b-12d3-a456-426614174000")
    seen = {}

    class StandaloneRunner:
        def stage_agent_request_admission(self, *args, **kwargs):
            seen["candidate"] = kwargs["tool_configuration"]
            return stage

        async def run_sync(self, *args, **kwargs):
            seen["admission_stage"] = kwargs["admission_stage"]
            return {"session_id": stage.session_id, "answer": "ok", "steps": []}

        def rollback_agent_request_stage(self, admission):
            seen["rollback"] = admission

    app = SimpleNamespace(
        state=SimpleNamespace(
            agent_runner=StandaloneRunner(),
            request_lifecycle_registry=None,
        )
    )
    raw_request = SimpleNamespace(app=app)
    request = AgentChatRequest(message="hello", stream=False, tool_configuration=candidate)
    response = await agent_chat(request, raw_request)
    assert response.status_code == 200
    assert seen["candidate"] is candidate
    assert seen["admission_stage"] is stage


@pytest.mark.asyncio
async def test_standalone_stream_prefetch_maps_prompt_failure_before_headers():
    catalog = AgentRunner(MagicMock(), get_config()).tool_catalog
    candidate = AgentToolConfigurationRequest(
        schema_version=1,
        catalog_revision=catalog.catalog_revision,
        manager_agent_tools=[],
        basic_agent_tools=[],
    )
    stage = SimpleNamespace(
        session_id="123e4567-e89b-12d3-a456-426614174002",
        _accepted=False,
    )
    seen = {}

    class StandaloneRunner:
        def stage_agent_request_admission(self, *args, **kwargs):
            return stage

        def rollback_agent_request_stage(self, admission):
            seen["rollback"] = admission

        def run_stream(self, *args, **kwargs):
            async def _stream():
                raise AgentToolConfigurationError(
                    "tool_prompt_render_failed", "render failed"
                )
                yield "never"

            return _stream()

    app = SimpleNamespace(
        state=SimpleNamespace(
            agent_runner=StandaloneRunner(),
            request_lifecycle_registry=None,
        )
    )
    request = AgentChatRequest(
        session_id=stage.session_id,
        message="hello",
        stream=True,
        tool_configuration=candidate,
    )
    response = await agent_chat(request, SimpleNamespace(app=app))
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await response(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/agent/chat",
            "raw_path": b"/v1/agent/chat",
            "query_string": b"",
            "headers": [],
            "server": ("test", 80),
            "client": ("test", 1),
            "scheme": "http",
            "root_path": "",
            "http_version": "1.1",
        },
        receive,
        send,
    )
    assert sent[0]["status"] == 500
    body = sent[-1]["body"]
    assert b"tool_prompt_render_failed" in body
    assert seen["rollback"] is stage
