"""Chat mode compact filter.

Interprets per-message metadata markers sent by the frontend:
- metadata.pre_compact: True   -> drop the message
- metadata.compact_summary: True + summary_id -> replace with envelope user message

This is the single source of truth for what the LLM sees in chat mode.
The frontend does NOT filter its own messages.
"""
from __future__ import annotations

from typing import Any, List, Optional

from ..config import CompactionPromptsConfig


def _get_meta(msg: Any) -> dict:
    meta = getattr(msg, "metadata", None)
    if isinstance(meta, dict):
        return meta
    return {}


def _envelope_msg(template_msg: Any, content: str) -> Any:
    """Build a new message of the same type as the template, with given content.

    Falls back to a simple SimpleNamespace if the type can't be instantiated
    with just (role, content) (e.g. pydantic models with required fields).
    """
    try:
        return type(template_msg)(role="user", content=content)
    except Exception:
        from types import SimpleNamespace

        return SimpleNamespace(role="user", content=content, metadata=None)


def filter_messages_for_llm(
    messages: List[Any],
    prompts: CompactionPromptsConfig,
) -> List[Any]:
    """Apply compact boundary logic. Returns a new list of message-shaped objects."""
    summary_idx: int = -1
    for i in range(len(messages) - 1, -1, -1):
        if _get_meta(messages[i]).get("compact_summary"):
            summary_idx = i
            break

    if summary_idx == -1:
        return [m for m in messages if not _get_meta(m).get("pre_compact")]

    summary_text = getattr(messages[summary_idx], "content", "") or ""
    envelope_content = (
        prompts.summary_envelope_preface
        + summary_text
        + prompts.summary_envelope_suffix
    )
    envelope_msg = _envelope_msg(messages[summary_idx], envelope_content)

    leading_systems = [
        m
        for m in messages[:summary_idx]
        if getattr(m, "role", None) == "system"
        and not _get_meta(m).get("pre_compact")
        and not _get_meta(m).get("compact_summary")
    ]

    result: List[Any] = [*leading_systems, envelope_msg]
    for m in messages[summary_idx + 1 :]:
        if _get_meta(m).get("pre_compact"):
            continue
        result.append(m)
    return result
