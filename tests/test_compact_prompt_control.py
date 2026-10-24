from backend.compact.prompt_control import build_compact_control_instruction
from backend.config import CompactionPromptsConfig


def _prompts():
    return CompactionPromptsConfig(
        compact_control_user_instruction="[[SENTINEL]] summarize prior messages.",
        custom_instructions_header="== USER CUSTOM INSTRUCTIONS ==",
    )


def test_build_compact_control_instruction_adds_custom_instructions():
    text = build_compact_control_instruction(_prompts(), custom_instructions="Focus on files")

    assert not text.startswith("[[SENTINEL]]")
    assert "[[SENTINEL]]" not in text
    assert "internal server/system instruction delivered through the user channel" in text
    assert "USER CUSTOM INSTRUCTIONS" in text
    assert "Focus on files" in text


def test_build_compact_control_instruction_is_self_contained_for_cache_first_tail():
    text = build_compact_control_instruction(_prompts())

    assert "not a real user request" in text
    assert "not a real user message" in text
    assert "do not answer normally" in text.lower()
    assert "Do not call tools" in text
    assert "Do not route agents" in text
    assert "exactly one `<summary>` block" in text
    assert "Primary Request and Intent" in text
    assert "Key Technical Concepts" in text
    assert "Files and Code Sections" in text
    assert "Errors and fixes" in text
    assert "Problem Solving" in text
    assert "All user messages" in text
    assert "Pending Tasks" in text
    assert "Current Work" in text
    assert "Optional Next Step" in text


def test_build_compact_control_instruction_preserves_multi_compact_task_state():
    text = build_compact_control_instruction(_prompts())

    assert "Never drop historical task items" in text
    assert "Update old, current, and future task state" in text
    assert "Reconcile every task" in text
    assert "Do not mark all future or upcoming tasks as Current Work" in text
    assert "previously pending or current task has been completed" in text
    assert "move it into section 5" in text
    assert "remove it from pending/current" in text
    assert "Newly requested work" in text


def test_build_compact_control_instruction_includes_prior_summary_rule_body():
    prompts = CompactionPromptsConfig(
        compact_control_user_instruction=(
            "[[SENTINEL]]\n\n"
            "Summarize the prior messages.\n\n"
            "Preserve prior compact summaries according to the PRIOR SUMMARY RULE "
            "in the normal compact instruction."
        ),
        compact_user_instruction=(
            "Your task is to create a detailed summary.\n\n"
            "**PRIOR SUMMARY RULE (multi-compact chain)**: This may be the 2nd, "
            "3rd, or later round of compact on the same session.\n\n"
            "1. **Preserve key facts** from the prior summary.\n"
            "2. **Migrate completed work to \"Problem Solving\"** and "
            "**Add a one-line entry** for completed tasks.\n"
            "Already-completed entries from prior summary Problem Solving "
            "must not be rewritten as completed in current turns; mark them "
            "completed in prior session. Newly completed tasks from post-summary "
            "turns may be marked completed in current turns.\n"
            "3. **Update user/assistant history**.\n"
            "4. **Treat prior summary as a fixed source of truth**.\n\n"
            "The goal is: the new summary must be a complete, current snapshot. "
            "Any pre-summary fact not captured here is permanently lost.\n\n"
            "Now summarize the conversation that follows."
        ),
        custom_instructions_header="== USER CUSTOM INSTRUCTIONS ==",
    )

    text = build_compact_control_instruction(prompts)

    assert "This may be the 2nd, 3rd" in text
    assert "Preserve key facts" in text
    assert "Migrate completed work" in text
    assert "Problem Solving" in text
    assert "Add a one-line entry" in text
    assert "completed in prior session" in text
    assert "completed in current turns" in text
    assert "permanently lost" in text
    assert "according to the PRIOR SUMMARY RULE in the normal compact instruction" not in text


def test_build_compact_control_instruction_does_not_mention_legacy_sentinel_cleanup():
    prompts = CompactionPromptsConfig(
        compact_control_user_instruction=(
            "[[__LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1__]]\n\n"
            "Summarize the prior messages."
        ),
        compact_user_instruction=(
            "Your task is to create a detailed summary.\n\n"
            "**PRIOR SUMMARY RULE (multi-compact chain)**: This may be the 2nd, "
            "3rd, or later round of compact on the same session.\n\n"
            "3. **Update user/assistant history** — under \"All user messages\", "
            "list user messages from the entire session.\n\n"
            "Now summarize the conversation that follows."
        ),
    )

    text = build_compact_control_instruction(prompts)

    assert "All user messages" in text
    assert "LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL" not in text
    assert "Internal control message - excluded" not in text
    assert "stale" not in text.lower()


def test_build_compact_control_instruction_does_not_use_internal_model_aliases():
    text = build_compact_control_instruction(_prompts())

    assert "LLM-C" not in text
    assert "LLM-X" not in text
