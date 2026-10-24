# Tool Feedback Global Fetch Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct malformed multi-tool feedback, enforce global fetch worker budgeting, update runtime memory config, and reduce safe diagnostic memory retention.

**Architecture:** Add strict malformed-transcript detection before permissive parser repair, use existing `AgentParsingError` observation feedback, enforce process-wide fetch slots around page fetches, and trim only diagnostic retention that is not part of cache-first prompt construction.

**Tech Stack:** Python, FastAPI backend, smolagents, SGLang config YAML, pytest.

## Global Constraints

- Do not make read-only/search tools serial.
- Treat `web_tools.fetch_max_workers=150` as process-wide across sessions.
- Do not parser-repair duplicate-key multi-tool transcripts into partial calls.
- Do not change `session.last_compactable_llm_messages` cache-first semantics.
- Do not clear historical image objects from actual `memory.steps` in this change.
- Do not run git commands unless explicitly permitted.

---

### Task 1: Strict Malformed Multi-Tool Feedback

**Files:**
- Modify: `backend/agent/local_llm_model.py`
- Modify: `backend/agent/agent_runner.py`
- Test: parser/agent tests under `tests/`

**Interfaces:**
- Produces: `LocalLLMModel.detect_malformed_tool_transcript(content: str) -> str | None`
- Consumes: `AgentParsingError` from smolagents.

- [ ] Add tests for duplicate-key transcript rejection and valid two-call transcript acceptance.
- [ ] Implement duplicate top-level `id/type/function` detection for `Calling tools:` arrays.
- [ ] In `_step_stream()`, check content/reasoning/full stream text before normal parsing and raise `AgentParsingError` with corrective feedback.
- [ ] Verify malformed output does not execute any tool and valid multi-call output still executes normally.

### Task 2: Process-Wide Fetch Budget

**Files:**
- Modify: `backend/tools/search_and_fetch.py`
- Test: `tests/test_search_and_fetch.py`

**Interfaces:**
- Produces: global weighted slot acquisition inside `_fetch_single` submission path.
- Consumes: `config.web_tools.fetch_max_workers` as process-wide max slots.

- [ ] Add a concurrency test that runs multiple `search_and_fetch_impl()` calls and asserts active fetch count never exceeds configured global cap.
- [ ] Implement a process-wide dynamically resizable semaphore/budget using the current config value.
- [ ] Wrap each page fetch worker with acquire/release in `finally`.
- [ ] Keep `executor.shutdown(wait=True, cancel_futures=True)` unchanged.

### Task 3: Config Updates

**Files:**
- Modify: `config/qwen3__5-config-infer.yaml`
- Modify: `config/config-misc.yaml`

- [ ] Set active infer `sglang.mem_fraction_static` to `0.78`.
- [ ] Set `web_tools.fetch_max_workers` to `150`.
- [ ] Verify scripts already pass these config values through.

### Task 4: Cache-First-Safe Memory Retention

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `backend/compact/prompt_diagnostics.py`
- Test: compact/session tests under `tests/`

**Interfaces:**
- Preserves: `session.last_compactable_llm_messages` unchanged except existing successful compact clearing.
- Changes: `session.compaction_archive` stores lightweight diagnostics, not full step objects.

- [ ] Add test that `_replace_agent_memory()` does not retain old step objects in `compaction_archive`.
- [ ] Change archive entry to store `archived_step_count` and metadata instead of `archived_steps`.
- [ ] Add/cap prompt snapshot canonical text and redact base64 data URLs.
- [ ] Do not clear actual historical images in `memory.steps`.

### Task 5: Verification

**Files:**
- Run relevant pytest targets.

- [ ] Run parser tests.
- [ ] Run search/fetch tests.
- [ ] Run compact/session tests.
- [ ] Report any skipped or failing verification clearly.
