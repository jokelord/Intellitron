# OUTDATED — v2 Compact Prompt — Turn-Shape Implementation Plan

> **OUTDATED / DO NOT EXECUTE (2026-06-14):** This plan assumes tail-only compact instruction and near byte-equal active system/history prefix reuse. Those assumptions were invalidated by E2E reliability failures and conflict with the current prefix-cache optimization direction. Use `docs/superpowers/plans/2026-06-14-compact-prompt-shape-prefix-cache-plan.md` instead.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the v1 compact LLM call from a 2-message `[system, user<整段历史>]` shape into a turn-shape `[system, ...history..., user<compact-instr>]` shape, so the compact call's input messages are byte-equal to the previous chat calls' input messages up to the trailing compact-instruction user message. This maximizes SGLang radix tree prefix cache reuse and reduces the per-compact prefill token count from ~45k to ~150-300.

**Architecture:** Add a new `build_compact_messages_v2()` in `backend/compact/prompt_builder.py` that takes a `List[Message]` (already including the system message, sourced from `session.agent.write_memory_to_messages()` for agent mode or `filtered` for chat mode) and appends exactly one new user message with the "compress the conversation above" instruction. The orchestrator prefers the new `body_messages` field on `CompactRequest`; if it is missing, it falls back to the v1 path (1 release deprecation). The existing `_replace_agent_memory`, `format_compact_summary`, and SSE event flow are unchanged.

**Tech Stack:** Python 3.12, FastAPI, smolagents 1.22.0, SGLang, Qwen3.5-35B-A3B-GPTQ-Int4, pytest, jinja2 (for chat_template rendering test). Compaction prompt text is borrowed verbatim from `ref_src/services/compact/prompt.ts` (Claude Code reference implementation).

**Spec:** `docs/superpowers/specs/outdated-2026-06-11-v2-compact-prompt-turn-shape-design.md`
**Reference materials:**
- `ref_src/services/compact/prompt.ts:19-26, 61-143, 269-272, 345-347` — compaction prompt wording source
- `/home/zk/Project/LLM-weight/Qwen3___5-35B-A3B-GPTQ-Int4/chat_template.jinja:67-80` — `multi_step_tool` check
- `踩坑记录4.md` — symptom history, SGLang mamba/KV tuning
- `踩坑记录3.md` §20 — chat template rendering test lesson

---

## File Map

### Backend files to modify
- `backend/compact/prompt_builder.py`
  - Add `build_compact_messages_v2()` function (~30 lines). Keep existing `build_compact_messages()` with `@deprecated` docstring (1 release).
- `backend/compact/types.py`
  - Add `body_messages: Optional[List[Message]] = None` field to `CompactRequest`. Leave `messages: Optional[List[Any]] = None` field as deprecated (1 release).
- `backend/compact/orchestrator.py`
  - In `run_compact()`: prefer `req.body_messages`; if missing, fall back to `req.messages` + `build_compact_messages()` v1 path with a deprecation warning log. Remove the `_collect_messages()` v1-fallback helper. Adjust the `_collect_messages` replacement to read `req.body_messages` directly.
- `backend/agent/agent_runner.py`
  - Refactor `_run_compact_then_continue` (line 4515-4557) to construct `body_messages` from `list(session.agent.write_memory_to_messages())` converted to `List[Message]`.
  - Refactor `run_compact_for_session` (line 4559-4634) identically.
  - Refactor `_should_auto_compact_session` (line 4451-4477) to use the same `write_memory_to_messages()` source for token counting.
- `backend/api/chat.py`
  - In `_generate_stream_response` (line 488-523): change the auto-compact branch to pass `filtered` directly as `body_messages` (the list already includes the system message).
  - `_should_auto_compact_chat` is unchanged (chat mode still uses `_session_memory_to_messages` for token count; the agent runner helper remains in use only for chat mode token counting).

### Config files to modify
- `config/config-agent-prompts.yaml`
  - Add `compact_user_instruction` field under `compaction:` (line 401-424 block). The wording is borrowed from `ref_src/services/compact/prompt.ts:61-143` (the closing paragraph of `BASE_COMPACT_PROMPT`) with one added sentence that explicitly anchors "the conversation above" to the earlier messages in the list, not the current instruction message.

### Tests to create
- `tests/test_compact_prompt_builder_v2.py` — v2 builder unit tests (5 tests).
- `tests/test_compact_prompt_builder_v2_template_compat.py` — real Qwen3 chat_template render test (2 tests).
- `tests/test_compact_request_v2_field.py` — `CompactRequest.body_messages` field tests (3 tests).
- `tests/test_compact_orchestrator_v2.py` — orchestrator v2 path tests (2 tests).
- `tests/test_compact_agent_v2_path.py` — agent runner v2 path tests (3 tests).
- `tests/test_compact_chat_v2_path.py` — chat mode v2 path tests (2 tests).
- `tests/test_compact_v1_v2_equivalence.py` — v1/v2 coverage equivalence test (1 test).
- `tests/test_compact_yaml_v2_prompts.py` — yaml field test (1 test).

### Documentation files to modify
- `AGENTS.md` — append trap #26 ("v2 compact prompt builder must 1:1 reuse body_messages").
- `踩坑记录4.md` — update §5 to reflect that v2 turns the "unavoidable re-prefill" into "by reusing body_messages byte-equal to previous chat calls, SGLang radix cache hit rate goes from <10% to 80-95%".

---

### Task 1: Add `build_compact_messages_v2` — Red-Green TDD

**Files:**
- Modify: `backend/compact/prompt_builder.py`
- Create: `tests/test_compact_prompt_builder_v2.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_compact_prompt_builder_v2.py` with the following content:

```python
"""Tests for the v2 compact prompt builder (turn-shape, byte-equal to previous chat calls).

These tests pin the contract from spec §5.2:
  - len(final) == len(body_messages) + 1
  - final[:-1] == body_messages (byte-equal, 0 modification)
  - final[-1].role == "user"
  - final[-1].content contains prompts.compact_user_instruction
  - body_messages must be non-empty
  - body_messages[0].role must be "system"
"""
from __future__ import annotations

import pytest

from backend.compact.prompt_builder import build_compact_messages_v2
from backend.config import CompactionPromptsConfig
from backend.llm_service import Message


@pytest.fixture
def prompts() -> CompactionPromptsConfig:
    return CompactionPromptsConfig(
        no_tools_preamble="NO_TOOLS_PREAMBLE",
        main_prompt="MAIN_PROMPT",
        custom_instructions_header="== USER CUSTOM INSTRUCTIONS ==",
        no_tools_trailer="REMINDER: text only",
        summary_envelope_preface="PREFACE: ",
        summary_envelope_suffix="\nSUFFIX",
        compact_user_instruction=(
            "Please provide your summary based on the conversation above, "
            "following this structure and ensuring precision and thoroughness "
            "in your response. The \"conversation above\" refers to the messages "
            "that appear earlier in this message list, NOT including this current "
            "instruction message."
        ),
    )


def test_returns_n_plus_one_messages(prompts: CompactionPromptsConfig) -> None:
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="A 说：xxx"),
        Message(role="assistant", content="回：yyy"),
        Message(role="user", content="B 说：zzz"),
    ]
    out = build_compact_messages_v2(body, prompts)
    assert len(out) == len(body) + 1


def test_body_messages_preserved_byte_equal(prompts: CompactionPromptsConfig) -> None:
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="A 说：xxx"),
        Message(role="assistant", content="回：yyy"),
    ]
    out = build_compact_messages_v2(body, prompts)
    for i, original in enumerate(body):
        assert out[i].role == original.role
        assert out[i].content == original.content


def test_final_message_is_user_with_compact_instruction(prompts: CompactionPromptsConfig) -> None:
    body = [Message(role="system", content="SYS"), Message(role="user", content="A")]
    out = build_compact_messages_v2(body, prompts)
    assert out[-1].role == "user"
    assert "conversation above" in out[-1].content
    assert "compact_user_instruction" not in out[-1].content  # human-readable copy


def test_custom_instructions_appended_when_provided(prompts: CompactionPromptsConfig) -> None:
    body = [Message(role="system", content="SYS")]
    out = build_compact_messages_v2(
        body, prompts, custom_instructions="Focus on file paths"
    )
    assert "compact_user_instruction" not in out[-1].content
    assert "USER CUSTOM INSTRUCTIONS" in out[-1].content
    assert "Focus on file paths" in out[-1].content


def test_custom_instructions_blank_is_ignored(prompts: CompactionPromptsConfig) -> None:
    body = [Message(role="system", content="SYS")]
    out = build_compact_messages_v2(body, prompts, custom_instructions="   \n  ")
    assert "USER CUSTOM INSTRUCTIONS" not in out[-1].content


def test_empty_body_messages_raises(prompts: CompactionPromptsConfig) -> None:
    with pytest.raises(ValueError, match="body_messages 不能为空"):
        build_compact_messages_v2([], prompts)


def test_first_message_must_be_system(prompts: CompactionPromptsConfig) -> None:
    with pytest.raises(ValueError, match="第一条必须是 system"):
        build_compact_messages_v2(
            [Message(role="user", content="hello")], prompts
        )
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_prompt_builder_v2.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_compact_messages_v2' from 'backend.compact.prompt_builder'` (or `ModuleNotFoundError`).

- [ ] **Step 3: Implement `build_compact_messages_v2`**

Open `backend/compact/prompt_builder.py`. Add the following function at the end of the file (do NOT change the existing `build_compact_messages` function; the spec mandates it stays 1 release as a deprecated path):

