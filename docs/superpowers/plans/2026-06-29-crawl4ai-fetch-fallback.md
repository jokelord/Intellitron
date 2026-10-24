# Crawl4AI Fetch Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a three-tier fetch path: `curl_cffi` primary, Crawl4AI browser fallback, and SearXNG snippet content fallback, while reducing `search_and_fetch` default `max_results` to `25`.

**Architecture:** Keep transport selection inside `backend/tools/web_tools.py` and keep snippet fallback inside `backend/tools/search_and_fetch.py`, because snippets come from search result records, not single-URL fetches. Extend config parsing in `backend/config.py` and repository config in `config/config-misc.yaml`.

**Tech Stack:** Python, FastAPI backend utilities, SearXNG, `curl_cffi`, optional `crawl4ai`, Playwright browser runtime via Crawl4AI, pytest.

**Git Rule:** Do not run git commands unless the user explicitly grants permission. Use implementation checkpoints instead of commit steps.

---

## File Structure

- Modify: `backend/config.py`
  - Add `WebToolsConfig.fetch_content_fallback`.
  - Add Crawl4AI config fields.
  - Parse new `web_tools` YAML keys.
- Modify: `backend/tools/web_tools.py`
  - Add `crawl4ai` to transport availability and resolution.
  - Add `_fetch_with_crawl4ai()`.
  - Include Crawl4AI config in `_get_fetch_config()` defaults.
  - Reuse existing block, extraction, and low-quality checks.
- Modify: `backend/tools/search_and_fetch.py`
  - Change default `max_results` from current value to `25`.
  - Pass each search result item into fetch outcomes so snippets are available after fetch failures.
  - Add `摘要兜底` status when both page fetch transports fail and SearXNG title/description exists.
- Modify: `config/config-misc.yaml`
  - Set `fetch_transport: curl_cffi`.
  - Set `fetch_fallback_transport: crawl4ai`.
  - Add `fetch_content_fallback: searxng_snippet`.
  - Add Crawl4AI tuning keys.
- Modify: `tests/test_web_fetch.py`
  - Add unit tests for Crawl4AI availability, transport order, fallback, and output normalization.
- Modify: `tests/test_search_and_fetch.py`
  - Add snippet fallback tests.
  - Update default max-result test to expect `25`.

---

### Task 1: Config Model And YAML Keys

**Files:**
- Modify: `backend/config.py:356-365`
- Modify: `backend/config.py:1261-1301`
- Modify: `config/config-misc.yaml:220-245`

- [ ] **Step 1: Write failing config test**

Add this test to an existing config test file if one exists. If no focused config test exists, create `tests/test_web_tools_config.py`:

```python
from backend.config import ConfigLoader


def test_web_tools_config_accepts_crawl4ai_and_snippet_fallback():
    loader = ConfigLoader()
    loader._data = {
        "web_tools": {
            "provider": "searxng",
            "fetch_transport": "curl_cffi",
            "fetch_fallback_transport": "crawl4ai",
            "fetch_content_fallback": "searxng_snippet",
            "crawl4ai_browser_type": "chromium",
            "crawl4ai_headless": True,
            "crawl4ai_wait_until": "networkidle",
            "crawl4ai_page_timeout_ms": 15000,
            "crawl4ai_word_count_threshold": 10,
            "crawl4ai_bypass_cache": True,
        }
    }

    cfg = loader._parse_web_tools_config()

    assert cfg.fetch_transport == "curl_cffi"
    assert cfg.fetch_fallback_transport == "crawl4ai"
    assert cfg.fetch_content_fallback == "searxng_snippet"
    assert cfg.crawl4ai_browser_type == "chromium"
    assert cfg.crawl4ai_headless is True
    assert cfg.crawl4ai_wait_until == "networkidle"
    assert cfg.crawl4ai_page_timeout_ms == 15000
    assert cfg.crawl4ai_word_count_threshold == 10
    assert cfg.crawl4ai_bypass_cache is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.sglang/bin/python -m pytest tests/test_web_tools_config.py::test_web_tools_config_accepts_crawl4ai_and_snippet_fallback -q`

