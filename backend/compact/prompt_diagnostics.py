from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable


_MAX_SNAPSHOT_CANONICAL_TEXT_CHARS = 4096
_DATA_IMAGE_RE = re.compile(r"data:image/[^\"'\\\s]+;base64,[A-Za-z0-9+/=]+")


@dataclass(frozen=True)
class PromptSnapshot:
    prompt_shape: str
    message_count: int
    prompt_chars: int
    content_hash: str
    canonical_text: str


@dataclass(frozen=True)
class PromptComparison:
    reference_shape: str
    candidate_shape: str
    same_hash: bool
    common_prefix_chars: int
    first_divergence_char: int | None


def _normalize_content(content: Any) -> Any:
    if isinstance(content, (str, int, float, bool)) or content is None:
        return content
    if isinstance(content, list):
        return [_normalize_content(item) for item in content]
    if isinstance(content, dict):
        return {str(k): _normalize_content(v) for k, v in sorted(content.items())}
    return str(content)


def _canonical_message_dict(message: Any) -> dict[str, Any]:
    from backend.agent.canonical_messages import message_to_template
    payload = message_to_template(message)
    payload["content"] = _normalize_content(payload["content"])
    return payload


def canonicalize_messages(messages: Iterable[Any]) -> str:
    payload = [_canonical_message_dict(m) for m in messages]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _snapshot_preview(canonical: str) -> str:
    preview = _DATA_IMAGE_RE.sub("<redacted-image-data>", canonical)
    if len(preview) <= _MAX_SNAPSHOT_CANONICAL_TEXT_CHARS:
        return preview
    suffix = "...<truncated>"
    return preview[: _MAX_SNAPSHOT_CANONICAL_TEXT_CHARS - len(suffix)] + suffix


def make_prompt_snapshot(prompt_shape: str, messages: Iterable[Any], *, tools: list[dict] | None = None) -> PromptSnapshot:
    message_list = list(messages)
    canonical = canonicalize_messages(message_list)
    if tools is not None:
        canonical = json.dumps({"tools": tools, "messages": json.loads(canonical)}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return PromptSnapshot(
        prompt_shape=prompt_shape,
        message_count=len(message_list),
        prompt_chars=len(canonical),
        content_hash=digest,
        canonical_text=_snapshot_preview(canonical),
    )


def compare_prompt_snapshots(
    reference: PromptSnapshot | None,
    candidate: PromptSnapshot,
) -> PromptComparison | None:
    if reference is None:
        return None
    common = 0
    for left, right in zip(reference.canonical_text, candidate.canonical_text):
        if left != right:
            break
        common += 1
    same_hash = reference.content_hash == candidate.content_hash
    first_divergence = None if same_hash else common
    return PromptComparison(
        reference_shape=reference.prompt_shape,
        candidate_shape=candidate.prompt_shape,
        same_hash=same_hash,
        common_prefix_chars=common,
        first_divergence_char=first_divergence,
    )


def extract_cached_tokens(usage: dict[str, Any] | None) -> int | None:
    if not isinstance(usage, dict):
        return None
    direct = usage.get("cached_tokens")
    if isinstance(direct, int):
        return direct
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
        return details["cached_tokens"]
    for key in ("cache_read_input_tokens", "cached_prompt_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            return value
    return None
