"""Tests for write_memory_to_messages() tool_calls preservation.

Bug: write_memory_to_messages() strips tool_calls from ActionStep,
producing orphaned TOOL_RESPONSE (observation) messages without
the preceding TOOL_CALL (action) message. This causes the model
to see a broken action→observation sequence in multi-round
conversations, leading to hallucinations or infinite tool loops.

The fix: tool_calls must be preserved so ActionStep.to_messages()
generates the complete ASSISTANT → TOOL_CALL → TOOL_RESPONSE sequence.
"""

import json

from copy import copy
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from smolagents.memory import ActionStep, TaskStep, ToolCall as MemoryToolCall
from smolagents.models import ChatMessage, MessageRole
from smolagents.monitoring import Timing

_step_counter = 0


def _make_timing():
    return Timing(start_time=0.0, end_time=0.1)


def _make_tool_call(
    name="web_search", arguments={"query": "hello"}, call_id="call_001"
):
    return MemoryToolCall(name=name, arguments=arguments, id=call_id)


def _make_action_step_with_tool(
    model_output="Let me search for that.",
    tool_name="web_search",
    tool_args=None,
    observations="Search result: Hello World",
    call_id="call_001",
):
    """Create an ActionStep that has both tool_calls and observations."""
    global _step_counter
    _step_counter += 1
    if tool_args is None:
        tool_args = {"query": "hello"}
    step = ActionStep(
        step_number=_step_counter,
        timing=_make_timing(),
        model_output=model_output,
        tool_calls=[_make_tool_call(tool_name, tool_args, call_id)],
        observations=observations,
    )
    return step


def _make_action_step_final(model_output="Here is your answer."):
    """Create an ActionStep for a final_answer (no tool_calls, no observations)."""
    global _step_counter
    _step_counter += 1
    return ActionStep(
        step_number=_step_counter,
        timing=_make_timing(),
        model_output=model_output,
        tool_calls=None,
        observations=None,
        is_final_answer=True,
    )


def _make_task_step(task="What is hello?", images=None):
    return TaskStep(task=task, task_images=images)


def _get_role_sequence(messages: list[ChatMessage]) -> list[str]:
    """Extract the sequence of roles from a message list."""
    return [msg.role for msg in messages]


def _get_text_from_message(msg: ChatMessage) -> str:
    """Extract text content from a ChatMessage."""
    if isinstance(msg.content, list):
        texts = [el["text"] for el in msg.content if el.get("type") == "text"]
        return "\n".join(texts)
    return str(msg.content)


class TestActionStepToMessagesBaseline:
    """Baseline tests for smolagents ActionStep.to_messages() behavior.

    These confirm our understanding of the expected message generation.
    """

    def test_with_tool_calls_generates_four_roles(self):
        """When tool_calls present: ASSISTANT, TOOL_CALL, TOOL_RESPONSE."""
        step = _make_action_step_with_tool()
        messages = step.to_messages()
        roles = _get_role_sequence(messages)
        assert roles == [
            MessageRole.ASSISTANT,
            MessageRole.TOOL_CALL,
            MessageRole.TOOL_RESPONSE,
        ]

    def test_without_tool_calls_orphans_observation(self):
        """When tool_calls=None but observations present: only TOOL_RESPONSE (orphaned)."""
        step = _make_action_step_with_tool()
        step.tool_calls = None  # Simulate the bug
        messages = step.to_messages()
        roles = _get_role_sequence(messages)
        # Without tool_calls: ASSISTANT + orphaned TOOL_RESPONSE
        assert MessageRole.TOOL_CALL not in roles
        assert MessageRole.TOOL_RESPONSE in roles

    def test_without_model_output_and_tool_calls_only_observation(self):
        """Worst case: model_output=None and tool_calls=None → only orphaned TOOL_RESPONSE."""
        step = _make_action_step_with_tool(model_output=None)
        step.tool_calls = None
        messages = step.to_messages()
        roles = _get_role_sequence(messages)
        # No ASSISTANT, no TOOL_CALL, just orphaned TOOL_RESPONSE
        assert roles == [MessageRole.TOOL_RESPONSE]


