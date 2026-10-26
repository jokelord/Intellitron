# Compact Prompt Shape Prefix-Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **2026-06-14 status/caution:** This implementation has since moved from "reliable default, cache-first opt-in" to cache-first default with reliable fallback, compact `extra_body` (`enable_thinking=false`), summary-only prompt, `source_token_budget`, and SGLang launch wiring for `schedule_policy`/`enable_cache_report`. Current code also uses `source_token_budget` as a cache-first eligibility gate: if the untrimmed cache-first source exceeds the budget, skip cache-first and run budgeted reliable fallback (`cache_first_skipped_reason=source_exceeds_budget`). Treat older snippets in this plan that say `prompt_shape: reliable_position1`, `prompt_control_enabled: false`, `<analysis> + <summary>`, or "always attempt full cache-first" as historical rollout notes, not current desired defaults. See `/踩坑记录4.md` §5.7.6.

**Goal:** Improve compact prompt-shape prefix-cache hit rate safely by adding observability, a stable compact-control sentinel, cache-first prompt plans, validation, and reliable fallback without changing the external compact timeline protocol.

**Architecture:** Current implementation uses cache-first tail compact-control as the default performance path, buffers fast-path output until validation passes, and retries once with the reliable position-1 compact shape on validation failure. Diagnostics are log/metadata only and never enter prompt text, raw timeline, dispatch text, or frontend bubbles.

**Tech Stack:** Python 3.12, FastAPI, smolagents, SGLang/vLLM/OpenAI-compatible streaming, pytest, Next.js/TypeScript frontend timeline projection tests.

**Spec:** `docs/superpowers/specs/2026-06-13-compact-prompt-shape-prefix-cache-prd.md`

---

## Current Context And Corrections

The PRD is the source of truth for this work. Older documents are useful for history, but several points must not be copied into implementation.

- `docs/superpowers/plans/outdated-2026-06-11-v2-compact-prompt-turn-shape-plan.md` is superseded for cache behavior. It still describes tail-only compact instruction and near-perfect prefix reuse as the desired implementation. That is no longer valid because current code intentionally uses agent system override and position-1 instruction for reliability.
- `docs/superpowers/specs/outdated-2026-06-11-v2-compact-prompt-turn-shape-design.md` is historical context only. Treat its earlier tail-only sections as historical rationale, not current architecture.
- `docs/superpowers/specs/outdated-2026-06-12-post-compact-slim-system-prompt-design.md` and `docs/superpowers/plans/outdated-2026-06-12-post-compact-slim-system-prompt-plan.md` are local UX latency workaround documents, not prefix-cache architecture. The slim path skips runtime context and tool schemas, which deliberately changes the normal prompt prefix. Do not generalize it as a cache-hit improvement.
- `backend/agent/agent_runner.py` comments around `_build_compact_body_messages()` currently claim SGLang per-request KV sharing is vacuous. That statement conflicts with the 2026-06-13 PRD's goal of recovering longest-prefix reuse where shape permits it. Update those comments during the implementation.

Hard constraints from the PRD and `AGENTS.md`:

- Do not add per-turn summarization.
- Do not feed raw timeline, dispatch card text, routing deltas, frontend bubbles, or UI state into LLM input.
- Do not revive `working_memory`, `unresolved_items`, `tool_ledger`, `recent_visible_history`, `agent_contract`, `user_goal`, or prompt-injected `context_pack`.
- Do not change `_replace_agent_memory()` envelope semantics.
- Do not include the current user turn in auto-compact input for chat or agent mode.
- Do not change compact SSE event families: `compact_started`, `compact_delta`, `compact_completed`, `compact_failed`.
- Do not expose failed fast-path deltas to UI unless retraction semantics are designed. This plan buffers fast-path deltas.

---

## File Map

Backend files to create:

- `backend/compact/prompt_diagnostics.py`: prompt snapshot, divergence comparison, usage/cached-token extraction helpers.
- `backend/compact/prompt_control.py`: stable compact-control sentinel helpers and user-message escaping.
- `backend/compact/prompt_plan.py`: `CompactPromptPlan` and builders for `reliable_position1` and `cache_first_tail` shapes.
- `backend/compact/validation.py`: compact summary validation before applying memory replacement.

Backend files to modify:

- `backend/config.py`: add compact config fields and compaction prompt fields.
- `config/config-misc.yaml`: add default-off cache-first config and validation thresholds.
- `config/config-agent-prompts.yaml`: add stable sentinel, system clause, and tail instruction text.
- `backend/compact/types.py`: extend `CompactRequest` with cache-first/reference messages.
- `backend/compact/prompt_builder.py`: keep existing position-1 builder and add a cache-first tail builder or delegate to `prompt_plan.py`.
- `backend/compact/orchestrator.py`: run prompt plans, collect diagnostics, buffer fast-path deltas, validate, fallback once.
- `backend/agent/agent_runner.py`: build both reliable and cache-first compact bodies, preserve current user ordering, keep `_replace_agent_memory()` unchanged, update comments.
- `backend/agent/local_llm_model.py`: record normal agent prompt snapshots after runtime-context injection.
- `backend/api/chat.py`: apply prompt-control system injection consistently when enabled, exclude current user from auto-compact, pass reliable and cache-first bodies.
- `backend/llm_service.py`: propagate streaming `usage` chunks so compact diagnostics can see token/cached-token data when the backend exposes it.
- `backend/api/compact.py`: no protocol fork; only ensure request fields remain compatible if manual chat compact uses new internals.

Frontend files to modify only if needed:

- `frontend/src/lib/timeline-projection.ts`: only add optional metadata passthrough if tests prove it is useful; default should remain non-rendering.
- `frontend/src/lib/timeline-projection.test.mts`: assert unknown compact metadata does not create chat text.
- `frontend/src/lib/compact-projection.test.mts`: assert compact metadata does not affect summary content.

Tests to add or extend:

- `tests/test_compact_config.py`
- `tests/test_compact_prompt_control.py`
- `tests/test_compact_prompt_plan.py`
- `tests/test_compact_prompt_diagnostics.py`
- `tests/test_compact_validation.py`
- `tests/test_compact_orchestrator_cache_first.py`
- `tests/test_compact_agent_cache_first.py`
- `tests/test_compact_chat_cache_first.py`
- `tests/test_llm_stream_usage_propagation.py`
- Existing compact tests listed in `AGENTS.md` Phase 1 baseline.

---

## Schedule

Day 1: Phase 0 observability and stream usage propagation.

Day 2: Config, YAML prompt control, sentinel escaping, prompt plan builders.

Day 3: Validation, orchestrator fallback, agent integration.

Day 4: Chat integration, frontend no-protocol-fork tests, docs cleanup, full verification.

E2E measurement after code passes: run reliable baseline first, then enable cache-first locally and compare cached-token/prefill metrics.

---

### Task 1: Add Config And YAML Controls

**Files:**
- Modify: `backend/config.py:397-449`, `backend/config.py:1005-1029`, `backend/config.py:1321-1340`
- Modify: `config/config-misc.yaml:8-22`
- Modify: `config/config-agent-prompts.yaml:403-519`
- Test: `tests/test_compact_config.py`

- [ ] **Step 1: Write failing config tests**

Append these tests to `tests/test_compact_config.py`:

```python
def test_compact_prompt_shape_config_defaults(monkeypatch, tmp_path):
    _make_minimal_config_tree(tmp_path, compact_section=None)
    loader = _loader_with_root(monkeypatch, tmp_path)
    cfg = loader.load()

    assert cfg.compact.diagnostics_enabled is True
    assert cfg.compact.prompt_shape == "reliable_position1"
    assert cfg.compact.prompt_control_enabled is False
    assert cfg.compact.validation_min_summary_chars == 200
    assert cfg.compact.validation_min_summary_prompt_ratio == 0.002
    assert cfg.compact.validation_require_summary_sections is True


def test_compact_prompt_shape_config_explicit_values(monkeypatch, tmp_path):
    _make_minimal_config_tree(
        tmp_path,
        compact_section={
            "diagnostics_enabled": False,
            "prompt_shape": "cache_first_tail_with_fallback",
            "prompt_control_enabled": True,
            "validation_min_summary_chars": 500,
            "validation_min_summary_prompt_ratio": 0.01,
            "validation_require_summary_sections": False,
        },
    )
    loader = _loader_with_root(monkeypatch, tmp_path)
    cfg = loader.load()

    assert cfg.compact.diagnostics_enabled is False
    assert cfg.compact.prompt_shape == "cache_first_tail_with_fallback"
    assert cfg.compact.prompt_control_enabled is True
    assert cfg.compact.validation_min_summary_chars == 500
    assert cfg.compact.validation_min_summary_prompt_ratio == 0.01
    assert cfg.compact.validation_require_summary_sections is False


def test_compaction_prompt_control_fields_loaded(monkeypatch, tmp_path):
    _make_minimal_config_tree(
        tmp_path,
        agent_prompts={
            "coordinator_1": {"instructions": "test"},
            "compaction": {
                "compact_control_sentinel": "[[SENTINEL]]",
                "compact_control_system_clause": "If latest user starts with [[SENTINEL]], summarize.",
                "compact_control_user_instruction": "[[SENTINEL]] summarize prior messages.",
            },
        },
    )
    loader = _loader_with_root(monkeypatch, tmp_path)
    cfg = loader.load()

    assert cfg.agent_prompts.compaction is not None
    assert cfg.agent_prompts.compaction.compact_control_sentinel == "[[SENTINEL]]"
    assert "latest user" in cfg.agent_prompts.compaction.compact_control_system_clause
    assert "prior messages" in cfg.agent_prompts.compaction.compact_control_user_instruction
```

