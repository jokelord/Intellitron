"""Test that the compaction YAML config has the compact_user_instruction field
with the position-1 amendment wording (2026-06-11, AGENTS.md trap #27)
and the multi-compact PRIOR SUMMARY RULE (2026-06-12, AGENTS.md trap #29).

Per spec §5.3 + position-1 amendment:
  - The instruction is placed at position 1 (right after system) in the final
    message list, not at the tail. The instruction therefore refers to
    "the conversation that follows this message" rather than the v1/v2-old
    "conversation above" wording.
  - Wording also includes a LANGUAGE LOCK (English regardless of the
    conversation language) and a STRUCTURE LOCK (a single 9-section
    <summary>, no free-form Chinese headers or reasoning block).
  - 2026-06-12 addition: PRIOR SUMMARY RULE instructs the compact LLM to
    preserve key facts from prior compact summaries and to drop only the
    information that has become obsolete (e.g., tasks completed since the
    prior summary).
"""
from __future__ import annotations

import yaml
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "config-agent-prompts.yaml"


def test_compaction_block_has_compact_user_instruction() -> None:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    compaction = cfg.get("compaction", {})
    assert "compact_user_instruction" in compaction, (
        "compaction: 块缺少 compact_user_instruction 字段 (spec §5.3)"
    )
    text = compaction["compact_user_instruction"]
    assert isinstance(text, str)
    assert len(text) > 50, "compact_user_instruction 措辞不应太短"


def test_compact_user_instruction_anchors_to_conversation_that_follows() -> None:
    """Per the 2026-06-11 position-1 amendment, the instruction lives at
    position 1 (right after system), so the conversation-to-summarize
    is the messages that follow it. The wording must explicitly anchor
    to 'the conversation that follows this message' or equivalent."""
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    text = cfg["compaction"]["compact_user_instruction"]
    # The new wording uses "conversation that follows this message".
    assert "conversation that follows" in text, (
        "compact_user_instruction must anchor to 'the conversation that follows "
        "this message' (position-1 amendment, 2026-06-11)."
    )
    # And it must explicitly EXCLUDE the instruction message itself.
    assert "NOT including" in text or "not including" in text, (
        "compact_user_instruction must explicitly exclude itself from the "
        "conversation to summarize."
    )


# ----------------------------------------------------------------------------
# PRIOR SUMMARY RULE (2026-06-12, AGENTS.md trap #29)
#
# On the 2nd, 3rd, ... round of compact, the input contains one or more
# prior compact summaries. Without an explicit rule, the LLM is free to
# drop or rewrite info from the prior summary, which is a real lossy-on-
# lossy risk. The PRIOR SUMMARY RULE locks down the expected behavior:
#   - Preserve key facts from prior summary in 4 specific 9-section slots
#     (Key Technical Concepts / Files and Code Sections / Errors and fixes /
#     Primary Request and Intent) unless factually wrong.
#   - Drop info that has become obsolete (e.g., tasks completed since the
#     prior summary should NOT reappear under Pending/Current Work /
#     Optional Next Step).
#   - Treat prior summary as a fixed source of truth for facts that did
#     not change — stable facts must not be paraphrased in ways that lose
#     information.
# ----------------------------------------------------------------------------


def test_prior_summary_rule_clause_present() -> None:
    """The PRIOR SUMMARY RULE clause must be present in the YAML."""
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    text = cfg["compaction"]["compact_user_instruction"]
    assert "PRIOR SUMMARY RULE" in text, (
        "compact_user_instruction must contain a 'PRIOR SUMMARY RULE' clause "
        "(multi-compact chain preservation, 2026-06-12)."
    )


def test_prior_summary_rule_explains_marker() -> None:
    """The clause must tell the LLM how to identify a prior summary in the
    input (it appears as a user-role message starting with the envelope
    preface)."""
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    text = cfg["compaction"]["compact_user_instruction"]
    # The marker is the envelope preface: "This session is being continued
    # from a previous conversation that ran out of context."
    assert "This session is being continued" in text, (
        "PRIOR SUMMARY RULE must explain how to identify a prior summary in "
        "the input (it starts with the envelope preface)."
    )


def test_prior_summary_rule_mandates_preserve_facts() -> None:
    """The clause must instruct the LLM to preserve key facts in 4 specific
    9-section slots."""
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    text = cfg["compaction"]["compact_user_instruction"]
    # The 4 protected 9-section slots
    protected_slots = [
        "Key Technical Concepts",
        "Files and Code Sections",
        "Errors and fixes",
        "Primary Request and Intent",
    ]
    for slot in protected_slots:
        assert slot in text, (
            f"PRIOR SUMMARY RULE must mandate preserving key facts in "
            f"'{slot}' from prior summaries."
        )