Expected: FAIL with an attribute error for `fetch_content_fallback` or Crawl4AI config fields.

- [ ] **Step 3: Implement config dataclass fields**

In `backend/config.py`, update `WebToolsConfig`:

```python
fetch_transport: str = "auto"  # auto | curl | curl_cffi | crawl4ai
fetch_fallback_transport: Optional[str] = "curl"
fetch_content_fallback: Optional[str] = None  # null | searxng_snippet
fetch_impersonate: Optional[str] = "chrome"
fetch_max_workers: int = 3
fetch_retry_count: int = 2
fetch_retry_backoff_seconds: float = 1.0
fetch_min_text_chars: int = 200
fetch_enable_readability_fallback: bool = True
fetch_block_markers: List[str] = field(default_factory=list)
crawl4ai_browser_type: str = "chromium"
crawl4ai_headless: bool = True
crawl4ai_wait_until: str = "networkidle"
crawl4ai_page_timeout_ms: int = 15000
crawl4ai_word_count_threshold: int = 10
crawl4ai_bypass_cache: bool = True
```

- [ ] **Step 4: Implement config parser fields**

In `_parse_web_tools_config()`, add fields to the `WebToolsConfig(...)` construction:

```python
fetch_content_fallback=self._get(
    "web_tools", "fetch_content_fallback", None
),
crawl4ai_browser_type=self._coerce_str(
    self._get("web_tools", "crawl4ai_browser_type", "chromium"), "chromium"
),
crawl4ai_headless=(
    True
    if self._get_bool("web_tools", "crawl4ai_headless", True) is None
    else self._get_bool("web_tools", "crawl4ai_headless", True)
),
crawl4ai_wait_until=self._coerce_str(
    self._get("web_tools", "crawl4ai_wait_until", "networkidle"), "networkidle"
),
crawl4ai_page_timeout_ms=(
    15000
    if self._get_int("web_tools", "crawl4ai_page_timeout_ms", 15000) is None
    else self._get_int("web_tools", "crawl4ai_page_timeout_ms", 15000)
),
crawl4ai_word_count_threshold=(
    10
    if self._get_int("web_tools", "crawl4ai_word_count_threshold", 10) is None
    else self._get_int("web_tools", "crawl4ai_word_count_threshold", 10)
),
crawl4ai_bypass_cache=(
    True
    if self._get_bool("web_tools", "crawl4ai_bypass_cache", True) is None
    else self._get_bool("web_tools", "crawl4ai_bypass_cache", True)
),
```

- [ ] **Step 5: Update repository config**

In `config/config-misc.yaml`, set:

```yaml
  fetch_transport: curl_cffi
  fetch_fallback_transport: crawl4ai
  fetch_content_fallback: searxng_snippet
  fetch_impersonate: chrome
  crawl4ai_browser_type: chromium
  crawl4ai_headless: true
  crawl4ai_wait_until: networkidle
  crawl4ai_page_timeout_ms: 15000
  crawl4ai_word_count_threshold: 10
  crawl4ai_bypass_cache: true
```

- [ ] **Step 6: Run config test to verify it passes**

Run: `./.sglang/bin/python -m pytest tests/test_web_tools_config.py -q`

Expected: PASS.

- [ ] **Step 7: Checkpoint**

Report changed files and test result. Do not run git commands without explicit user permission.

---

### Task 2: Crawl4AI Transport Contract

**Files:**
- Modify: `backend/tools/web_tools.py:96-125`
- Modify: `backend/tools/web_tools.py:310-331`
- Modify: `backend/tools/web_tools.py:509-547`
- Modify: `tests/test_web_fetch.py`

- [ ] **Step 1: Write failing transport resolution tests**

Add these methods inside `class TestWebFetchRobustness` in `tests/test_web_fetch.py`:

```python
def test_resolve_fetch_transport_accepts_crawl4ai(self):
    from backend.tools.web_tools import _resolve_fetch_transport

    assert _resolve_fetch_transport("crawl4ai") == "crawl4ai"


def test_get_fetch_config_includes_crawl4ai_defaults(self):
    from backend.tools import web_tools

    class WebTools:
        proxy = None
        fetch_transport = "curl_cffi"
        fetch_fallback_transport = "crawl4ai"
        fetch_content_fallback = "searxng_snippet"
        fetch_impersonate = "chrome"
        fetch_max_workers = 3
        fetch_retry_count = 0
        fetch_retry_backoff_seconds = 0.0
        fetch_min_text_chars = 200
        fetch_enable_readability_fallback = True
        fetch_block_markers = []
        crawl4ai_browser_type = "chromium"
        crawl4ai_headless = True
        crawl4ai_wait_until = "networkidle"
        crawl4ai_page_timeout_ms = 15000
        crawl4ai_word_count_threshold = 10
        crawl4ai_bypass_cache = True

    class Config:
        web_tools = WebTools()

    with patch("backend.tools.web_tools.get_config", return_value=Config()):
        cfg = web_tools._get_fetch_config()

    assert cfg["fetch_fallback_transport"] == "crawl4ai"
    assert cfg["fetch_content_fallback"] == "searxng_snippet"
    assert cfg["crawl4ai_browser_type"] == "chromium"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.sglang/bin/python -m pytest tests/test_web_fetch.py::TestWebFetchRobustness::test_resolve_fetch_transport_accepts_crawl4ai tests/test_web_fetch.py::TestWebFetchRobustness::test_get_fetch_config_includes_crawl4ai_defaults -q`

Expected: FAIL because `_get_fetch_config()` does not include the new keys yet.

- [ ] **Step 3: Extend `_get_fetch_config()`**

In `backend/tools/web_tools.py`, add to the normal config dict:

```python
"fetch_content_fallback": web_tools.fetch_content_fallback,
"crawl4ai_browser_type": web_tools.crawl4ai_browser_type,
"crawl4ai_headless": web_tools.crawl4ai_headless,
"crawl4ai_wait_until": web_tools.crawl4ai_wait_until,
"crawl4ai_page_timeout_ms": web_tools.crawl4ai_page_timeout_ms,
"crawl4ai_word_count_threshold": web_tools.crawl4ai_word_count_threshold,
"crawl4ai_bypass_cache": web_tools.crawl4ai_bypass_cache,
```

Add to the exception fallback dict:

```python
"fetch_content_fallback": None,
"crawl4ai_browser_type": "chromium",
"crawl4ai_headless": True,
"crawl4ai_wait_until": "networkidle",
"crawl4ai_page_timeout_ms": 15000,
"crawl4ai_word_count_threshold": 10,
"crawl4ai_bypass_cache": True,
```

- [ ] **Step 4: Extend transport availability**

In `_transport_is_available()`:

```python
if transport == "crawl4ai":
    try:
        import crawl4ai  # noqa: F401

        return True
    except Exception:
        return False
```

