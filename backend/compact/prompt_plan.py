from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..config import CompactionPromptsConfig
from ..llm_service import Message
from .prompt_builder import build_compact_messages_v2
from .prompt_control import build_compact_control_instruction


CompactPromptShape = Literal["reliable_position1", "cache_first_tail"]


@dataclass(frozen=True)
class CompactPromptPlan:
    shape: CompactPromptShape
    messages: list[Message]
    stream_visible: bool
    metadata: dict | None = None
    tools: list[dict] | None = None


def build_compact_system(prompts: CompactionPromptsConfig) -> str:
    return "\n\n".join(
        part.rstrip()
        for part in (
            prompts.no_tools_preamble or "",
            prompts.main_prompt or "",
            prompts.no_tools_trailer or "",
        )
        if part is not None
    )


def build_reliable_source_with_compact_system(
    source_messages: list[Message],
    prompts: CompactionPromptsConfig,
) -> list[Message]:
    compact_system = build_compact_system(prompts)
    if not source_messages:
        return [Message(role="system", content=compact_system)]
    tail = source_messages[1:] if source_messages[0].role == "system" else list(source_messages)
    return [Message(role="system", content=compact_system), *tail]


def build_reliable_position1_plan(
    reliable_source_messages: list[Message],
    prompts: CompactionPromptsConfig,
    custom_instructions: str | None = None,
) -> CompactPromptPlan:
    return CompactPromptPlan(
        shape="reliable_position1",
        messages=build_compact_messages_v2(
            reliable_source_messages,
            prompts,
            custom_instructions=custom_instructions,
        ),
        stream_visible=True,
    )


def build_cache_first_tail_plan(
    source_messages: list[Message],
    prompts: CompactionPromptsConfig,
    custom_instructions: str | None = None,
    tools: list[dict] | None = None,
) -> CompactPromptPlan:
    if not source_messages:
        raise ValueError("cache-first compact requires source_messages")
    if source_messages[0].role != "system":
        raise ValueError("cache-first compact requires a system message at index 0")
    instruction = build_compact_control_instruction(prompts, custom_instructions)
    return CompactPromptPlan(
        shape="cache_first_tail",
        messages=[*source_messages, Message(role="user", content=instruction)],
        stream_visible=False,
        tools=tools,
    )
