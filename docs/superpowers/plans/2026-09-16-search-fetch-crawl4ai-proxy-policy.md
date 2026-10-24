# Search and Fetch Crawl4AI Proxy Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved three-stage `curl -> Crawl4AI -> SearXNG snippet` recovery path with explicit proxy propagation and configurable Stage 1 text-quality thresholds.

**Architecture:** Keep curl as the first full-page transport and Crawl4AI as the second transport. Keep SearXNG snippet fallback as the third degraded-content path. SearXNG routes China-mainland engines directly and non-mainland engines through the host VPN proxy; backend direct fetches use the optional `fetch_proxy`, which must be propagated to Crawl4AI `BrowserConfig`.

**Tech Stack:** Python, FastAPI backend configuration, Crawl4AI, Playwright, Docker Compose, SearXNG YAML, pytest.

**Spec:** `docs/spec/2026-09-16-search-fetch-crawl4ai-proxy-policy-design.md`

## Global Constraints

- Stage 1 remains `curl`; Stage 2 remains `Crawl4AI`; Stage 3 remains `SearXNG` snippet fallback.
- Do not switch to, investigate, or fix curl-cffi in this plan.
- Do not lower or redesign `fetch_max_workers`, `fetch_crawl4ai_max_concurrency`, or `fetch_worker_processes`.
- Keep `SEARXNG_VERSION=latest`; do not pin SearXNG.
- Domestic SearXNG engines are direct; non-domestic SearXNG engines use the host VPN proxy.
- When `fetch_proxy` is configured, pass it to Crawl4AI `BrowserConfig.proxy_config`; when it is null/empty, Crawl4AI remains direct.
- Add `fetch_quality_min_text_chars: 1000` and `fetch_quality_min_text_lines: 6` under `web_tools`.
- Preserve the existing low-quality compatibility rule: reject empty text or text that is below both configured thresholds.
- Preserve the existing `fetch_min_text_chars: 200` candidate-extraction meaning.
- Do not modify third-party dependency source, frontend protocol, Agent prompt/schema, or prefix-cache behavior.
- Do not use Git commands unless the user explicitly authorizes them; do not commit or push.

---

### Task 1: Externalize Stage 1 quality thresholds

**Files:**
- Modify: `config/config-agent-operation.yaml`
- Modify: `backend/config.py`
- Modify: `backend/tools/web_tools.py`
- Test: `tests/test_web_tools_config.py`
- Test: `tests/test_web_fetch.py`

**Interfaces:**
- Produces `WebToolsConfig.fetch_quality_min_text_chars: int` with default `1000`.
- Produces `WebToolsConfig.fetch_quality_min_text_lines: int` with default `6`.
- Produces `_get_fetch_config()` entries with the same names.
- Changes `_looks_like_low_quality_text()` to consume explicit thresholds while preserving the existing empty-or-both-below decision.

- [ ] Add both YAML keys under `web_tools` with Chinese comments distinguishing them from `fetch_min_text_chars`.
- [ ] Add dataclass fields and loader validation for non-negative integer values without changing existing fetch configuration defaults.
- [ ] Pass both values through `_get_fetch_config()` into the Stage 1 quality check.
- [ ] Keep the default decision equivalent to `not text or (chars < 1000 and lines < 6)`.
- [ ] Add boundary tests for empty, short single-line, long single-line, short multi-line, and long multi-line text.
- [ ] Run the focused configuration and quality tests and record the exact command/output in the sub-agent report.

### Task 2: Propagate proxy to Crawl4AI and update the Crawl4AI dependency pin

**Files:**
- Modify: `backend/tools/fetch_worker_runtime.py`
- Modify: `config/config-agent-operation.yaml`
- Modify: `backend/config.py`
- Modify: `backend/tools/web_tools.py`
- Modify: `requirements/requirements_for_sglang.txt`
- Test: `tests/test_crawl4ai_concurrency.py`
- Test: `tests/test_fetch_isolation_paths.py`
- Test: add or modify the narrowest appropriate Crawl4AI runtime/config test

**Interfaces:**
- Consumes `fetch_proxy`, `crawl4ai_enable_stealth`, and existing browser settings from the config payload.
- Creates `BrowserConfig(proxy_config=...)` when `fetch_proxy` is non-empty and creates a direct BrowserConfig when it is empty.
- Keeps the existing isolated worker, context ownership, cancellation, and batch dispatcher contracts.
- Updates the Crawl4AI pin from `0.9.0` to `0.9.3`; changes the Playwright pin only if the target Crawl4AI compatibility checks require it.

- [ ] Read the approved SPEC and project `AGENTS.md` before editing.
- [ ] Add only the necessary Crawl4AI runtime configuration keys; do not alter concurrency values.
- [ ] Build the target Crawl4AI `ProxyConfig` using the installed/target public API and pass it to `BrowserConfig`.
- [ ] Ensure null/empty `fetch_proxy` does not inherit environment proxy variables.
- [ ] Preserve fresh browser-context isolation and existing cancellation cleanup.
- [ ] Upgrade the Crawl4AI requirement pin and verify the chosen Playwright pin against the target package.
- [ ] Add a test proving configured proxy reaches BrowserConfig and null proxy remains direct.
- [ ] Run focused config/runtime tests plus dependency import/doctor or the strongest available local smoke test; record exact results.

### Task 3: Make SearXNG engine-level proxy routing explicit

**Files:**
- Modify: `deploy/searxng/core-config/settings.yml`
- Modify: `deploy/searxng/docker-compose.yml`
- Modify: `deploy/searxng/searxng-backend-managed-setup.md` only where active behavior is documented
- Test: add or modify only tests that verify the new active policy; do not restore stale engine expectations

**Interfaces:**
- Produces a SearXNG policy with no global outgoing proxy, per-engine proxy for non-mainland engines, and no proxy override for mainland engines.
- Adds Docker host-gateway resolution for `host.docker.internal`.
- Preserves `redis://valkey:6379/0`, local `127.0.0.1:8080` binding, and `SEARXNG_VERSION=latest`.

- [ ] Read the approved SPEC and project `AGENTS.md` before editing.
- [ ] Replace non-canonical proxy key spelling with the selected official SearXNG form.
- [ ] Remove blank-string domestic proxy overrides; express domestic direct behavior through the absence of per-engine proxy under a no-global-proxy policy.
- [ ] Add the Compose `host.docker.internal:host-gateway` mapping without changing unrelated services.
- [ ] Preserve the current chosen engine set unless a runtime/config correctness issue requires a narrowly scoped correction.
- [ ] Update only stale documentation or tests that directly describe this active policy.
- [ ] Validate YAML/Compose syntax locally and record any runtime Docker verification that cannot run in the current sandbox.

### Final integration and verification

- [ ] Review each sub-agent's changed files and reports against the SPEC and this plan.
- [ ] Run focused config, fetch, runtime, and SearXNG policy tests without claiming sandbox-blocked socket tests pass.
- [ ] Run `compileall` for modified Python modules.
- [ ] Run a final whole-change review covering the three-stage flow, proxy propagation, threshold semantics, dependency compatibility, and the no-concurrency/no-curl-cffi/latest constraints.

