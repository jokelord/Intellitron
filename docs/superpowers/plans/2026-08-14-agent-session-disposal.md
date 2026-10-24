# Agent Session Lifecycle-Safe Disposal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace raw Agent session deletion with bounded, targeted, resource-complete async disposal and restore the selected NVFP4 compile settings to `1/1`.

**Architecture:** `AgentRunner` pins one exact top-level session object and deduplicates disposal by public ID under the existing admission lock. One configured deadline covers targeted top-level/child abort, scope barriers, image/state release, child metadata cleanup, and identity-checked store removal.

**Tech Stack:** Python 3.12, FastAPI, asyncio/threading, smolagents memory, Pillow, pytest, YAML, Bash syntax checks.

## Global Constraints

- Do not run Git or edit frontend files.
- Never use provider abort-all; every provider cancellation uses the captured request ID with `fallback_to_abort_all=False`.
- `agent.session_disposal_timeout_seconds` defaults to `30.0` and must be finite and strictly positive.
- Disposal must wait for captured top-level, Answer Now, and manager-owned child work before releasing canonical resources.
- Final session removal must be conditional on the exact pinned object so stale callbacks cannot delete a reused ID.
- `config/qwen3__6-NVIDIA-NVFP4-config-infer.yaml` must remain `compile_max_jobs: 1` and `triton_num_compile_threads: 1`.

---

### Task 1: Configuration And Selected NVFP4 Guard

**Files:**
- Modify: `backend/config.py`
- Modify: `config/config-misc.yaml`
- Modify: `config/qwen3__6-NVIDIA-NVFP4-config-infer.yaml`
- Test: `tests/test_multi_agent_config.py`
- Test: `tests/test_sglang_compile_cache_manager.py`

**Interfaces:**
- Produces: `AgentConfig.session_disposal_timeout_seconds: float`
- Consumes: existing `ConfigLoader._get_float()` and `_validate_agent_config()`.

- [ ] Add RED tests proving the default is `30.0`, finite positive overrides parse, and `0`, negatives, `nan`, `inf`, and `-inf` raise `ValueError` naming `agent.session_disposal_timeout_seconds`.
- [ ] Run the focused config and selected launcher tests and confirm failures from the absent field and current `9/9` YAML.
- [ ] Add the dataclass field, parser assignment, finite-positive validation, YAML default, and selected inference `1/1` values.
- [ ] Re-run focused tests and confirm they pass.

### Task 2: Pinned Session Disposal Core

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Test: `tests/test_session_store_cleanup.py`

**Interfaces:**
- Produces: `SessionStore.delete_if_same(session_id: str, expected: AgentSession) -> bool`
- Produces: `AgentRunner.dispose_session(session_id: str) -> dict[str, str]`
- Produces: bounded `SessionDisposalError` diagnostics for timeout/failure.

- [ ] Add RED tests for idle disposal, repeated/concurrent idempotency, identity-checked same-ID replacement safety, and admission rejection while disposal is active.
- [ ] Add RED tests that canonical `TaskStep.task_images`, `ActionStep.observations_images`, compact payloads, prompt snapshots, vision cleanup state, agent memory/state, request-manager state, and abort state are released only after scope barriers.
- [ ] Implement exact-object deletion, shared disposal tasks, deadline handling from `config.agent.session_disposal_timeout_seconds`, deduplicated PIL close/cache removal, and bounded diagnostics.
- [ ] Re-run focused disposal tests and confirm they pass.

### Task 3: Active, Answer Now, And Child Disposal

**Files:**
- Modify: `backend/agent/agent_runner.py`
- Test: `tests/test_session_store_cleanup.py`
- Test: `tests/test_manager_agent_dispatch.py`
- Test: `tests/test_agent_stop_api.py`

**Interfaces:**
- Consumes: `AgentSession.capture_active_run_scopes()`, `wait_for_run_scopes()`, `session_request_manager`, `abort_manager`, and manager-owned child registries.
- Produces: manager-scoped targeted abort/barrier/child cleanup behavior.

- [ ] Add RED active disposal tests that capture provider request/scope before abort, assert targeted-only abort, block resource release until top-level/Answer Now scopes finish, and prove timeout returns bounded diagnostics without raw deletion.
- [ ] Add RED child tests proving only manager-owned child sessions/runs/mappings/buffers/subscribers are aborted, awaited, closed, and removed while sibling-manager resources remain.
- [ ] Implement one-deadline top-level/child barrier, terminal subscriber closure, request/abort cleanup, and identity-safe child/top-level removal.
- [ ] Re-run session, Stop, Answer Now, and child tests and confirm they pass.

### Task 4: API Integration, Full Verification, And Report

**Files:**
- Modify: `backend/api/agent.py`
- Test: `tests/test_agent_stop_api.py`
- Modify: `.superpowers/sdd/task-5-report.md`

**Interfaces:**
- Consumes: `await AgentRunner.dispose_session(session_id)`.
- Produces: lifecycle-safe `DELETE /v1/agent/session/{session_id}` response after success or bounded diagnostic HTTP error.

- [ ] Add RED route tests proving async delegation, idempotent success, and bounded timeout/failure response shape.
- [ ] Replace direct `sessions.delete()` with awaited runner disposal and generic bounded error mapping.
- [ ] Run lifecycle/session/Stop/Answer Now/child/omission/config/launcher suites.
- [ ] Run Python compilation and `bash -n` on affected launcher/cache scripts.
- [ ] Append exact RED/GREEN/final command results and warning/constraint notes to `.superpowers/sdd/task-5-report.md`.

No commit step is permitted by the user instruction.
