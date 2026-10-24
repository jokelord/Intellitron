"""Verify that the v1 and v2 builders cover the same set of input tokens.

This is NOT a byte-equal equivalence test (the v1 builder flattens to a single user
string; the v2 builder preserves turn structure). It IS a coverage equivalence test:
given the same body_messages input, the set of substrings the LLM sees must be
identical. This protects against accidentally narrowing the input scope (e.g.
forgetting to include the assistant turn text) during the v2 migration.

Per spec §9.1 [V2] test_compact_v1_v2_equivalence.py.

v2 turn shape (2026-06-11 position-1 amendment, see AGENTS.md trap #27):
   [system, user<compact-instr>, user, assistant, user, assistant, ...]
   ^body[0]  ^body+1[0]              ^body+1[1:] (byte-equal to body[1:])

The compact instruction is now at index 1 (right after system), not at the tail.
This is the lost-in-the-middle fix: at 40k context, a tail-placed instruction is
ignored and the model drifts into the user's language.

Note: v1 returns dicts and v2 returns `Message` objects (see AGENTS.md trap #20
re: cross-layer message type contract). We extract `.content` from each via a
uniform helper so the assertion only cares about string coverage, not shape.
"""
from __future__ import annotations

from backend.compact.prompt_builder import build_compact_messages, build_compact_messages_v2
from backend.config import CompactionPromptsConfig
from backend.llm_service import Message


def _prompts() -> CompactionPromptsConfig:
    return CompactionPromptsConfig(
        no_tools_preamble="PREAMBLE",
        main_prompt="MAIN_PROMPT",
        custom_instructions_header="== USER CUSTOM INSTRUCTIONS ==",
        no_tools_trailer="TRAILER",
        summary_envelope_preface="PREFACE",
        summary_envelope_suffix="SUFFIX",
        compact_user_instruction="Please summarize the conversation above.",
    )


def _content(m: object) -> str:
    if isinstance(m, dict):
        return m["content"]
    return m.content  # type: ignore[attr-defined]


def _role(m: object) -> str:
    if isinstance(m, dict):
        return m["role"]
    return m.role  # type: ignore[attr-defined]


def test_v1_v2_cover_same_substrings() -> None:
    body = [
        Message(role="system", content="SYS_TOKEN"),
        Message(role="user", content="USER_TOKEN_1"),
        Message(role="assistant", content="ASSISTANT_TOKEN_1"),
        Message(role="user", content="USER_TOKEN_2"),
        Message(role="assistant", content="ASSISTANT_TOKEN_2"),
    ]
    prompts = _prompts()
    v1 = build_compact_messages(body_messages=body, prompts=prompts)
    v2 = build_compact_messages_v2(body_messages=body, prompts=prompts)

    # v1 output is 2 messages: [system(指令), user<整段历史字符串>]
    assert len(v1) == 2
    assert _role(v1[0]) == "system"
    assert _role(v1[1]) == "user"
    v1_user_content = _content(v1[1])
    # v2 output is len(body) + 1 messages:
    #   [body[0], user<compact-instr>, body[1], body[2], ..., body[N-1]]
    assert len(v2) == len(body) + 1
    # The instruction lives at index 1 (position-1 amendment), not at the tail.
    assert _role(v2[1]) == "user"
    assert "summarize" in _content(v2[1])
    # v2_combined = every body message's content + the instruction content.
    v2_combined = "\n".join(_content(m) for m in v2)

    # For each body token, it must appear in v1's user content and in v2's combined text.
    for token in ("USER_TOKEN_1", "ASSISTANT_TOKEN_1", "USER_TOKEN_2", "ASSISTANT_TOKEN_2"):
        assert token in v1_user_content, f"v1 dropped token {token!r}"
        assert token in v2_combined, f"v2 dropped token {token!r}"
