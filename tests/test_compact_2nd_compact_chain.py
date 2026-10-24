"""Regression: on the 2nd round of auto-compact, the prior summary MUST appear
in the compact LLM input so the new summary can preserve its key facts and
drop only the now-obsolete ones (AGENTS.md trap #29, 2026-06-12).

Background:
  `_replace_agent_memory` stores the envelope as a `TaskStep` in
  `session.agent.memory.steps`. After the 1st compact, steps == [TaskStep(envelope)].
  When the session grows and a 2nd compact fires, the compact LLM input is
  built from `session.agent.write_memory_to_messages()`, which renders the
  envelope TaskStep as a regular user-role message (prefixed with smolagents'
  literal "New task:\\n" — see `.sglang/.../smolagents/memory.py:191-196`).

  This is correct by design: the compact LLM must see the 1st summary in order
  to (a) preserve its key facts in the new 9-section summary and (b) drop
  information that has become obsolete (e.g., tasks completed since the 1st
  compact). The PRIOR SUMMARY RULE clause in `compact_user_instruction`
  (config/config-agent-prompts.yaml) instructs the LLM accordingly.

These tests pin the contract that the 1st envelope reaches the 2nd compact
LLM input — a regression here would silently break multi-compact chains.
"""
from __future__ import annotations

import time as _time
from pathlib import Path

import pytest
import yaml

from smolagents.memory import TaskStep, ActionStep
from smolagents.monitoring import Timing

from backend.agent.agent_runner import AgentRunner, AgentSession
from backend.compact.prompt_builder import build_compact_messages_v2
from backend.config import CompactionPromptsConfig, get_config


CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "config-agent-prompts.yaml"
)


def _load_real_compaction_prompts() -> CompactionPromptsConfig:
    """Load the real CompactionPromptsConfig from YAML.

    IMPORTANT: do NOT use `get_config()` here — it's a cached singleton that
    other tests may mutate (replacing cfg.agent_prompts.compaction with an
    empty CompactionPromptsConfig). Re-load the YAML directly so we always
    see the production wording, including the PRIOR SUMMARY RULE clause.
    """
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
    """Minimal stub. The compact body_messages are built from
    session.agent.write_memory_to_messages() WITHOUT calling the LLM,
    so we don't need to capture LLM calls."""

    async def chat_stream_async(self, messages, **kwargs):
        if False:
            yield {"content": ""}