def test_prior_summary_rule_mandates_drop_obsolete() -> None:
    """The clause must instruct the LLM to drop info that is now obsolete
    (e.g., tasks completed since the prior summary).

    2026-06-12 clarification: the rule is not just "drop" — it's
    "migrate to Problem Solving". A task that was pending in the prior
    summary and is now complete must be MOVED to section 5 "Problem Solving"
    as a one-line entry like "Refactored foo (completed in turns N-M)".
    This is the durable home for "this task is now done" across compacts,
    so the next compact knows not to re-do it.

    Pin both the "drop from Pending/Current Work" directive AND the
    "move to Problem Solving" directive.
    """
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    text = cfg["compaction"]["compact_user_instruction"]
    # Migration directives: the LLM must (a) remove from Pending/Optional Next Step
    # AND (b) add a one-line entry to Problem Solving.
    migration_keywords = ["Migrate completed work", "Problem Solving", "Add a one-line entry"]
    for kw in migration_keywords:
        assert kw in text, (
            f"PRIOR SUMMARY RULE clause 2 must instruct the LLM to migrate "
            f"completed work to 'Problem Solving' (2026-06-12 clarification). "
            f"Missing keyword: '{kw}'."
        )
    # And the obsolete/drop language must still be present (backwards compat).
    assert "completed in the turns" in text or "completed" in text, (
        "PRIOR SUMMARY RULE must still mention the obsolete-task concept."
    )


def test_prior_summary_rule_preserves_prior_problem_solving_completion_origin() -> None:
    """Completed work already in a prior summary must not become current-turn work.

    Regression: after several compacts, work completed before the latest compact
    was rewritten as "completed in current turns". The prompt must distinguish
    (a) already-completed Problem Solving entries from the prior summary and
    (b) tasks newly completed after that prior summary.
    """
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    text = cfg["compaction"]["compact_user_instruction"]

    assert "Problem Solving" in text
    assert "completed in prior session" in text
    assert "completed in current turn" in text or "completed in current turns" in text
    assert "must not be rewritten" in text or "do not rewrite" in text.lower()


def test_prior_summary_rule_clause2_migrates_to_problem_solving() -> None:
    """Sub-test: clause 2's migrate-to-Problem-Solving directive must:
    (a) name "Problem Solving" as the destination
    (b) require a one-line entry per completed task
    (c) instruct NOT to leave the task under "Current Work" or "Optional Next Step"
    """
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    text = cfg["compaction"]["compact_user_instruction"]
    # (a) "Problem Solving" is the destination
    assert "Problem Solving" in text, (
        "clause 2 must name 'Problem Solving' as the destination for completed tasks."
    )
    # (b) one-line entry per task
    assert "one-line" in text.lower() or "one line" in text.lower(), (
        "clause 2 must require a one-line entry per completed task (not a full transcript)."
    )
    # (c) Current Work reflects the *next* task, not the just-completed one
    # (the LLM should update Current Work, not leave the just-completed task there)
    assert "reflect the" in text.lower() and "next" in text.lower(), (
        "clause 2 must instruct the LLM to update 'Current Work' to the *next* in-progress task."
    )


def test_prior_summary_rule_explains_permanence() -> None:
    """The clause must make clear that this is the *only* place where
    pre-summary context is preserved — once the new summary is written,
    the next continuation will see only this new summary."""
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    text = cfg["compaction"]["compact_user_instruction"]
    permanence_keywords = ["permanently lost", "will see only", "only place", "the only place"]
    assert any(kw in text for kw in permanence_keywords), (
        "PRIOR SUMMARY RULE must explain that the new summary is the *only* "
        "place where pre-summary context is preserved — info not captured "
        "here is permanently lost when the next continuation reads the new "
        "summary."
    )


def test_compact_prompt_does_not_mention_legacy_sentinel_cleanup() -> None:
    """The active compact prompt should not teach the model the old sentinel.

    Stale legacy entries are removed by backend post-processing, not by
    mentioning LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL in the live prompt.
    """
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    text = cfg["compaction"]["compact_user_instruction"]

    assert "All user messages" in text
    assert "LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL" not in text
    assert "Internal control message - excluded" not in text
    assert "stale" not in text.lower()


def test_compact_yaml_uses_tail_only_control_without_normal_system_clause() -> None:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    compaction = cfg["compaction"]
    control_instruction = compaction["compact_control_user_instruction"]

    assert "compact_control_sentinel" not in compaction
    assert "compact_control_system_clause" not in compaction
    assert not control_instruction.lstrip().startswith("[[__LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1__]]")
    assert "LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL" not in control_instruction
    assert "internal server/system instruction delivered through the user channel" in control_instruction
    assert "not a real user request" in control_instruction
    assert "Do not call tools" in control_instruction
    assert "Do not route agents" in control_instruction
    assert "exactly one `<summary>` block" in control_instruction


def test_compact_yaml_prompt_visible_text_does_not_use_internal_model_aliases() -> None:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    text = "\n".join(
        value
        for key, value in cfg["compaction"].items()
        if key != "status_message" and isinstance(value, str)
    )

    assert "LLM-C" not in text
    assert "LLM-X" not in text


def test_tail_compact_instruction_has_explicit_task_state_reconciliation() -> None:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    text = cfg["compaction"]["compact_control_user_instruction"]

    assert "Reconcile every task" in text
    assert "Do not mark all future or upcoming tasks as Current Work" in text
    assert "Pending Tasks" in text
    assert "Current Work" in text
    assert "Optional Next Step" in text
    assert "Problem Solving" in text
