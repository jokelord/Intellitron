from __future__ import annotations

from ..config import CompactionPromptsConfig
from .validation import REQUIRED_SECTIONS


_PRIOR_SUMMARY_REFERENCE = (
    "Preserve prior compact summaries according to the PRIOR SUMMARY RULE "
    "in the normal compact instruction."
)


def _extract_prior_summary_rule(prompts: CompactionPromptsConfig) -> str | None:
    instruction = prompts.compact_user_instruction or ""
    marker = "**PRIOR SUMMARY RULE"
    start = instruction.find(marker)
    if start < 0:
        return None
    tail = instruction[start:].strip()
    end_marker = "\n\nNow summarize the conversation that follows."
    end = tail.find(end_marker)
    if end >= 0:
        tail = tail[:end].rstrip()
    return tail or None


def _strip_legacy_control_prefix(text: str) -> str:
    lines = (text or "").splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and ("LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL" in lines[0] or lines[0].strip().startswith("[[")):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def build_compact_control_instruction(
    prompts: CompactionPromptsConfig,
    custom_instructions: str | None = None,
) -> str:
    configured_instruction = _strip_legacy_control_prefix(prompts.compact_control_user_instruction)
    control_instruction = (
        "This message is an internal server/system instruction delivered through the user channel. "
        "It is not a real user message and not a real user request. Do not answer normally. Do not call tools. "
        "Do not route agents. Do only context compaction for the prior conversation messages. Do not mention, quote, "
        "paraphrase, list, or create a placeholder for this internal instruction anywhere in the summary, including "
        "section 6 \"All user messages\". Never write lines like `Internal System Instruction: ...`, "
        "`(Excluded per instructions)`, or similar placeholders.\n\n"
        "Output exactly one `<summary>` block with the 9 required sections. Do not output routing actions, "
        "tool calls, reasoning blocks, markdown headings, or any text before/after the `<summary>` block.\n\n"
        "Never drop historical task items. Reconcile every task from prior summaries and post-summary turns before "
        "writing sections 5, 7, 8, and 9. Update old, current, and future task state. If a previously pending "
        "or current task has been completed in the post-summary turns, move it into section 5 \"Problem Solving\" "
        "and remove it from pending/current task sections. Do not mark all future or upcoming tasks as Current Work: "
        "section 8 \"Current Work\" must contain only the single task actively being worked on now, while not-yet-started "
        "requested work belongs in section 7 \"Pending Tasks\" or section 9 \"Optional Next Step\". Newly requested "
        "work goes into section 8 \"Current Work\" when it is in progress, or section 7 \"Pending Tasks\" / section 9 "
        "\"Optional Next Step\" when it is future work."
    )
    if configured_instruction:
        control_instruction = f"{control_instruction}\n\n{configured_instruction}"
    prior_summary_rule = _extract_prior_summary_rule(prompts)
    if prior_summary_rule:
        control_instruction = control_instruction.replace(
            _PRIOR_SUMMARY_REFERENCE,
            "Preserve prior compact summaries according to the prior-summary rules below.",
        )
    parts = [control_instruction]
    sections = "\n".join(f"   {index}. {section}" for index, section in enumerate(REQUIRED_SECTIONS, start=1))
    parts.append(
        "CACHE-FIRST COMPACT FORMAT REQUIREMENTS:\n"
        "Your response MUST contain exactly one `<summary>` block with these 9 numbered sections in English:\n"
        f"{sections}\n"
        "Do not output routing actions, tool calls, reasoning blocks, markdown headings, or any text before/after the `<summary>` block."
    )
    parts.append(
        "INTERNAL INSTRUCTION EXCLUSION:\n"
        "Do not list internal server/system compaction instructions under section 6 \"All user messages\". "
        "Do not add placeholder lines such as \"Internal System Instruction\", \"System Instruction (Internal)\", "
        "\"Context compaction instruction\", or \"excluded per instructions\"."
    )
    if prior_summary_rule and "Migrate completed work" not in control_instruction:
        parts.append("CACHE-FIRST PRIOR SUMMARY RULES:")
        parts.append(prior_summary_rule)
    if custom_instructions and custom_instructions.strip():
        parts.append(prompts.custom_instructions_header)
        parts.append(custom_instructions.strip())
    return "\n\n".join(part for part in parts if part)
