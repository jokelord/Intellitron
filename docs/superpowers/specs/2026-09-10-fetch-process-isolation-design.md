# Search/fetch native failure isolation

User approved implementation on 2026-09-10 after reviewing the diagnosis and proposed worker architecture.

## Problem and evidence

The original uvicorn PID 257641 aborted with `free(): invalid pointer`. Its core shows detection in `lxml.etree`; this does not establish the first corrupting operation or exclude other native libraries. Limiting Crawl4AI to two browsers does not establish correctness, and the added timed thread join releases capacity before its thread exits. The ordinary executor path also lacks a hard overall crawl timeout. Failed primary transports can fall back for errors other than block pages.

## Required behavior

Keep independent URL fetches concurrent. Isolate native HTML extraction, curl_cffi, and Crawl4AI in supervised, persistent subprocesses. Existing curl subprocess transport may remain orchestrated by the parent; the parent must not parse responses using native extraction libraries. Reuse crawlers within one worker event loop and browser configuration. Batch arriving URLs with Crawl4AI's official `arun_many` and `MemoryAdaptiveDispatcher`, delivering completed results without waiting for unrelated slow pages. Preserve duplicate URL request identities and isolate browser state between logical requests.

Use a global Crawl4AI page task ceiling of 50, a configurable pool of four worker interpreters, and a configurable memory threshold of 85 percent. The task ceiling is shared across searches and worker processes; batching must not multiply it. Queue, initialization, network, parsing, retry and cancellation checks share the caller's deadline. Memory waiting is bounded by that deadline. The search-wide fetch slot wait must also honor cancellation.

Cancellation targets one request first. A task acknowledges completion only after cleanup. If native work or cleanup does not respond within a configurable one-second grace, terminate and reap that worker's process group. Fail its outstanding requests explicitly, ignore stale responses, and replace the worker for subsequent calls. A forced worker recycle may affect other work sharing that worker; a normal targeted cancellation must not. Shutdown is bounded and closes all owned subprocesses. Use fresh interpreters, not a fork of multithreaded uvicorn.

## Boundaries and compatibility

Preserve tool output strings, result ordering, snippet fallback, curl hedging and cookie publication behavior. No LLM messages, tool schemas, prompt prefixes, inference configuration, or dependency package sources change. No git commands or worktrees are authorized. Fix misleading comments and tests from the prior four-part attempted repair.

The process supervisor offers `get_fetch_pool(cfg).call(operation, payload, timeout, check_abort)` and `shutdown_fetch_pool()`. JSON-only IPC carries uniquely identified operations. Worker runtime offers async `handle(operation, payload)` and `shutdown()`. Native operations execute only there; existing orchestration uses thin parent adapters. Third-party output cannot corrupt IPC or block on an unread pipe.

## Acceptance

Tests must show a real worker SIGABRT leaves its caller alive, later calls succeed, timeout/Stop reap uncooperative work and descendants, targeted cancellation preserves unrelated requests, and simultaneous requests share capacity. Runtime tests verify actual batching/reuse and dispatcher wiring, including memory pressure and duplicate/redirect routing. Run the existing fetch/search/config regressions after adapting unit tests to explicit local implementation boundaries. Attempt a real Crawl4AI smoke against a local HTTP server; report environmental limitations honestly and do not claim a real 50-page throughput result from mocked tests.