In `_resolve_fetch_transport()`, no special mapping is needed beyond returning normalized names, but ensure `crawl4ai` is not rejected.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.sglang/bin/python -m pytest tests/test_web_fetch.py::TestWebFetchRobustness::test_resolve_fetch_transport_accepts_crawl4ai tests/test_web_fetch.py::TestWebFetchRobustness::test_get_fetch_config_includes_crawl4ai_defaults -q`

Expected: PASS.

- [ ] **Step 6: Checkpoint**

Report changed files and test result. Do not run git commands without explicit user permission.

---

### Task 3: Crawl4AI Fetch Implementation

**Files:**
- Modify: `backend/tools/web_tools.py`
- Modify: `tests/test_web_fetch.py`

- [ ] **Step 1: Write failing test for `curl_cffi -> crawl4ai` fallback order**

Add this method inside `class TestWebFetchRobustness` in `tests/test_web_fetch.py`:

```python
@patch("backend.tools.web_tools._get_fetch_config")
@patch("backend.tools.web_tools._fetch_with_crawl4ai")
@patch("backend.tools.web_tools._fetch_with_curl_cffi")
def test_web_fetch_falls_back_from_curl_cffi_to_crawl4ai(
    self,
    mock_fetch_cffi,
    mock_fetch_crawl4ai,
    mock_get_fetch_config,
):
    from backend.tools.web_tools import web_fetch

    mock_get_fetch_config.return_value = {
        "proxy": None,
        "fetch_transport": "curl_cffi",
        "fetch_fallback_transport": "crawl4ai",
        "fetch_content_fallback": "searxng_snippet",
        "fetch_impersonate": "chrome",
        "fetch_retry_count": 0,
        "fetch_retry_backoff_seconds": 0.0,
        "fetch_min_text_chars": 120,
        "fetch_enable_readability_fallback": True,
        "fetch_block_markers": None,
        "crawl4ai_browser_type": "chromium",
        "crawl4ai_headless": True,
        "crawl4ai_wait_until": "networkidle",
        "crawl4ai_page_timeout_ms": 15000,
        "crawl4ai_word_count_threshold": 10,
        "crawl4ai_bypass_cache": True,
    }
    mock_fetch_cffi.side_effect = TimeoutError("primary timed out")
    mock_fetch_crawl4ai.return_value = {
        "status_code": 200,
        "final_url": "https://example.com/article",
        "content_type": "text/markdown",
        "body": "# Recovered article\n\n" + ("Recovered through Crawl4AI. " * 60),
        "transport": "crawl4ai",
        "profile": "chromium",
    }

    result = web_fetch("https://example.com/article", timeout=12)

    assert "Recovered through Crawl4AI." in result
    assert mock_fetch_cffi.called
    assert mock_fetch_crawl4ai.called
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.sglang/bin/python -m pytest tests/test_web_fetch.py::TestWebFetchRobustness::test_web_fetch_falls_back_from_curl_cffi_to_crawl4ai -q`

Expected: FAIL because `_fetch_with_crawl4ai` does not exist or `_fetch_webpage_content()` does not call it.

- [ ] **Step 3: Add `_fetch_with_crawl4ai()`**

Add this function near `_fetch_with_curl_cffi()` in `backend/tools/web_tools.py`:

```python
def _fetch_with_crawl4ai(
    url: str,
    timeout: int,
    cfg: dict,
    check_abort: Optional[Callable] = None,
) -> dict:
    """Fetch a webpage via Crawl4AI browser rendering when available."""
    if check_abort and check_abort():
        raise AbortError("Fetch aborted by user")

    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
    except Exception as exc:
        raise RuntimeError("crawl4ai is not installed") from exc

    import asyncio

    async def _run() -> dict:
        browser_config = BrowserConfig(
            browser_type=str(cfg.get("crawl4ai_browser_type") or "chromium"),
            headless=bool(cfg.get("crawl4ai_headless", True)),
        )
        run_config = CrawlerRunConfig(
            word_count_threshold=int(cfg.get("crawl4ai_word_count_threshold") or 10),
            wait_until=str(cfg.get("crawl4ai_wait_until") or "networkidle"),
            page_timeout=int(cfg.get("crawl4ai_page_timeout_ms") or timeout * 1000),
            bypass_cache=bool(cfg.get("crawl4ai_bypass_cache", True)),
        )
        async with AsyncWebCrawler(config=browser_config) as crawler:
            result = await crawler.arun(url=url, config=run_config)

        if check_abort and check_abort():
            raise AbortError("Fetch aborted by user")
        if not getattr(result, "success", True):
            error = getattr(result, "error_message", "crawl4ai fetch failed")
            raise RuntimeError(str(error))

        markdown = getattr(result, "markdown", None)
        body = ""
        content_type = "text/markdown"
        if markdown:
            body = str(markdown)
        else:
            cleaned_html = getattr(result, "cleaned_html", None)
            html = cleaned_html or getattr(result, "html", None)
            if html:
                body = str(html)
                content_type = "text/html"

        final_url = getattr(result, "url", None) or url
        status_code = getattr(result, "status_code", None) or 200
        return {
            "status_code": int(status_code),
            "final_url": str(final_url),
            "content_type": content_type,
            "body": body,
            "transport": "crawl4ai",
            "profile": str(cfg.get("crawl4ai_browser_type") or "chromium"),
        }

    try:
        return asyncio.run(_run())
    except RuntimeError as exc:
        if "asyncio.run() cannot be called" not in str(exc):
            raise
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()
```

- [ ] **Step 4: Wire Crawl4AI into `_fetch_webpage_content()`**

Inside the transport dispatch in `_fetch_webpage_content()`:

```python
if transport == "crawl4ai":
    response = _fetch_with_crawl4ai(
        url=url,
        timeout=timeout,
        cfg=cfg,
        check_abort=check_abort,
    )