- [ ] **Step 2: Run the failing tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_config.py -q`

Expected: FAIL with `AttributeError` for the new config/prompt fields.

- [ ] **Step 3: Add config fields**

Modify `backend/config.py` dataclasses:

```python
@dataclass
class CompactionPromptsConfig:
    """Compact 命令的提示词模板（从 config-agent-prompts.yaml: compaction 段加载）"""

    status_message: str = "Compressing conversation context..."
    no_tools_preamble: str = ""
    main_prompt: str = ""
    custom_instructions_header: str = ""
    no_tools_trailer: str = ""
    summary_envelope_preface: str = ""
    summary_envelope_suffix: str = ""
    compact_user_instruction: str = ""
    compact_control_sentinel: str = "[[__LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1__]]"
    compact_control_system_clause: str = ""
    compact_control_user_instruction: str = ""


@dataclass
class CompactConfig:
    """Compact (/compact) 命令的配置"""

    enabled: bool = True
    auto_trigger: bool = True
    strategy: str = "performance"
    performance_ratio: float = 0.5
    utilization_summary_reserve: int = 20000
    utilization_buffer: int = 13000
    max_summary_tokens: int = 20000
    temperature: float = 0.3
    top_p: float = 0.9
    cycle_min_steps_between_compacts: int = 0
    image_token_estimate_per_image: int = 512
    diagnostics_enabled: bool = True
    prompt_shape: str = "reliable_position1"
    prompt_control_enabled: bool = False
    validation_min_summary_chars: int = 200
    validation_min_summary_prompt_ratio: float = 0.002
    validation_require_summary_sections: bool = True
```

Modify `_parse_agent_prompts_config()`:

```python
compaction = CompactionPromptsConfig(
    status_message=comp_raw.get("status_message", "Compressing conversation context..."),
    no_tools_preamble=comp_raw.get("no_tools_preamble", ""),
    main_prompt=comp_raw.get("main_prompt", ""),
    custom_instructions_header=comp_raw.get("custom_instructions_header", ""),
    no_tools_trailer=comp_raw.get("no_tools_trailer", ""),
    summary_envelope_preface=comp_raw.get("summary_envelope_preface", ""),
    summary_envelope_suffix=comp_raw.get("summary_envelope_suffix", ""),
    compact_user_instruction=comp_raw.get("compact_user_instruction", ""),
    compact_control_sentinel=comp_raw.get(
        "compact_control_sentinel",
        "[[__LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1__]]",
    ),
    compact_control_system_clause=comp_raw.get("compact_control_system_clause", ""),
    compact_control_user_instruction=comp_raw.get("compact_control_user_instruction", ""),
)
```

Modify `_parse_compact_config()`:

```python
return CompactConfig(
    enabled=raw.get("enabled", True),
    auto_trigger=raw.get("auto_trigger", True),
    strategy=raw.get("strategy", "performance"),
    performance_ratio=float(raw.get("performance_ratio", 0.5)),
    utilization_summary_reserve=int(raw.get("utilization_summary_reserve", 20000)),
    utilization_buffer=int(raw.get("utilization_buffer", 13000)),
    max_summary_tokens=int(raw.get("max_summary_tokens", 20000)),
    temperature=float(raw.get("temperature", 0.3)),
    top_p=float(raw.get("top_p", 0.9)),
    cycle_min_steps_between_compacts=int(raw.get("cycle_min_steps_between_compacts", 0)),
    image_token_estimate_per_image=int(raw.get("image_token_estimate_per_image", 512)),
    diagnostics_enabled=bool(raw.get("diagnostics_enabled", True)),
    prompt_shape=str(raw.get("prompt_shape", "cache_first_tail_with_fallback")),
    prompt_control_enabled=bool(raw.get("prompt_control_enabled", True)),
    validation_min_summary_chars=int(raw.get("validation_min_summary_chars", 200)),
    validation_min_summary_prompt_ratio=float(raw.get("validation_min_summary_prompt_ratio", 0.002)),
    validation_require_summary_sections=bool(raw.get("validation_require_summary_sections", True)),
)
```

- [ ] **Step 4: Add YAML controls**

Add these keys under `config/config-misc.yaml:compact`:

```yaml
  diagnostics_enabled: true
  # cache_first_tail_with_fallback is the default performance path.
  # reliable_position1 remains the validation fallback.
  prompt_shape: cache_first_tail_with_fallback
  # When true, normal chat/agent system prompts get a stable compact-control clause.
  # Required for cache-first compact.
  prompt_control_enabled: true
  extra_body:
    chat_template_kwargs:
      enable_thinking: false
  source_token_budget: 6000
  validation_min_summary_chars: 200
  validation_min_summary_prompt_ratio: 0.002
  validation_require_summary_sections: true
```

Add these keys under `config/config-agent-prompts.yaml:compaction`:

```yaml
  compact_control_sentinel: "[[__LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1__]]"
  compact_control_system_clause: |
    INTERNAL COMPACT CONTROL RULE:
    If and only if the latest user message begins exactly with [[__LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1__]], ignore normal routing, normal task execution, and tool calling for that one response. Produce only the requested context-compaction summary of the prior messages. This internal control message is generated by the server only during explicit context compaction.
  compact_control_user_instruction: |
    [[__LLM_DEPLOYMENT_INTERNAL_COMPACT_CONTROL_V1__]]

    Summarize the prior messages in this conversation for context compaction. The message containing this control sentinel is an internal instruction and is NOT part of the conversation to summarize.

    **LANGUAGE LOCK**: Write the entire summary in English, regardless of the language used in the conversation being summarized.

    **STRUCTURE LOCK**: Your response MUST contain exactly one `<summary>` block with the same 9 numbered sections required by the normal compact instruction. Do not output a separate reasoning block.

    Preserve prior compact summaries according to the PRIOR SUMMARY RULE in the normal compact instruction.
```

- [ ] **Step 5: Run config tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_config.py -q`

Expected: PASS.

---

### Task 2: Add Prompt Diagnostics Helpers

**Files:**
- Create: `backend/compact/prompt_diagnostics.py`
- Test: `tests/test_compact_prompt_diagnostics.py`

- [ ] **Step 1: Write failing diagnostics tests**

Create `tests/test_compact_prompt_diagnostics.py`:

```python
from backend.compact.prompt_diagnostics import (
    compare_prompt_snapshots,
    extract_cached_tokens,
    make_prompt_snapshot,
)
from backend.llm_service import Message


def test_make_prompt_snapshot_is_stable_for_same_messages():
    messages = [Message(role="system", content="SYS"), Message(role="user", content="hello")]
    a = make_prompt_snapshot("normal_agent", messages)
    b = make_prompt_snapshot("normal_agent", messages)

    assert a.prompt_shape == "normal_agent"
    assert a.message_count == 2
    assert a.prompt_chars == b.prompt_chars
    assert a.content_hash == b.content_hash


def test_compare_prompt_snapshots_reports_first_divergence():
    reference = make_prompt_snapshot(
        "normal_agent",
        [Message(role="system", content="same"), Message(role="user", content="old")],
    )
    candidate = make_prompt_snapshot(
        "compact_candidate",
        [Message(role="system", content="same"), Message(role="user", content="new")],
    )

    comparison = compare_prompt_snapshots(reference, candidate)

    assert comparison.reference_shape == "normal_agent"
    assert comparison.candidate_shape == "compact_candidate"
    assert comparison.common_prefix_chars > 0
    assert comparison.first_divergence_char is not None
    assert comparison.same_hash is False


def test_extract_cached_tokens_handles_common_usage_shapes():
    assert extract_cached_tokens({"cached_tokens": 12}) == 12
    assert extract_cached_tokens({"prompt_tokens_details": {"cached_tokens": 34}}) == 34
    assert extract_cached_tokens({"cache_read_input_tokens": 56}) == 56
    assert extract_cached_tokens({"prompt_tokens": 100}) is None
```

- [ ] **Step 2: Run failing diagnostics tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_prompt_diagnostics.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'backend.compact.prompt_diagnostics'`.

- [ ] **Step 3: Implement diagnostics helpers**

Create `backend/compact/prompt_diagnostics.py`:

```python
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable


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
    if isinstance(message, dict):
        role = message.get("role", "user")
        content = message.get("content", "")
    else:
        role = getattr(message, "role", "user")
        content = getattr(message, "content", "")
    return {"role": str(role), "content": _normalize_content(content)}


def canonicalize_messages(messages: Iterable[Any]) -> str:
    payload = [_canonical_message_dict(m) for m in messages]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def make_prompt_snapshot(prompt_shape: str, messages: Iterable[Any]) -> PromptSnapshot:
    message_list = list(messages)
    canonical = canonicalize_messages(message_list)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return PromptSnapshot(
        prompt_shape=prompt_shape,
        message_count=len(message_list),
        prompt_chars=len(canonical),
        content_hash=digest,
        canonical_text=canonical,
    )


