import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from smolagents.agents import ActionOutput, ToolOutput
from smolagents.memory import ActionStep, ToolCall
from smolagents.monitoring import Timing

from backend.agent.agent_runner import AgentRunner, AgentSession
from backend.config import get_config


FULL_TEXT = "观" * 200_001 + "::exact-suffix"


def _runner():
    config = deepcopy(get_config())
    config.model.model_type = "text"
    return AgentRunner(MagicMock(), config)


def _step():
    return ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
        model_output=FULL_TEXT,
        observations=FULL_TEXT,
    )


def test_extract_sync_steps_thought_preserves_200001_character_suffix():
    runner = _runner()
    agent = SimpleNamespace(memory=SimpleNamespace(steps=[_step()]))

    sync_steps = runner._extract_sync_steps(agent)

    assert sync_steps[0]["thought"] == FULL_TEXT


def test_extract_sync_steps_observation_preserves_200001_character_suffix():
    runner = _runner()
    agent = SimpleNamespace(memory=SimpleNamespace(steps=[_step()]))

    sync_steps = runner._extract_sync_steps(agent)

    assert sync_steps[0]["observation"] == FULL_TEXT


def _emitted_memory_steps():
    runner = _runner()
    agent = SimpleNamespace(memory=SimpleNamespace(steps=[_step()]))
    queued = []
    loop = SimpleNamespace(call_soon_threadsafe=lambda callback, value: callback(value))
    queue = SimpleNamespace(put_nowait=queued.append)
    runner._emit_memory_steps(agent, loop, queue)
    return queued


def test_emit_memory_steps_thought_preserves_200001_character_suffix():
    queued = _emitted_memory_steps()

    assert next(item for item in queued if item["step_type"] == "thought")["content"] == FULL_TEXT


def test_emit_memory_steps_observation_preserves_200001_character_suffix():
    queued = _emitted_memory_steps()

    assert next(item for item in queued if item["step_type"] == "observation")["content"] == FULL_TEXT


@pytest.mark.anyio
async def test_sync_final_answer_preserves_200001_character_suffix():
    runner = _runner()
    agent = MagicMock()
    agent.memory.steps = [_step()]
    agent.run.return_value = FULL_TEXT
    session = AgentSession(
        session_id="full-sync-final",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )

    result = await runner._run_basic_sync(session, "inspect", None)

    assert result["answer"] == FULL_TEXT


async def _stream_payloads():
    runner = _runner()
    call = ToolCall(name="read_file", arguments={"path": "large"}, id="call-large")

    def stream():
        yield ActionStep(
            step_number=1,
            timing=Timing(start_time=0.0, end_time=0.1),
            model_output=FULL_TEXT,
        )
        yield ToolOutput(
            id="call-large",
            output=FULL_TEXT,
            observation=FULL_TEXT,
            is_final_answer=False,
            tool_call=call,
        )
        yield ActionOutput(output=FULL_TEXT, is_final_answer=True)

    agent = MagicMock()
    agent.memory.steps = []
    agent.tools = {}
    agent.managed_agents = {}
    agent.run.side_effect = lambda *args, **kwargs: stream()
    session = AgentSession(
        session_id="full-content-session",
        agent=agent,
        mode="tool_calling",
        current_agent_name="basic_agent",
    )
    payloads = []
    async for event in runner._run_basic_stream(session, "inspect", None):
        payloads.append(json.loads(event[6:]))
    return session, payloads


@pytest.mark.anyio
async def test_run_basic_stream_thought_preserves_200001_character_suffix():
    _, payloads = await _stream_payloads()

    thought = next(
        item for item in payloads
        if item.get("type") == "agent_step" and item.get("step_type") == "thought"
    )
    assert thought["content"] == FULL_TEXT


@pytest.mark.anyio
async def test_run_basic_stream_observation_preserves_200001_character_suffix():
    _, payloads = await _stream_payloads()

    observation = next(
        item for item in payloads
        if item.get("type") == "agent_step" and item.get("step_type") == "observation"
    )
    assert observation["content"] == FULL_TEXT


@pytest.mark.anyio
async def test_streaming_final_and_hub_replay_preserve_full_text():
    session, payloads = await _stream_payloads()

    final_content = next(item for item in payloads if item.get("type") == "content")
    assert final_content["content"] == FULL_TEXT

    token = session.event_hub.claim_producer()
    for payload in payloads:
        session.event_hub.append(payload, producer_token=token)
    session.event_hub.close("completed", producer_token=token)
    session.event_hub.release_producer(token)
    replay = [record async for record in session.event_hub.subscribe(0)]
    replay_observation = next(
        record.payload for record in replay
        if record.payload.get("step_type") == "observation"
    )
    assert replay_observation["content"] == FULL_TEXT
