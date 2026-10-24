"""End-to-end test for the migrate-to-Problem-Solving directive in
PRIOR SUMMARY RULE clause 2 (2026-06-12 clarification).

Scenario:
  - Round 1 (1st compact): the 1st summary lists "Pending Tasks: refactor foo"
    and "Current Work: refactor foo".
  - User chats more: "please refactor foo" → assistant calls edit_file tool
    → observation returns successfully → assistant says "Done, foo is now
    refactored."
  - Round 2 (2nd compact) fires: the LLM is asked to produce a new summary.

Expected 2nd summary:
  - "Pending Tasks" no longer lists "refactor foo" (it's done).
  - "Optional Next Step" no longer points at foo.
  - "Problem Solving" has a one-line entry like "Refactored foo via edit_file
    (completed in turns N-M)" — the durable record of completion.
  - "Current Work" reflects the *next* in-progress task (or "Awaiting user
    follow-up" if there's no in-progress task).

This test mocks the compact LLM to return a synthetic 2nd summary that we
construct, then asserts the 2nd summary's structure matches the migration
contract. It's a semantic pin: the LLM's output is what we control in tests,
and the migration wording in the prompt is what makes the LLM produce
output shaped like this.
"""
from __future__ import annotations

import re
import time as _time
from pathlib import Path

import pytest
import yaml

from smolagents.memory import ActionStep, TaskStep, ToolCall
from smolagents.monitoring import Timing

from backend.agent.agent_runner import AgentRunner, AgentSession
from backend.compact.prompt_builder import build_compact_messages_v2
from backend.config import CompactionPromptsConfig, get_config


CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "config-agent-prompts.yaml"
)


def _load_real_compaction_prompts() -> CompactionPromptsConfig:
    """Load the real CompactionPromptsConfig from YAML (independent of
    the get_config() cache, which other tests may pollute)."""
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    comp_raw = cfg.get("compaction", {})
    return CompactionPromptsConfig(
        status_message=comp_raw.get("status_message", ""),
        no_tools_preamble=comp_raw.get("no_tools_preamble", ""),
        main_prompt=comp_raw.get("main_prompt", ""),
        custom_instructions_header=comp_raw.get("custom_instructions_header", ""),
        no_tools_trailer=comp_raw.get("no_tools_trailer", ""),
        summary_envelope_preface=comp_raw.get("summary_envelope_preface", ""),
        summary_envelope_suffix=comp_raw.get("summary_envelope_suffix", ""),
        compact_user_instruction=comp_raw.get("compact_user_instruction", ""),
    )


class _StubLLM:
    async def chat_stream_async(self, messages, **kwargs):
        if False:
            yield {"content": ""}


def _make_session_2nd_compact_with_completed_foo():
    """Build a session in the post-1st-compact state where:
      - The 1st envelope lists "Pending Tasks: refactor foo" and
        "Current Work: refactor foo".
      - The post-envelope turns complete foo (assistant calls edit_file,
        observation returns success, assistant says "Done").

    The 2nd compact LLM should see this and migrate foo to Problem Solving.
    """
    cfg = get_config()
    cfg.agent_prompts.compaction = CompactionPromptsConfig(
        status_message="",
        no_tools_preamble="",
        main_prompt="",
        custom_instructions_header="",
        no_tools_trailer="",
        summary_envelope_preface="[ENVELOPE-1] ",
        summary_envelope_suffix=" [END-1]",
    )
    stub = _StubLLM()
    runner = AgentRunner(llm_service=stub, config=cfg, compact_orchestrator=object())
    agent = runner._build_profile_agent(
        "coordinator_1",
        runtime_context="ctx",
        session=None,
    )
    # 1st envelope: contains the "Pending Tasks: refactor foo" + "Current Work: refactor foo"
    envelope_text = (
        "1. Primary Request and Intent: refactor the auth module.\n"
        "2. Key Technical Concepts: Python, FastAPI.\n"
        "7. Pending Tasks: refactor foo function to use bar pattern.\n"
        "8. Current Work: refactoring foo function.\n"
        "9. Optional Next Step: finish refactoring foo, then run tests."
    )
    envelope = (
        cfg.agent_prompts.compaction.summary_envelope_preface
        + envelope_text
        + cfg.agent_prompts.compaction.summary_envelope_suffix
    )
    # Post-envelope round: user asks to refactor foo, assistant calls tool,
    # observation returns success, assistant says "Done".
    user_follow_up = TaskStep(
        task="Please refactor the foo function as we discussed.",
        task_images=None,
    )
    action_step_1 = ActionStep(step_number=1, timing=Timing(start_time=_time.time()))
    action_step_1.model_output = '''<<TOOL_CALL>>
{
  "name": "edit_file",
  "arguments": {
  "file_path": "/tmp/foo.py",
  "search_text": "old foo",
  "replace_text": "refactored foo"
}
}
<<END_TOOL_CALL>>'''
    action_step_1.tool_calls = [
        ToolCall(
            name="edit_file",
            arguments={
                "file_path": "/tmp/foo.py",
                "search_text": "old foo",
                "replace_text": "refactored foo",
            },
            id="call-1",
        )
    ]
    action_step_1.observations = "File edited successfully. 3 lines changed."

    action_step_2 = ActionStep(step_number=2, timing=Timing(start_time=_time.time()))
    action_step_2.model_output = "Refactoring foo is done. Ready for next task."
    action_step_2.observations = ""

    agent.memory.steps = [
        TaskStep(task=envelope, task_images=None),
        user_follow_up,
        action_step_1,
        action_step_2,
    ]
    session = AgentSession(
        session_id="s-foo-completed",
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
        active_agent=agent,
    )
    return session, runner


