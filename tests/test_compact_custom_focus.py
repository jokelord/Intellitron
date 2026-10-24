from backend.compact.custom_focus import build_next_turn_focus_instruction


def test_build_next_turn_focus_instruction_includes_exact_upcoming_user_instruction():
    text = build_next_turn_focus_instruction("Please inspect compact code")

    assert text is not None
    assert "After compaction, the user's next task" in text
    assert "Do not answer or execute the task" in text
    assert "Use it only to decide" in text
    assert "Upcoming user instruction:" in text
    assert "Please inspect compact code" in text
    assert "conversation history" in text
    assert "LLM-C" not in text
    assert "LLM-X" not in text


def test_build_next_turn_focus_instruction_returns_none_for_blank_text():
    assert build_next_turn_focus_instruction("   ") is None
    assert build_next_turn_focus_instruction(None) is None


def test_build_next_turn_focus_instruction_extracts_text_blocks_only():
    text = build_next_turn_focus_instruction(
        [
            {"type": "text", "text": "First text"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            {"type": "input_text", "text": "Second text"},
            {"type": "input_image", "image_url": "data:image/png;base64,BBB"},
        ]
    )

    assert text is not None
    assert "First text" in text
    assert "Second text" in text
    assert "base64" not in text
    assert "image/png" not in text