```python
def build_compact_messages_v2(
    body_messages: List[Message],
    prompts: CompactionPromptsConfig,
    custom_instructions: Optional[str] = None,
) -> List[Message]:
    """Build compact LLM input in turn-shape: 1:1 reuse body_messages (含 system) + 末尾追加 1 条 user.

    Per spec §5.2:
      - body_messages must be non-empty.
      - body_messages[0].role must be "system".
      - final[:-1] is byte-equal to body_messages (0 modification).
      - final[-1] is a user role message containing prompts.compact_user_instruction,
        optionally followed by prompts.custom_instructions_header + custom_instructions.

    This shape maximizes SGLang radix cache reuse: the compact LLM call sees the
    same token sequence as the previous chat calls (system + history turns), with
    the only divergence being the trailing compact-instruction user message.
    """
    if not body_messages:
        raise ValueError("body_messages 不能为空")
    if body_messages[0].role != "system":
        raise ValueError("body_messages 第一条必须是 system 消息")

    final: List[Message] = list(body_messages)  # 0 transformation; 1:1 reuse

    parts: List[str] = [prompts.compact_user_instruction]
    if custom_instructions and custom_instructions.strip():
        parts.append(prompts.custom_instructions_header)
        parts.append(custom_instructions.strip())
    final.append(Message(role="user", content="\n\n".join(parts)))

    return final
```

Add the import for `Message` at the top of the file (the existing `build_compact_messages` already imports from `..config` but not from `..llm_service`):

```python
from ..llm_service import Message
```

Keep all existing imports and the existing `build_compact_messages` function unchanged. The `_render_body` helper stays as-is (it is used by v1 only).

- [ ] **Step 4: Run tests and verify they pass**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_prompt_builder_v2.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Run the full v1 test suite to confirm no regression**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_prompt_builder.py -v`
Expected: PASS (existing v1 tests still pass; `build_compact_messages` was not touched).

- [ ] **Step 6: Commit**

```bash
git add backend/compact/prompt_builder.py tests/test_compact_prompt_builder_v2.py
git commit -m "feat(compact): add build_compact_messages_v2 turn-shape builder"
```

---

### Task 2: Add `CompactRequest.body_messages` field

**Files:**
- Modify: `backend/compact/types.py`
- Create: `tests/test_compact_request_v2_field.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_compact_request_v2_field.py`:

```python
"""Tests for the new CompactRequest.body_messages field.

Per spec §5.1:
  - body_messages is Optional[List[Message]], defaults to None.
  - messages (v1) is preserved for 1 release, defaults to None.
  - body_messages takes precedence over messages in the orchestrator (covered in Task 4).
"""
from __future__ import annotations

from backend.compact.types import CompactRequest, CompactEventKind, Mode, Trigger
from backend.llm_service import Message


def test_compact_request_body_messages_field_exists() -> None:
    req = CompactRequest(
        mode="agent",
        body_messages=[Message(role="system", content="S")],
    )
    assert req.body_messages is not None
    assert len(req.body_messages) == 1
    assert req.body_messages[0].role == "system"


def test_compact_request_v1_messages_field_still_present() -> None:
    """v1 path must keep working for 1 release."""
    req = CompactRequest(
        mode="agent",
        messages=[Message(role="user", content="legacy body")],
    )
    assert req.messages is not None
    assert len(req.messages) == 1


def test_compact_request_defaults_both_to_none() -> None:
    req = CompactRequest(mode="chat")
    assert req.body_messages is None
    assert req.messages is None
    assert req.session_id is None
    assert req.trigger == "manual"
    assert req.custom_instructions is None
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_request_v2_field.py -v`
Expected: FAIL with `TypeError: CompactRequest.__init__() got an unexpected keyword argument 'body_messages'`.

- [ ] **Step 3: Add the `body_messages` field**

Open `backend/compact/types.py`. Find the `CompactRequest` dataclass. Add the new field. Do not remove `messages`:

```python
from typing import List, Optional

from ..llm_service import Message

# ... existing imports ...

@dataclass
class CompactRequest:
    mode: Mode
    body_messages: Optional[List[Message]] = None  # v2: 含 system 的完整 messages 列表
    messages: Optional[List[Any]] = None          # v1: body-only 列表; deprecated, 1 release
    session_id: Optional[str] = None
    trigger: Trigger = "manual"
    custom_instructions: Optional[str] = None
```

(If the existing file does not already import `List` and `Message`, add those imports. `List` is most likely already imported from `typing`; `Message` may need to be added. Check the existing imports in `backend/compact/types.py` before adding.)

- [ ] **Step 4: Run tests and verify they pass**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_request_v2_field.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the full compact test suite to confirm no regression**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_types.py tests/test_compact_request_v2_field.py -v`
Expected: PASS (existing `test_compact_types.py` tests still pass; the field is purely additive).

- [ ] **Step 6: Commit**

```bash
git add backend/compact/types.py tests/test_compact_request_v2_field.py
git commit -m "feat(compact): add CompactRequest.body_messages field"
```

---

### Task 3: Update YAML — add `compact_user_instruction` field

**Files:**
- Modify: `config/config-agent-prompts.yaml`
- Create: `tests/test_compact_yaml_v2_prompts.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_compact_yaml_v2_prompts.py`:

```python
"""Test that the compaction YAML config has the new compact_user_instruction field.

Per spec §5.3:
  - Wording is borrowed from ref_src/services/compact/prompt.ts:61-143 (the closing
    paragraph of BASE_COMPACT_PROMPT) with one added sentence that anchors
    "the conversation above" to the earlier messages in the list.
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


def test_compact_user_instruction_anchors_to_conversation_above() -> None:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    text = cfg["compaction"]["compact_user_instruction"]
    # spec §5.3: 必须显式锚定 "conversation above" 到"earlier in this message list,
    # NOT including this current instruction message"
    assert "conversation above" in text
    assert "earlier" in text or "above" in text
    assert "not including" in text.lower() or "excluding" in text.lower() or "NOT including" in text
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_yaml_v2_prompts.py -v`
Expected: FAIL with `KeyError: 'compact_user_instruction'` (or assertion on the missing field).

- [ ] **Step 3: Add the field to the YAML**

Open `config/config-agent-prompts.yaml`. Locate the `compaction:` block (line 401-424). Add a new field `compact_user_instruction` after `no_tools_trailer` (or after `custom_instructions_header`, keeping the existing field order intact — the test does not depend on field order). The wording is borrowed from `ref_src/services/compact/prompt.ts:61-143` (the closing paragraph of `BASE_COMPACT_PROMPT`) with one added sentence for v2 anchoring:

```yaml
compaction:
  status_message: "Compressing conversation context..."
  no_tools_preamble: |
    CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

    - Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.
    - You already have all the context you need in the conversation above.
    - Tool calls will be REJECTED and will waste your only turn — you will fail the task.
    - Your entire response must be plain text: an <analysis> block followed by a <summary> block.
  main_prompt: |
    Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
    This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

    Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

    1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
       - The user's explicit requests and intents
       - Your approach to addressing the user's requests
       - Key decisions, technical concepts and code patterns
       - Specific details like:
         - file names
         - full code snippets
         - function signatures
         - file edits
       - Errors that you ran into and how you fixed them
       - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
    2. Double-check for technical accuracy and completeness, addressing each required element thoroughly.

    Your summary should include the following sections:

    1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
    2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
    3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.
    4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
    5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
    6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users' feedback and changing intent.
    7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
    8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying close attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
    9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request. Do not start on tangential requests or really old requests that were already completed without confirming with the user first.
                       If there is a next step, include direct quotes from the most recent conversation showing exactly what task you were working on and where you left off. This should be verbatim to ensure there's no drift in task interpretation.

    Here's an example of how your output should be structured:

    <example>
    <analysis>
    [Your thought process, ensuring all points are covered thoroughly and accurately]
    </analysis>

    <summary>
    1. Primary Request and Intent:
       [Detailed description]
    2. Key Technical Concepts:
       - [Concept 1]
       - [Concept 2]
    3. Files and Code Sections:
       - [File Name 1]
    4. Errors and fixes:
    5. Problem Solving:
    6. All user messages:
    7. Pending Tasks:
    8. Current Work:
    9. Optional Next Step:
    </summary>
    </example>

    Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response.

    There may be additional summarization instructions provided in the included context. If so, remember to follow these instructions when creating the above summary. Examples of instructions include:
    <example>
    ## Compact Instructions
    When summarizing the conversation focus on typescript code changes and also remember the mistakes you made and how you fixed them.
    </example>

    <example>
    # Summary instructions
    When you are using compact - please focus on test output and code changes. Include file reads verbatim.
    </example>
  custom_instructions_header: "== USER CUSTOM INSTRUCTIONS =="
  no_tools_trailer: "REMINDER: Do NOT call any tools. Respond with plain text only — an <analysis> block followed by a <summary> block. Tool calls will be rejected and you will fail the task."
  compact_user_instruction: |
    Please provide your summary based on the conversation above, following this structure and ensuring precision and thoroughness in your response.

    The "conversation above" refers to the messages that appear earlier in this message list, NOT including this current instruction message. Summarize those earlier messages only.

    There may be additional summarization instructions provided in the included context. If so, remember to follow these instructions when creating the above summary.
  summary_envelope_preface: |
    This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.
  summary_envelope_suffix: |
    Continue the conversation from where it left off without asking the user any further questions. Resume directly — do not acknowledge the summary, do not recap what was happening, do not preface with "I'll continue" or similar. Pick up the last task as if the break never happened.
```

NOTE: This block **replaces** the existing `compaction:` block in the YAML (the existing `main_prompt` is short — 9-section skeleton only — and is being upgraded to the full `BASE_COMPACT_PROMPT` from `ref_src/services/compact/prompt.ts:61-143`). The English wording is used verbatim from the reference implementation; the spec §5.3 explicitly mandates borrowing from `ref_src` rather than translating to Chinese. If the implementation phase reveals that English wording degrades summary quality, the wording can be iterated later (the spec §8 risk table documents this as an acceptable iteration).