def compare_prompt_snapshots(reference: PromptSnapshot | None, candidate: PromptSnapshot) -> PromptComparison | None:
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
```

- [ ] **Step 4: Run diagnostics tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_prompt_diagnostics.py -q`

Expected: PASS.

---

### Task 3: Propagate Streaming Usage Chunks

**Files:**
- Modify: `backend/llm_service.py:797-812`, `backend/llm_service.py:1608+` if the OpenAI proxy stream has equivalent parsing
- Test: `tests/test_llm_stream_usage_propagation.py`

- [ ] **Step 1: Write failing usage propagation tests**

Create `tests/test_llm_stream_usage_propagation.py`:

```python
from backend.llm_service import _openai_stream_payload_to_chunk


def test_stream_payload_to_chunk_preserves_usage_only_payload():
    chunk = _openai_stream_payload_to_chunk({
        "choices": [],
        "usage": {"prompt_tokens": 123, "completion_tokens": 4, "cached_tokens": 100},
    })

    assert chunk == {
        "content": None,
        "reasoning": None,
        "tool_calls": None,
        "usage": {"prompt_tokens": 123, "completion_tokens": 4, "cached_tokens": 100},
    }


def test_stream_payload_to_chunk_preserves_content_and_usage_together():
    chunk = _openai_stream_payload_to_chunk({
        "choices": [{"delta": {"content": "hello"}}],
        "usage": {"prompt_tokens": 10},
    })

    assert chunk["content"] == "hello"
    assert chunk["usage"] == {"prompt_tokens": 10}


def test_stream_payload_to_chunk_returns_none_for_empty_payload():
    assert _openai_stream_payload_to_chunk({"choices": [{"delta": {}}]}) is None
```

- [ ] **Step 2: Run failing usage tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_llm_stream_usage_propagation.py -q`

Expected: FAIL because `_openai_stream_payload_to_chunk` does not exist.

- [ ] **Step 3: Add shared stream-payload helper**

Add near the top of `backend/llm_service.py` after request-id helpers:

```python
def _openai_stream_payload_to_chunk(parsed: dict) -> dict | None:
    choices = parsed.get("choices") or []
    delta = choices[0].get("delta", {}) if choices else {}
    content = delta.get("content")
    reasoning = delta.get("reasoning_content")
    tool_calls = delta.get("tool_calls")
    usage = parsed.get("usage")

    if not (content or reasoning or tool_calls or usage):
        return None

    chunk = {
        "content": content,
        "reasoning": reasoning,
        "tool_calls": tool_calls,
    }
    if usage:
        chunk["usage"] = usage
    return chunk
```

Replace the SGLang stream parsing block with:

```python
parsed = json.loads(data)
chunk_payload = _openai_stream_payload_to_chunk(parsed)
if chunk_payload is not None:
    yield chunk_payload
```

Apply the same helper to the OpenAI-compatible proxy stream parser if it has duplicated choice/delta parsing.

- [ ] **Step 4: Run usage tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_llm_stream_usage_propagation.py -q`

Expected: PASS.

---

### Task 4: Add Stable Compact-Control Sentinel Helpers

**Files:**
- Create: `backend/compact/prompt_control.py`
- Modify: `backend/agent/agent_runner.py:2391-2466`, `backend/agent/agent_runner.py:2531-2550`, `backend/agent/agent_runner.py:4860-4905`
- Modify: `backend/api/chat.py:416-538`
- Test: `tests/test_compact_prompt_control.py`

- [ ] **Step 1: Write failing prompt-control tests**

Create `tests/test_compact_prompt_control.py`:

```python
from backend.compact.prompt_control import (
    append_compact_control_clause,
    build_compact_control_instruction,
    ensure_compact_control_system_message,
    escape_user_compact_control_sentinel,
)
from backend.config import CompactConfig, CompactionPromptsConfig
from backend.llm_service import Message


def _cfg(enabled=True):
    return CompactConfig(prompt_control_enabled=enabled)


def _prompts():
    return CompactionPromptsConfig(
        compact_control_sentinel="[[SENTINEL]]",
        compact_control_system_clause="If latest user starts with [[SENTINEL]], compact.",
        compact_control_user_instruction="[[SENTINEL]] summarize prior messages.",
        custom_instructions_header="== USER CUSTOM INSTRUCTIONS ==",
    )


def test_append_compact_control_clause_is_stable_and_idempotent():
    first = append_compact_control_clause("base", _prompts(), _cfg(True))
    second = append_compact_control_clause(first, _prompts(), _cfg(True))

    assert first == second
    assert first.startswith("base")
    assert "[[SENTINEL]]" in first


def test_append_compact_control_clause_noops_when_disabled():
    assert append_compact_control_clause("base", _prompts(), _cfg(False)) == "base"


def test_ensure_compact_control_system_message_appends_to_existing_system():
    messages = [Message(role="system", content="SYS"), Message(role="user", content="hello")]
    out = ensure_compact_control_system_message(messages, _prompts(), _cfg(True))

    assert out[0].role == "system"
    assert out[0].content.startswith("SYS")
    assert "[[SENTINEL]]" in out[0].content
    assert out[1].content == "hello"


def test_ensure_compact_control_system_message_inserts_system_when_missing():
    messages = [Message(role="user", content="hello")]
    out = ensure_compact_control_system_message(messages, _prompts(), _cfg(True))

    assert out[0].role == "system"
    assert "[[SENTINEL]]" in out[0].content
    assert out[1].role == "user"
    assert out[1].content == "hello"


def test_escape_user_compact_control_sentinel_only_escapes_exact_prefix():
    assert escape_user_compact_control_sentinel("[[SENTINEL]] hi", _prompts(), _cfg(True)).startswith(" ")
    assert escape_user_compact_control_sentinel("hello [[SENTINEL]]", _prompts(), _cfg(True)) == "hello [[SENTINEL]]"
    assert escape_user_compact_control_sentinel("[[SENTINEL]] hi", _prompts(), _cfg(False)) == "[[SENTINEL]] hi"


def test_build_compact_control_instruction_adds_custom_instructions():
    text = build_compact_control_instruction(_prompts(), custom_instructions="Focus on files")

    assert text.startswith("[[SENTINEL]]")
    assert "USER CUSTOM INSTRUCTIONS" in text
    assert "Focus on files" in text
```

- [ ] **Step 2: Run failing prompt-control tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_prompt_control.py -q`

Expected: FAIL because `backend.compact.prompt_control` does not exist.

- [ ] **Step 3: Implement prompt-control helpers**

Create `backend/compact/prompt_control.py`:

```python
from __future__ import annotations

from typing import Iterable

from ..config import CompactConfig, CompactionPromptsConfig
from ..llm_service import Message


def _enabled(prompts: CompactionPromptsConfig | None, compact: CompactConfig | None) -> bool:
    return bool(compact and compact.prompt_control_enabled and prompts)


def append_compact_control_clause(
    instructions: str | None,
    prompts: CompactionPromptsConfig | None,
    compact: CompactConfig | None,
) -> str | None:
    if not _enabled(prompts, compact):
        return instructions
    clause = (prompts.compact_control_system_clause or "").strip()
    if not clause:
        return instructions
    base = (instructions or "").rstrip()
    if clause in base:
        return base
    return f"{base}\n\n{clause}" if base else clause


def ensure_compact_control_system_message(
    messages: Iterable[Message],
    prompts: CompactionPromptsConfig | None,
    compact: CompactConfig | None,
) -> list[Message]:
    out = list(messages)
    if not _enabled(prompts, compact):
        return out
    clause = (prompts.compact_control_system_clause or "").strip()
    if not clause:
        return out
    if out and out[0].role == "system":
        if clause in str(out[0].content or ""):
            return out
        out[0] = Message(
            role="system",
            content=f"{str(out[0].content or '').rstrip()}\n\n{clause}".strip(),
        )
        return out
    return [Message(role="system", content=clause), *out]


def escape_user_compact_control_sentinel(
    text: str,
    prompts: CompactionPromptsConfig | None,
    compact: CompactConfig | None,
) -> str:
    if not _enabled(prompts, compact):
        return text
    sentinel = prompts.compact_control_sentinel or ""
    if sentinel and text.startswith(sentinel):
        return " " + text
    return text


def escape_user_messages(
    messages: Iterable[Message],
    prompts: CompactionPromptsConfig | None,
    compact: CompactConfig | None,
) -> list[Message]:
    out: list[Message] = []
    for message in messages:
        if message.role != "user" or not isinstance(message.content, str):
            out.append(message)
            continue
        out.append(Message(
            role=message.role,
            content=escape_user_compact_control_sentinel(message.content, prompts, compact),
        ))
    return out


def build_compact_control_instruction(
    prompts: CompactionPromptsConfig,
    custom_instructions: str | None = None,
) -> str:
    parts = [prompts.compact_control_user_instruction.strip()]
    if custom_instructions and custom_instructions.strip():
        parts.append(prompts.custom_instructions_header)
        parts.append(custom_instructions.strip())
    return "\n\n".join(part for part in parts if part)
```

- [ ] **Step 4: Wire sentinel into normal agent system prompt only when enabled**

In `AgentRunner._build_profile_agent()`, after `instructions` is computed and VLM notes are appended, add:

```python
from ..compact.prompt_control import append_compact_control_clause

