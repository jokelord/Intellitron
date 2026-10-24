from __future__ import annotations

from types import SimpleNamespace

from PIL import Image
import pytest
from smolagents.memory import ActionStep, AgentMemory
from smolagents.models import (
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
)
from smolagents.monitoring import Timing


def _call(command: str, call_id: str):
    return ChatMessageToolCall(
        id=call_id,
        type="function",
        function=ChatMessageToolCallFunction(
            name="exec_command", arguments={"command": command}
        ),
    )


def test_single_executable_observation_reaches_action_step_image_history():
    from backend.agent.agent_runner import SmartToolCallingAgent
    from backend.computer_use.executable_protocol import (
        VisionDesktopCommandResult,
        make_trusted_observation_attachment,
    )
    from backend.agent.computer_use_history import mark_computer_use_observation

    image = Image.new("RGB", (4, 4), color="red")
    session = SimpleNamespace(
        session_id="11111111-1111-4111-8111-111111111111",
        is_child_context=False,
        answering_now=False,
        answer_now=False,
        behavior_records={},
        computer_use_history_omission_stage=None,
    )
    result = VisionDesktopCommandResult(
        state="OBSERVATION_READY",
        text="observation ready",
        attachment=make_trusted_observation_attachment(
            image=image,
            observation_id="observation-1",
            frame_id="frame-1",
            geometry_version=3,
        ),
    )

    class ExecutableTool:
        name = "exec_command"

        def forward(self, command):
            assert command.startswith("vision_desktop observe")
            return result

    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        SmartToolCallingAgent, "__init__", lambda self, **kwargs: None
    ):
        agent = SmartToolCallingAgent.__new__(SmartToolCallingAgent)
    agent.tools = {"exec_command": ExecutableTool()}
    agent.managed_agents = {}
    agent.orchestrator = __import__(
        "backend.agent.orchestrator", fromlist=["ToolOrchestrator"]
    ).ToolOrchestrator()
    agent._agent_session = session
    agent._manager_agent_max_parallel_children = None
    agent.model = SimpleNamespace()
    agent.logger = SimpleNamespace(log=lambda *_args, **_kwargs: None)
    agent.state = {}
    agent.execute_tool_call = lambda tool_name, arguments: result
    agent.memory = AgentMemory(system_prompt="system")
    old_image = Image.new("RGB", (4, 4), color="blue")
    mark_computer_use_observation(
        old_image,
        context_id=session.session_id,
        observation_id="observation-old",
        geometry_version=2,
    )
    agent.memory.steps = [
        ActionStep(
            step_number=0,
            timing=Timing(start_time=0.0, end_time=0.1),
            observations_images=[old_image],
        )
    ]
    session.agent = agent

    memory_step = ActionStep(
        step_number=1,
        timing=Timing(start_time=0.0, end_time=0.1),
    )
    events = list(
        agent.process_tool_calls(
            SimpleNamespace(tool_calls=[_call(
                "vision_desktop observe --target-description Settings --action left_click",
                "call-1",
            )]),
            memory_step,
        )
    )

    assert events
    assert not any(
        isinstance(event, dict) and event.get("_orchestration_event")
        for event in events
    )
    assert memory_step.observations_images == [image]
    assert session.computer_use_history_omission_stage is not None
    assert getattr(image, "_trusted_computer_use_observation", None) is not None
    assert "observation ready" in memory_step.observations


def test_dependent_vision_operations_cannot_share_one_model_turn():
    from backend.agent.agent_runner import SmartToolCallingAgent

    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        SmartToolCallingAgent, "__init__", lambda self, **kwargs: None
    ):
        agent = SmartToolCallingAgent.__new__(SmartToolCallingAgent)
    with pytest.raises(
        ValueError, match="dependent vision_desktop operations must be issued"
    ):
        list(
            agent.process_tool_calls(
                SimpleNamespace(
                    tool_calls=[
                        _call(
                            "vision_desktop observe --target-description app --action left_click",
                            "call-1",
                        ),
                        _call("vision_desktop inspect_region --x 500 --y 500 --zoom 4", "call-2"),
                    ]
                ),
                ActionStep(
                    step_number=1,
                    timing=Timing(start_time=0.0, end_time=0.1),
                ),
            )
        )