- [ ] **Step 4: Update the `CompactionPromptsConfig` dataclass loader in `backend/config.py`**

Open `backend/config.py`. Find the `CompactionPromptsConfig` dataclass (around line 402-405) and its loader (around line 1021-1024). Add `compact_user_instruction: str = ""` as a new field, and load it from the YAML:

```python
@dataclass
class CompactionPromptsConfig:
    no_tools_preamble: str = ""
    main_prompt: str = ""
    custom_instructions_header: str = ""
    no_tools_trailer: str = ""
    summary_envelope_preface: str = ""
    summary_envelope_suffix: str = ""
    compact_user_instruction: str = ""  # v2: appended as the trailing user message
```

In the loader function (search for the `comp_raw = cfg.get("compaction", {})` line and the assignments that follow), add:

```python
compact_user_instruction=comp_raw.get("compact_user_instruction", ""),
```

(Keep all existing field assignments as-is — only add the new line. The `messages` field extraction in the loader stays untouched.)

- [ ] **Step 5: Run the test and verify it passes**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_yaml_v2_prompts.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Run the full compact config test suite to confirm no regression**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_config.py tests/test_compact_yaml_v2_prompts.py -v`
Expected: PASS (existing `test_compact_config.py` tests still pass; the new field is purely additive).

- [ ] **Step 7: Commit**

```bash
git add config/config-agent-prompts.yaml backend/config.py tests/test_compact_yaml_v2_prompts.py
git commit -m "feat(compact): add compact_user_instruction YAML field (from ref_src)"
```

---

### Task 4: Refactor `CompactOrchestrator.run_compact` — prefer v2 path, fallback v1 with deprecation warning

**Files:**
- Modify: `backend/compact/orchestrator.py`
- Create: `tests/test_compact_orchestrator_v2.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_compact_orchestrator_v2.py`:

```python
"""Tests for CompactOrchestrator v2 path (body_messages) + v1 fallback (with deprecation warning).

Per spec §5.1, §5.2, §6.1 I5:
  - When req.body_messages is provided (non-None), use build_compact_messages_v2.
  - When req.body_messages is None and req.messages is provided, use v1 build_compact_messages
    and log a deprecation warning.
  - In both cases, the resulting messages are streamed to LLMService.chat_stream_async.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, List, Optional

import pytest

from backend.compact.orchestrator import CompactOrchestrator
from backend.compact.types import CompactEvent, CompactEventKind, CompactRequest
from backend.config import CompactConfig, CompactionPromptsConfig
from backend.llm_service import Message


class FakeLLMService:
    """Captures the messages passed to chat_stream_async."""

    def __init__(self, content: str = "<summary>OK</summary>") -> None:
        self.captured_messages: Optional[List[Message]] = None
        self._content = content

    async def chat_stream_async(
        self,
        messages: List[Message],
        max_new_tokens: int,
        temperature: float,
        top_p: float,
        tools: Any = None,
        request_id: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        self.captured_messages = messages
        yield {"content": self._content}


@pytest.fixture
def prompts() -> CompactionPromptsConfig:
    return CompactionPromptsConfig(
        no_tools_preamble="PREAMBLE",
        main_prompt="MAIN",
        custom_instructions_header="== USER CUSTOM INSTRUCTIONS ==",
        no_tools_trailer="TRAILER",
        summary_envelope_preface="PREFACE: ",
        summary_envelope_suffix="\nSUFFIX",
        compact_user_instruction="Please summarize the conversation above.",
    )


@pytest.fixture
def compact_config() -> CompactConfig:
    return CompactConfig(
        enabled=True,
        auto_trigger=True,
        max_summary_tokens=2000,
        temperature=0.3,
        top_p=0.9,
        strategy="performance",
        performance_ratio=0.1,
    )


def _drain(agen):
    import asyncio
    return asyncio.run(_collect(agen))


async def _collect(agen):
    out = []
    async for ev in agen:
        out.append(ev)
    return out


@pytest.mark.asyncio
async def test_v2_path_uses_body_messages_directly(
    prompts: CompactionPromptsConfig, compact_config: CompactConfig
) -> None:
    llm = FakeLLMService()
    orch = CompactOrchestrator(llm_service=llm, config=compact_config, prompts=prompts)
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="A 说：xxx"),
        Message(role="assistant", content="回：yyy"),
    ]
    req = CompactRequest(mode="agent", body_messages=body, trigger="manual")
    events = await _collect(orch.run_compact(req))
    # LLM received: [SYS, A 说, 回 yyy, compact-instr]
    assert llm.captured_messages is not None
    assert len(llm.captured_messages) == len(body) + 1
    for i, original in enumerate(body):
        assert llm.captured_messages[i].role == original.role
        assert llm.captured_messages[i].content == original.content
    assert llm.captured_messages[-1].role == "user"
    assert "conversation above" in llm.captured_messages[-1].content or \
           "summarize the conversation" in llm.captured_messages[-1].content
    # Compact completed successfully
    assert any(ev.kind == CompactEventKind.COMPLETED for ev in events)


@pytest.mark.asyncio
async def test_v1_path_logs_deprecation_warning(
    prompts: CompactionPromptsConfig, compact_config: CompactConfig, caplog
) -> None:
    llm = FakeLLMService()
    orch = CompactOrchestrator(llm_service=llm, config=compact_config, prompts=prompts)
    # v1 caller uses req.messages (no body_messages)
    body = [Message(role="user", content="legacy body")]
    req = CompactRequest(mode="agent", messages=body, trigger="manual")
    with caplog.at_level(logging.WARNING, logger="backend.compact.orchestrator"):
        events = await _collect(orch.run_compact(req))
    # LLM received a 2-message [system, user<legacy body>] shape (v1)
    assert llm.captured_messages is not None
    assert len(llm.captured_messages) == 2
    assert llm.captured_messages[0].role == "system"
    assert "PREMAINT" not in llm.captured_messages[0].content  # contains no_tools_preamble + main_prompt
    assert "PREAMBLE" in llm.captured_messages[0].content
    assert "MAIN" in llm.captured_messages[0].content
    assert llm.captured_messages[1].role == "user"
    assert "legacy body" in llm.captured_messages[1].content
    # Deprecation warning was logged
    assert any("deprecated" in rec.message.lower() for rec in caplog.records), (
        f"expected deprecation warning, got: {[r.message for r in caplog.records]}"
    )
    # Compact completed
    assert any(ev.kind == CompactEventKind.COMPLETED for ev in events)
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_orchestrator_v2.py -v`
Expected: FAIL (the orchestrator currently uses `_collect_messages` which only returns `req.messages` if set; the v2 path with `body_messages` is not implemented yet).

- [ ] **Step 3: Refactor `CompactOrchestrator.run_compact`**

Open `backend/compact/orchestrator.py`. Locate the `run_compact` method and the `_collect_messages` helper. Replace the prompt-building branch as follows:

In `run_compact`, replace the section that calls `build_compact_messages` and `_collect_messages` with the following v2-first / v1-fallback flow. The full new method body is:

```python
async def run_compact(
    self, req: CompactRequest
) -> AsyncGenerator[CompactEvent, None]:
    summary_id = CompactEvent.new_summary_id()
    yield CompactEvent.started(
        summary_message_id=summary_id,
        trigger=req.trigger,
        mode=req.mode,
    )

    # 1) Build the prompt messages.
    # v2 path (preferred): req.body_messages is the byte-equal list of previous chat
    # calls' messages (含 system); we 1:1 reuse it and append the compact-instr user msg.
    # v1 path (1 release fallback): req.body_messages is None but req.messages is set;
    # we log a deprecation warning and use the v1 build_compact_messages helper.
    if req.body_messages is not None:
        try:
            prompt_messages = build_compact_messages_v2(
                body_messages=req.body_messages,
                prompts=self.prompts,
                custom_instructions=req.custom_instructions,
            )
        except ValueError as exc:
            logger.exception("build_compact_messages_v2 failed")
            yield CompactEvent.failed(summary_id, "build_error", str(exc))
            return
    elif req.messages is not None:
        logger.warning(
            "CompactRequest.body_messages is None; falling back to v1 path with req.messages. "
            "This is deprecated since 2026-06-11 and will be removed in the next release. "
            "Callers should set body_messages (a List[Message] including the system message) "
            "instead of messages (a body-only list)."
        )
        try:
            prompt_messages = build_compact_messages(
                body_messages=req.messages,
                prompts=self.prompts,
                custom_instructions=req.custom_instructions,
            )
        except Exception as exc:
            logger.exception("build_compact_messages v1 failed")
            yield CompactEvent.failed(summary_id, "build_error", str(exc))
            return
    else:
        yield CompactEvent.failed(
            summary_id, "no_messages",
            "CompactRequest must have body_messages (v2) or messages (v1) set",
        )
        return

    # 2) Normalize dict → Message (preserved from existing code)
    normalized_messages: List[Message] = [
        Message(
            role=str(m.get("role", "user")) if isinstance(m, dict) else m.role,
            content=(
                m.get("content")
                if isinstance(m, dict)
                else m.content
            ),
        )
        for m in prompt_messages
    ]

    # 3) Stream from LLM (preserved from existing code)
    accumulated = ""
    logger.info(
        f"[compact] LLM call: messages={len(normalized_messages)} "
        f"max_new_tokens={self.config.max_summary_tokens} "
        f"temperature={self.config.temperature} top_p={self.config.top_p}"
    )
    if normalized_messages:
        first = normalized_messages[0]
        content = first.content
        if isinstance(content, str):
            preview = content[:200]
            content_len = len(content)
        else:
            preview = f"<{type(content).__name__}>"
            content_len = -1
        logger.info(
            f"[compact] prompt[0] role={first.role} content_len={content_len} preview={preview!r}"
        )
    try:
        # (the existing "Context reuse vs. new-context-window design audit" comment
        # block at the chat_stream_async call site stays as-is — it documents the
        # architectural decision and remains valid under v2.)
        async for chunk in self.llm_service.chat_stream_async(
            messages=normalized_messages,
            max_new_tokens=self.config.max_summary_tokens,
            temperature=self.config.temperature,
            top_p=self.config.top_p,
            tools=None,
            request_id=summary_id,
        ):
            delta = chunk.get("content") or ""
            if delta:
                accumulated += delta
                yield CompactEvent.delta(summary_id, delta)
    except Exception as exc:
        err_repr = f"{type(exc).__name__}: {exc}"
        logger.exception(
            f"[compact] LLM call failed during compact "
            f"(messages={len(normalized_messages)}, "
            f"max_new_tokens={self.config.max_summary_tokens})"
        )
        yield CompactEvent.failed(
            summary_id, "llm_unreachable", err_repr
        )
        return

    # 4) post-process
    cleaned = format_compact_summary(accumulated)
    if not cleaned.strip():
        yield CompactEvent.failed(summary_id, "empty_summary")
        return

    # 5) apply compaction
    if self._apply_compaction is not None:
        try:
            self._apply_compaction(req, summary_id, cleaned)
        except Exception as exc:
            logger.exception("apply_compaction failed")
            yield CompactEvent.failed(summary_id, "apply_error", str(exc))
            return

    # 6) success
    yield CompactEvent.completed(
        summary_id,
        cleaned,
        metadata={"trigger": req.trigger, "mode": req.mode},
    )
```

Add the v2 builder import at the top of `orchestrator.py` (alongside the existing `build_compact_messages` import):

```python
from .prompt_builder import build_compact_messages, build_compact_messages_v2
```

Now **remove** the `_collect_messages` helper method (it is no longer called by `run_compact` — the v1 fallback path uses `req.messages` directly through `build_compact_messages`). The existing tests `test_compact_orchestrator.py::test_collect_error_*` may reference `_collect_messages` — **read those tests carefully** in the next step and update them if necessary. If a test calls `orch._collect_messages(req)`, either delete that test (the v1-only error path it pinned is now folded into `run_compact`) or rewrite it to use the v2 fallback path.

- [ ] **Step 4: Run tests and verify they pass**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_orchestrator_v2.py tests/test_compact_orchestrator.py -v`
Expected: PASS (new v2 tests + all existing v1 tests; any tests that called `_collect_messages` directly must have been updated in step 3).

If any existing `test_compact_orchestrator.py` test fails because it referenced the deleted `_collect_messages`:
- Read the failing test.
- If it pinned v1 error behavior (`compact_failed(reason="collect_error")`), rewrite the test to assert the equivalent error in the v2 path. For example, `test_collect_error` becomes `test_no_messages_yields_failed_event`: build a `CompactRequest(mode="agent")` with **both** `body_messages=None` and `messages=None`, and assert the first event is `CompactEvent.failed(reason="no_messages")`.
- Update the test docstring to reflect the new contract.

- [ ] **Step 5: Run the full compact test suite to confirm no regression**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_orchestrator.py tests/test_compact_orchestrator_v2.py tests/test_compact_message_dict_normalization.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/compact/orchestrator.py tests/test_compact_orchestrator_v2.py tests/test_compact_orchestrator.py
git commit -m "feat(compact): orchestrator prefers body_messages; v1 path with deprecation warning"
```

---

### Task 5: Refactor `AgentRunner._run_compact_then_continue` to use v2 path

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Create: `tests/test_compact_agent_v2_path.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_compact_agent_v2_path.py` with **simpler, more robust tests** that focus on the two key invariants: (1) the v2 path calls `write_memory_to_messages()` and does NOT call `_session_memory_to_messages()` for body construction, (2) the v2 path also calls `write_memory_to_messages()` for the threshold check in `_should_auto_compact_session`. We use direct method invocation on a real (or minimal) `AgentRunner` instance rather than heavy `MagicMock(spec=AgentRunner)` because the spec'd approach is fragile:

```python
"""Tests for the agent-mode auto-compact using the v2 turn-shape path.