elif transport == "curl_cffi":
    response = _fetch_with_curl_cffi(
        url=url,
        timeout=timeout,
        proxy=proxy,
        headers=headers,
        impersonate=impersonate,
        check_abort=check_abort,
    )
else:
    response = _fetch_with_curl(
        url=url,
        timeout=timeout,
        proxy=proxy,
        headers=headers,
        cookie_jar_path=cookie_jar_path,
        check_abort=check_abort,
    )
```

For `transport_profiles`, use:

```python
transport_profiles = (
    profile_attempts if transport == "curl_cffi" else [("desktop", "")]
)
```

Crawl4AI should run once per transport attempt, not once per curl_cffi impersonation profile.

- [ ] **Step 5: Run test to verify it passes**

Run: `./.sglang/bin/python -m pytest tests/test_web_fetch.py::TestWebFetchRobustness::test_web_fetch_falls_back_from_curl_cffi_to_crawl4ai -q`

Expected: PASS.

- [ ] **Step 6: Run web fetch test file**

Run: `./.sglang/bin/python -m pytest tests/test_web_fetch.py -q`

Expected: PASS.

- [ ] **Step 7: Checkpoint**

Report changed files and test result. Do not run git commands without explicit user permission.

---

### Task 4: SearXNG Snippet Content Fallback

**Files:**
- Modify: `backend/tools/search_and_fetch.py`
- Modify: `backend/tools/web_tools.py`
- Modify: `tests/test_search_and_fetch.py`

- [ ] **Step 1: Write failing snippet fallback test**

Add this method inside `class TestSearchAndFetchImpl` in `tests/test_search_and_fetch.py`:

```python
@patch("backend.tools.search_and_fetch._fetch_single")
@patch("backend.tools.search_and_fetch._do_search_internal")
@patch("backend.tools.search_and_fetch._get_fetch_content_fallback", return_value="searxng_snippet", create=True)
def test_search_and_fetch_uses_searxng_snippet_after_fetch_failure(
    self,
    mock_content_fallback,
    mock_search,
    mock_fetch,
):
    from backend.tools.search_and_fetch import search_and_fetch_impl

    mock_search.return_value = [
        {
            "title": "Example result",
            "url": "https://example.com/article",
            "description": "Useful search snippet from SearXNG.",
        }
    ]
    mock_fetch.side_effect = RuntimeError("low quality extracted content")

    result = search_and_fetch_impl("snippet query", max_results=1)

    assert "共找到 1 个结果，成功抓取 0 个" in result
    assert "摘要兜底 1 个" in result
    assert "状态: 摘要兜底" in result
    assert "[FULL_FETCH_FAILED - low quality extracted content]" in result
    assert "Example result" in result
    assert "Useful search snippet from SearXNG." in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.sglang/bin/python -m pytest tests/test_search_and_fetch.py::TestSearchAndFetchImpl::test_search_and_fetch_uses_searxng_snippet_after_fetch_failure -q`

Expected: FAIL because `_get_fetch_content_fallback()` and `摘要兜底` handling do not exist.

- [ ] **Step 3: Add content fallback config helper**

In `backend/tools/search_and_fetch.py`, add:

```python
def _get_fetch_content_fallback() -> Optional[str]:
    """Read final content fallback mode from config."""
    try:
        config = get_config()
        value = getattr(config.web_tools, "fetch_content_fallback", None)
    except Exception:
        value = None
    normalized = (str(value).strip().lower() if value is not None else "")
    return normalized or None
```

- [ ] **Step 4: Add snippet formatter**

In `backend/tools/search_and_fetch.py`, add:

```python
def _build_searxng_snippet_content(item: dict, error: str) -> Optional[str]:
    title = str(item.get("title") or "").strip()
    description = str(item.get("description") or "").strip()
    if not title and not description:
        return None
    parts = [f"[FULL_FETCH_FAILED - {error}]", "Search result snippet:"]
    if title:
        parts.append(title)
    if description:
        parts.append(description)
    return "\n".join(parts)
