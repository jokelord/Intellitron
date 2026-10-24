"""Post-processing for raw LLM compact output.

Strips the <analysis> draft block (model's scratchpad) and unwraps the
<summary> tags into a 'Summary:' labeled plain-text body. Borrowed from
ref_src/services/compact/prompt.ts formatCompactSummary().
"""
from __future__ import annotations

import re

ANALYSIS_BLOCK_RE = re.compile(
    r"<analysis>.*?</analysis>\s*", re.DOTALL | re.IGNORECASE
)
SUMMARY_OPEN_RE = re.compile(r"<summary>\s*", re.IGNORECASE)
SUMMARY_CLOSE_RE = re.compile(r"\s*</summary>", re.IGNORECASE)
INTERNAL_COMPACT_CONTROL_LINE_RE = re.compile(
    r"(?:^\s*(?:(?:[-*]|\d+\.)\s*)?(?:[\"'“”])?\s*(?:user\s*:\s*)?(?:\[\[_*)?LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL|Internal control message - excluded)",
    re.IGNORECASE,
)
INTERNAL_SYSTEM_INSTRUCTION_LINE_RE = re.compile(
    r"^\s*(?:(?:[-*]|\d+\.)\s*)?(?:[\"'“”])?\s*(?:Internal\s+System\s+Instruction|System\s+Instruction\s*\(\s*Internal\s*\))\s*:\s*[\"'“”]?.*(?:internal\s+server/system\s+instruction|Do\s+only\s+context\s+compaction|Context\s+compaction\s+instruction|Excluded\s+per\s+instructions|excluded\s+per\s+instructions)",
    re.IGNORECASE,
)
SUMMARY_SECTION_HEADER_RE = re.compile(
    r"^\s*(?:\d+\.\s*)?(Primary Request and Intent|Key Technical Concepts|Files and Code Sections|Errors and fixes|Problem Solving|All user messages|Pending Tasks|Current Work|Optional Next Step)\s*:?.*$",
    re.IGNORECASE,
)


def _remove_internal_compact_control_lines(text: str) -> str:
    if not text:
        return text
    lines: list[str] = []
    in_all_user_messages = False
    for line in text.splitlines():
        match = SUMMARY_SECTION_HEADER_RE.match(line)
        if match:
            in_all_user_messages = match.group(1).lower() == "all user messages"
            lines.append(line)
            continue
        if in_all_user_messages:
            if INTERNAL_COMPACT_CONTROL_LINE_RE.search(line):
                continue
            if INTERNAL_SYSTEM_INSTRUCTION_LINE_RE.search(line):
                continue
        lines.append(line)
    return "\n".join(lines).strip()


def format_compact_summary(raw: str) -> str:
    if not raw:
        return ""
    cleaned = ANALYSIS_BLOCK_RE.sub("", raw).strip()
    cleaned = SUMMARY_OPEN_RE.sub("Summary:\n", cleaned)
    cleaned = SUMMARY_CLOSE_RE.sub("", cleaned).strip()
    cleaned = _remove_internal_compact_control_lines(cleaned)
    return cleaned