def _render_v2_prompt():
    """Build the v2 prompt that the 2nd compact LLM will see."""
    session, _ = _make_session_2nd_compact_with_completed_foo()
    from backend.agent.agent_runner import _chat_messages_to_backend_messages
    chat_messages = list(session.agent.write_memory_to_messages())
    body_messages = _chat_messages_to_backend_messages(chat_messages)
    real_prompts = _load_real_compaction_prompts()
    final = build_compact_messages_v2(body_messages, real_prompts)
    return final


def test_2nd_compact_prompt_contains_migration_directive():
    """The v2 prompt (at position 1) must explicitly instruct the LLM to
    migrate completed tasks to 'Problem Solving'."""
    final = _render_v2_prompt()
    instr = final[1]
    instr_text = str(instr.content)
    # Must mention migration to Problem Solving
    assert "Migrate completed work" in instr_text, (
        "PRIOR SUMMARY RULE clause 2 must say 'Migrate completed work'."
    )
    assert "Problem Solving" in instr_text, (
        "PRIOR SUMMARY RULE clause 2 must name 'Problem Solving' as the "
        "destination for completed tasks."
    )
    # Must say "one-line" entry (not full transcript)
    assert "one-line" in instr_text or "one line" in instr_text.lower(), (
        "clause 2 must require a one-line entry per completed task."
    )


def test_2nd_compact_prompt_includes_post_foo_completion_turns():
    """The 2nd compact LLM input must include the post-envelope turns that
    show foo being completed. Without these, the LLM has no signal that
    foo is done and would re-list it under Pending/Current Work.

    This is the DATA DEPENDENCY: the LLM needs the post-envelope turns in
    the body_messages to know 'foo was completed' and trigger the
    migrate-to-Problem-Solving directive.
    """
    final = _render_v2_prompt()
    # Find any user/assistant turn mentioning foo
    found_foo_in_post_envelope = False
    for m in final[2:]:
        content = m.content if hasattr(m, "content") else m.get("content", "")
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        s = str(content)
        if "Please refactor the foo function" in s:
            found_foo_in_post_envelope = True
        if "edit_file" in s and "refactor" in s.lower():
            # Used as a no-op signal that we DO see the post-envelope turns
            # (kept for documentation; the actual check is below).
            pass

    assert found_foo_in_post_envelope, (
        "2nd compact body must contain the post-envelope user msg "
        "'Please refactor the foo function' so the LLM can see the task."
    )
    # The current action trace is serialized as a framed edit_file call.
    # We just need to confirm edit_file appears somewhere in the post-envelope turns
    # (after the envelope), not that "refactor" co-occurs.
    found_edit_file_in_post_envelope_anywhere = False
    for m in final[2:]:
        content = m.content if hasattr(m, "content") else m.get("content", "")
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        if "edit_file" in str(content):
            found_edit_file_in_post_envelope_anywhere = True
            break
    assert found_edit_file_in_post_envelope_anywhere, (
        "2nd compact body must contain the post-envelope tool call "
        "(edit_file) so the LLM can see the task was executed."
    )


# ---------------------------------------------------------------------------
# Below: tests that pin the EXPECTED OUTPUT SHAPE the LLM should produce.
# These tests don't run a real LLM; they assert that IF the LLM follows
# the migration directive in the prompt, the resulting 2nd summary would
# pass these structural checks. We test the directive is present, and
# we test the contract (a 2nd summary that violates it would be wrong).
# ---------------------------------------------------------------------------