```

- [ ] **Step 5: Apply snippet fallback in exception handling**

In `search_and_fetch_impl()`, compute once near `deadline_abort`:

```python
content_fallback = _get_fetch_content_fallback()
```

In the generic fetch exception branch, before assigning `状态: 失败`:

```python
error = str(e)
snippet_content = None
if content_fallback == "searxng_snippet":
    snippet_content = _build_searxng_snippet_content(item, error)
if snippet_content:
    indexed_results[idx] = {
        "title": item["title"],
        "url": item["url"],
        "content": snippet_content,
        "status": "摘要兜底",
        "error": error,
    }
else:
    indexed_results[idx] = {
        "title": item["title"],
        "url": item["url"],
        "content": None,
        "status": "失败",
        "error": error,
    }
```

Keep timeout outcomes as `超时` unless they raise through the generic exception branch. Do not synthesize snippets for not-done futures unless a later explicit requirement asks for timeout snippet fallback.

- [ ] **Step 6: Update summary counts and output rendering**

Add count:

```python
snippet_count = sum(1 for o in fetch_outcomes if o["status"] == "摘要兜底")
```

Update summary line:

```python
f"共找到 {total} 个结果，成功抓取 {success_count} 个，"
f"摘要兜底 {snippet_count} 个，超时 {timeout_count} 个，失败 {fail_count} 个"
```

Add rendering branch:

```python
elif outcome["status"] == "摘要兜底":
    lines.append(f"状态: 摘要兜底 ({outcome['error']})")
    content = outcome["content"] or ""
    if len(content) > _MAX_CONTENT_LENGTH:
        content = content[:_MAX_CONTENT_LENGTH] + "\n...(内容已截断)"
    lines.append("内容:")
    lines.append(content)
```

- [ ] **Step 7: Run snippet fallback test to verify it passes**

Run: `./.sglang/bin/python -m pytest tests/test_search_and_fetch.py::TestSearchAndFetchImpl::test_search_and_fetch_uses_searxng_snippet_after_fetch_failure -q`

Expected: PASS.

- [ ] **Step 8: Add no-snippet failure test**

Add this method inside `class TestSearchAndFetchImpl` in `tests/test_search_and_fetch.py`:

```python
@patch("backend.tools.search_and_fetch._fetch_single")
@patch("backend.tools.search_and_fetch._do_search_internal")
@patch("backend.tools.search_and_fetch._get_fetch_content_fallback", return_value="searxng_snippet", create=True)
def test_search_and_fetch_counts_failure_when_snippet_missing(
    self,
    mock_content_fallback,
    mock_search,
    mock_fetch,
):
    from backend.tools.search_and_fetch import search_and_fetch_impl

    mock_search.return_value = [
        {"title": "", "url": "https://example.com/article", "description": ""}
    ]
    mock_fetch.side_effect = RuntimeError("empty extracted content")

    result = search_and_fetch_impl("missing snippet", max_results=1)

    assert "摘要兜底 0 个" in result
    assert "失败 1 个" in result
    assert "状态: 失败 (empty extracted content)" in result