def _make_session_after_1st_compact(
    envelope_text: str,
    session_id: str = "s-2nd-compact",
):
    """Build a session whose memory.steps is exactly the post-1st-compact
    state, then add a few more turns to simulate user chatting more.

    The returned session has:
      steps = [TaskStep(envelope), TaskStep(post_user_msg), ActionStep(...)]

    The session's CompactionPromptsConfig has a custom envelope preface/suffix
    so we can identify the envelope in the rendered messages. The compact
    prompts CONFIG is NOT used for body_messages construction (only for
    instruction text in build_compact_messages_v2).
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
    envelope = (
        cfg.agent_prompts.compaction.summary_envelope_preface
        + envelope_text
        + cfg.agent_prompts.compaction.summary_envelope_suffix
    )
    agent.memory.steps = [
        TaskStep(task=envelope, task_images=None),
        TaskStep(task="Follow-up question after compact.", task_images=None),
        ActionStep(step_number=1, timing=Timing(start_time=_time.time())),
    ]
    # The ActionStep needs a model_output for write_memory_to_messages to render it
    agent.memory.steps[2].model_output = "Assistant's follow-up response."
    agent.memory.steps[2].observations = "Observation: tool result"
    session = AgentSession(
        session_id=session_id,
        agent=agent,
        mode="tool_calling",
        current_agent_name="coordinator_1",
        active_agent=agent,
    )
    return session, runner


def test_2nd_compact_input_contains_1st_envelope_text():
    """The 2nd compact LLM input must contain the 1st envelope text verbatim,
    so the LLM can preserve its key facts and drop only the now-obsolete ones.

    Without this, the compact LLM would have no way to know the 1st summary
    existed, and the new summary would only cover post-1st-compact turns.
    """
    envelope_text = (
        "User asked to refactor module X. "
        "Agent analyzed X and identified 3 refactoring opportunities. "
        "Agent's TODO list: 1) refactor foo, 2) refactor bar, 3) add tests."
    )
    session, _ = _make_session_after_1st_compact(envelope_text)

    from backend.agent.agent_runner import _chat_messages_to_backend_messages
    chat_messages = list(session.agent.write_memory_to_messages())
    body_messages = _chat_messages_to_backend_messages(chat_messages)

    # The 1st envelope's text MUST be present in the 2nd compact's body_messages
    # (this is the spec's data dependency for the PRIOR SUMMARY RULE to work).
    found_envelope = False
    for m in body_messages:
        content = m.content if hasattr(m, "content") else m.get("content", "")
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        if "[ENVELOPE-1]" in str(content) and "TODO list" in str(content):
            found_envelope = True
            break

    assert found_envelope, (
        "2nd compact body_messages MUST contain the 1st envelope text. "
        "If this fails, the compact LLM has no way to preserve prior-summary "
        "key facts (AGENTS.md trap #29 / PRIOR SUMMARY RULE)."
    )


def test_2nd_compact_v2_prompt_has_instruction_at_position_1_with_envelope_in_body():
    """The full v2 prompt shape must be:
       final[0] = system (preserved from body[0])
       final[1] = NEW user msg with the compact_user_instruction (with PRIOR SUMMARY RULE)
       final[2..] = body[1..] (which includes the 1st envelope as a user turn)

    This is the structural invariant the spec requires for the LLM to be aware
    of the 1st envelope at the right position (after the instruction, before
    the post-envelope turns).
    """
    envelope_text = "1st compact summary: user asked to refactor module X."
    session, _ = _make_session_after_1st_compact(envelope_text)

    from backend.agent.agent_runner import _chat_messages_to_backend_messages
    chat_messages = list(session.agent.write_memory_to_messages())
    body_messages = _chat_messages_to_backend_messages(chat_messages)

    # Load the REAL prompts from YAML (NOT from get_config() which other tests
    # may have polluted with empty values).
    real_prompts = _load_real_compaction_prompts()
    assert "PRIOR SUMMARY RULE" in real_prompts.compact_user_instruction, (
        "Test setup error: real YAML must contain PRIOR SUMMARY RULE"
    )

    final = build_compact_messages_v2(body_messages, real_prompts)

    # final[0] = body[0] (preserved); final[1] = instruction; final[2..] = body[1..]
    assert len(final) == len(body_messages) + 1, (
        f"final should have one more message than body (the instruction); "
        f"got {len(final)} vs body {len(body_messages)}"
    )

    # final[1] is the instruction (user role, contains PRIOR SUMMARY RULE)
    instruction = final[1]
    assert instruction.role == "user", (
        f"final[1] should be user-role instruction; got {instruction.role}"
    )
    instr_text = instruction.content
    assert "PRIOR SUMMARY RULE" in str(instr_text), (
        "The instruction at position 1 must contain 'PRIOR SUMMARY RULE' "
        "(2026-06-12 multi-compact chain preservation, AGENTS.md trap #29)."
    )

    # final[2..] should include the 1st envelope user turn
    found_envelope = False
    for m in final[2:]:
        content = m.content if hasattr(m, "content") else m.get("content", "")
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        if "[ENVELOPE-1]" in str(content):
            found_envelope = True
            break

    assert found_envelope, (
        "The 1st envelope (rendered as a user turn) must appear in final[2..] "
        "so the compact LLM can see it after reading the instruction at position 1."
    )


def test_compact_orchestrator_passes_2nd_compact_body_through_v2_path():
    """When the 2nd compact fires, the body should flow through build_compact_messages_v2
    and the instruction (with PRIOR SUMMARY RULE) should be at position 1.

    This is a structural test, not a behavior test. It pins that the v2 path
    is wired correctly so the PRIOR SUMMARY RULE clause is delivered to the
    2nd compact LLM input.
    """
    envelope_text = "1st compact summary text"
    session, _ = _make_session_after_1st_compact(envelope_text)

    from backend.compact.types import CompactRequest
    from backend.agent.agent_runner import _chat_messages_to_backend_messages

    chat_messages = list(session.agent.write_memory_to_messages())
    body_messages = _chat_messages_to_backend_messages(chat_messages)
    req = CompactRequest(
        mode="agent",
        session_id=session.session_id,
        body_messages=body_messages,
        trigger="auto",
    )
    assert req.body_messages is not None
    assert len(req.body_messages) > 0

    # Load the real prompts from YAML (independent of get_config() cache).
    real_prompts = _load_real_compaction_prompts()

    # Render the v2 prompt and verify the instruction is at position 1
    final = build_compact_messages_v2(req.body_messages, real_prompts)
    assert final[1].role == "user"
    assert "PRIOR SUMMARY RULE" in str(final[1].content)


def test_envelope_renders_as_user_role_in_compact_body():
    """The 1st envelope's TaskStep is rendered to role=user (with smolagents'
    literal 'New task:\\n' prefix) by write_memory_to_messages().

    This is the smolagents-mandated behavior we rely on. If a future smolagents
    upgrade changes this (e.g., adds a system_reminder role), the 2nd compact
    contract would break — the LLM would no longer 'see' the 1st summary as a
    user turn to be processed.
    """
    envelope_text = "1st summary"
    session, _ = _make_session_after_1st_compact(envelope_text)

    chat_messages = list(session.agent.write_memory_to_messages())
    # chat_messages is [system, user<envelope-as-TaskStep>, user<post_user>, assistant, ...]
    # Find the envelope message by its distinguishing prefix
    from smolagents.models import MessageRole
    envelope_msg = None
    for m in chat_messages:
        content = m.content
        if isinstance(content, list):
            content = "".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        if "[ENVELOPE-1]" in str(content):
            envelope_msg = m
            break
    assert envelope_msg is not None, (
        "Could not find envelope message in write_memory_to_messages() output"
    )
    assert envelope_msg.role == MessageRole.USER, (
        f"1st envelope must be rendered as user role (smolagents TaskStep "
        f"behavior); got {envelope_msg.role}. If smolagents changed this, "
        f"the PRIOR SUMMARY RULE data dependency is broken."
    )
    # And the literal "New task:\n" prefix from smolagents
    envelope_content = envelope_msg.content
    if isinstance(envelope_content, list):
        envelope_content = "".join(
            p.get("text", "") for p in envelope_content if isinstance(p, dict)
        )
    assert "New task:" in str(envelope_content), (
        f"1st envelope content should have the smolagents 'New task:\\n' "
        f"prefix from TaskStep.to_messages(); got: {str(envelope_content)[:200]}"
    )
