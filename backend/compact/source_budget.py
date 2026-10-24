from __future__ import annotations

from typing import Iterable, Protocol

from ..config import CompactionPromptsConfig
from ..llm_service import Message
from .tokenizer import TokenCounter


class MessageCounter(Protocol):
    def count_messages(self, messages, image_count: int = 0) -> int: ...


def _is_prior_compact_summary(message: Message, prompts: CompactionPromptsConfig) -> bool:
    if message.role != "user" or not isinstance(message.content, str):
        return False
    preface = (prompts.summary_envelope_preface or "").strip()
    return bool(preface and message.content.strip().startswith(preface))


def _count(counter: MessageCounter, messages: list[Message]) -> int:
    return int(counter.count_messages(messages))


def _is_tool_call_message(message: Message) -> bool:
    if message.role != "assistant" or not isinstance(message.content, str):
        return False
    content = message.content.lstrip()
    if content.startswith(("<<TOOL_CALL>>", "<<<TOOL_CALL>>>")):
        return True
    # Immutable historical traces may still contain retired triple-angle or
    # Calling-tools openers. This read-only predicate only preserves
    # action/observation pairing; it never parses, repairs, routes, or executes.
    return content.startswith("Calling tools:")


def _is_observation_message(message: Message) -> bool:
    return message.role == "user" and isinstance(message.content, str) and message.content.lstrip().startswith("Observation")


def _restore_tool_observation_markers(
    original: list[Message],
    trimmed: list[Message],
    *,
    max_tokens: int,
    counter: MessageCounter,
) -> list[Message]:
    positions = {id(message): index for index, message in enumerate(original)}
    selected_ids = {id(message) for message in trimmed}

    without_orphans: list[Message] = []
    for message in trimmed:
        position = positions.get(id(message))
        if (
            position is not None
            and position > 0
            and _is_observation_message(message)
            and _is_tool_call_message(original[position - 1])
            and id(original[position - 1]) not in selected_ids
        ):
            continue
        without_orphans.append(message)

    result: list[Message] = []
    marker = Message(role="user", content="Observation omitted due compact source budget.")

    for index, message in enumerate(without_orphans):
        if result:
            previous = result[-1]
            previous_pos = positions.get(id(previous))
            current_pos = positions.get(id(message))
            search_end = current_pos if current_pos is not None else len(original)
            if (
                previous_pos is not None
                and search_end > previous_pos + 1
                and _is_tool_call_message(previous)
                and any(
                    _is_observation_message(skipped) and id(skipped) not in selected_ids
                    for skipped in original[previous_pos + 1 : search_end]
                )
            ):
                candidate = [*result, marker, message, *without_orphans[index + 1 :]]
                if _count(counter, candidate) <= max_tokens:
                    result.append(marker)
                else:
                    result.pop()
        result.append(message)

    if result:
        previous = result[-1]
        previous_pos = positions.get(id(previous))
        if previous_pos is not None and _is_tool_call_message(previous) and any(
            _is_observation_message(skipped) and id(skipped) not in selected_ids
            for skipped in original[previous_pos + 1 :]
        ):
            candidate = [*result, marker]
            if _count(counter, candidate) <= max_tokens:
                result.append(marker)
            else:
                result.pop()

    return result


def trim_messages_to_token_budget(
    messages: Iterable[Message],
    *,
    max_tokens: int | None,
    prompts: CompactionPromptsConfig,
    counter: MessageCounter | None = None,
) -> list[Message]:
    """Bound compact source size while preserving the durable context core.

    Auto compact must not send an unbounded 40k+ prompt when prefix cache misses.
    Keep the system prompt, prior compact summaries, and the newest tail turns.
    """

    items = list(messages)
    if not items or not max_tokens or max_tokens <= 0:
        return items

    counter = counter or TokenCounter.get()
    if _count(counter, items) <= max_tokens:
        return items

    system: list[Message] = []
    body = items
    if items and items[0].role == "system":
        system = [items[0]]
        body = items[1:]

    summaries = [message for message in body if _is_prior_compact_summary(message, prompts)]
    while summaries and _count(counter, [*system, *summaries]) > max_tokens:
        summaries.pop(0)
    if system and _count(counter, system) > max_tokens:
        system = []
    summary_ids = {id(message) for message in summaries}
    tail_candidates = [message for message in body if id(message) not in summary_ids]

    selected_tail: list[Message] = []
    for message in reversed(tail_candidates):
        candidate = [*system, *summaries, message, *selected_tail]
        if _count(counter, candidate) > max_tokens:
            if not _is_observation_message(message):
                break
            continue
        selected_tail.insert(0, message)

    trimmed = [*system, *summaries, *selected_tail]
    return _restore_tool_observation_markers(items, trimmed, max_tokens=max_tokens, counter=counter)