instructions = append_compact_control_clause(
    instructions,
    self.config.agent_prompts.compaction,
    self.config.compact,
)
```

In `_build_coordinator_turn_message()`, escape raw task before template rendering:

```python
from ..compact.prompt_control import escape_user_compact_control_sentinel

raw_task = escape_user_compact_control_sentinel(
    raw_task,
    self.config.agent_prompts.compaction,
    self.config.compact,
)
```

In `run_stream()`, create a model-only message after `session_start` and before routing:

```python
from ..compact.prompt_control import escape_user_compact_control_sentinel

model_message = escape_user_compact_control_sentinel(
    message,
    self.config.agent_prompts.compaction,
    self.config.compact,
)
```

Then pass `model_message` into `_run_coordinator_routing_stream()` and downstream agent execution calls that currently receive `message`. Keep frontend-visible user text unchanged.

- [ ] **Step 5: Wire sentinel into chat LLM messages only when enabled**

In `backend/api/chat.py`, after `filtered` is constructed and before auto-compact/current-user splitting, add:

```python
from ..compact.prompt_control import ensure_compact_control_system_message, escape_user_messages

prompts_cfg = get_config().agent_prompts.compaction
compact_cfg = get_config().compact
filtered = escape_user_messages(filtered, prompts_cfg, compact_cfg)
filtered = ensure_compact_control_system_message(filtered, prompts_cfg, compact_cfg)
```

Do not run this when `prompt_control_enabled` is false.

- [ ] **Step 6: Run prompt-control tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_prompt_control.py -q`

Expected: PASS.

---

### Task 5: Add Compact Prompt Plans

**Files:**
- Create: `backend/compact/prompt_plan.py`
- Modify: `backend/compact/types.py:16-24`
- Test: `tests/test_compact_prompt_plan.py`, `tests/test_compact_types.py`

- [ ] **Step 1: Write failing prompt-plan tests**

Create `tests/test_compact_prompt_plan.py`:

```python
from backend.compact.prompt_plan import (
    build_cache_first_tail_plan,
    build_reliable_position1_plan,
    build_reliable_source_with_compact_system,
)
from backend.config import CompactionPromptsConfig
from backend.llm_service import Message


def _prompts():
    return CompactionPromptsConfig(
        no_tools_preamble="PREAMBLE",
        main_prompt="MAIN",
        no_tools_trailer="TRAILER",
        compact_user_instruction="Summarize messages that follow.",
        custom_instructions_header="== USER CUSTOM INSTRUCTIONS ==",
        compact_control_sentinel="[[SENTINEL]]",
        compact_control_user_instruction="[[SENTINEL]] summarize prior messages.",
    )


def test_reliable_source_replaces_system_and_preserves_history_tail():
    source = [Message(role="system", content="ACTIVE SYSTEM"), Message(role="user", content="Q1")]
    reliable = build_reliable_source_with_compact_system(source, _prompts())

    assert reliable[0].role == "system"
    assert "PREAMBLE" in reliable[0].content
    assert "MAIN" in reliable[0].content
    assert "TRAILER" in reliable[0].content
    assert "ACTIVE SYSTEM" not in reliable[0].content
    assert reliable[1].content == "Q1"


def test_reliable_source_handles_no_system_chat_history():
    source = [Message(role="user", content="Q1"), Message(role="assistant", content="A1")]
    reliable = build_reliable_source_with_compact_system(source, _prompts())

    assert reliable[0].role == "system"
    assert "PREAMBLE" in reliable[0].content
    assert [m.content for m in reliable[1:]] == ["Q1", "A1"]


def test_reliable_position1_plan_uses_current_builder_shape():
    source = build_reliable_source_with_compact_system(
        [Message(role="system", content="ACTIVE"), Message(role="user", content="Q1")],
        _prompts(),
    )
    plan = build_reliable_position1_plan(source, _prompts())

    assert plan.shape == "reliable_position1"
    assert plan.messages[0].role == "system"
    assert plan.messages[1].role == "user"
    assert "Summarize messages that follow" in plan.messages[1].content
    assert plan.messages[2].content == "Q1"


def test_cache_first_tail_plan_preserves_source_prefix_and_appends_sentinel_instruction():
    source = [Message(role="system", content="ACTIVE WITH CONTROL"), Message(role="user", content="Q1")]
    plan = build_cache_first_tail_plan(source, _prompts(), custom_instructions="Focus on files")

    assert plan.shape == "cache_first_tail"
    assert plan.messages[0].content == "ACTIVE WITH CONTROL"
    assert plan.messages[1].content == "Q1"
    assert plan.messages[-1].role == "user"
    assert plan.messages[-1].content.startswith("[[SENTINEL]]")
    assert "Focus on files" in plan.messages[-1].content
```

Append this test to `tests/test_compact_types.py`:

```python
def test_compact_request_cache_first_fields_default_to_none():
    req = CompactRequest(mode="agent")
    assert req.body_messages is None
    assert req.cache_first_body_messages is None
    assert req.reference_messages is None
```

- [ ] **Step 2: Run failing prompt-plan tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_prompt_plan.py tests/test_compact_types.py -q`

Expected: FAIL because `prompt_plan.py` and new `CompactRequest` fields do not exist.

- [ ] **Step 3: Extend `CompactRequest`**

Modify `backend/compact/types.py`:

```python
@dataclass
class CompactRequest:
    mode: Mode
    body_messages: Optional[List[Message]] = None
    messages: Optional[List[Any]] = None
    cache_first_body_messages: Optional[List[Message]] = None
    reference_messages: Optional[List[Message]] = None
    session_id: Optional[str] = None
    trigger: Trigger = "manual"
    custom_instructions: Optional[str] = None
```

- [ ] **Step 4: Implement prompt-plan builders**

Create `backend/compact/prompt_plan.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..config import CompactionPromptsConfig
from ..llm_service import Message
from .prompt_builder import build_compact_messages_v2
from .prompt_control import build_compact_control_instruction


CompactPromptShape = Literal["reliable_position1", "cache_first_tail"]


@dataclass(frozen=True)
class CompactPromptPlan:
    shape: CompactPromptShape
    messages: list[Message]
    stream_visible: bool


def build_compact_system(prompts: CompactionPromptsConfig) -> str:
    return "\n\n".join(
        part.rstrip()
        for part in (
            prompts.no_tools_preamble or "",
            prompts.main_prompt or "",
            prompts.no_tools_trailer or "",
        )
        if part is not None
    )


def build_reliable_source_with_compact_system(
    source_messages: list[Message],
    prompts: CompactionPromptsConfig,
) -> list[Message]:
    compact_system = build_compact_system(prompts)
    if not source_messages:
        return [Message(role="system", content=compact_system)]
    tail = source_messages[1:] if source_messages[0].role == "system" else list(source_messages)
    return [Message(role="system", content=compact_system), *tail]


def build_reliable_position1_plan(
    reliable_source_messages: list[Message],
    prompts: CompactionPromptsConfig,
    custom_instructions: str | None = None,
) -> CompactPromptPlan:
    return CompactPromptPlan(
        shape="reliable_position1",
        messages=build_compact_messages_v2(
            reliable_source_messages,
            prompts,
            custom_instructions=custom_instructions,
        ),
        stream_visible=True,
    )


def build_cache_first_tail_plan(
    source_messages: list[Message],
    prompts: CompactionPromptsConfig,
    custom_instructions: str | None = None,
) -> CompactPromptPlan:
    if not source_messages:
        raise ValueError("cache-first compact requires source_messages")
    if source_messages[0].role != "system":
        raise ValueError("cache-first compact requires a system message at index 0")
    instruction = build_compact_control_instruction(prompts, custom_instructions)
    return CompactPromptPlan(
        shape="cache_first_tail",
        messages=[*source_messages, Message(role="user", content=instruction)],
        stream_visible=False,
    )
```

- [ ] **Step 5: Run prompt-plan tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_prompt_plan.py tests/test_compact_types.py -q`

Expected: PASS.

---

### Task 6: Add Compact Output Validation

**Files:**
- Create: `backend/compact/validation.py`
- Test: `tests/test_compact_validation.py`

- [ ] **Step 1: Write failing validation tests**

Create `tests/test_compact_validation.py`:

```python
from backend.compact.validation import validate_compact_summary
from backend.config import CompactConfig


def _cfg(**overrides):
    cfg = CompactConfig(
        validation_min_summary_chars=50,
        validation_min_summary_prompt_ratio=0.01,
        validation_require_summary_sections=True,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


VALID_SUMMARY = """
1. Primary Request and Intent:
   User asked for implementation planning.
2. Key Technical Concepts:
   Prefix cache and compact prompt shape.
3. Files and Code Sections:
   backend/compact/orchestrator.py.
4. Errors and fixes:
   None.
5. Problem Solving:
   Designed fallback validation.
6. All user messages:
   One planning request.
7. Pending Tasks:
   Implement plan.
8. Current Work:
   Awaiting implementation.
9. Optional Next Step:
   Run tests.
"""


def test_validate_rejects_empty_summary():
    result = validate_compact_summary("", prompt_chars=10000, config=_cfg())
    assert result.ok is False
    assert result.reason == "empty_summary"


def test_validate_rejects_routing_xml():
    result = validate_compact_summary("<action>switch_agent</action><target>coordinator_2</target>", 10000, _cfg())
    assert result.ok is False
    assert result.reason == "routing_or_tool_output"


def test_validate_rejects_tool_call_markers():
    result = validate_compact_summary('Calling tools:\n[{"type":"function"}]', 10000, _cfg())
    assert result.ok is False
    assert result.reason == "routing_or_tool_output"


def test_validate_rejects_too_short_summary():
    result = validate_compact_summary("short", prompt_chars=10000, config=_cfg())
    assert result.ok is False
    assert result.reason == "too_short"


def test_validate_rejects_missing_required_sections():
    result = validate_compact_summary("This is long enough but has no section skeleton." * 10, 1000, _cfg())
    assert result.ok is False
    assert result.reason == "missing_summary_sections"


def test_validate_accepts_required_sections():
    result = validate_compact_summary(VALID_SUMMARY, prompt_chars=1000, config=_cfg())
    assert result.ok is True
    assert result.reason is None
```

