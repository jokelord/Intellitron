# Search/fetch process isolation implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development. The user has approved implementation in the current workspace. Do not use git or create worktrees. Reuse agents for related fixes; do not write per-step summaries.

**Goal:** Preserve concurrent web fetching while containing native crashes and bounding request cleanup.

**Architecture:** A supervised JSON subprocess pool runs native parsing and browser operations. Each worker owns an async crawler runtime with official multi-URL and memory-aware dispatch; the parent preserves fetch orchestration and global budgets.

**Tech Stack:** Python 3.12, asyncio, subprocesses, Crawl4AI 0.9.0, pytest, existing uv-managed `.sglang` environment.

**Spec:** `docs/superpowers/specs/2026-09-10-fetch-process-isolation-design.md`

## Global constraints

- No git, dependency source edits, prompt/schema changes or frontend protocol changes.
- `fetch_crawl4ai_max_concurrency=50`, `fetch_worker_processes=4`, `fetch_worker_cancel_grace_seconds=1.0`.
- `crawl4ai_memory_threshold_percent=85.0`, `crawl4ai_memory_check_interval_seconds=0.1`, `crawl4ai_batch_wait_seconds=0.01`.
- Counts must be positive integers; intervals finite and positive except a zero batch wait is valid; memory percent strictly between zero and 100.

## Task 1: Supervised process lifecycle

Files: `backend/tools/fetch_process_pool.py`, `tests/test_fetch_process_pool.py`, test helper modules.

Interface: `FetchProcessPool(worker_count=4, cancel_grace_seconds=1.0, handler_module='backend.tools.fetch_worker_runtime')`; `.call(operation, payload, timeout, check_abort=None) -> JSON value`; `.close()`; module `get_fetch_pool(cfg)`, `shutdown_fetch_pool()`. Runtime protocol: async `handle(operation, payload)` and `shutdown()`.

- [ ] Write failing real-process tests: abort a child during a request, assert caller remains alive and next call succeeds; cancel one concurrent request and assert the other completes; hang work and assert deadline plus cleanup grace; close while requests run and assert no abandoned processes.
- [ ] Implement fresh-interpreter startup, request IDs, bounded IPC/deadline polling, targeted cancellation and cleanup acknowledgements, process-group kill/reap escalation, generation-safe restart and shutdown.
- [ ] Run `./.sglang/bin/python -m pytest tests/test_fetch_process_pool.py -q` and inspect exit status and process cleanup.

## Task 2: Native adapters and Crawl4AI runtime

Files: `backend/tools/web_tools.py`, `backend/tools/search_and_fetch.py`, `backend/tools/fetch_worker_runtime.py`, optional focused crawler runtime module, `backend/main.py`, fetch/search/runtime tests.

Consumes Task 1 pool interface and Task 3 configuration. Produces async worker operation handler and shutdown hooks, parent native operation adapters, and deadline-aware search orchestration.

- [ ] Write failing tests for actual wrapper isolation, crawler reuse, a multi-URL batch, dispatcher configuration/memory waiting, per-request cancellation, duplicate/redirect routing and slot retention until cleanup.
- [ ] Move native extraction and curl_cffi into workers; keep pure parent transport/cookie logic. Replace per-URL crawler and thread joins with reusable worker event-loop batching and official dispatch. Make capacity global and all waits abort/deadline aware.
- [ ] Adapt existing unit tests to explicit local helpers where they patch internals; retain production subprocess integration coverage without production test bypasses.
- [ ] Run focused new tests, then `./.sglang/bin/python -m pytest tests/test_crawl4ai_concurrency.py tests/test_web_fetch.py tests/test_search_and_fetch.py tests/test_search_and_fetch_transport_report.py -q`.

## Task 3: Configuration and correction of prior diagnosis

Files: `backend/config.py`, `config/config-misc.yaml`, `tests/test_web_tools_config.py`, `tests/test_fetch_worker_config.py`, `docs/search-fetch-isolation-2026-09-10.md`.

- [ ] Write failing tests for defaults, explicit values and invalid inputs including bool, null, nonpositive counts and nonfinite floats.
- [ ] Implement dataclass/loader wiring and revise misleading browser-count/root-cause comments. Document isolation scope, budgets, forced-recycle behavior and the limits of core evidence.
- [ ] Run `./.sglang/bin/python -m pytest tests/test_web_tools_config.py tests/test_fetch_worker_config.py -q`.

## Integrated review and validation

- [ ] Review production call paths for native work remaining in uvicorn, lifecycle races, missing global limits, unrelated-request cancellation and duplicated timeout budgets.
- [ ] Run combined relevant regressions and real-process fault injection. Run a local HTTP/Crawl4AI smoke if browser execution is available, recording actual runtime evidence.
- [ ] Correct findings using the same implementers, rerun affected checks, and report changed behavior, measured results and remaining limitations. No performance guarantee without measurement.