```

- [ ] **Step 9: Run search-and-fetch tests**

Run: `./.sglang/bin/python -m pytest tests/test_search_and_fetch.py -q`

Expected: PASS after Task 5 updates the default max-results expectation.

- [ ] **Step 10: Checkpoint**

Report changed files and test result. Do not run git commands without explicit user permission.

---

### Task 5: Default `max_results` Reduction To 25

**Files:**
- Modify: `backend/tools/search_and_fetch.py:176-186`
- Modify: `tests/test_search_and_fetch.py:514-535`

- [ ] **Step 1: Write or update failing default test**

Replace the existing default test body with:

```python
@patch("backend.tools.search_and_fetch._fetch_single")
@patch("backend.tools.search_and_fetch._do_search_internal")
def test_default_max_results_is_twenty_five(self, mock_search, mock_fetch):
    """When tool args omit max_results, search_and_fetch defaults to 25."""
    from backend.tools.search_and_fetch import search_and_fetch_impl

    mock_search.return_value = [
        {
            "title": f"Site {i}",
            "url": f"https://site{i}.com",
            "description": f"Desc {i}",
        }
        for i in range(30)
    ]
    mock_fetch.return_value = "content"

    result = search_and_fetch_impl("default results")

    assert mock_search.call_args.kwargs["max_results"] == 25
    assert mock_fetch.call_count == 25
    assert "共找到 25 个结果" in result
    assert "--- [25] Site 24 ---" in result
    assert "--- [26]" not in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.sglang/bin/python -m pytest tests/test_search_and_fetch.py::TestSearchAndFetchImpl::test_default_max_results_is_twenty_five -q`

Expected: FAIL while implementation default is not `25`.

- [ ] **Step 3: Change implementation default and docstring**

In `backend/tools/search_and_fetch.py`:

```python
def search_and_fetch_impl(
    query: str,
    max_results: int = 25,
    check_abort: Optional[callable] = None,
) -> str:
```

Update docstring:

```python
max_results: Maximum number of search results to process (default 25).
```

- [ ] **Step 4: Run default test to verify it passes**

Run: `./.sglang/bin/python -m pytest tests/test_search_and_fetch.py::TestSearchAndFetchImpl::test_default_max_results_is_twenty_five -q`

Expected: PASS.

- [ ] **Step 5: Run full search-and-fetch test file**

Run: `./.sglang/bin/python -m pytest tests/test_search_and_fetch.py -q`

Expected: PASS.

- [ ] **Step 6: Checkpoint**

Report changed files and test result. Do not run git commands without explicit user permission.

---

### Task 6: Verification And Optional Integration Check

**Files:**
- No new production files required.
- Optional create: `tests/test_crawl4ai_fetch_integration.py`

- [ ] **Step 1: Run focused unit tests**

Run: `./.sglang/bin/python -m pytest tests/test_web_fetch.py tests/test_search_and_fetch.py tests/test_web_tools_config.py -q`

Expected: PASS.

- [ ] **Step 2: Verify effective runtime config**

Run:

```bash
./.sglang/bin/python - <<'PY'
from backend.config import get_config
cfg = get_config().web_tools
print('fetch_transport=', cfg.fetch_transport)
print('fetch_fallback_transport=', cfg.fetch_fallback_transport)
print('fetch_content_fallback=', cfg.fetch_content_fallback)
print('crawl4ai_browser_type=', cfg.crawl4ai_browser_type)
PY
```

Expected output includes:

```text
fetch_transport= curl_cffi
fetch_fallback_transport= crawl4ai
fetch_content_fallback= searxng_snippet
crawl4ai_browser_type= chromium
```

- [ ] **Step 3: Optional import check**

Run:

```bash
./.sglang/bin/python - <<'PY'
import importlib.util
print('crawl4ai:', 'installed' if importlib.util.find_spec('crawl4ai') else 'missing')
PY
```

Expected: `crawl4ai: installed` if local integration is expected. If missing, unit tests should still pass because Crawl4AI is optional and mocked.

- [ ] **Step 4: Optional local integration test**

Only run if Crawl4AI and browser runtime are installed:

```bash
RUN_CRAWL4AI_INTEGRATION=1 ./.sglang/bin/python -m pytest tests/test_crawl4ai_fetch_integration.py -q
```

Expected: PASS or SKIP with a clear reason when the environment lacks Crawl4AI/browser runtime.

- [ ] **Step 5: Final checkpoint**

Report:

```text
Changed files:
- backend/config.py
- backend/tools/web_tools.py
- backend/tools/search_and_fetch.py
- config/config-misc.yaml
- tests/test_web_fetch.py
- tests/test_search_and_fetch.py
- tests/test_web_tools_config.py

Verification:
- web fetch tests: PASS
- search_and_fetch tests: PASS
- config tests: PASS
- effective config: curl_cffi -> crawl4ai -> searxng_snippet
```

Do not run git commands unless the user grants permission.