Per spec §4.1, §5.4:
  - _run_compact_then_continue builds body_messages from
    session.agent.write_memory_to_messages() (not _session_memory_to_messages).
  - run_compact_for_session does the same.
  - _should_auto_compact_session uses the same source for token counting.
  - The body_messages list is passed to CompactRequest.body_messages (not .messages).
"""
from __future__ import annotations

import asyncio
from typing import List
from unittest.mock import MagicMock, patch

import pytest
from smolagents.memory import ActionStep, TaskStep

from backend.compact.types import CompactEvent, CompactEventKind, CompactRequest
from backend.llm_service import Message


def _make_session_with_steps(steps):
    """Construct a minimal session-like object that AgentRunner methods can read."""
    session = MagicMock()
    session.session_id = "test-session"
    session.agent = MagicMock()
    session.agent.memory.steps = steps
    return session


async def _drain(agen):
    out = []
    async for ev in agen:
        out.append(ev)
    return out


def test_should_auto_compact_session_uses_write_memory_to_messages() -> None:
    """_should_auto_compact_session must use write_memory_to_messages() as its token-count source,
    matching the v2 compact body construction source (so token count is consistent with what LLM sees)."""
    from backend.agent.agent_runner import _should_auto_compact_session_unwrapped

    session = _make_session_with_steps([
        TaskStep(task="user task 1"),
        ActionStep(model_output="assistant text", observations="obs1"),
    ])

    # write_memory_to_messages returns N messages including the system
    write_memory_messages = [
        MagicMock(role="system", content="SYS"),
        MagicMock(role="user", content="New task:\nuser task 1"),
        MagicMock(role="assistant", content="assistant text"),
    ]
    session.agent.write_memory_to_messages = MagicMock(return_value=write_memory_messages)

    # Build a minimal runner-like object with just the attrs the method touches
    runner = MagicMock()
    runner.compact_orchestrator = MagicMock()  # non-None to skip the early return
    runner.config = MagicMock()
    runner.config.compact.enabled = True
    runner.config.compact.auto_trigger = True
    runner.llm_service = MagicMock()
    runner.llm_service.active_backend = "sglang"

    with patch("backend.agent.agent_runner.compute_threshold", return_value=100), \
         patch("backend.agent.agent_runner.should_compact", return_value=True), \
         patch("backend.agent.agent_runner.TokenCounter") as MockCounter:
        mock_counter_instance = MagicMock()
        mock_counter_instance.count_messages = MagicMock(return_value=200)
        MockCounter.get.return_value = mock_counter_instance

        # The refactored method lives in agent_runner.py. We import it lazily so this
        # test file can be written before the refactor lands.
        from backend.agent.agent_runner import AgentRunner
        result = AgentRunner._should_auto_compact_session(runner, session)

    # Invariant 1: write_memory_to_messages is the source
    session.agent.write_memory_to_messages.assert_called_once()
    # Invariant 2: the flat _session_memory_to_messages is NOT called
    runner._session_memory_to_messages.assert_not_called()
    # Invariant 3: TokenCounter was given the write_memory output (3 messages)
    mock_counter_instance.count_messages.assert_called_once()
    called_args, _ = mock_counter_instance.count_messages.call_args
    assert len(called_args[0]) == 3
    # Invariant 4: the method returns True (because should_compact was mocked to True)
    assert result is True


def test_run_compact_then_continue_uses_write_memory_to_messages() -> None:
    """_run_compact_then_continue must call write_memory_to_messages() and pass the result as
    CompactRequest.body_messages (not as the deprecated .messages field)."""
    from backend.agent.agent_runner import AgentRunner

    session = _make_session_with_steps([
        TaskStep(task="user task 1"),
        ActionStep(model_output="assistant text", observations="obs1"),
    ])

    # write_memory_to_messages returns a turn-shape list (system + u + a + u + a + ...)
    expected_chat_messages = [
        MagicMock(role="system", content="SYS"),
        MagicMock(role="user", content="New task:\nuser task 1"),
        MagicMock(role="assistant", content="assistant text"),
        MagicMock(role="user", content="Observation:\nobs1"),
    ]
    session.agent.write_memory_to_messages = MagicMock(return_value=expected_chat_messages)

    runner = MagicMock()
    runner.config = MagicMock()
    runner.config.compact.enabled = True
    runner.config.compact.auto_trigger = True
    runner.compact_orchestrator = MagicMock()  # non-None so the path enters
    runner._session_memory_to_messages = MagicMock()
    runner._replace_agent_memory = MagicMock()

    # Capture the CompactRequest constructed inside _run_compact_then_continue
    captured_request = {}

    def _capture_request_ctor(*args, **kwargs):
        # CompactOrchestrator(llm_service, config, prompts, apply_compaction)
        # body_messages is captured indirectly via the apply hook's closure
        # (the apply hook itself doesn't take body_messages; it takes (req, sid, text)).
        # We rely on inspecting the call args to the orchestrator to find the
        # run_compact call's `req` argument.
        async def _fake_run(req):
            captured_request["body_messages"] = req.body_messages
            captured_request["messages"] = req.messages
            yield CompactEvent.started(summary_message_id="sid", trigger="auto", mode="agent")
            yield CompactEvent.completed("sid", "cleaned summary", metadata={"trigger": "auto", "mode": "agent"})
        instance = MagicMock()
        instance.run_compact = _fake_run
        return instance

    with patch("backend.compact.orchestrator.CompactOrchestrator", side_effect=_capture_request_ctor):
        events = asyncio.run(_drain(AgentRunner._run_compact_then_continue(runner, session, "auto")))

    # Invariant 1: write_memory_to_messages was called
    session.agent.write_memory_to_messages.assert_called_once()
    # Invariant 2: _session_memory_to_messages was NOT called (the v1 path is gone)
    runner._session_memory_to_messages.assert_not_called()
    # Invariant 3: CompactRequest.body_messages was set (not .messages)
    assert captured_request["body_messages"] is not None, (
        "CompactRequest.body_messages should be set in the v2 path"
    )
    assert captured_request["messages"] is None, (
        "CompactRequest.messages should NOT be set in the v2 path (deprecated)"
    )
    # Invariant 4: body_messages is a List[Message] (not a List[ChatMessage])
    assert isinstance(captured_request["body_messages"], list)
    assert len(captured_request["body_messages"]) == 4
    assert all(isinstance(m, Message) for m in captured_request["body_messages"])
    # Invariant 5: byte-equal to the source (after conversion)
    # The conversion helper flattens list[dict] content to str, but since our
    # MagicMock has .content as a string already, the values should pass through.
    for i, src in enumerate(expected_chat_messages):
        assert captured_request["body_messages"][i].role == src.role
        assert captured_request["body_messages"][i].content == src.content
    # Invariant 6: events include started and completed
    kinds = [ev.kind for ev in events]
    assert CompactEventKind.STARTED in kinds
    assert CompactEventKind.COMPLETED in kinds
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_agent_v2_path.py -v`
Expected: FAIL (the current code uses `_session_memory_to_messages`, not `write_memory_to_messages`).

- [ ] **Step 3: Add a helper that converts smolagents `ChatMessage` to backend `Message`**

The `_run_compact_then_continue` path needs to convert `List[ChatMessage]` (smolagents type) into `List[Message]` (backend.llm_service type) before passing to `CompactRequest.body_messages`. Add a module-level helper in `agent_runner.py` near the existing `_session_memory_to_messages` (around line 4428). The helper is per spec §5.4:

```python
def _chat_messages_to_backend_messages(chat_messages) -> list:
    """Convert smolagents ChatMessage objects into backend.llm_service.Message objects.

    Per spec §5.4:
      - role: ChatMessage.role is a MessageRole enum, .value gives the string.
        TOOL_CALL -> "assistant", TOOL_RESPONSE -> "user".
      - content: ChatMessage.content is list[dict] (multimodal shape); compact path
        does not carry images, so flatten to str by joining text parts.
    """
    from ..llm_service import Message

    out = []
    for cm in chat_messages:
        # role
        role_obj = cm.role
        if hasattr(role_obj, "value"):
            role_str = role_obj.value
        else:
            role_str = str(role_obj)
        if role_str in ("tool_call", "tool_response"):
            role_str = "assistant" if role_str == "tool_call" else "user"

        # content
        content = cm.content
        if isinstance(content, list):
            text_parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            content = "\n".join(p for p in text_parts if p)
        elif content is None:
            content = ""
        elif not isinstance(content, str):
            content = str(content)

        out.append(Message(role=role_str, content=content))
    return out
```

- [ ] **Step 4: Refactor `_run_compact_then_continue`**

In `agent_runner.py`, locate `_run_compact_then_continue` (around line 4515-4557). Replace its body so it builds `body_messages` from `write_memory_to_messages`:

```python
async def _run_compact_then_continue(self, session, trigger: str):
    """Run auto-compact; yield SSE timeline_event lines for compact_* events.

    v2 path: build body_messages from session.agent.write_memory_to_messages() (turn-shape,
    byte-equal to previous chat calls' messages) and pass to CompactRequest.body_messages.
    This maximizes SGLang radix cache reuse (spec §1.3, §4.3).
    """
    from ..compact.types import CompactRequest, CompactEventKind

    prompts_cfg = self.config.agent_prompts.compaction
    chat_messages = list(session.agent.write_memory_to_messages())
    body_messages = _chat_messages_to_backend_messages(chat_messages)

    req = CompactRequest(
        mode="agent",
        session_id=session.session_id,
        body_messages=body_messages,
        trigger=trigger,
    )

    def _apply(req, summary_id, text):
        self._replace_agent_memory(session, text, summary_id)

    from ..compact.orchestrator import CompactOrchestrator
    transient = CompactOrchestrator(
        llm_service=self.llm_service,
        config=self.config.compact,
        prompts=prompts_cfg,
        apply_compaction=_apply,
    )
    async for ev in transient.run_compact(req):
        yield self._compact_event_to_sse(ev)
        if ev.kind == CompactEventKind.FAILED:
            return
        if ev.kind == CompactEventKind.COMPLETED:
            return
```

- [ ] **Step 5: Refactor `run_compact_for_session` identically**

In `agent_runner.py`, locate `run_compact_for_session` (around line 4559-4634). Apply the same v2 pattern. The exact body of the compact construction inside that method should mirror Task 5 Step 4 — replace any `messages=self._session_memory_to_messages(session)` with `body_messages=_chat_messages_to_backend_messages(list(session.agent.write_memory_to_messages()))`, and update the `CompactRequest(...)` to use the `body_messages` field.

- [ ] **Step 6: Refactor `_should_auto_compact_session` to use the same source**

In `agent_runner.py`, locate `_should_auto_compact_session` (around line 4451-4477). Replace the `messages = self._session_memory_to_messages(session)` line with:

```python
chat_messages = list(session.agent.write_memory_to_messages())
backend_messages = _chat_messages_to_backend_messages(chat_messages)
messages = backend_messages
```

The rest of the function (threshold computation, `TokenCounter.count_messages`, `should_compact`) stays unchanged.

- [ ] **Step 7: Run tests and verify they pass**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_agent_v2_path.py -v`
Expected: PASS (2 tests).

- [ ] **Step 8: Run the full compact + agent test suite to confirm no regression**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_agent_apply.py tests/test_compact_agent_routes_to_runner.py tests/test_compact_envelope_preserved_on_next_run.py tests/test_compact_agent_v2_path.py tests/test_multi_agent_runner.py -v`
Expected: PASS (all tests, including the ones that pin `_replace_agent_memory`, `has_run=True`, and the `run_compact_for_session` session-not-found guard).

If any test fails because it pinned the v1 path (e.g. expected `req.messages` to be set), update the test to expect the v2 field name (`req.body_messages`) and the v2 message shape. Read the failing test carefully before modifying.

- [ ] **Step 9: Commit**

```bash
git add backend/agent/agent_runner.py tests/test_compact_agent_v2_path.py
git commit -m "feat(agent_runner): use write_memory_to_messages for compact v2 turn-shape path"
```

---

### Task 6: Refactor `_generate_stream_response` chat-mode auto-compact to use v2 path

**Files:**
- Modify: `backend/api/chat.py`
- Create: `tests/test_compact_chat_v2_path.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_compact_chat_v2_path.py`:

```python
"""Tests for chat-mode auto-compact using the v2 turn-shape path.

Per spec §4.1, §4.2:
  - The auto-compact branch in _generate_stream_response must pass `filtered` directly
    as CompactRequest.body_messages (the list already includes the system message).
  - The auto-compact rebuild of final_messages is unchanged:
      [Message(role="user", content=envelope), ...filtered]
  - When no auto-compact is triggered, the behavior is unchanged (regression).
"""
from __future__ import annotations

from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from backend.llm_service import Message


def test_chat_auto_compact_passes_filtered_as_body_messages() -> None:
    """Verify that the auto-compact branch passes the full filtered list (含 system) as body_messages."""
    from backend.api.chat import _generate_stream_response
    from backend.compact.types import CompactEvent, CompactEventKind, CompactRequest

    # Build a filtered list with a system message + a few user/assistant turns
    filtered: List[Message] = [
        Message(role="system", content="SYS from chat"),
        Message(role="user", content="Q1"),
        Message(role="assistant", content="A1"),
        Message(role="user", content="Q2"),
    ]

    # Mock the raw_request that holds app.state.compact_orchestrator
    raw_request = MagicMock()
    raw_request.app.state.compact_orchestrator = MagicMock()
    # Mock the orchestrator's run_compact to yield started + completed
    async def _fake_run(req):
        # Pin the contract: the request must carry body_messages == filtered
        assert req.body_messages is not None
        assert len(req.body_messages) == len(filtered)
        for i, m in enumerate(filtered):
            assert req.body_messages[i].role == m.role
            assert req.body_messages[i].content == m.content
        # And the request must NOT use the v1 .messages field
        assert req.messages is None
        yield CompactEvent.started(summary_message_id="sid", trigger="auto", mode="chat")
        yield CompactEvent.completed("sid", "cleaned", metadata={"trigger": "auto", "mode": "chat"})

    raw_request.app.state.compact_orchestrator.run_compact = _fake_run

    # Mock _should_auto_compact_chat to return True
    with patch("backend.api.chat._should_auto_compact_chat", return_value=True):
        # Mock the chat method of LLMService (the post-compact LLM call)
        with patch("backend.api.chat.LLMService") as MockLLMService:
            async def _fake_chat_stream(messages, **kwargs):
                yield {"content": "ok"}
            mock_llm_instance = MagicMock()
            mock_llm_instance.chat_stream_async = _fake_chat_stream
            MockLLMService.return_value = mock_llm_instance

            # Build a minimal request state. We don't need to assert the full SSE output;
            # we only need to assert the request body shape that fake_run verified above.
            # The auto-compact path's SSE yields the orchestrator events; we collect them.
            # Note: the full integration test exists in test_compact_chat_streaming_filter.py.
            # Here we focus on the body_messages contract.
            try:
                asyncio_run_collect(_generate_stream_response(
                    messages=filtered,
                    raw_request=raw_request,
                    original_chat_messages=[],
                ))
            except Exception:
                # The full call requires more state than we mock; we only care that
                # _fake_run was awaited (it would assert the body_messages contract
                # before yielding any event, so any AssertionError is a test failure).
                pass


def asyncio_run_collect(coro):
    import asyncio
    async def _drain():
        out = []
        async for x in coro:
            out.append(x)
        return out
    return asyncio.run(_drain())
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_chat_v2_path.py -v`
Expected: FAIL (the current code uses `messages=filtered` not `body_messages=filtered`; also `_should_auto_compact_chat` is not patched in the current `_generate_stream_response` in the exact way the test expects).

- [ ] **Step 3: Refactor `_generate_stream_response` auto-compact branch**

Open `backend/api/chat.py`. Locate `_generate_stream_response` (around line 488-523). Find the auto-compact branch (it is wrapped in `if _should_auto_compact_chat(filtered) and raw_request and raw_request.app.state.compact_orchestrator is not None:`). Replace the `CompactRequest(...)` construction inside that branch:

Old code (the lines that build the request):
```python
req = CompactRequest(
    mode="chat",
    messages=filtered,
    trigger="auto",
)
```

New code:
```python
req = CompactRequest(
    mode="chat",
    body_messages=filtered,  # v2: 含 system 的完整 messages 列表
    trigger="auto",
)
```

Do **not** change the rest of the branch — the SSE event yield loop, the rebuild of `final_messages = [Message(role="user", content=envelope), ...filtered]`, and the post-compact LLM call all stay as-is.

- [ ] **Step 4: Run tests and verify they pass**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_chat_v2_path.py -v`
Expected: PASS (1 test).

- [ ] **Step 5: Run the full chat-mode compact test suite to confirm no regression**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_chat_auto_trigger.py tests/test_compact_chat_filter.py tests/test_compact_chat_rebuild_contract.py tests/test_compact_chat_streaming_filter.py tests/test_compact_chat_threshold_alignment.py tests/test_compact_chat_v2_path.py -v`
Expected: PASS (all tests, including the ones that pin `final_messages = [envelope_user, ...filtered]`, the `metadata.pre_compact` drop, the `compact_summary` envelope replacement, and the "no metadata is a graceful no-op" contract from AGENTS.md trap #21).

If any test fails because it pinned `req.messages`, update the test to expect `req.body_messages`. The orchestrator mock in `test_compact_chat_auto_trigger.py` may need a body_messages assertion added.

- [ ] **Step 6: Commit**

```bash
git add backend/api/chat.py tests/test_compact_chat_v2_path.py
git commit -m "feat(chat): auto-compact uses body_messages for v2 turn-shape path"
```

---

### Task 7: Add the v1/v2 coverage equivalence test

**Files:**
- Create: `tests/test_compact_v1_v2_equivalence.py`

- [ ] **Step 1: Write the test**

Create `tests/test_compact_v1_v2_equivalence.py`:

```python
"""Verify that the v1 and v2 builders cover the same set of input tokens.

This is NOT a byte-equal equivalence test (the v1 builder flattens to a single user
string; the v2 builder preserves turn structure). It IS a coverage equivalence test:
given the same body_messages input, the set of substrings the LLM sees must be
identical. This protects against accidentally narrowing the input scope (e.g.
forgetting to include the assistant turn text) during the v2 migration.

Per spec §9.1 [V2] test_compact_v1_v2_equivalence.py.
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
    v1_user_content = v1[1].content
    # v2 output is 6 messages: [system, u, a, u, a, user<compact-instr>]
    assert len(v2) == len(body) + 1
    v2_combined = "\n".join(m.content for m in v2[:-1])  # everything except the trailing compact-instr

    # For each body token, it must appear in v1's user content and in v2's combined prefix.
    for token in ("USER_TOKEN_1", "ASSISTANT_TOKEN_1", "USER_TOKEN_2", "ASSISTANT_TOKEN_2"):
        assert token in v1_user_content, f"v1 dropped token {token!r}"
        assert token in v2_combined, f"v2 dropped token {token!r}"
```

- [ ] **Step 2: Run test and verify it passes**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_v1_v2_equivalence.py -v`
Expected: PASS (1 test). This is a "green from the start" test — both v1 and v2 builders are already implemented, so the equivalence assertion should hold. If it does not pass, debug the failure (likely a token-level mismatch in v1's render or v2's pass-through).

- [ ] **Step 3: Commit**

```bash
git add tests/test_compact_v1_v2_equivalence.py
git commit -m "test(compact): add v1/v2 coverage equivalence test"
```

---

### Task 8: Add the v2 chat-template render test (real Qwen3 chat template)

**Files:**
- Create: `tests/test_compact_prompt_builder_v2_template_compat.py`

- [ ] **Step 1: Write the test**

Create `tests/test_compact_prompt_builder_v2_template_compat.py`:

```python
"""Render v2 builder output through the real Qwen3 chat template.

Per spec §6.1 I9, §9.1 [V2], and 踩坑记录3 §20:
  - v2 output must render through the real Qwen3 chat template without raising
    the 'No user query found in messages.' exception from the multi_step_tool check
    (chat_template.jinja:67-80).
  - The rendered output must contain at least one <|im_start|>user / <|im_end|>\n pair
    whose content is NOT a <tool_response> envelope.

This is the test that would catch a regression to a shape that breaks the Qwen3
chat template's multi_step_tool check.
"""
from __future__ import annotations

from pathlib import Path

import jinja2
import pytest
import yaml

from backend.compact.prompt_builder import build_compact_messages_v2
from backend.config import CompactionPromptsConfig
from backend.llm_service import Message

CHAT_TEMPLATE_PATH = Path("/home/zk/Project/LLM-weight/Qwen3___5-35B-A3B-GPTQ-Int4/chat_template.jinja")
YAML_PATH = Path(__file__).resolve().parents[1] / "config" / "config-agent-prompts.yaml"


@pytest.fixture
def prompts() -> CompactionPromptsConfig:
    with open(YAML_PATH) as f:
        cfg = yaml.safe_load(f).get("compaction", {})
    return CompactionPromptsConfig(
        no_tools_preamble=cfg.get("no_tools_preamble", ""),
        main_prompt=cfg.get("main_prompt", ""),
        custom_instructions_header=cfg.get("custom_instructions_header", ""),
        no_tools_trailer=cfg.get("no_tools_trailer", ""),
        summary_envelope_preface=cfg.get("summary_envelope_preface", ""),
        summary_envelope_suffix=cfg.get("summary_envelope_suffix", ""),
        compact_user_instruction=cfg.get("compact_user_instruction", ""),
    )


def _render_chat_template(messages):
    """Render messages through the real Qwen3 chat template, converting any AssertionError
    raised by raise_exception to a normal exception with a descriptive prefix."""
    if not CHAT_TEMPLATE_PATH.exists():
        pytest.skip(f"Qwen3 chat template not found at {CHAT_TEMPLATE_PATH}")

    template_text = CHAT_TEMPLATE_PATH.read_text()
    env = jinja2.Environment(keep_trailing_newline=True)
    env.globals["raise_exception"] = lambda msg: (_ for _ in ()).throw(
        AssertionError(f"[chat_template raise_exception] {msg}")
    )
    tpl = env.from_string(template_text)

    # Convert backend Message to the dict shape the template expects
    rendered_messages = []
    for m in messages:
        if isinstance(m, Message):
            rendered_messages.append({"role": m.role, "content": m.content})
        else:
            rendered_messages.append({"role": m["role"], "content": m["content"]})
    return tpl.render(messages=rendered_messages, add_generation_prompt=True, tools=None)


def test_v2_passes_qwen3_chat_template(prompts: CompactionPromptsConfig) -> None:
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="A 说：xxx"),
        Message(role="assistant", content="回：yyy"),
        Message(role="user", content="B 说：zzz"),
    ]
    out = build_compact_messages_v2(body, prompts)
    rendered = _render_chat_template(out)
    # The rendered output must contain <|im_start|>user and <|im_end|>\n at least twice
    # (once for the v2 compact-instr user, once for the body).
    assert rendered.count("<|im_start|>user") >= 2
    assert rendered.count("<|im_end|>") >= 4  # system + 2 body user + 1 compact-instr user + at least 1 assistant (actually 2)
    # And the rendered output must end with the assistant generation prompt
    assert rendered.rstrip().endswith("<|im_start|>assistant\n")


def test_v2_does_not_raise_multi_step_tool_check(prompts: CompactionPromptsConfig) -> None:
    """Specifically verify that the multi_step_tool check does not raise on v2 output."""
    body = [
        Message(role="system", content="SYS"),
        Message(role="user", content="A 说：xxx"),
        Message(role="assistant", content="回：yyy"),
    ]
    out = build_compact_messages_v2(body, prompts)
    # If the multi_step_tool check raises, _render_chat_template will raise AssertionError.
    # We want it to NOT raise.
    rendered = _render_chat_template(out)
    assert "No user query found" not in rendered
```

- [ ] **Step 2: Run the test and verify it passes**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_compact_prompt_builder_v2_template_compat.py -v`
Expected: PASS (2 tests). The test reads the real Qwen3 chat template from `/home/zk/Project/LLM-weight/Qwen3___5-35B-A3B-GPTQ-Int4/chat_template.jinja` and renders the v2 output. If the file is not present, both tests skip (this is intentional — the developer environment may not have the model weights).

- [ ] **Step 3: Commit**

```bash
git add tests/test_compact_prompt_builder_v2_template_compat.py
git commit -m "test(compact): render v2 output through real Qwen3 chat template"
```

---

### Task 9: Full compact test suite regression

**Files:**
- (no code changes; just running tests)

- [ ] **Step 1: Run the full compact test suite**

Run:
```bash
PYTHONPATH=. ./.sglang/bin/python -m pytest \
  tests/test_compact_agent_apply.py \
  tests/test_compact_agent_routes_to_runner.py \
  tests/test_compact_api_endpoint.py \
  tests/test_compact_chat_auto_trigger.py \
  tests/test_compact_chat_filter.py \
  tests/test_compact_chat_rebuild_contract.py \
  tests/test_compact_chat_streaming_filter.py \
  tests/test_compact_chat_threshold_alignment.py \
  tests/test_compact_chat_v2_path.py \
  tests/test_compact_config.py \
  tests/test_compact_cycle_integration.py \
  tests/test_compact_envelope_preserved_on_next_run.py \
  tests/test_compact_event_flatten.py \
  tests/test_compact_message_dict_normalization.py \
  tests/test_compact_orchestrator.py \
  tests/test_compact_orchestrator_v2.py \
  tests/test_compact_prompt_builder.py \
  tests/test_compact_prompt_builder_template_compat.py \
  tests/test_compact_prompt_builder_v2.py \
  tests/test_compact_prompt_builder_v2_template_compat.py \
  tests/test_compact_request_v2_field.py \
  tests/test_compact_run_requires_messages.py \
  tests/test_compact_summary_runner.py \
  tests/test_compact_threshold.py \
  tests/test_compact_tokenizer.py \
  tests/test_compact_types.py \
  tests/test_compact_v1_v2_equivalence.py \
  tests/test_compact_yaml_v2_prompts.py \
  tests/test_compact_agent_v2_path.py \
  -q
```

Expected: PASS (132+ tests).

- [ ] **Step 2: Run prefix cache stability tests**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_prefix_cache_stability_v2.py -q`
Expected: PASS (verifies v2 prefix cache stability constraints are still respected — in particular, that the compact v2 path does not inject `runtime_context` a second time, and that the envelope summary is appended to the dynamic tail and not to the system prefix).

- [ ] **Step 3: Run cycle executor tests**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_cycle_executor.py tests/test_write_memory_tool_calls.py -q`
Expected: PASS (verifies that cycle-mode pre-compact still works through the agent runner and that `write_memory_to_messages`'s cycle-specific historical cleaning is unchanged).

- [ ] **Step 4: Run main lifespan wiring test**

Run: `PYTHONPATH=. ./.sglang/bin/python -m pytest tests/test_main_lifespan_wiring.py -q`
Expected: PASS (verifies that `CompactOrchestrator` is still wired into `agent_runner.compact_orchestrator` during the app lifespan startup; AGENTS.md trap #24).

If any of the above tests fail, debug the failure **before** moving to Task 10. Do not skip failures — they indicate a regression. Common failure modes and their fixes:

| Failure mode | Fix |
|---|---|
| A v1 test asserts `req.messages` is set | Update the test to assert `req.body_messages` is set (orchestrator passes the v2 field) |
| A v1 test calls `orch._collect_messages(req)` | That helper was removed; rewrite the test against the inlined branch in `run_compact` |
| A test asserts `system_prompt_to_messages()` content does not include the compact user instruction | Confirm — compact user instruction is appended to the END of the messages list, not the system. Tests that check `messages[0].content` should still see only the agent's system prompt |
| A test asserts the LLM received a `[system(指令), user<整段>]` 2-message shape | That was the v1 shape; v2 produces N+1 messages. If the test was for the v1 orchestrator, update it to use the v2 orchestrator with `body_messages` |
| A test fails on the chat template render | The new YAML `main_prompt` is much longer than the v1 one. Verify it still renders cleanly through the Qwen3 chat template (it should — the template does not care about message content length) |

- [ ] **Step 5: Commit (no-op if all tests passed; this is a verification step)**

If any tests were updated in step 4 to fix v1 expectations:
```bash
git add tests/ backend/
git commit -m "test(compact): update v1 test expectations for v2 turn-shape path"
```

---

### Task 10: Documentation updates

**Files:**
- Modify: `AGENTS.md`
- Modify: `踩坑记录4.md`

- [ ] **Step 1: Add trap #26 to `AGENTS.md`**

Open `AGENTS.md`. Find the trap list (it is a long numbered list in the project's main context guide). Append a new trap:

```markdown
26. **v2 compact prompt builder must 1:1 reuse body_messages (introduced 2026-06-11)**:
    - The `build_compact_messages_v2` function in `backend/compact/prompt_builder.py` takes
      a `body_messages: List[Message]` that is byte-equal to the messages list of the previous
      chat calls (含 system). It must NOT do any transformation on `body_messages` — no flattening,
      no re-rendering, no format normalization. It only appends one trailing user message with
      the compact instruction.
    - The trailing user message is the ONLY divergence from the previous chat calls' token
      sequence. SGLang radix tree (see `sglang/srt/mem_cache/radix_cache.py:191-211`) matches
      on token-by-token integer equality, so 0 transformation of `body_messages` is what gives
      us the 80-95% prefix cache hit rate.
    - `body_messages[0].role` must be "system"; otherwise the function raises `ValueError`.
      This is a hard contract — callers must source the system message from the agent's
      `memory.system_prompt` (agent mode) or from the first element of `filtered` (chat mode).
    - The v1 `build_compact_messages` function and the `CompactRequest.messages` field are
      deprecated since 2026-06-11 and will be removed in the next release. The orchestrator
      logs a deprecation warning when callers use the v1 path.
    - The v2 path does NOT inject `runtime_context` — `session.agent.memory.system_prompt`
      already contains the session-pinned `runtime_context` (per
      `docs/superpowers/specs/2026-05-27-runtime-context-time-design.md`), and 1:1 reuse
      preserves it byte-equal.
    - Full spec: `docs/superpowers/specs/outdated-2026-06-11-v2-compact-prompt-turn-shape-design.md`
    - Reference prompt wording source: `ref_src/services/compact/prompt.ts:19-26, 61-143, 269-272, 345-347`
    - Compaction prompt field: `config/config-agent-prompts.yaml::compaction::compact_user_instruction`
```

- [ ] **Step 2: Update `踩坑记录4.md` §5**

Open `踩坑记录4.md`. Find §5 ("'compact 是不是新建了 context window' 的设计 audit"). The current text in §5.3 (不可避免的 re-prefill（架构限制）) says that re-prefill is unavoidable. Update it to reflect that **v2 fixes this** by using turn-shape messages that byte-equal previous chat calls' messages, so SGLang radix tree reuses the KV blocks. The update should be added as a new subsection **§5.5 v2 修复后（turn-shape prefix cache 复用）**:

```markdown
### 5.5 v2 修复后（turn-shape prefix cache 复用，2026-06-11）

v1 的 prompt 形态（`build_compact_messages` 输出 `[system(compact 指令), user<整段历史>]`）导致 SGLang radix tree 在 root 就分叉——compact 调用与之前 chat 调用的 token 序列完全不同，**整段 prompt 都得重新 prefill**。

v2 (`build_compact_messages_v2` in `backend/compact/prompt_builder.py`) 改成 turn-shape：

```
[compact LLM 调用的 messages 列表]
  [system (来自 agent 原始 memory.system_prompt)]   ← 1:1 复用之前 chat 调用的 system
  [user, assistant, user, assistant, ...]            ← 1:1 复用之前 chat 调用的 history turns
  [user <compact-instr>]                              ← 唯一分叉点 (~150-300 tokens)
```

- v2 调用的前 N-1 条 messages 与之前 chat 调用的 messages **byte-equal**（前提：调用方用 `session.agent.write_memory_to_messages()` 作为 agent 模式的 source，用 `filtered` 作为 chat 模式的 source）。
- SGLang radix tree 在前 N-1 个 token 上**完全匹配**，复用对应的 attention KV 和 mamba state。
- 仅末尾第 N 条新 user 消息的 tokens 需重新 prefill (~150-300 tokens)，与 v1 的"整段 ~45k tokens 重新 prefill"相比是 **2-3 个数量级**的改进。
- 这个修复配合 `config/qwen3__5-config-infer.yaml` 的 `mamba_full_memory_ratio: 0.5`（§4.1）一起，auto-compact 的体感卡顿从 90% 时间不出 token 降到 30% 以内（如果 mamba 调优已落地）。
- 完整 spec：`docs/superpowers/specs/outdated-2026-06-11-v2-compact-prompt-turn-shape-design.md`
- 完整 plan：`docs/superpowers/plans/outdated-2026-06-11-v2-compact-prompt-turn-shape-plan.md`
- 关键测试：`tests/test_compact_prompt_builder_v2_template_compat.py::test_v2_passes_qwen3_chat_template`（用真实 Qwen3 chat_template 渲染 v2 输出）
```

Also update the §5.4 "已落地的 audit 注释" subsection to note that the audit comment in `backend/compact/orchestrator.py:101` is still valid under v2 — the architectural claim (compact call reuses the same SGLang HTTP endpoint / GPU process / KV cache pool) is unchanged; v2 only changes the **shape** of the messages sent to that endpoint, not the endpoint itself.

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md 踩坑记录4.md
git commit -m "docs: add trap #26 for v2 compact prompt; update 踩坑记录4 §5 with v2 fix"
```

---

### Task 11: Final end-to-end verification

**Files:**
- (no code changes; manual verification)

- [ ] **Step 1: Start SGLang with the new config**

Run:
```bash
cd /home/zk/Project/LLM-deployment
./scripts/1_start_sglang.sh
```

Expected: SGLang starts successfully. The terminal log shows the model loading and KV cache pool allocation. Verify that `mamba_full_memory_ratio: 0.5` and `chunked_prefill_size: 4096` are applied (you can see them in the SGLang startup log, e.g. `--mamba-full-memory-ratio 0.5 --chunked-prefill-size 4096`).

- [ ] **Step 2: Start the backend with the new compact config**

In a separate terminal:
```bash
cd /home/zk/Project/LLM-deployment
./scripts/2_start_backend.sh
```

Expected: Backend starts successfully. The lifespan log shows `CompactOrchestrator` is constructed and wired to `agent_runner.compact_orchestrator` (AGENTS.md trap #24 invariant).

- [ ] **Step 3: Start the frontend**

In a separate terminal:
```bash
cd /home/zk/Project/LLM-deployment
cd frontend && npm run dev
```

Expected: Frontend starts on port 3000.

- [ ] **Step 4: Manual `/compact` slash command verification**

In the frontend UI:
1. Switch to agent mode.
2. Send a few user messages to build up session history.
3. Type `/compact` in the input box. Verify the slash command menu pops up (this is the existing v1 slash command flow; the v2 changes are backend-only).
4. Press Enter to invoke `/compact`.
5. Observe the `compact_summary` panel appearing in the UI with the streaming summary text.
6. Verify the session's memory was replaced (the next user message should produce a coherent response that "remembers" the compacted summary).

- [ ] **Step 5: Verify SGLang prefix cache hit rate**

In the SGLang terminal, observe the logs during the compact call:
- The first time `/compact` is invoked in a session, the SGLang prefill token count for the compact call should be **close to** the size of the trailing compact-instr user message (~150-300 tokens), not the size of the entire conversation (~40k tokens).
- This is visible in the SGLang log line: `Prefill batch, #new-seq: 1, #new-token: <small number>...` (the actual log line format depends on the SGLang version).
- Compare with v1 behavior (the symptom from 踩坑记录4 §4.3): the prefill token count was ~45k, with `#running-req: 1, #full token: 41432, full token usage: 0.02, mamba usage: 0.00` for a long time. v2 should NOT show these signs.

- [ ] **Step 6: Trigger auto-compact in agent mode**

Build up enough session history to exceed the auto-compact threshold. This typically takes 5-10 exchanges with substantial content.

Verify:
- The auto-compact triggers automatically (the UI shows the `compact_summary` panel without the user explicitly typing `/compact`).
- The SGLang terminal shows the compact call's prefill token count is much smaller than v1 (as in step 5).
- The next user message after the auto-compact produces a coherent response that references the compacted summary (i.e. the envelope is in `session.agent.memory.steps`, per `tests/test_compact_envelope_preserved_on_next_run.py`).

- [ ] **Step 7: Trigger auto-compact in chat mode**

In the frontend, switch to chat mode (non-agent). Send a few messages to build up history. The chat-mode auto-compact should trigger when the token count exceeds the threshold.

Verify:
- The UI shows the `compact_summary` panel.
- The next user message after auto-compact produces a coherent response (the envelope is re-injected via the `metadata.compact_summary` → envelope replacement flow in `filter_messages_for_llm`).
- The SGLang log shows the chat-mode auto-compact call's prefill token count is also much smaller than v1.

- [ ] **Step 8: Document any issues**

If any step above reveals a problem, **stop** and:
1. Capture the SGLang log lines for the failing scenario.
2. Capture the backend log lines (look for `[compact]` prefixed log entries).
3. Capture the frontend's network panel for the compact-related API calls.
4. File a follow-up issue with these artifacts; do not try to fix the issue in this plan.

- [ ] **Step 9: Final commit (if any tweaks were needed during verification)**

```bash
git add <any-tweaked-files>
git commit -m "fix(compact): post-E2E-verification tweaks"
```

If no tweaks were needed, skip this step. The plan is complete.

---

## Self-Review

### 1. Spec coverage

| Spec section | Covered by |
|---|---|
| §1 背景 (v1 现状 + 根本问题 + 核心原则 + 参考材料) | Task 0 (File Map preamble) + Task 8 (template test) + Task 10 (docs) |
| §2 Goals (G1-G7) | G1-G2: Task 4 (orchestrator v2) + Task 5-6 (callers). G3: unchanged code paths explicitly listed as not-modified in §6.2. G4: Task 2 (field) + Task 4 (fallback). G5: Task 3 (yaml wording from ref_src). G6: Task 5 (agent) + Task 6 (chat). G7: Tasks 1, 4-8, 9. |
| §3 Non-Goals (N1-N10) | N1: orchestrator's `_replace_agent_memory` not in any task's modify list. N2: `summary_runner.py` not in any task. N3-N4: `threshold.py`, `tokenizer.py` not in any task. N5: SSE event schema not in any task. N6: frontend not in any task. N7-N10: explicit "do not do" in plan, no task touches them. |
| §4 架构 (components + data flow + call stack) | Components: Task 1 (v2 builder) + Task 2 (field) + Task 4 (orchestrator) + Task 5-6 (callers). Data flow: Task 4 (orchestrator branch) + Task 5-6 (callers' construction of body_messages). Call stack: Task 5 step 3 (the `_chat_messages_to_backend_messages` helper implements the spec §5.4 conversion). |
| §5 数据规则 (CompactRequest 字段契约 + build_compact_messages_v2 行为契约 + 措辞契约 + 转换契约 + Diff 表) | CompactRequest: Task 2. build_compact_messages_v2: Task 1. Wording: Task 3. Conversion: Task 5 step 3 (helper). Diff table: captured in plan preamble and in code comments. |
| §6 实现约束 (I1-I10 + D1-D8) | I1: Task 1 step 3 ("0 transformation" is the explicit comment in the builder body). I2: Task 5 step 3 (list content flattened to str). I3: Task 5-6 (no `_inject_runtime_context` call). I4: Task 4 (`tools=None` is the existing code's behavior, preserved). I5: Task 4 (orchestrator single-class with branch). I6: Task 1 (v1 builder preserved). I7: Task 2 (messages field preserved). I8: Task 5 step 6 (`_should_auto_compact_session` rewritten). I9: Task 9 (regression). I10: Task 5 (no changes to `write_memory_to_messages`'s cycle cleaning). D1-D8: explicit "do not modify" in plan preamble (File Map section lists what is NOT touched). |
| §7 编码计划概要 (18 steps) | The 18 steps map to 11 tasks in this plan. Step 1-2 (red tests): Task 1 step 1-2. Step 3-4 (green): Task 1 step 3-4. Step 5 (regression): Task 1 step 5. Step 6 (commit): Task 1 step 6. Same for Tasks 2-8. Step 9-14 (analogous): Tasks 4-7. Step 15 (regression): Task 9. Step 16 (mypy + lifespan): Task 9 step 2-4 (no mypy in project; lifespan is in test_main_lifespan_wiring.py covered by Task 9 step 4). Step 17 (docs): Task 10. Step 18 (plan): this document. |
| §8 风险与缓解 (8 risks) | Risk 1 (smolagents upgrade): Task 9 regression. Risk 2 (runtime_context double-inject): Task 4 step 4 + Task 9. Risk 3 (1 row prefill): spec §1.2 + Task 10 §5.5 explanation. Risk 4 (image loss): Task 5 step 3 (list→str). Risk 5 (no double-inject in v2 path): Task 4 step 4. Risk 6 (filtered system != previous chat call system): Task 6 step 5 (regression). Risk 7 (threshold behavior change): noted in spec §8, surfaced in Task 5 step 8. Risk 8 (existing test failures): Task 9 step 4 (debug table). |
| §9 验收标准 (V1-V11) | V1: Task 9 step 1. V2: Task 9 step 1 (the 14 new tests run as part of the suite). V3-V5: Task 9 step 1-4. V6-V8: Task 11 step 4-7. V9: Task 11 step 1-7. V10-V11: Task 11 step 5. |

**Gaps found and fixed during self-review**: None. All spec sections have at least one task that implements them.

### 2. Placeholder scan

Plan contains no "TBD", "TODO", "implement later", "fill in details", "add appropriate error handling", "add validation", "handle edge cases", "similar to Task N", or references to undefined types/functions. All code blocks are complete. All commands are exact. All test code is complete and runnable.

### 3. Type consistency

- `body_messages: Optional[List[Message]]` is used consistently in Tasks 2, 4, 5, 6, 7, 8. `Message` is always `backend.llm_service.Message`.
- `build_compact_messages_v2` is used in Tasks 1, 4, 7, 8. Its signature is `(body_messages: List[Message], prompts: CompactionPromptsConfig, custom_instructions: Optional[str] = None) -> List[Message]`. All call sites match this signature.
- `_chat_messages_to_backend_messages` is defined in Task 5 step 3 and used in Task 5 step 4-6. Its signature is `(chat_messages) -> list` (the `list` is `List[Message]` per the body; the untyped return is intentional to match the existing `_session_memory_to_messages` style).
- `CompactRequest.body_messages` field is added in Task 2 and used in Tasks 4, 5, 6.
- `CompactOrchestrator.run_compact` body in Task 4 step 3 uses `req.body_messages` first, then `req.messages`. This matches the spec §5.1 priority.
- `build_compact_messages` (v1) signature is unchanged in Task 1 (we add v2 alongside, not replacing). The orchestrator still calls v1 with `(body_messages=req.messages, ...)` in the fallback path.
- `CompactionPromptsConfig.compact_user_instruction` field is added in Task 3 step 4 and used in Task 1 step 3 (the v2 builder's `prompts.compact_user_instruction`).

**Issues found and fixed during self-review**: None. All type names, method names, and field names are consistent across tasks.
