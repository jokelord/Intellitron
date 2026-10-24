"""Regression boundaries after removal of legacy textual tool parsing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from smolagents.models import ChatMessage, MessageRole

from backend.agent.local_llm_model import LocalLLMModel
from backend.agent.text_tool_protocol import TextToolProtocolError


@pytest.mark.parametrize("opener", ["Calling tools:", "调用工具:"])
def test_exact_legacy_transcript_opener_is_rejected_not_parsed(opener):
    model = LocalLLMModel(llm_service=MagicMock())

    with pytest.raises(TextToolProtocolError) as exc_info:
        model._resolve_assistant_control(f"{opener}\n[]", None, [])

    assert exc_info.value.code == "unsupported_legacy_protocol"


def test_reasoning_control_markers_do_not_enter_visible_protocol_parser():
    reasoning = "<<TOOL_CALL>>\n{\"name\":\"read_file\",\"arguments\":{}}\n<<END_TOOL_CALL>>"
    service = MagicMock()
    service.chat.return_value = iter(
        [
            {"reasoning": reasoning},
            {"content": "ordinary visible answer"},
        ]
    )
    model = LocalLLMModel(llm_service=service)

    deltas = list(
        model.generate_stream(
            [ChatMessage(role=MessageRole.USER, content="answer")],
            request_id="reasoning-boundary",
        )
    )

    assert [delta.content for delta in deltas] == [
        reasoning,
        "ordinary visible answer",
    ]
    assert deltas[0].reasoning is True
    assert deltas[1].reasoning is False
    assert model.get_stream_state("reasoning-boundary") == {
        "reasoning": reasoning,
        "content": "ordinary visible answer",
    }