- [ ] **Step 2: Run failing validation tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_validation.py -q`

Expected: FAIL because `backend.compact.validation` does not exist.

- [ ] **Step 3: Implement validation**

Create `backend/compact/validation.py`:

```python
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
```

- [ ] **Step 4: Run validation tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_validation.py -q`

Expected: PASS.

---

### Task 7: Add Orchestrator Cache-First Attempts And Fallback

**Files:**
- Modify: `backend/compact/orchestrator.py:46-269`
- Test: `tests/test_compact_orchestrator_cache_first.py`

- [ ] **Step 1: Write failing orchestrator tests**

Create `tests/test_compact_orchestrator_cache_first.py`:

```python
from __future__ import annotations

from typing import Any, AsyncGenerator

import pytest

from backend.compact.orchestrator import CompactOrchestrator
from backend.compact.types import CompactEventKind, CompactRequest
from backend.config import CompactConfig, CompactionPromptsConfig
from backend.llm_service import Message


VALID = """<summary>
1. Primary Request and Intent:
   Plan work.
2. Key Technical Concepts:
   Prefix cache.
3. Files and Code Sections:
   backend/compact/orchestrator.py.
4. Errors and fixes:
   None.
5. Problem Solving:
   Fallback.
6. All user messages:
   User asked.
7. Pending Tasks:
   Implement.
8. Current Work:
   Tests.
9. Optional Next Step:
   Run pytest.
</summary>"""


class SequencedLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def chat_stream_async(self, messages, max_new_tokens, temperature, top_p, tools=None, request_id=None):
        self.calls.append(list(messages))
        output = self.outputs.pop(0)
        yield {"content": output[: len(output) // 2]}
        yield {"content": output[len(output) // 2 :]}
        yield {"usage": {"prompt_tokens": 1000, "completion_tokens": 100, "cached_tokens": 900}}


def prompts():
    return CompactionPromptsConfig(
        no_tools_preamble="PREAMBLE",
        main_prompt="MAIN",
        no_tools_trailer="TRAILER",
        compact_user_instruction="Summarize messages that follow.",
        compact_control_sentinel="[[SENTINEL]]",
        compact_control_user_instruction="[[SENTINEL]] summarize prior messages.",
    )


def cfg(shape="cache_first_tail_with_fallback"):
    return CompactConfig(
        prompt_shape=shape,
        prompt_control_enabled=True,
        validation_min_summary_chars=20,
        validation_min_summary_prompt_ratio=0.0,
        validation_require_summary_sections=True,
        max_summary_tokens=2000,
        temperature=0.3,
        top_p=0.9,
    )


async def collect(agen):
    out = []
    async for event in agen:
        out.append(event)
    return out


@pytest.mark.asyncio
async def test_cache_first_invalid_output_is_not_streamed_and_falls_back():
    llm = SequencedLLM(["<action>switch_agent</action><target>coordinator_2</target>", VALID])
    applied = []
    orch = CompactOrchestrator(llm, cfg(), prompts(), apply_compaction=lambda req, sid, text: applied.append(text))
    req = CompactRequest(
        mode="agent",
        body_messages=[Message(role="system", content="COMPACT"), Message(role="user", content="history")],
        cache_first_body_messages=[Message(role="system", content="ACTIVE"), Message(role="user", content="history")],
        trigger="manual",
    )

    events = await collect(orch.run_compact(req))

    assert len(llm.calls) == 2
    assert all("switch_agent" not in (event.text or "") for event in events)
    assert [event.kind for event in events].count(CompactEventKind.COMPLETED) == 1
    assert len(applied) == 1
    assert "Primary Request" in applied[0]


@pytest.mark.asyncio
async def test_cache_first_valid_output_applies_without_fallback():
    llm = SequencedLLM([VALID])
    applied = []
    orch = CompactOrchestrator(llm, cfg(), prompts(), apply_compaction=lambda req, sid, text: applied.append(text))
    req = CompactRequest(
        mode="agent",
        body_messages=[Message(role="system", content="COMPACT"), Message(role="user", content="history")],
        cache_first_body_messages=[Message(role="system", content="ACTIVE"), Message(role="user", content="history")],
        trigger="manual",
    )

    events = await collect(orch.run_compact(req))

    assert len(llm.calls) == 1
    assert any(event.kind == CompactEventKind.COMPLETED for event in events)
    completed = [event for event in events if event.kind == CompactEventKind.COMPLETED][0]
    assert completed.metadata["prompt_shape"] == "cache_first_tail"
    assert completed.metadata["fallback_used"] is False
    assert len(applied) == 1


@pytest.mark.asyncio
async def test_reliable_default_keeps_streaming_delta_behavior():
    llm = SequencedLLM([VALID])
    orch = CompactOrchestrator(llm, cfg("reliable_position1"), prompts())
    req = CompactRequest(
        mode="agent",
        body_messages=[Message(role="system", content="COMPACT"), Message(role="user", content="history")],
        trigger="manual",
    )

    events = await collect(orch.run_compact(req))

    assert len(llm.calls) == 1
    assert any(event.kind == CompactEventKind.DELTA for event in events)
    assert any(event.kind == CompactEventKind.COMPLETED for event in events)
```

- [ ] **Step 2: Run failing orchestrator tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_orchestrator_cache_first.py -q`

Expected: FAIL because orchestrator has no prompt-plan/fallback behavior.

- [ ] **Step 3: Refactor `CompactOrchestrator.run_compact()` into plan attempts**

In `backend/compact/orchestrator.py`, add imports:

```python
from .prompt_diagnostics import compare_prompt_snapshots, extract_cached_tokens, make_prompt_snapshot
from .prompt_plan import (
    CompactPromptPlan,
    build_cache_first_tail_plan,
    build_reliable_position1_plan,
    build_reliable_source_with_compact_system,
)
from .validation import validate_compact_summary
```

Add private helpers inside `CompactOrchestrator`:

```python
    def _build_prompt_plans(self, req: CompactRequest) -> list[CompactPromptPlan]:
        if req.body_messages is None and req.messages is not None:
            prompt_messages = build_compact_messages(
                body_messages=req.messages,
                prompts=self.prompts,
                custom_instructions=req.custom_instructions,
            )
            normalized = [
                Message(role=str(m.get("role", "user")), content=m.get("content"))
                if isinstance(m, dict) else m
                for m in prompt_messages
            ]
            return [CompactPromptPlan("reliable_position1", normalized, stream_visible=True)]

        if req.body_messages is None:
            raise ValueError("CompactRequest must have body_messages (v2) or messages (v1) set")

        reliable_source = build_reliable_source_with_compact_system(req.body_messages, self.prompts)
        reliable = build_reliable_position1_plan(
            reliable_source,
            self.prompts,
            custom_instructions=req.custom_instructions,
        )

        if (
            self.config.prompt_shape == "cache_first_tail_with_fallback"
            and self.config.prompt_control_enabled
            and req.cache_first_body_messages
        ):
            cache_first = build_cache_first_tail_plan(
                req.cache_first_body_messages,
                self.prompts,
                custom_instructions=req.custom_instructions,
            )
            return [cache_first, reliable]

        return [reliable]
```

Add an attempt runner for cache-first buffering. Do not use this helper for `stream_visible=True` reliable attempts unless it is rewritten as an async generator that yields deltas live.

```python
    async def _run_attempt(self, summary_id: str, plan: CompactPromptPlan):
        accumulated = ""
        usage = None
        t_start = time.monotonic()
        first_chunk_at = None
        visible_events: list[CompactEvent] = []

        async for chunk in self.llm_service.chat_stream_async(
            messages=plan.messages,
            max_new_tokens=self.config.max_summary_tokens,
            temperature=self.config.temperature,
            top_p=self.config.top_p,
            tools=None,
            request_id=summary_id,
        ):
            delta = chunk.get("content") or ""
            if delta:
                if first_chunk_at is None:
                    first_chunk_at = time.monotonic()
                accumulated += delta
                if plan.stream_visible:
                    visible_events.append(CompactEvent.delta(summary_id, delta))
            if chunk.get("usage"):
                usage = chunk.get("usage")

        t_end = time.monotonic()
        prefill_ms = int((first_chunk_at - t_start) * 1000) if first_chunk_at else None
        decode_ms = int((t_end - first_chunk_at) * 1000) if first_chunk_at else None
        return accumulated, usage, visible_events, prefill_ms, decode_ms, int((t_end - t_start) * 1000)
