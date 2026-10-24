"""Construct the compact LLM prompt from yaml templates and the body messages.

The v1 (build_compact_messages) builder returns a 2-message list: a system
message containing the preamble, the main prompt (with optional custom
instructions), and the trailer; plus a user message whose content IS the
conversation to summarize.

The v2 (build_compact_messages_v2) builder takes the caller's full messages
list (含 system) and inserts the compact instruction as a NEW user message at
position 1 (right after system), with body_messages[1..] appended unchanged
after. This is the "position-1 amendment" (2026-06-11) to the original v2
turn-shape design.

Why position 1 and not the tail (which the original v2 design used):
  At 40k+ token context, the model's effective attention at the tail is ~0
  (lost-in-the-middle effect), and the model gets dragged into the user's
  language. E2E test with a Chinese user payload produced a Chinese free-form
  `### / ####` summary, completely ignoring the YAML main_prompt's
  <analysis> + 9-section <summary> English format. Putting the instruction
  at the beginning treats it as the user's "opening question" and the
  conversation that follows becomes the "data to apply the format to".

Trade-off vs. byte-equal ideal:
  v2 spec's original byte-equal ideal (instruction at tail, body_messages
  1:1 unchanged) is no longer strictly byte-equal — body_messages[0]
  (system) remains byte-equal at index 0, but a new user message is
  inserted at index 1 and body_messages[1..] is appended after. In
  practice SGLang does not share KV across distinct requests, so the
  byte-equal property was already a no-op in production; we trade
  theoretical purity for actual model compliance.

The split [system: instructions, user: conversation] still matters because
Qwen3's chat template
(~/Project/LLM-weight/.../chat_template.jinja) has a multi_step_tool
check (line 79) that raises 'No user query found in messages.' if every
user message in the input looks like a <tool_response>...</tool_response>
envelope. The v1 (build_compact_messages) original 2-message design
[system, user] is the natural chat-template-aligned shape.
"""
from __future__ import annotations

import json
from typing import Any, List, Optional

from ..config import CompactionPromptsConfig
from ..llm_service import Message


def build_compact_messages(
    body_messages: List[Any],
    prompts: CompactionPromptsConfig,
    custom_instructions: Optional[str] = None,
) -> List[dict]:
    """Compact LLM input builder (v1, body-only, packed into one user message).

    Deprecated since 2026-06-11; use build_compact_messages_v2 which takes
    a full messages list (含 system) and 1:1 reuses it for SGLang radix cache.
    Will be removed in the next release.
    """
    body_text = _render_body(body_messages)
    # System turn: instructions + format constraints + tool refusal.
    system_parts: List[str] = [prompts.no_tools_preamble, prompts.main_prompt]
    if custom_instructions and custom_instructions.strip():
        system_parts.append(prompts.custom_instructions_header)
        system_parts.append(custom_instructions.strip())
    system_parts.append(prompts.no_tools_trailer)
    system_content = "\n\n".join(p for p in system_parts if p)

    # User turn: the actual conversation to summarize. Wrapped with
    # the same "Conversation to summarize:" delimiter that the previous
    # single-system-message version used, so the resulting prompt is
    # semantically equivalent to the v1 design.
    user_content = "Conversation to summarize:\n\n" + body_text

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def _render_body(messages: List[Any]) -> str:
    lines: List[str] = []
    for m in messages:
        if isinstance(m, dict):
            role = m.get("role", "user")
            content = m.get("content", "")
        else:
            role = getattr(m, "role", "user")
            content = getattr(m, "content", "")
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)
        elif not isinstance(content, str):
            content = str(content)
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)


def build_compact_messages_v2(
    body_messages: List[Message],
    prompts: CompactionPromptsConfig,
    custom_instructions: Optional[str] = None,
) -> List[Message]:
    """Build compact LLM input in turn-shape: body_messages (含 system) with a NEW
    user message inserted at position 1 (right after system) carrying the compact
    instruction.

    Per spec §5.2 + position-1 amendment (2026-06-11, AGENTS.md trap #27):
      - body_messages must be non-empty.
      - body_messages[0].role must be "system".
      - final[0] is body_messages[0] byte-equal (system, prefix-cache anchor).
      - final[1] is a NEW user message with content = prompts.compact_user_instruction,
        optionally followed by prompts.custom_instructions_header + custom_instructions.
      - final[2:] is body_messages[1:] byte-equal (the original history turns).

    Why position 1 (not the tail as the original v2 design used):
      At 40k+ token context, the model's effective attention at the tail is ~0
      (lost-in-the-middle effect), and the model gets dragged into the user's
      language. E2E test with a Chinese user payload produced a Chinese free-form
      `### / ####` summary, completely ignoring the YAML main_prompt's
      <analysis> + 9-section <summary> English format. Putting the instruction
      at the beginning treats it as the user's "opening question" and the
      conversation that follows becomes the "data to apply the format to".

    Trade-off: v2 spec's byte-equal ideal is broken (now diverges at index 1
    in addition to a single trailing divergence), but byte-equal was already a
    no-op in SGLang (cross-request KV is not shared), so the trade costs
    nothing in practice and gains actual model compliance.
    """
    if not body_messages:
        raise ValueError("body_messages 不能为空")
    if body_messages[0].role != "system":
        raise ValueError("body_messages 第一条必须是 system 消息")

    parts: List[str] = [prompts.compact_user_instruction]
    if custom_instructions and custom_instructions.strip():
        parts.append(prompts.custom_instructions_header)
        parts.append(custom_instructions.strip())
    instruction_msg = Message(
        role="user",
        content="\n\n".join(parts),
    )

    final: List[Message] = [body_messages[0], instruction_msg] + list(body_messages[1:])
    return final
