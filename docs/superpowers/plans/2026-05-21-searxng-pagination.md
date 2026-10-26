# SearXNG Pagination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `web_search` and `search_and_fetch` automatically aggregate SearXNG results across `pageno=1,2,3...` while keeping the LLM-visible interface limited to `query` and `max_results`.

**Architecture:** Put pagination logic inside `backend/tools/web_tools.py` so `search_and_fetch` inherits the behavior through `web_search` instead of duplicating pagination. Add one config-backed page cap and update the active agent prompt text so the model understands `max_results` as an aggregated target count.

**Tech Stack:** Python 3.12, Bash, pytest, YAML config/prompt files

---

### Task 1: Add Failing Tests for SearXNG Pagination

**Files:**
- Modify: `tests/test_searxng_search.py`
- Modify: `tests/test_web_tools_config.py`

- [ ] **Step 1: Add a request-builder test for `pageno`**

```python
def test_build_search_request_includes_pageno():
    request = web_tools._build_searxng_search_request(
        "llm deployment",
        {"searxng_base_url": "https://searx.example.com", "searxng_search_path": "/search", "searxng_format": "json", "searxng_language": "zh-CN", "searxng_safe_search": 0},
        pageno=3,
    )
    assert "pageno=3" in request.full_url
```

- [ ] **Step 2: Add a multi-page aggregation test**

```python
def test_web_search_aggregates_multiple_pages_until_max_results(...):
    ...
    assert "[Result 3](https://c.example.com)" in result
    assert opener.open.call_count == 2
```

- [ ] **Step 3: Add a config parsing test for page cap**

```python
def test_parse_nested_searxng_pagination_config():
    ...
    assert cfg.searxng_max_pages == 5
```

- [ ] **Step 4: Run focused tests to verify they fail**

Run: `~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_searxng_search.py tests/test_web_tools_config.py -q`
Expected: FAIL because request builder, config, and pagination behavior are not implemented yet.

### Task 2: Implement Config-Backed Pagination in `web_search`

**Files:**
- Modify: `backend/config.py`
- Modify: `config/config-misc.yaml`
- Modify: `backend/tools/web_tools.py`
- Test: `tests/test_searxng_search.py`
- Test: `tests/test_web_tools_config.py`

- [ ] **Step 1: Add config field to `WebToolsConfig`**

```python
searxng_max_pages: int = 3
```

- [ ] **Step 2: Parse the new config field from `config-misc.yaml`**

```python
searxng_max_pages=self._coerce_int(searxng.get("max_pages"), 3)
```

- [ ] **Step 3: Add the config key to `config/config-misc.yaml`**

```yaml
  searxng:
    ...
    max_pages: 3
```

- [ ] **Step 4: Extend the SearXNG request builder with `pageno`**

```python
def _build_searxng_search_request(query: str, cfg: dict, pageno: int = 1) -> urllib.request.Request:
    ...
    params.append(("pageno", str(max(1, pageno))))
```

- [ ] **Step 5: Add URL normalization and paginated aggregation helpers**

```python
def _normalize_searxng_result_url(url: str) -> str:
    ...

def _search_with_searxng(query: str, timeout: int, cfg: dict, max_results: int) -> list[dict[str, str]]:
    ...
```

- [ ] **Step 6: Implement stop conditions**

```python
for pageno in range(1, max_pages + 1):
    ...
    if len(results) >= max_results:
        break
    if not page_results or new_results_count == 0:
        break
```

- [ ] **Step 7: Preserve first-page error semantics and later-page partial success**

```python
if pageno == 1:
    raise
break
```

- [ ] **Step 8: Run focused tests**

Run: `~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_searxng_search.py tests/test_web_tools_config.py -q`
Expected: PASS

### Task 3: Keep `search_and_fetch` Aligned With Aggregated Search Results

**Files:**
- Modify: `backend/tools/search_and_fetch.py` (only if needed for tests/wording)
- Modify: `tests/test_search_and_fetch.py`

- [ ] **Step 1: Add a test proving `search_and_fetch` still requests `max_results` through `web_search`**

```python
assert mock_search.call_args.kwargs["max_results"] == 20
```

- [ ] **Step 2: Add a test name/comment clarifying that the search count is post-pagination aggregate**

```python
assert "共找到 10 个结果" in result
```

- [ ] **Step 3: Run focused composite tests**

Run: `~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_search_and_fetch.py -q`
Expected: PASS

### Task 4: Update Agent Prompt Guidance

**Files:**
- Modify: `config/config-agent-prompts.yaml`
- Add or Modify: `tests/test_searxng_docs.py` or a prompt-facing test file already used in this migration area

- [ ] **Step 1: Update prompt text for `search_and_fetch` / `web_search` usage**

```yaml
- 这两个工具中的 max_results 表示希望最终聚合得到的结果数量，而不是单页数量；后端会自动按 SearXNG 的 pageno=1,2,3... 翻页并去重，你不需要手动控制分页。
- 对新闻、时效性事件、广泛搜索任务，可根据需要把 max_results 提到 10 到 20。
```

- [ ] **Step 2: Add a test that the prompt text mentions automatic pagination and aggregated `max_results` semantics**

```python
assert "自动" in text and "pageno" in text and "max_results" in text
```

- [ ] **Step 3: Run the prompt/doc test**

Run: `~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_searxng_docs.py -q`
Expected: PASS

### Task 5: Full Verification

**Files:**
- Verify: `tests/test_web_tools_config.py`
- Verify: `tests/test_searxng_search.py`
- Verify: `tests/test_search_and_fetch.py`
- Verify: `tests/test_web_fetch.py`
- Verify: `tests/test_searxng_docs.py`

- [ ] **Step 1: Run impacted regression suite**

Run: `~/Project/LLM-deployment/.sglang/bin/python -m pytest tests/test_web_tools_config.py tests/test_searxng_search.py tests/test_search_and_fetch.py tests/test_web_fetch.py tests/test_searxng_docs.py tests/test_cycle_config.py tests/test_cycle_executor.py tests/test_agent_session_context.py tests/test_multi_agent_runner.py -q`
Expected: PASS

- [ ] **Step 2: Commit**

```bash
git add backend/config.py backend/tools/web_tools.py config/config-agent-prompts.yaml config/config-misc.yaml tests/test_searxng_search.py tests/test_search_and_fetch.py tests/test_web_tools_config.py tests/test_searxng_docs.py
git commit -m "feat: paginate searxng search results"
```
