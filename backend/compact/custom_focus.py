from __future__ import annotations

from typing import Any


def _text_from_content(content: Any) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in {"text", "input_text"}:
                continue
            value = item.get("text")
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        text = "\n".join(parts).strip()
        return text or None
    text = str(content).strip()
    return text or None


def build_next_turn_focus_instruction(next_turn_user_message: Any) -> str | None:
    text = _text_from_content(next_turn_user_message)
    if not text:
        return None
    return (
        "After compaction, the user's next task will be the instruction below. "
        "When writing the summary, prioritize preserving prior details that are relevant "
        "to that upcoming task. Do not answer or execute the task, and do not treat this "
        "internal focus note as conversation history or as part of the conversation being "
        "summarized. Use it only to decide which historical information needs extra detail.\n\n"
        "Upcoming user instruction:\n"
        f"{text}"
    )
