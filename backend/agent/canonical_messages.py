"""Repository-owned, metadata-preserving tool-message boundaries.

No execution or UI projection belongs here. Arguments are objects internally;
only the OpenAI HTTP boundary serializes them as strings.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, AbstractSet, Sequence

MESSAGE_FIELDS = ("tool_calls", "tool_call_id", "name", "reasoning_content")


def _get(message: Any, field: str, default=None):
    return message.get(field, default) if isinstance(message, dict) else getattr(message, field, default)


def call_to_dict(call: Any) -> dict:
    function = _get(call, "function", None)
    name = _get(function, "name") if function is not None else _get(call, "name")
    args = _get(function, "arguments") if function is not None else _get(call, "arguments")
    if isinstance(args, str):
        args = json.loads(args)
    if not isinstance(args, dict):
        raise ValueError("Canonical tool arguments must be an object")
    return {"id": _get(call, "id"), "type": "function", "function": {"name": name, "arguments": deepcopy(args)}}


def canonicalize_model_messages(messages: Sequence[Any]) -> list:
    from backend.llm_service import Message
    result = []
    for message in messages:
        role = _get(message, "role", "user")
        role = getattr(role, "value", role)
        role = {"tool-call": "assistant", "tool_call": "assistant",
                "tool-response": "user", "tool_response": "user"}.get(role, role)
        values = {field: deepcopy(_get(message, field)) for field in MESSAGE_FIELDS}
        if values["tool_calls"] is not None:
            values["tool_calls"] = [call_to_dict(call) for call in values["tool_calls"]]
        content = deepcopy(_get(message, "content", ""))
        if content is None:
            content = ""
        result.append(Message(role=str(role), content=content, **values))
    return result


def clone_message(message: Any, *, content=None):
    clone = canonicalize_model_messages([message])[0]
    if content is not None:
        clone.content = deepcopy(content)
    return clone


def message_to_template(message: Any) -> dict:
    canonical = canonicalize_model_messages([message])[0]
    value = {"role": canonical.role, "content": canonical.content}
    for field in MESSAGE_FIELDS:
        part = getattr(canonical, field)
        if part is not None:
            value[field] = part
    return value


def message_to_wire(message: Any) -> dict:
    value = message_to_template(message)
    for call in value.get("tool_calls", ()):
        call["function"]["arguments"] = json.dumps(call["function"]["arguments"], ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return value


@dataclass(frozen=True)
class CanonicalToolResult:
    call_id: str
    name: str
    content: str | list[dict]
    status: str = "completed"


def build_switch_agent_schema(allowed_agent_names: AbstractSet[str]) -> dict:
    return {"type": "function", "function": {
        "name": "switch_agent", "description": "Switch to an allowed agent.",
        "parameters": {"type": "object", "properties": {
            "target_agent": {"type": "string", "enum": sorted(allowed_agent_names)}},
            "required": ["target_agent"], "additionalProperties": False}}}