```

This helper buffers event objects in memory and is safe for cache-first attempts because `cache_first_tail` has `stream_visible=False`. For the default `reliable_position1` path, keep the existing live-streaming behavior by iterating `llm_service.chat_stream_async()` inline inside `run_compact()` and yielding `CompactEvent.delta(summary_id, delta)` immediately when each delta arrives. The reliable path may still validate at the end before applying memory; if validation fails, emit `compact_failed` and let the frontend remove the panel as it already does for failures.

Update `run_compact()` behavior:

```python
plans = self._build_prompt_plans(req)
yield CompactEvent.started(summary_id, req.trigger, req.mode)
reference_snapshot = make_prompt_snapshot("reference", req.reference_messages) if req.reference_messages else None

last_failure_reason = None
for attempt_index, plan in enumerate(plans, start=1):
    try:
        if plan.stream_visible:
            raw, usage = "", None
            t_start = time.monotonic()
            first_chunk_at = None
            async for chunk in self.llm_service.chat_stream_async(
                messages=plan.messages,
                max_new_tokens=self.config.max_summary_tokens,
                temperature=self.config.temperature,
                top_p=self.config.top_p,
                tools=None,
                request_id=summary_id,
            ):
                delta = chunk.get("content") or ""
                if delta:
                    if first_chunk_at is None:
                        first_chunk_at = time.monotonic()
                    raw += delta
                    yield CompactEvent.delta(summary_id, delta)
                if chunk.get("usage"):
                    usage = chunk.get("usage")
            t_end = time.monotonic()
            prefill_ms = int((first_chunk_at - t_start) * 1000) if first_chunk_at else None
            decode_ms = int((t_end - first_chunk_at) * 1000) if first_chunk_at else None
            total_ms = int((t_end - t_start) * 1000)
        else:
            raw, usage, _visible_events, prefill_ms, decode_ms, total_ms = await self._run_attempt(summary_id, plan)
    except Exception as exc:
        err_repr = f"{type(exc).__name__}: {exc}"
        logger.exception("[compact] LLM call failed during compact")
        yield CompactEvent.failed(summary_id, "llm_unreachable", err_repr)
        return

    cleaned = format_compact_summary(raw)
    snapshot = make_prompt_snapshot(plan.shape, plan.messages)
    comparison = compare_prompt_snapshots(reference_snapshot, snapshot)
    validation = validate_compact_summary(cleaned, snapshot.prompt_chars, self.config)
    cached_tokens = extract_cached_tokens(usage)

    logger.info(
        "[compact-diag] "
        f"attempt={attempt_index} shape={plan.shape} "
        f"validation={'passed' if validation.ok else 'failed'} "
        f"validation_reason={validation.reason} "
        f"prompt_chars={snapshot.prompt_chars} "
        f"prompt_tokens_sglang={(usage or {}).get('prompt_tokens')} "
        f"cached_tokens_sglang={cached_tokens} "
        f"completion_tokens_sglang={(usage or {}).get('completion_tokens')} "
        f"prefill_ms={prefill_ms} decode_ms={decode_ms} total_ms={total_ms} "
        f"first_divergence_char={comparison.first_divergence_char if comparison else None}"
    )

    if not validation.ok:
        last_failure_reason = validation.reason
        if plan.shape == "cache_first_tail":
            continue
        yield CompactEvent.failed(summary_id, validation.reason or "validation_failed")
        return

    if not plan.stream_visible:
        yield CompactEvent.delta(summary_id, cleaned)

    if self._apply_compaction is not None:
        try:
            self._apply_compaction(req, summary_id, cleaned)
        except Exception as exc:
            logger.exception("apply_compaction failed")
            yield CompactEvent.failed(summary_id, "apply_error", f"{type(exc).__name__}: {exc}")
            return

    yield CompactEvent.completed(
        summary_id,
        cleaned,
        metadata={
            "trigger": req.trigger,
            "mode": req.mode,
            "prompt_shape": plan.shape,
            "fallback_used": attempt_index > 1,
            "cached_tokens": cached_tokens,
            "prompt_tokens": (usage or {}).get("prompt_tokens"),
            "completion_tokens": (usage or {}).get("completion_tokens"),
        },
    )
    return

yield CompactEvent.failed(summary_id, last_failure_reason or "validation_failed")
```

Keep the v1 fallback deprecation warning path from current code. Do not remove current v1 tests in this task.

- [ ] **Step 4: Run orchestrator cache-first tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_orchestrator_cache_first.py tests/test_compact_orchestrator_v2.py tests/test_compact_orchestrator_instrumentation.py -q`

Expected: PASS after updating instrumentation assertions to accept the expanded `[compact-diag]` fields.

---

### Task 8: Integrate Cache-First Source In Agent Mode

**Files:**
- Modify: `backend/agent/agent_runner.py:2118-2144`, `backend/agent/agent_runner.py:4629-4843`
- Modify: `backend/agent/local_llm_model.py:389-394`, `backend/agent/local_llm_model.py:526-531`
- Test: `tests/test_compact_agent_cache_first.py`, extend `tests/test_compact_agent_system_override.py`

- [ ] **Step 1: Write failing agent cache-first tests**

Create `tests/test_compact_agent_cache_first.py`:

```python
from types import MethodType
from unittest.mock import MagicMock, patch
import asyncio

from backend.agent.agent_runner import AgentRunner, AgentSession
from backend.compact.types import CompactEvent
from backend.config import CompactConfig, CompactionPromptsConfig
from backend.llm_service import Message


def _prompts():
    return CompactionPromptsConfig(
        no_tools_preamble="COMPACT SYSTEM",
        main_prompt="MAIN",
        no_tools_trailer="TRAILER",
        compact_user_instruction="Summarize follows.",
        compact_control_sentinel="[[SENTINEL]]",
        compact_control_system_clause="If latest user starts with [[SENTINEL]], compact.",
        compact_control_user_instruction="[[SENTINEL]] summarize prior messages.",
    )


def _session():
    agent = MagicMock()
    agent.memory.steps = []
    agent.write_memory_to_messages.return_value = [
        MagicMock(role="system", content="ACTIVE SYSTEM WITH CONTROL"),
        MagicMock(role="user", content="old task"),
    ]
    s = AgentSession(session_id="s1", agent=agent, mode="tool_calling")
    s.runtime_context = "Current local time: pinned"
    return s


async def _drain(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


def test_agent_cache_first_body_uses_active_system_plus_runtime_context():
    runner = MagicMock()
    runner.config = MagicMock()
    runner.config.compact = CompactConfig(prompt_control_enabled=True, prompt_shape="cache_first_tail_with_fallback")
    runner.config.agent_prompts.compaction = _prompts()
    runner._build_cache_first_compact_body_messages = MethodType(
        AgentRunner._build_cache_first_compact_body_messages, runner
    )

    body = runner._build_cache_first_compact_body_messages(_session())

    assert body is not None
    assert body[0].role == "system"
    assert "ACTIVE SYSTEM WITH CONTROL" in body[0].content
    assert "Current local time: pinned" in body[0].content
    assert "COMPACT SYSTEM" not in body[0].content
    assert body[1].content == "old task"


def test_agent_cache_first_body_disabled_when_prompt_control_disabled():
    runner = MagicMock()
    runner.config = MagicMock()
    runner.config.compact = CompactConfig(prompt_control_enabled=False, prompt_shape="cache_first_tail_with_fallback")
    runner.config.agent_prompts.compaction = _prompts()
    runner._build_cache_first_compact_body_messages = MethodType(
        AgentRunner._build_cache_first_compact_body_messages, runner
    )

    assert runner._build_cache_first_compact_body_messages(_session()) is None


def test_run_compact_then_continue_passes_both_reliable_and_cache_first_bodies():
    session = _session()
    runner = MagicMock()
    runner.config = MagicMock()
    runner.config.compact = CompactConfig(prompt_control_enabled=True, prompt_shape="cache_first_tail_with_fallback")
    runner.config.agent_prompts.compaction = _prompts()
    runner.llm_service = MagicMock()
    runner._replace_agent_memory = MagicMock()
    runner._compact_event_to_sse = AgentRunner._compact_event_to_sse
    runner._build_compact_body_messages = MethodType(AgentRunner._build_compact_body_messages, runner)
    runner._build_cache_first_compact_body_messages = MethodType(AgentRunner._build_cache_first_compact_body_messages, runner)

    captured = {}

    def capture_orchestrator(*args, **kwargs):
        async def fake_run(req):
            captured["body_messages"] = req.body_messages
            captured["cache_first_body_messages"] = req.cache_first_body_messages
            yield CompactEvent.started("sid", "auto", "agent")
            yield CompactEvent.completed("sid", "cleaned", metadata={"trigger": "auto", "mode": "agent"})
        inst = MagicMock()
        inst.run_compact = fake_run
        return inst

    with patch("backend.compact.orchestrator.CompactOrchestrator", side_effect=capture_orchestrator):
        asyncio.run(_drain(AgentRunner._run_compact_then_continue(runner, session, "auto")))

    assert captured["body_messages"][0].content.startswith("COMPACT SYSTEM")
    assert "ACTIVE SYSTEM WITH CONTROL" in captured["cache_first_body_messages"][0].content
```

