# Glob / Grep Search Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two robust local search tools, `glob` and `grep`, for the Agent backend, limited to YAML-configured absolute whitelist paths and optimized for local LLM consumption.

**Architecture:** Add a new `backend/tools/search_ops.py` module with ripgrep-first implementations and Python fallbacks, expose both tools via `agent_runner.py`, add whitelist and limit settings to `AgentConfig` / `config-misc.yaml`, and teach the LLM to use these tools through docstrings plus prompt guidance.

**Tech Stack:** Python 3.12, pathlib, fnmatch, re, subprocess, smolagents wrapper generation, pytest

---

## File Structure

| File | Responsibility | New/Modify |
|------|---------------|------------|
| `backend/tools/search_ops.py` | `glob_files` / `grep_search`, whitelist validation, ripgrep + Python fallback | New |
| `backend/config.py` | Agent search config fields and parsing | Modify |
| `config/config-misc.yaml` | Absolute whitelist paths and search tuning knobs | Modify |
| `backend/agent/agent_runner.py` | Register `glob` / `grep` tools | Modify |
| `config/config-agent-prompts.yaml` | Guide LLM to prefer `glob` / `grep` for local search | Modify |
| `tests/test_search_ops.py` | Unit tests for search tools | New |
| `tests/test_get_tools_no_load_images.py` | Assert tool registration | Modify |

---

### Task 1: Write failing tests for search tools and registration

**Files:**
- Create: `tests/test_search_ops.py`
- Modify: `tests/test_get_tools_no_load_images.py`

- [ ] Step 1: Add tests covering whitelist enforcement, `glob`, `grep` modes, pagination, and registration.
- [ ] Step 2: Run `./.sglang/bin/python -m pytest tests/test_search_ops.py tests/test_get_tools_no_load_images.py -v` and verify failures are due to missing config fields / missing module / missing tools.

### Task 2: Add search config fields and YAML defaults

**Files:**
- Modify: `backend/config.py`
- Modify: `config/config-misc.yaml`

- [ ] Step 1: Add `search_whitelist_paths`, `search_exclude_globs`, `glob_default_limit`, `glob_max_limit`, `grep_default_head_limit`, `grep_max_head_limit`, `grep_max_columns` to `AgentConfig`.
- [ ] Step 2: Parse the new fields from `config/config-misc.yaml` with sane defaults.
- [ ] Step 3: Put explicit absolute whitelist paths in YAML, including `/tmp`.

### Task 3: Implement `backend/tools/search_ops.py`

**Files:**
- Create: `backend/tools/search_ops.py`

- [ ] Step 1: Implement config loading and whitelist normalization helpers.
- [ ] Step 2: Implement `glob_files()` with absolute-path validation, ripgrep-first execution, Python fallback, sorting, truncation, and LLM-friendly output.
- [ ] Step 3: Implement `grep_search()` with `files_with_matches`, `content`, `count`, `include`, context flags, pagination, ripgrep-first execution, Python fallback, and LLM-friendly output.

### Task 4: Register tools and teach the prompt

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Modify: `config/config-agent-prompts.yaml`

- [ ] Step 1: Import and register `glob` / `grep` in `_get_tools()`.
- [ ] Step 2: Add a prompt rule telling the model to use `glob` for path search and `grep` for content search instead of `exec_command`.

### Task 5: Run focused verification and fix issues

**Files:**
- Modify only if test failures require it

- [ ] Step 1: Run `./.sglang/bin/python -m pytest tests/test_search_ops.py tests/test_get_tools_no_load_images.py -v`.
- [ ] Step 2: If failures remain, fix the implementation and rerun the same command until green.

### Task 6: Run broader regression checks

**Files:**
- No planned code changes

- [ ] Step 1: Run `./.sglang/bin/python -m pytest tests/test_browser_command_visibility.py tests/test_agent_session_context.py -v` to ensure tool registration/prompt changes did not break nearby behavior.
- [ ] Step 2: Report final status with evidence from the fresh test output.