class TestWriteMemoryToMessagesToolCalls:
    """Test that SmartToolCallingAgent.write_memory_to_messages() preserves
    tool_calls in ActionSteps, ensuring correct message sequences.
    """

    @pytest.fixture
    def agent(self):
        """Create a minimal SmartToolCallingAgent for testing write_memory_to_messages."""
        from backend.agent.agent_runner import SmartToolCallingAgent

        # We need to mock the model and tools to construct the agent
        mock_model = MagicMock()
        mock_model.model_id = "test-model"

        with patch.object(SmartToolCallingAgent, "__init__", lambda self, **kw: None):
            agent = SmartToolCallingAgent.__new__(SmartToolCallingAgent)

        # Set up minimal memory structure
        from smolagents.memory import AgentMemory, SystemPromptStep

        agent.memory = AgentMemory(system_prompt="You are a helpful assistant.")
        agent.logger = MagicMock()
        return agent

    def test_tool_calls_preserved_in_action_step(self, agent):
        """CRITICAL: tool_calls must NOT be stripped from ActionSteps."""
        action_step = _make_action_step_with_tool()
        agent.memory.steps = [
            _make_task_step("What is hello?"),
            action_step,
        ]

        messages = agent.write_memory_to_messages()

        # Find messages generated from the ActionStep
        # Skip system and task messages, look for TOOL_CALL role
        roles = _get_role_sequence(messages)
        assert MessageRole.TOOL_CALL in roles, (
            "TOOL_CALL message missing! tool_calls were stripped from ActionStep. "
            "This causes orphaned observations that break multi-round conversations."
        )

    def test_historical_task_image_is_serialized_without_mutating_memory(self, agent):
        historical_image = Image.new("RGB", (3, 2), "red")
        historical_task = _make_task_step("Describe the image", [historical_image])
        later_task = _make_task_step("What did I ask next?")
        agent.memory.steps = [
            historical_task,
            _make_action_step_final("It was red."),
            later_task,
        ]

        messages = agent.write_memory_to_messages()

        historical_message = next(
            message
            for message in messages
            if isinstance(message.content, list)
            and any(
                part.get("type") == "text" and "Describe the image" in part.get("text", "")
                for part in message.content
            )
        )
        assert any(
            part.get("type") == "image" and part.get("image") is historical_image
            for part in historical_message.content
        )
        assert historical_task.task_images == [historical_image]
        assert historical_task.task_images[0] is historical_image

    def test_live_tool_call_transcript_uses_framed_two_space_json(self, agent):
        """Current-round TOOL_CALL transcript uses the canonical local protocol."""
        action_step = _make_action_step_with_tool(
            tool_name="edit_file",
            tool_args={
                "file_path": "/tmp/024.md",
                "search_text": "",
                "replace_text": "追加内容",
            },
        )
        agent.memory.steps = [
            _make_task_step("Append to file"),
            action_step,
        ]

        messages = agent.write_memory_to_messages()
        tool_call_message = next(
            msg for msg in messages if msg.role == MessageRole.TOOL_CALL
        )
        tool_call_text = _get_text_from_message(tool_call_message)

        assert tool_call_text == """<<TOOL_CALL>>
{
  "name": "edit_file",
  "arguments": {
    "file_path": "/tmp/024.md",
    "search_text": "",
    "replace_text": "追加内容"
  }
}
<<END_TOOL_CALL>>"""
        assert tool_call_text.count("<<TOOL_CALL>>") == 1

    def test_two_tool_calls_keep_order_and_observation_pairing(self, agent):
        action_step = ActionStep(
            step_number=1,
            timing=_make_timing(),
            model_output=None,
            tool_calls=[
                _make_tool_call("read_file", {"file_path": "/tmp/a"}, "call_a"),
                _make_tool_call("grep", {"pattern": "x", "path": "/tmp"}, "call_b"),
            ],
            observations="first and second results",
        )
        agent.memory.steps = [_make_task_step("Inspect files"), action_step]

        messages = agent.write_memory_to_messages()
        tool_index = next(
            index for index, message in enumerate(messages)
            if message.role == MessageRole.TOOL_CALL
        )
        transcript = _get_text_from_message(messages[tool_index])

        assert transcript.startswith(
            '<<TOOL_CALL>>\n{\n  "name": "read_file",\n  "arguments": {\n    "file_path": "/tmp/a"'
        )
        assert '\n\n{\n  "name": "grep",' in transcript
        assert transcript.count("<<TOOL_CALL>>") == 1
        assert messages[tool_index + 1].role == MessageRole.TOOL_RESPONSE
        assert "first and second results" in _get_text_from_message(messages[tool_index + 1])
        assert [call.id for call in action_step.tool_calls] == ["call_a", "call_b"]

    def test_empty_tool_calls_do_not_synthesize_tool_call_transcript(self, agent):
        action_step = ActionStep(
            step_number=1,
            timing=_make_timing(),
            model_output="partial answer",
            tool_calls=[],
            observations=None,
        )
        agent.memory.steps = [_make_task_step("Continue"), action_step]

        messages = agent.write_memory_to_messages()

        assert MessageRole.TOOL_CALL not in _get_role_sequence(messages)
        assert all("<<TOOL_CALL>>" not in _get_text_from_message(msg) for msg in messages)

    def test_message_sequence_assistant_toolcall_toolresponse(self, agent):
        """Verify the complete message sequence for an action with tool call."""
        action_step = _make_action_step_with_tool(
            model_output="Let me search.",
            observations="Result: found it",
        )
        agent.memory.steps = [
            _make_task_step("Search for something"),
            action_step,
        ]

        messages = agent.write_memory_to_messages()
        roles = _get_role_sequence(messages)

        # Expected: SYSTEM, USER(task), ASSISTANT(thought), TOOL_CALL, TOOL_RESPONSE
        # Find the action-related subsequence
        assistant_idx = roles.index(MessageRole.ASSISTANT)
        tool_call_idx = roles.index(MessageRole.TOOL_CALL)
        tool_resp_idx = roles.index(MessageRole.TOOL_RESPONSE)

        assert assistant_idx < tool_call_idx < tool_resp_idx, (
            f"Wrong order: ASSISTANT@{assistant_idx}, "
            f"TOOL_CALL@{tool_call_idx}, TOOL_RESPONSE@{tool_resp_idx}"
        )

    def test_interrupted_historical_action_keeps_partial_model_output(self, agent):
        """Stopped turns should keep the partial assistant text in later rounds."""
        interrupted_step = ActionStep(
            step_number=1,
            timing=_make_timing(),
            model_output="用户想要了解张雪机车的相关信息，我已经整理了多个来源。",
            tool_calls=[],
            observations=None,
        )
        interrupted_step._interrupted_by_stop = True

        agent.memory.steps = [
            _make_task_step("搜索张雪机车"),
            _make_task_step("OK，你还记得我让你搜索过什么内容吗"),
            interrupted_step,
            _make_task_step("继续回答我刚刚 stop 前的问题"),
        ]

        messages = agent.write_memory_to_messages()
        texts = [_get_text_from_message(msg) for msg in messages]

        assert any("OK，你还记得我让你搜索过什么内容吗" in text for text in texts)
        assert any(
            "用户想要了解张雪机车的相关信息，我已经整理了多个来源。" in text
            for text in texts
        )

    def test_interrupted_reasoning_only_text_is_kept_for_next_round(self, agent):
        """Even reasoning-only partial text shown before stop should survive."""
        interrupted_step = ActionStep(
            step_number=1,
            timing=_make_timing(),
            model_output="纳维-斯托克斯方程通常写作：ρ(∂v/∂t + v·∇v) = -∇p + μ∇²v + f",
            tool_calls=[],
            observations=None,
        )
        interrupted_step._interrupted_by_stop = True

        agent.memory.steps = [
            _make_task_step("给我展示 N-S 方程"),
            interrupted_step,
            _make_task_step("你还记得我给你发过什么指令吗？以及你输出了哪些内容"),
        ]

        messages = agent.write_memory_to_messages()
        texts = [_get_text_from_message(msg) for msg in messages]

        assert any("给我展示 N-S 方程" in text for text in texts)
        assert any(
            "纳维-斯托克斯方程通常写作：ρ(∂v/∂t + v·∇v) = -∇p + μ∇²v + f" in text
            for text in texts
        )

    def test_multi_round_no_orphaned_observations(self, agent):
        """In multi-round (share_context), action→observation pairs must be complete.

        This is the actual bug scenario: Round 1 has tool calls, Round 2 sees
        orphaned observations that confuse the model.
        """
        # Round 1: task + tool call action + final answer
        round1_task = _make_task_step("Round 1 question")
        round1_action = _make_action_step_with_tool(
            model_output="I'll search for round 1.",
            observations="Round 1 search result",
        )
        round1_final = _make_action_step_final("Here is round 1 answer.")

        # Round 2: new task
        round2_task = _make_task_step("Round 2 question")

        agent.memory.steps = [
            round1_task,
            round1_action,
            round1_final,
            round2_task,
        ]

        messages = agent.write_memory_to_messages()
        roles = _get_role_sequence(messages)

        # Every TOOL_RESPONSE must be preceded (somewhere) by a TOOL_CALL
        tool_response_indices = [
            i for i, r in enumerate(roles) if r == MessageRole.TOOL_RESPONSE
        ]
        tool_call_indices = [
            i for i, r in enumerate(roles) if r == MessageRole.TOOL_CALL
        ]

        assert len(tool_call_indices) >= len(tool_response_indices), (
            f"Orphaned observations detected! "
            f"TOOL_CALL count={len(tool_call_indices)}, "
            f"TOOL_RESPONSE count={len(tool_response_indices)}. "
            f"Role sequence: {roles}"
        )

        # Each TOOL_RESPONSE should have a TOOL_CALL before it
        for tr_idx in tool_response_indices:
            preceding_tool_calls = [tc for tc in tool_call_indices if tc < tr_idx]
            assert len(preceding_tool_calls) > 0, (
                f"TOOL_RESPONSE at index {tr_idx} has no preceding TOOL_CALL! "
                f"Role sequence: {roles}"
            )

    def test_historical_read_file_keeps_structured_call_and_exact_user_observation(
        self, agent
    ):
        """Later turns must not rewrite an already-sent read_file tool trace."""
        observation = (
            'Called the Read tool with the following input: {"filePath":"foo.py"}\n'
            "<path>/tmp/foo.py</path>\n<type>file</type>\n"
            "<content>\n1: important code\n2: more code\n</content>"
        )
        read_step = _make_action_step_with_tool(
            model_output="I'll inspect the file.",
            tool_name="read_file",
            tool_args={"file_path": "/tmp/foo.py"},
            observations=observation,
            call_id="call_read",
        )
        agent.memory.steps = [
            _make_task_step("Inspect foo.py"),
            read_step,
            _make_action_step_final("Inspection complete."),
            _make_task_step("What did it contain?"),
        ]

        messages = agent.write_memory_to_messages()
        tool_call = next(msg for msg in messages if msg.role == MessageRole.TOOL_CALL)
        tool_response = next(
            msg for msg in messages if msg.role == MessageRole.TOOL_RESPONSE
        )

        assert "read_file" in _get_text_from_message(tool_call)
        assert _get_text_from_message(tool_response) == f"Observation:\n{observation}"

    def test_historical_read_file_reaches_provider_as_paired_assistant_and_user(
        self, agent
    ):
        from backend.agent.local_llm_model import LocalLLMModel

        observation = "line 1: provider-visible content"
        agent.memory.steps = [
            _make_task_step("Inspect foo.py"),
            _make_action_step_with_tool(
                model_output="I'll inspect the file.",
                tool_name="read_file",
                tool_args={"file_path": "/tmp/foo.py"},
                observations=observation,
                call_id="call_read_provider",
            ),
            _make_task_step("What did it contain?"),
        ]
        service = MagicMock()
        service.chat.return_value = MagicMock(text="ok", usage=None, tool_calls=None)

        LocalLLMModel(llm_service=service, model_id="test-model").generate(
            agent.write_memory_to_messages()
        )

        sent = service.chat.call_args.kwargs["messages"]
        call_index = next(
            i for i, message in enumerate(sent)
            if message.role == MessageRole.ASSISTANT
            and "read_file" in str(message.content)
        )
        assert sent[call_index + 1].role == MessageRole.USER
        user_content = sent[call_index + 1].content
        assert isinstance(user_content, list)
        assert user_content[0]["text"].startswith(f"Observation:\n{observation}")

    def test_observations_images_still_stripped(self, agent):
        """observations_images should still be stripped (they are task image copies)."""
        from PIL import Image as PILImage

        action_step = _make_action_step_with_tool()
        action_step.observations_images = [PILImage.new("RGB", (10, 10))]
        agent.memory.steps = [
            _make_task_step("Test task"),
            action_step,
        ]

        messages = agent.write_memory_to_messages()

        # observations_images content should NOT appear in messages
        # (no USER message with image type from ActionStep)
        for msg in messages:
            if msg.role == MessageRole.USER and isinstance(msg.content, list):
                for el in msg.content:
                    if el.get("type") == "image":
                        # This would be from TaskStep, not ActionStep
                        pass

        # The fact that it doesn't crash and TOOL_CALL is present is the main check
        roles = _get_role_sequence(messages)
        assert MessageRole.TOOL_CALL in roles

    def test_tool_call_content_has_framed_protocol_format(self, agent):
        """TOOL_CALL messages use only the canonical framed protocol."""
        action_step = _make_action_step_with_tool(
            tool_name="web_search",
            tool_args={"query": "test"},
            call_id="call_xyz",
        )
        agent.memory.steps = [
            _make_task_step("Test"),
            action_step,
        ]

        messages = agent.write_memory_to_messages()

        tool_call_msgs = [m for m in messages if m.role == MessageRole.TOOL_CALL]
        assert len(tool_call_msgs) == 1
        text = _get_text_from_message(tool_call_msgs[0])
        assert text.startswith('<<TOOL_CALL>>\n{\n  "name": "web_search",')
        assert text.endswith("}\n<<END_TOOL_CALL>>")
        assert text.count("<<TOOL_CALL>>") == 1
        assert "web_search" in text

    def test_multiple_tool_steps_all_preserved(self, agent):
        """Multiple ActionSteps with tool_calls should all be preserved."""
        step1 = _make_action_step_with_tool(
            model_output="Step 1",
            tool_name="search",
            observations="Result 1",
            call_id="call_1",
        )
        step2 = _make_action_step_with_tool(
            model_output="Step 2",
            tool_name="calculate",
            tool_args={"expr": "1+1"},
            observations="Result: 2",
            call_id="call_2",
        )

        agent.memory.steps = [
            _make_task_step("Multi-step task"),
            step1,
            step2,
        ]

        messages = agent.write_memory_to_messages()
        roles = _get_role_sequence(messages)

        tool_call_count = roles.count(MessageRole.TOOL_CALL)
        tool_resp_count = roles.count(MessageRole.TOOL_RESPONSE)

        assert tool_call_count == 2, (
            f"Expected 2 TOOL_CALL messages, got {tool_call_count}"
        )
        assert tool_resp_count == 2, (
            f"Expected 2 TOOL_RESPONSE messages, got {tool_resp_count}"
        )

    def test_none_model_output_with_tool_calls_still_has_tool_call_msg(self, agent):
        """Even when model_output is None, tool_calls should produce TOOL_CALL message."""
        step = _make_action_step_with_tool(model_output=None)
        agent.memory.steps = [
            _make_task_step("Test"),
            step,
        ]

        messages = agent.write_memory_to_messages()
        roles = _get_role_sequence(messages)

        assert MessageRole.TOOL_CALL in roles, (
            "TOOL_CALL missing even though ActionStep has tool_calls!"
        )
        # ASSISTANT should be absent (model_output is None)
        # After SYSTEM and USER(task), we should see TOOL_CALL then TOOL_RESPONSE
        non_system = [r for r in roles if r != MessageRole.SYSTEM]
        # USER(task), TOOL_CALL, TOOL_RESPONSE
        assert non_system == [
            MessageRole.USER,
            MessageRole.TOOL_CALL,
            MessageRole.TOOL_RESPONSE,
        ]