- [ ] **Step 2: Run failing agent tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_agent_cache_first.py tests/test_compact_agent_system_override.py -q`

Expected: FAIL because `_build_cache_first_compact_body_messages` does not exist.

- [ ] **Step 3: Record normal prompt snapshots in LocalLLMModel**

After runtime context injection in `LocalLLMModel.generate()` and `generate_stream()`, add:

```python
        if self._session is not None:
            try:
                from backend.compact.prompt_diagnostics import make_prompt_snapshot
                self._session.last_llm_prompt_snapshot = make_prompt_snapshot(
                    "normal_agent",
                    llm_messages,
                )
            except Exception as exc:
                logger.debug(f"prompt snapshot skipped: {exc}")
```

Add field to `AgentSession`:

```python
    last_llm_prompt_snapshot: Any | None = None
```

- [ ] **Step 4: Add agent cache-first body builder**

In `backend/agent/agent_runner.py`, add near `_build_compact_body_messages()`:

```python
    def _build_cache_first_compact_body_messages(self, session) -> list[Message] | None:
        if not self.config.compact.prompt_control_enabled:
            return None
        prompts_cfg = self.config.agent_prompts.compaction
        if prompts_cfg is None:
            return None
        chat_messages = list(session.agent.write_memory_to_messages())
        body_messages = _chat_messages_to_backend_messages(chat_messages)
        if not body_messages or body_messages[0].role != "system":
            return None
        if session.runtime_context:
            body_messages = list(body_messages)
            body_messages[0] = Message(
                role="system",
                content=f"{body_messages[0].content}\n\n{session.runtime_context}",
            )
        return body_messages
```

This intentionally does not call `_build_compact_body_messages()` because cache-first must use the active system prompt, not the compact system override.

- [ ] **Step 5: Pass cache-first and reference messages to `CompactRequest`**

In `_run_compact_then_continue()` and `run_compact_for_session()`, construct requests like:

```python
body_messages = self._build_compact_body_messages(session, prompts_cfg)
cache_first_body_messages = self._build_cache_first_compact_body_messages(session)
reference_messages = cache_first_body_messages
req = CompactRequest(
    mode="agent",
    session_id=session.session_id,
    body_messages=body_messages,
    cache_first_body_messages=cache_first_body_messages,
    reference_messages=reference_messages,
    trigger=trigger,
)
```

Do not modify `_replace_agent_memory()`.

- [ ] **Step 6: Update stale comments**

In `_build_compact_body_messages()` comments, replace the claim that prefix-cache byte-equality is vacuous with:

```python
        Trade-off (intentional): this reliable fallback shape diverges from
        the active agent prompt at token 0, so SGLang cannot reuse the prior
        normal request's longest prefix for this attempt. It remains the
        correctness fallback because active routing/tool prompts have been
        observed to produce routing XML/tool-call text instead of summaries.
        Cache-first experiments must use _build_cache_first_compact_body_messages()
        plus validation and fallback rather than weakening this reliable path.
```

- [ ] **Step 7: Run agent compact tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_agent_cache_first.py tests/test_compact_agent_v2_path.py tests/test_compact_agent_system_override.py tests/test_compact_agent_apply.py -q`

Expected: PASS.

---

### Task 9: Integrate Cache-First Source In Chat Mode

**Files:**
- Modify: `backend/api/chat.py:416-538`
- Test: `tests/test_compact_chat_cache_first.py`, existing `tests/test_compact_chat_v2_path.py`, `tests/test_compact_chat_rebuild_contract.py`

- [ ] **Step 1: Write failing chat cache-first tests**

Create `tests/test_compact_chat_cache_first.py`:

```python
import asyncio
from unittest.mock import MagicMock, patch

from backend.api.chat import ChatMessage, _generate_stream_response
from backend.compact.types import CompactEvent
from backend.config import CompactConfig, CompactionPromptsConfig, get_config
from backend.llm_service import Message


async def _drain(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


def _set_compact_config():
    cfg = get_config()
    cfg.compact.prompt_shape = "cache_first_tail_with_fallback"
    cfg.compact.prompt_control_enabled = True
    cfg.agent_prompts.compaction.compact_control_sentinel = "[[SENTINEL]]"
    cfg.agent_prompts.compaction.compact_control_system_clause = "If latest user starts with [[SENTINEL]], compact."
    cfg.agent_prompts.compaction.compact_control_user_instruction = "[[SENTINEL]] summarize prior messages."
    return cfg


def test_chat_auto_compact_passes_reliable_and_cache_first_bodies(monkeypatch):
    _set_compact_config()
    chat_messages = [
        ChatMessage(role="user", content="old user"),
        ChatMessage(role="assistant", content="old assistant"),
        ChatMessage(role="user", content="current user"),
    ]
    raw_request = MagicMock()
    captured = {}

    async def fake_run(req):
        captured["body_messages"] = req.body_messages
        captured["cache_first_body_messages"] = req.cache_first_body_messages
        yield CompactEvent.started("sid", "auto", "chat")
        yield CompactEvent.completed("sid", "cleaned", metadata={"trigger": "auto", "mode": "chat"})

    raw_request.app.state.compact_orchestrator.run_compact = fake_run
    service = MagicMock()

    async def fake_chat_stream(messages, **kwargs):
        yield {"content": "ok"}

    service.chat_stream_async = fake_chat_stream

    with patch("backend.api.chat._should_auto_compact_chat", return_value=True):
        asyncio.run(_drain(_generate_stream_response(
            service=service,
            messages=[Message(role=m.role, content=m.content) for m in chat_messages],
            model="qwen",
            max_tokens=None,
            temperature=None,
            top_p=None,
            raw_request=raw_request,
            original_chat_messages=chat_messages,
        )))

    assert captured["body_messages"][0].role == "system"
    assert "COMPACT" in captured["body_messages"][0].content or "Respond with TEXT ONLY" in captured["body_messages"][0].content
    assert captured["cache_first_body_messages"][0].role == "system"
    assert "[[SENTINEL]]" in captured["cache_first_body_messages"][0].content
    assert all(m.content != "current user" for m in captured["body_messages"])
    assert all(m.content != "current user" for m in captured["cache_first_body_messages"])


def test_chat_prompt_control_disabled_preserves_no_system_default(monkeypatch):
    cfg = get_config()
    cfg.compact.prompt_control_enabled = False
    chat_messages = [ChatMessage(role="user", content="hello")]
    raw_request = MagicMock()
    raw_request.app.state.compact_orchestrator = MagicMock()
    service = MagicMock()

    captured = {}

    async def fake_chat_stream(messages, **kwargs):
        captured["messages"] = list(messages)
        yield {"content": "ok"}

    service.chat_stream_async = fake_chat_stream

    with patch("backend.api.chat._should_auto_compact_chat", return_value=False):
        asyncio.run(_drain(_generate_stream_response(
            service=service,
            messages=[Message(role="user", content="hello")],
            model="qwen",
            max_tokens=None,
            temperature=None,
            top_p=None,
            raw_request=raw_request,
            original_chat_messages=chat_messages,
        )))

    assert captured["messages"][0].role == "user"
    assert captured["messages"][0].content == "hello"
```

- [ ] **Step 2: Run failing chat tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_chat_cache_first.py -q`

Expected: FAIL because chat mode does not pass `cache_first_body_messages` and reliable body does not force compact system.

- [ ] **Step 3: Build controlled chat messages**

In `backend/api/chat.py`, after metadata filtering converts to `filtered`, add:

```python
from ..compact.prompt_control import ensure_compact_control_system_message, escape_user_messages
from ..compact.prompt_plan import build_reliable_source_with_compact_system

compact_cfg = get_config().compact
prompts_cfg = get_config().agent_prompts.compaction
controlled_filtered = escape_user_messages(filtered, prompts_cfg, compact_cfg)
controlled_filtered = ensure_compact_control_system_message(controlled_filtered, prompts_cfg, compact_cfg)
filtered = controlled_filtered
```

Only these helpers should mutate model-input messages. Do not mutate `original_chat_messages` or frontend-visible state.

- [ ] **Step 4: Pass both chat compact bodies**

In the auto-compact branch:

```python
current_user_message: Optional[Message] = None
compact_source_messages = filtered
if filtered and getattr(filtered[-1], "role", None) == "user":
    current_user_message = filtered[-1]
    compact_source_messages = filtered[:-1]

reliable_body_messages = build_reliable_source_with_compact_system(
    compact_source_messages,
    prompts_cfg,
)
cache_first_body_messages = compact_source_messages if compact_cfg.prompt_control_enabled else None

compact_req = CompactRequest(
    mode="chat",
    body_messages=reliable_body_messages,
    cache_first_body_messages=cache_first_body_messages,
    reference_messages=cache_first_body_messages,
    trigger="auto",
)
```

Keep final rebuild unchanged:

```python
new_messages: List[Message] = [Message(role="user", content=envelope_content)]
if current_user_message is not None:
    new_messages.append(Message(role=current_user_message.role, content=current_user_message.content))
```

- [ ] **Step 5: Run chat compact tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_chat_cache_first.py tests/test_compact_chat_v2_path.py tests/test_compact_chat_rebuild_contract.py tests/test_compact_chat_streaming_filter.py -q`

Expected: PASS.

---

### Task 10: Preserve Timeline Protocol And Frontend Non-Rendering Metadata

