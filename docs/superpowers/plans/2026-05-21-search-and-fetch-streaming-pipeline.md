# Search-And-Fetch Streaming Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `search_and_fetch` submit webpage fetches immediately as each SearXNG page yields new deduped URLs, while keeping `web_search` and `web_fetch` unchanged in role.

**Architecture:** Keep single-page SearXNG search and URL normalization helpers in `backend/tools/web_tools.py`, but move the page-by-page streaming orchestration into `backend/tools/search_and_fetch.py`. Use one shared `ThreadPoolExecutor(max_workers=fetch_max_workers)` so search pagination can continue feeding new work while earlier fetches are still in flight.

**Tech Stack:** Python 3.12, pytest, concurrent.futures, existing config system

---

### Task 1: Add Failing Tests for Page-By-Page Streaming Submission

**Files:**
- Modify: `tests/test_search_and_fetch.py`

- [ ] **Step 1: Add a test that page 1 fetches are submitted before page 2 completes**

```python
def test_streaming_pipeline_submits_page_one_results_immediately(...):
    ...
    assert submit_calls[:4] == ["https://a.com", "https://b.com", "https://c.com", "https://d.com"]
```

- [ ] **Step 2: Add a test that later pages only submit newly deduped URLs**

```python
def test_streaming_pipeline_submits_only_new_urls_from_later_pages(...):
    ...
    assert "https://dup.com" not in newly_submitted_urls
    assert "https://new.com" in newly_submitted_urls
```

- [ ] **Step 3: Add a test that `max_results` is enforced after dedupe**

```python
def test_streaming_pipeline_stops_after_reaching_deduped_max_results(...):
    ...
    assert total_results == 20
```

- [ ] **Step 4: Run focused tests to verify they fail**

Run: `~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_search_and_fetch.py -q`
Expected: FAIL because `search_and_fetch` still waits for the full search result list before fetching.

### Task 2: Add Internal Search-Page Iterator Support for the Composite Tool

**Files:**
- Modify: `backend/tools/web_tools.py`
- Modify: `tests/test_searxng_search.py` only if helper-facing tests are needed

- [ ] **Step 1: Add a small helper that fetches exactly one SearXNG page and returns extracted results**

```python
def _fetch_searxng_page(query: str, timeout: int, cfg: dict, opener, pageno: int) -> list[dict[str, str]]:
    ...
```

- [ ] **Step 2: Keep URL normalization helper reusable for composite-tool dedupe**

```python
def _normalize_searxng_result_url(url: str) -> str:
    ...
```

- [ ] **Step 3: Verify helper-focused search tests still pass**

Run: `~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_searxng_search.py -q`
Expected: PASS

### Task 3: Implement the Streaming Pipeline in `search_and_fetch`

**Files:**
- Modify: `backend/tools/search_and_fetch.py`
- Test: `tests/test_search_and_fetch.py`

- [ ] **Step 1: Replace the current two-phase search-then-fetch flow with page-by-page orchestration**

```python
for pageno in range(1, max_pages + 1):
    page_results = _fetch_searxng_page(...)
    for item in page_results:
        normalized = _normalize_searxng_result_url(item["url"])
        if normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        submit fetch task immediately
```

- [ ] **Step 2: Preserve `fetch_max_workers` as the only concurrency cap**

```python
executor = ThreadPoolExecutor(max_workers=min(max_results, _get_fetch_max_workers()))
```

- [ ] **Step 3: Stop paging once deduped results reach `max_results`**

```python
if len(search_results) >= max_results:
    break
```

- [ ] **Step 4: Wait for submitted fetch tasks at the end and keep current result formatting**

```python
done, not_done = wait(submitted_futures, timeout=...)
```

- [ ] **Step 5: Run focused composite-tool tests**

Run: `~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_search_and_fetch.py -q`
Expected: PASS

### Task 4: Preserve Output Simplicity and Tool Isolation

**Files:**
- Modify: `tests/test_search_and_fetch.py`
- Modify: `tests/test_searxng_search.py` only if isolation assertions are needed

- [ ] **Step 1: Add a test that observation output still only shows final counts, not pagination internals**

```python
assert "共找到 6 个结果，成功抓取 6 个" in result
assert "第1页" not in result
assert "去重" not in result
assert "pageno" not in result
```

- [ ] **Step 2: Re-run focused tests for `web_search` to ensure it remains a pure search tool**

Run: `~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_searxng_search.py -q`
Expected: PASS

### Task 5: Full Verification

**Files:**
- Verify: `tests/test_web_tools_config.py`
- Verify: `tests/test_searxng_search.py`
- Verify: `tests/test_search_and_fetch.py`
- Verify: `tests/test_web_fetch.py`
- Verify: `tests/test_searxng_docs.py`

- [ ] **Step 1: Run the impacted regression suite**

Run: `~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_web_tools_config.py tests/test_searxng_search.py tests/test_search_and_fetch.py tests/test_web_fetch.py tests/test_searxng_docs.py tests/test_cycle_config.py tests/test_cycle_executor.py tests/test_agent_session_context.py tests/test_multi_agent_runner.py -q`
Expected: PASS

- [ ] **Step 2: Commit**

```bash
git add backend/tools/search_and_fetch.py backend/tools/web_tools.py tests/test_search_and_fetch.py tests/test_searxng_search.py docs/superpowers/specs/2026-05-21-search-and-fetch-streaming-pipeline-design.md docs/superpowers/plans/2026-05-21-search-and-fetch-streaming-pipeline.md
git commit -m "feat: stream search-and-fetch page results"
```
