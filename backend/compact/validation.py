from __future__ import annotations

from dataclasses import dataclass, field

from ..config import CompactConfig


REQUIRED_SECTIONS = (
    "Primary Request and Intent",
    "Key Technical Concepts",
    "Files and Code Sections",
    "Errors and fixes",
    "Problem Solving",
    "All user messages",
    "Pending Tasks",
    "Current Work",
    "Optional Next Step",
)

ROUTING_OR_TOOL_MARKERS = (
    "<<TOOL_CALL>>",
    "<<END_TOOL_CALL>>",
    "<<SWITCH_AGENT>>",
    "<<<TOOL_CALL>>>",
    "<<<END_TOOL_CALL>>>",
    "<<<SWITCH_AGENT>>>",
    "<action>switch_agent</action>",
    "<target>coordinator_2</target>",
    "<target>basic_agent</target>",
    "Calling tools:",
    '"type":"function"',
    '"type": "function"',
    '"function":{"name"',
    '"function": {"name"',
)


@dataclass(frozen=True)
class CompactValidationResult:
    ok: bool
    reason: str | None = None
    details: dict = field(default_factory=dict)


def validate_compact_summary(
    cleaned_summary: str,
    prompt_chars: int,
    config: CompactConfig,
) -> CompactValidationResult:
    text = (cleaned_summary or "").strip()
    if not text:
        return CompactValidationResult(False, "empty_summary")

    marker_source = text.replace(" ", "")
    if any(marker in text or marker in marker_source for marker in ROUTING_OR_TOOL_MARKERS):
        return CompactValidationResult(False, "routing_or_tool_output")

    min_chars = max(
        int(config.validation_min_summary_chars),
        int(prompt_chars * float(config.validation_min_summary_prompt_ratio)),
    )
    if len(text) < min_chars:
        return CompactValidationResult(
            False,
            "too_short",
            {"summary_chars": len(text), "min_chars": min_chars},
        )

    if config.validation_require_summary_sections:
        missing = [section for section in REQUIRED_SECTIONS if section not in text]
        if missing:
            return CompactValidationResult(
                False,
                "missing_summary_sections",
                {"missing_sections": missing},
            )

    return CompactValidationResult(True)