**Files:**
- Modify only if tests fail: `frontend/src/lib/timeline-projection.ts`, `frontend/src/lib/compact-projection.ts`
- Test: `frontend/src/lib/timeline-projection.test.mts`, `frontend/src/lib/compact-projection.test.mts`

- [ ] **Step 1: Add frontend regression tests for metadata non-rendering**

Append to `frontend/src/lib/timeline-projection.test.mts`:

```ts
test('compact diagnostics metadata does not render as chat text', () => {
  const started = projectTimelineEvent({
    kind: 'compact_started',
    summary_message_id: 'compact-1',
    metadata: { prompt_shape: 'cache_first_tail', cached_tokens: 900 },
  });

  assert.equal(started.kind, 'compact_started');
  assert.equal('text' in started, false);
});
```

Append to `frontend/src/lib/compact-projection.test.mts`:

```ts
test('compact_completed metadata does not get appended to summary content', () => {
  const messages: Message[] = [{
    id: 'compact-1',
    role: 'system',
    content: '',
    timestamp: new Date('2026-06-14T00:00:00Z'),
    bubbleKind: 'compact_summary',
    compactionBoundaryId: 'compact-1',
    compactStatus: 'streaming',
  } as Message];
  const { setMessages, getMessages } = makeSetMessages(messages);
  const ref = makeRef<Set<string>>(new Set());

  applyCompactProjection({
    kind: 'compact_completed',
    summaryMessageId: 'compact-1',
    text: 'Clean summary',
    metadata: { prompt_shape: 'cache_first_tail', cached_tokens: 900 },
  }, setMessages, ref);

  assert.equal(getMessages()[0].content, 'Clean summary');
});
```

- [ ] **Step 2: Run frontend tests**

Run: `node --test --experimental-strip-types "frontend/src/lib/timeline-projection.test.mts" "frontend/src/lib/compact-projection.test.mts"`

Expected: PASS. If it fails because metadata leaks into content, fix projection helpers without adding new visible UI.

---

### Task 11: Documentation Cleanup

**Files:**
- Modify: `AGENTS.md`
- Modify: `docs/superpowers/specs/outdated-2026-06-11-v2-compact-prompt-turn-shape-design.md`
- Modify: `docs/superpowers/plans/outdated-2026-06-11-v2-compact-prompt-turn-shape-plan.md`
- Modify: `docs/superpowers/specs/outdated-2026-06-12-post-compact-slim-system-prompt-design.md`
- Modify: `docs/superpowers/plans/outdated-2026-06-12-post-compact-slim-system-prompt-plan.md`

- [ ] **Step 1: Verify old turn-shape plan is marked as superseded**

At the top of `docs/superpowers/plans/outdated-2026-06-11-v2-compact-prompt-turn-shape-plan.md`, ensure this warning exists:

```markdown
> **Superseded on 2026-06-14:** Do not execute this plan for prefix-cache optimization. It assumes a tail-only compact instruction and byte-equal active system/history shape that was later rejected by E2E reliability failures. Use `docs/superpowers/plans/2026-06-14-compact-prompt-shape-prefix-cache-plan.md` instead.
```

- [ ] **Step 2: Verify slim system prompt documents are marked as outdated**

At the top of `docs/superpowers/specs/outdated-2026-06-12-post-compact-slim-system-prompt-design.md`, ensure this warning exists:

```markdown
> **2026-06-14 prefix-cache caution:** The slim path is a first-post-compact UX latency workaround. It deliberately removes runtime context/tool schemas for one call and therefore should not be generalized as a prefix-cache hit-rate strategy. Cache-first compact work must preserve stable normal prefixes and use validation/fallback.
```

At the top of `docs/superpowers/plans/outdated-2026-06-12-post-compact-slim-system-prompt-plan.md`, ensure it says not to execute that plan as current prefix-cache work.

- [ ] **Step 3: Add AGENTS trap for cache-first compact work**

Append a new trap to `AGENTS.md` after the current compact traps:

```markdown
34. **Compact cache-first prompt shape is experimental and must keep reliable fallback**:
   - Current default compact shape is `cache_first_tail_with_fallback` with `compact.prompt_control_enabled: true`; reliable `reliable_position1` remains the correctness fallback.
   - Cache-first uses active system + stable compact-control sentinel + tail instruction; it must buffer fast-path output until validation passes.
   - Any output containing routing XML, `switch_agent`, `Calling tools:`, or tool-call JSON must be rejected before `_replace_agent_memory()`.
   - The reliable compact-specific system prompt fallback must remain available and must not be deleted during cache experiments.
   - `source_token_budget` is a cache-miss latency guard, not a cache-hit optimization; it can reduce `#new-token` but breaks full prefix equality.
   - Do not change `compact.performance_ratio` without explicit user approval; it is an E2E threshold tuning knob.
   - Diagnostics are log/metadata only; never inject prompt hashes, cached-token counts, timeline text, or UI bubble state into LLM messages.
```

- [ ] **Step 4: Run docs grep check**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_prefix_cache_stability_v2.py -q`

Expected: PASS. This is a proxy check that no old sidecar fields were revived while editing docs and code.

---

### Task 12: Full Verification And E2E Measurement

**Files:**
- No source modifications unless verification fails.

- [ ] **Step 1: Run compact-focused backend tests**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_compact_*.py tests/test_llm_stream_usage_propagation.py -q`

Expected: PASS.

- [ ] **Step 2: Run prefix/cache and agent regression baseline**

Run: `PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_multi_agent_runner.py tests/test_agent_session_context.py tests/test_search_and_fetch.py tests/test_multi_tool_parsing.py tests/test_timeline_projection.py tests/test_prefix_cache_stability_v2.py tests/test_compact_*.py -q`

Expected: PASS.

- [ ] **Step 3: Run frontend projection tests**

Run: `node --test --experimental-strip-types "frontend/src/lib/timeline-projection.test.mts" "frontend/src/lib/slash-commands.test.mts" "frontend/src/lib/slash-commands-state.test.mts" "frontend/src/lib/compact-client.test.mts" "frontend/src/lib/compact-client-extra.test.mts" "frontend/src/lib/compact-client-llmclient.test.mts" "frontend/src/lib/message-types.test.mts" "frontend/src/lib/compact-body.test.mts" "frontend/src/lib/compact-projection.test.mts"`

Expected: PASS.

- [ ] **Step 4: Run frontend type-check and build**

Run in `frontend/`: `npm run type-check`

Expected: PASS.

Run in `frontend/`: `npm run build`

Expected: PASS.

- [ ] **Step 5: Reliable baseline E2E measurement**

Historical reliable baseline config (not current default):

```yaml
compact:
  prompt_shape: reliable_position1
  prompt_control_enabled: false
  diagnostics_enabled: true
```

Run a long chat/agent scenario that triggers auto-compact. Record from backend logs:

```text
[compact-diag] attempt=1 shape=reliable_position1 validation=passed prompt_tokens_sglang=... cached_tokens_sglang=... prefill_ms=... decode_ms=... total_ms=...
```

Expected: compact succeeds and existing UI projection works. Cached tokens may be zero; this is a fallback/baseline, not the current desired fast path.

- [ ] **Step 6: Cache-first local experiment**

Current fast-path config should include:

```yaml
compact:
  prompt_shape: cache_first_tail_with_fallback
  prompt_control_enabled: true
  diagnostics_enabled: true
  extra_body:
    chat_template_kwargs:
      enable_thinking: false
  source_token_budget: 6000
```

Restart backend so prompt templates are rebuilt with the stable control clause. Restart SGLang if changing `schedule_policy` or `enable_cache_report`.

Run the same long scenario. Record:

```text
[compact-diag] attempt=1 shape=cache_first_tail validation=passed prompt_tokens_sglang=... cached_tokens_sglang=... prefill_ms=...
```

If attempt 1 fails validation, record:

```text
[compact-diag] attempt=1 shape=cache_first_tail validation=failed validation_reason=...
[compact-diag] attempt=2 shape=reliable_position1 validation=passed fallback_used=true ...
```

Expected: no failed fast-path text appears in the compact card. If fallback succeeds, `_replace_agent_memory()` applies exactly once.

---

## Self-Review

Spec coverage:

- Phase 0 diagnostics are covered by Tasks 2, 3, 7, and 12.
- Stable compact-control sentinel is covered by Tasks 1 and 4.
- Cache-first prompt plan is covered by Task 5.
- Validation before memory replacement is covered by Tasks 6 and 7.
- Fallback once then fail visibly is covered by Task 7.
- Chat current-user exclusion is covered by Task 9.
- Agent current-user exclusion and `_replace_agent_memory()` invariants are preserved by Task 8 and existing tests.
- Unified timeline compatibility is covered by Task 10.

No placeholders:

- All new functions/classes referenced in tests are defined in task snippets.
- All commands include exact paths and expected results.
- Config names are concrete and default-off for behavior-changing paths.

Type consistency:

- `CompactRequest.cache_first_body_messages` and `reference_messages` are `Optional[List[Message]]` everywhere.
- Prompt shapes use only `"reliable_position1"`, `"cache_first_tail"`, and config value `"cache_first_tail_with_fallback"`.
- Validation returns `CompactValidationResult(ok, reason, details)` and orchestrator uses `validation.ok` and `validation.reason` consistently.

Implementation warning:

- Do not commit these changes unless the user explicitly asks. The task snippets intentionally omit `git commit` commands to follow the repository collaboration rule.