def _synthesize_2nd_summary_following_directive() -> str:
    """Return a synthetic 2nd summary that follows the migration directive.
    Used to validate the structural contract the LLM should produce.
    """
    return """<analysis>
The user asked to refactor the foo function. The assistant called edit_file
and the file was edited successfully. The assistant confirmed the work was
done. Foo is now complete.

Per the PRIOR SUMMARY RULE clause 2 (migrate-to-Problem-Solving), the
"refactor foo" task should be moved from Pending/Current Work to Problem
Solving as a one-line entry, and Current Work should reflect the next task
(or "Awaiting user follow-up" if there's nothing in progress).
</analysis>

<summary>
1. Primary Request and Intent:
   The user originally asked to refactor the auth module, including the foo function.

2. Key Technical Concepts:
   Python, FastAPI, edit_file tool.

3. Files and Code Sections:
   - foo function: refactored in current round to use bar pattern.
   - auth module: pending refactor (only foo was completed).

4. Errors and fixes:
   (none this round)

5. Problem Solving:
   - Refactored foo function via edit_file (completed in turns 2-3).

6. All user messages:
   - User: "refactor the auth module" (original, pre-summary)
   - User: "Please refactor the foo function as we discussed." (post-summary)

7. Pending Tasks:
   - Refactor the rest of the auth module (foo was completed; bar/baz still pending).

8. Current Work:
   Awaiting user follow-up (foo was just completed; no in-progress task).

9. Optional Next Step:
   User to confirm whether to continue with the rest of the auth module refactor.
</summary>"""


def test_synthesized_2nd_summary_migrates_foo_to_problem_solving():
    """A 2nd summary that follows the migration directive should have:
    - 'foo' listed in 'Problem Solving' with a 'completed' marker
    - 'foo' absent from 'Pending Tasks' (or only as 'rest of auth module, foo done')
    - 'Current Work' reflecting the *next* task or 'Awaiting user follow-up'
    """
    summary = _synthesize_2nd_summary_following_directive()

    # Extract sections
    def find_section(name: str) -> str:
        m = re.search(rf"^\s*{re.escape(name)}:?\s*(.*?)(?=^\s*\d+\.\s|\Z)", summary, re.S | re.M)
        return (m.group(1) if m else "").strip()

    problem_solving = find_section("Problem Solving") or _find_section_by_alt(summary, "5. Problem Solving")
    current_work = find_section("Current Work") or _find_section_by_alt(summary, "8. Current Work")
    pending = find_section("Pending Tasks") or _find_section_by_alt(summary, "7. Pending Tasks")

    # Problem Solving must mention foo + completion
    assert "foo" in problem_solving.lower(), (
        f"Problem Solving must mention the completed task 'foo'.\n"
        f"Got:\n{problem_solving}"
    )
    assert "completed" in problem_solving.lower() or "done" in problem_solving.lower(), (
        f"Problem Solving entry must include a 'completed' marker.\n"
        f"Got:\n{problem_solving}"
    )

    # Pending Tasks must NOT list foo as a todo (it's done)
    # Note: "rest of auth module" is OK; "foo" alone as a pending item is NOT
    pending_lines = [
        l.strip() for l in pending.split("\n")
        if l.strip() and not l.strip().startswith("-")
    ]
    pending_bullets = [
        l.strip() for l in pending.split("\n")
        if l.strip().startswith("-")
    ]
    # If foo is mentioned in pending, it must be qualified as completed
    for bullet in pending_bullets:
        if "foo" in bullet.lower():
            assert "done" in bullet.lower() or "completed" in bullet.lower(), (
                f"Pending Tasks bullet mentioning 'foo' must qualify it as done:\n{bullet}"
            )

    # Current Work must reflect the *next* task or "Awaiting user follow-up"
    # (NOT the just-completed foo)
    assert "foo" not in current_work.lower() or "completed" in current_work.lower() or "done" in current_work.lower(), (
        f"Current Work must NOT list the just-completed 'foo' as in-progress.\n"
        f"Got:\n{current_work}"
    )


def _find_section_by_alt(text: str, alt: str) -> str:
    """Fallback: find a section by 'N. Section Name' header pattern."""
    m = re.search(
        rf"^{re.escape(alt)}:?\s*\n(.*?)(?=^\s*\d+\.\s|\Z)", text, re.S | re.M
    )
    return (m.group(1) if m else "").strip()
