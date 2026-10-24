# tests/test_search_and_fetch.py
"""Tests for search_and_fetch composite tool."""

from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import urllib.request
from unittest.mock import MagicMock, patch

import pytest


def _completed_future(result=None, error=None):
    future = Future()
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(result)
    return future


class TestParseSearchResults:
    """Test _parse_search_results markdown parser."""

    def test_parse_two_results(self):
        """Parse standard markdown search output with two results."""
        from backend.tools.search_and_fetch import _parse_search_results

        raw = (
            "## Search Results\n\n"
            "- [Python Official](https://python.org)\n"
            "  The official Python website.\n\n"
            "- [Real Python](https://realpython.com)\n"
            "  Python tutorials and guides.\n\n"
        )
        results = _parse_search_results(raw)
        assert len(results) == 2
        assert results[0]["title"] == "Python Official"
        assert results[0]["url"] == "https://python.org"
        assert results[0]["description"] == "The official Python website."
        assert results[1]["title"] == "Real Python"
        assert results[1]["url"] == "https://realpython.com"
        assert results[1]["description"] == "Python tutorials and guides."

    def test_parse_no_description(self):
        """Parse result without description line."""
        from backend.tools.search_and_fetch import _parse_search_results

        raw = "## Search Results\n\n- [Example](https://example.com)\n\n"
        results = _parse_search_results(raw)
        assert len(results) == 1
        assert results[0]["title"] == "Example"
        assert results[0]["url"] == "https://example.com"
        assert results[0]["description"] == ""

    def test_parse_empty_input(self):
        """Empty or non-matching input returns empty list."""
        from backend.tools.search_and_fetch import _parse_search_results

        assert _parse_search_results("") == []
        assert _parse_search_results("No search results found.") == []

    def test_parse_error_message(self):
        """Search error message returns empty list."""
        from backend.tools.search_and_fetch import _parse_search_results

        msg = "No search results found. The search service may be temporarily unavailable."
        assert _parse_search_results(msg) == []


class TestSearchAndFetchImpl:
    """Test search_and_fetch_impl function with mocked internals."""

    @patch(
        "backend.tools.search_and_fetch._fetch_webpage_content",
        side_effect=TimeoutError("request timed out"),
    )
    def test_fetch_single_uses_exception_based_internal_fetch_contract(
        self,
        mock_fetch_content,
    ):
        """_fetch_single should rely on internal exceptions, not parse tool strings."""
        from backend.tools.search_and_fetch import _fetch_single

        with pytest.raises(TimeoutError, match="Request timed out for https://slow.com"):
            _fetch_single("https://slow.com", timeout=10)

        mock_fetch_content.assert_called_once_with(
            "https://slow.com", timeout=10, check_abort=None
        )

    @patch("backend.tools.search_and_fetch._fetch_single")
    @patch("backend.tools.search_and_fetch._do_search_internal")
    def test_basic_search_and_fetch(self, mock_search, mock_fetch):
        """Two results, both fetched successfully."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_search.return_value = [
            {"title": "Site A", "url": "https://a.com", "description": "Desc A"},
            {"title": "Site B", "url": "https://b.com", "description": "Desc B"},
        ]
        mock_fetch.side_effect = lambda url, timeout, check_abort: (
            "Content of A" if "a.com" in url else "Content of B"
        )

        result = search_and_fetch_impl("test query", max_results=5)

        assert '=== 搜索结果: "test query" ===' in result
        assert "共找到 2 个结果，成功抓取 2 个，超时 0 个，失败 0 个" in result
        assert "--- [1] Site A ---" in result
        assert "URL: https://a.com" in result
        assert "状态: 成功" in result
        assert "Content of A" in result
        assert "--- [2] Site B ---" in result
        assert "Content of B" in result
        assert "=== 搜索结束 ===" in result

    @patch("backend.tools.search_and_fetch.wait")
    @patch("backend.tools.search_and_fetch.ThreadPoolExecutor")
    @patch("backend.tools.search_and_fetch._get_fetch_max_workers", return_value=50)
    @patch("backend.tools.search_and_fetch._get_search_timeout", return_value=15)
    @patch("backend.tools.search_and_fetch.get_config")
    def test_streaming_pipeline_submits_page_one_results_immediately(
        self,
        mock_get_config,
        mock_get_search_timeout,
        mock_get_fetch_max_workers,
        mock_executor_cls,
        mock_wait,
    ):
        from backend.tools.search_and_fetch import search_and_fetch_impl

        page_1 = [
            {"title": "A", "url": "https://a.com", "description": ""},
            {"title": "B", "url": "https://b.com", "description": ""},
            {"title": "C", "url": "https://c.com", "description": ""},
            {"title": "D", "url": "https://d.com", "description": ""},
        ]
        page_2 = [
            {"title": "E", "url": "https://e.com", "description": ""},
            {"title": "F", "url": "https://f.com", "description": ""},
        ]

        mock_get_config.return_value = MagicMock(
            web_tools=MagicMock(fetch_max_workers=50)
        )

        submit_calls = []
        executor = MagicMock()
        executor.submit.side_effect = lambda fn, url, timeout, check_abort: (
            submit_calls.append(url) or _completed_future(f"content for {url}")
        )
        mock_executor_cls.return_value = executor
        mock_wait.side_effect = lambda futures, timeout: (set(futures), set())

        with patch(
            "backend.tools.search_and_fetch._fetch_searxng_page",
            side_effect=[page_1, page_2],
        ), patch(
            "backend.tools.search_and_fetch._get_config",
            return_value={"searxng_max_pages": 3},
        ):
            result = search_and_fetch_impl("streaming query", max_results=6)

        assert submit_calls == [
            "https://a.com",
            "https://b.com",
            "https://c.com",
            "https://d.com",
            "https://e.com",
            "https://f.com",
        ]
        assert "共找到 6 个结果，成功抓取 6 个" in result

    @patch("backend.tools.search_and_fetch.wait")
    @patch("backend.tools.search_and_fetch.ThreadPoolExecutor")
    @patch("backend.tools.search_and_fetch._get_fetch_max_workers", return_value=50)
    @patch("backend.tools.search_and_fetch._get_search_timeout", return_value=15)
    @patch("backend.tools.search_and_fetch.get_config")
    def test_streaming_pipeline_submits_only_new_urls_from_later_pages(
        self,
        mock_get_config,
        mock_get_search_timeout,
        mock_get_fetch_max_workers,
        mock_executor_cls,
        mock_wait,
    ):
        from backend.tools.search_and_fetch import search_and_fetch_impl

        page_1 = [
            {"title": "A", "url": "https://dup.com", "description": ""},
            {"title": "B", "url": "https://b.com", "description": ""},
        ]
        page_2 = [
            {"title": "A2", "url": "https://dup.com/", "description": ""},
            {"title": "C", "url": "https://new.com", "description": ""},
        ]
        page_3 = []

        mock_get_config.return_value = MagicMock(
            web_tools=MagicMock(fetch_max_workers=50)
        )

        submit_calls = []
        executor = MagicMock()
        executor.submit.side_effect = lambda fn, url, timeout, check_abort: (
            submit_calls.append(url) or _completed_future(f"content for {url}")
        )
        mock_executor_cls.return_value = executor
        mock_wait.side_effect = lambda futures, timeout: (set(futures), set())

        with patch(
            "backend.tools.search_and_fetch._fetch_searxng_page",
            side_effect=[page_1, page_2, page_3],
        ), patch(
            "backend.tools.search_and_fetch._get_config",
            return_value={"searxng_max_pages": 3},
        ):
            result = search_and_fetch_impl("dedupe query", max_results=5)

        assert submit_calls == ["https://dup.com", "https://b.com", "https://new.com"]
        assert "共找到 3 个结果，成功抓取 3 个" in result

    @patch("backend.tools.search_and_fetch.wait")
    @patch("backend.tools.search_and_fetch.ThreadPoolExecutor")
    @patch("backend.tools.search_and_fetch._get_fetch_max_workers", return_value=50)
    @patch("backend.tools.search_and_fetch._get_search_timeout", return_value=15)
    @patch("backend.tools.search_and_fetch.get_config")
    def test_streaming_pipeline_stops_after_reaching_deduped_max_results(
        self,
        mock_get_config,
        mock_get_search_timeout,
        mock_get_fetch_max_workers,
        mock_executor_cls,
        mock_wait,
    ):
        from backend.tools.search_and_fetch import search_and_fetch_impl

        page_1 = [
            {"title": f"Site {i}", "url": f"https://site{i}.com", "description": ""}
            for i in range(6)
        ]
        page_2 = [
            {"title": f"Site {i}", "url": f"https://site{i}.com", "description": ""}
            for i in range(4, 10)
        ]

        mock_get_config.return_value = MagicMock(
            web_tools=MagicMock(fetch_max_workers=50)
        )

        submit_calls = []
        executor = MagicMock()
        executor.submit.side_effect = lambda fn, url, timeout, check_abort: (
            submit_calls.append(url) or _completed_future(f"content for {url}")
        )
        mock_executor_cls.return_value = executor
        mock_wait.side_effect = lambda futures, timeout: (set(futures), set())

        with patch(
            "backend.tools.search_and_fetch._fetch_searxng_page",
            side_effect=[page_1, page_2],
        ) as mock_fetch_page, patch(
            "backend.tools.search_and_fetch._get_config",
            return_value={"searxng_max_pages": 5},
        ):
            result = search_and_fetch_impl("max results query", max_results=8)

        assert len(submit_calls) == 8
        assert mock_fetch_page.call_count == 2
        assert "共找到 8 个结果，成功抓取 8 个" in result

    @patch("backend.tools.search_and_fetch.wait")
    @patch("backend.tools.search_and_fetch.ThreadPoolExecutor")
    @patch("backend.tools.search_and_fetch._get_fetch_max_workers", return_value=50)
    @patch("backend.tools.search_and_fetch._get_search_timeout", return_value=15)
    @patch("backend.tools.search_and_fetch.get_config")
    def test_streaming_pipeline_output_hides_pagination_and_dedupe_internals(
        self,
        mock_get_config,
        mock_get_search_timeout,
        mock_get_fetch_max_workers,
        mock_executor_cls,
        mock_wait,
    ):
        from backend.tools.search_and_fetch import search_and_fetch_impl

        page_1 = [
            {"title": "A", "url": "https://a.com", "description": ""},
        ]

        mock_get_config.return_value = MagicMock(
            web_tools=MagicMock(fetch_max_workers=50)
        )

        executor = MagicMock()
        executor.submit.side_effect = lambda fn, url, timeout, check_abort: _completed_future(
            f"content for {url}"
        )
        mock_executor_cls.return_value = executor
        mock_wait.side_effect = lambda futures, timeout: (set(futures), set())

        with patch(
            "backend.tools.search_and_fetch._fetch_searxng_page",
            side_effect=[page_1],
        ), patch(
            "backend.tools.search_and_fetch._get_config",
            return_value={"searxng_max_pages": 3},
        ):
            result = search_and_fetch_impl("quiet query", max_results=1)

        assert "共找到 1 个结果，成功抓取 1 个" in result
        assert "第1页" not in result
        assert "去重" not in result
        assert "pageno" not in result

    @patch("backend.tools.search_and_fetch.wait")
    @patch("backend.tools.search_and_fetch.ThreadPoolExecutor")
    @patch("backend.tools.search_and_fetch._do_search_internal")
    @patch("backend.tools.search_and_fetch._get_fetch_max_workers", return_value=2, create=True)
    def test_executor_shutdown_waits_for_fetch_workers_after_timeout_or_abort(
        self,
        mock_get_fetch_max_workers,
        mock_search,
        mock_executor_cls,
        mock_wait,
    ):
        """search_and_fetch must not leave curl_cffi fetch threads running.

        Regression for stop-then-next-turn native crash (`free(): invalid
        pointer`): search_and_fetch used executor.shutdown(wait=False,
        cancel_futures=True), so already-running curl_cffi/libcurl fetch
        workers survived after the tool returned. A new user turn could start
        another search_and_fetch while those old C-extension requests were
        still unwinding. The tool must wait for workers to exit before it
        returns control to the agent loop.
        """
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_search.return_value = [
            {"title": "Slow Site", "url": "https://slow.example", "description": ""},
        ]

        executor = MagicMock()
        future = MagicMock()
        executor.submit.return_value = future
        mock_executor_cls.return_value = executor
        mock_wait.return_value = (set(), {future})

        result = search_and_fetch_impl("shutdown wait", max_results=1)

        assert "超时 1 个" in result
        future.cancel.assert_called_once_with()
        executor.shutdown.assert_called_once_with(wait=True, cancel_futures=True)

    @patch("backend.tools.search_and_fetch._get_search_timeout", return_value=5)
    @patch("backend.tools.search_and_fetch._get_fetch_max_workers", return_value=2)
    @patch("backend.tools.search_and_fetch._do_search_internal")
    @patch("backend.tools.search_and_fetch._fetch_single")
    def test_fetch_max_workers_is_global_across_parallel_search_calls(
        self,
        mock_fetch,
        mock_search,
        mock_get_fetch_max_workers,
        mock_get_search_timeout,
    ):
        """fetch_max_workers caps process-wide fetches, not per tool call."""
        mock_search.return_value = [
            {"title": "A", "url": "https://a.example", "description": ""},
            {"title": "B", "url": "https://b.example", "description": ""},
        ]

        active = 0
        max_active = 0
        lock = threading.Lock()

        def slow_fetch(url, timeout, check_abort):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return f"content for {url}"

        mock_fetch.side_effect = slow_fetch

        from backend.tools.search_and_fetch import search_and_fetch_impl

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(search_and_fetch_impl, f"query {idx}", 2)
                for idx in range(2)
            ]
            results = [future.result(timeout=5) for future in futures]

        assert all("成功抓取 2 个" in result for result in results)
        assert max_active <= 2

    @patch("backend.tools.search_and_fetch._fetch_single")
    @patch("backend.tools.search_and_fetch._do_search_internal")
    def test_empty_search_results(self, mock_search, mock_fetch):
        """Search returns no results."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_search.return_value = []

        result = search_and_fetch_impl("nonexistent query")

        assert "共找到 0 个结果" in result
        assert "=== 搜索结束 ===" in result
        mock_fetch.assert_not_called()

    @patch("backend.tools.search_and_fetch._fetch_single")
    @patch("backend.tools.search_and_fetch._do_search_internal")
    @patch("backend.tools.search_and_fetch._get_fetch_content_fallback", return_value=None)
    def test_partial_fetch_failure(
        self, mock_get_fetch_content_fallback, mock_search, mock_fetch
    ):
        """One result fetched, one fails."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_search.return_value = [
            {"title": "Good Site", "url": "https://good.com", "description": "Good"},
            {"title": "Bad Site", "url": "https://bad.com", "description": "Bad"},
        ]

        def fetch_side_effect(url, timeout, check_abort):
            if "bad.com" in url:
                raise Exception("Connection refused")
            return "Good content here"

        mock_fetch.side_effect = fetch_side_effect

        result = search_and_fetch_impl("mixed query")

        assert "成功抓取 1 个" in result
        assert "失败 1 个" in result
        assert "--- [1] Good Site ---" in result
        assert "状态: 成功" in result
        assert "Good content here" in result
        assert "--- [2] Bad Site ---" in result
        assert "状态: 失败" in result
        assert "Connection refused" in result

    @patch("backend.tools.search_and_fetch._fetch_single")
    @patch("backend.tools.search_and_fetch._do_search_internal")
    @patch("backend.tools.search_and_fetch._get_fetch_max_workers", return_value=1)
    @patch("backend.tools.search_and_fetch._get_search_timeout", return_value=15)
    @patch("backend.tools.search_and_fetch.get_config")
    def test_fetch_failure_uses_searxng_snippet_fallback_when_configured(
        self,
        mock_get_config,
        mock_get_search_timeout,
        mock_get_fetch_max_workers,
        mock_search,
        mock_fetch,
    ):
        """Generic fetch failures can degrade to SearXNG snippet content."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_get_config.return_value = MagicMock(
            web_tools=MagicMock(fetch_content_fallback="searxng_snippet")
        )
        mock_search.return_value = [
            {
                "title": "Snippet Site",
                "url": "https://snippet.example",
                "description": "Search result summary text.",
            }
        ]
        mock_fetch.side_effect = Exception("full fetch failed")

        result = search_and_fetch_impl("snippet fallback", max_results=1)

        assert "成功抓取 0 个" in result
        assert "摘要兜底 1 个" in result
        assert "失败 0 个" in result
        assert "状态: 摘要兜底 (full fetch failed)" in result
        assert "source=searxng_snippet" in result
        assert "Snippet Site" in result
        assert "Search result summary text." in result

    @patch("backend.tools.search_and_fetch._fetch_single")
    @patch("backend.tools.search_and_fetch._do_search_internal")
    @patch("backend.tools.search_and_fetch._get_fetch_max_workers", return_value=1)
    @patch("backend.tools.search_and_fetch._get_search_timeout", return_value=15)
    @patch("backend.tools.search_and_fetch.get_config")
    def test_done_timeout_uses_searxng_snippet_fallback_when_configured(
        self,
        mock_get_config,
        mock_get_search_timeout,
        mock_get_fetch_max_workers,
        mock_search,
        mock_fetch,
    ):
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_get_config.return_value = MagicMock(
            web_tools=MagicMock(fetch_content_fallback="searxng_snippet")
        )
        mock_search.return_value = [
            {
                "title": "Slow Snippet Site",
                "url": "https://slow-snippet.example",
                "description": "Search result summary after timeout.",
            }
        ]
        mock_fetch.side_effect = TimeoutError("Request timed out")

        result = search_and_fetch_impl("timeout snippet fallback", max_results=1)

        assert "成功抓取 0 个" in result
        assert "超时 0 个" in result
        assert "失败 0 个" in result
        assert "摘要兜底 1 个" in result
        assert "状态: 摘要兜底 (Request timed out)" in result
        assert "source=searxng_snippet" in result
        assert "Slow Snippet Site" in result
        assert "Search result summary after timeout." in result

    @patch("backend.tools.search_and_fetch._fetch_single")
    @patch("backend.tools.search_and_fetch._do_search_internal")
    @patch("backend.tools.search_and_fetch._get_fetch_max_workers", return_value=1)
    @patch("backend.tools.search_and_fetch._get_search_timeout", return_value=15)
    @patch("backend.tools.search_and_fetch.get_config")
    def test_fetch_failure_without_snippet_remains_failure(
        self,
        mock_get_config,
        mock_get_search_timeout,
        mock_get_fetch_max_workers,
        mock_search,
        mock_fetch,
    ):
        """Snippet fallback needs at least a title or description."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_get_config.return_value = MagicMock(
            web_tools=MagicMock(fetch_content_fallback="searxng_snippet")
        )
        mock_search.return_value = [
            {"title": "", "url": "https://empty.example", "description": ""}
        ]
        mock_fetch.side_effect = Exception("full fetch failed")

        result = search_and_fetch_impl("no snippet", max_results=1)

        assert "成功抓取 0 个" in result
        assert "摘要兜底 0 个" in result
        assert "失败 1 个" in result
        assert "状态: 失败 (full fetch failed)" in result
        assert "source=searxng_snippet" not in result

    @patch("backend.tools.search_and_fetch._fetch_single")
    @patch("backend.tools.search_and_fetch._do_search_internal")
    def test_result_format_validation(self, mock_search, mock_fetch):
        """Validate full output structure: title, URL, status, content sections."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_search.return_value = [
            {"title": "Test Page", "url": "https://test.com", "description": "A test"},
        ]
        mock_fetch.return_value = "Page body text"

        result = search_and_fetch_impl("format test")

        # Header
        assert result.startswith('=== 搜索结果: "format test" ===')
        # Footer
        assert result.strip().endswith("=== 搜索结束 ===")
        # Entry structure
        assert "--- [1] Test Page ---" in result
        assert "URL: https://test.com" in result
        assert "状态: 成功" in result
        assert "内容:" in result
        assert "Page body text" in result

    def test_empty_query(self):
        """Empty query returns error message without calling search."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        result = search_and_fetch_impl("")
        assert "错误" in result or "error" in result.lower() or "空" in result

        result2 = search_and_fetch_impl("   ")
        assert "错误" in result2 or "error" in result2.lower() or "空" in result2

    @patch("backend.tools.search_and_fetch._fetch_single")
    @patch("backend.tools.search_and_fetch._do_search_internal")
    def test_content_truncation(self, mock_search, mock_fetch):
        """Content longer than 8000 chars should be truncated."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_search.return_value = [
            {"title": "Long Page", "url": "https://long.com", "description": "Long"},
        ]
        mock_fetch.return_value = "x" * 10000

        result = search_and_fetch_impl("long content")

        # The content section should not contain the full 10000 chars
        assert "x" * 8000 in result
        assert "x" * 8001 not in result

    @patch("backend.tools.search_and_fetch._fetch_single")
    @patch("backend.tools.search_and_fetch._do_search_internal")
    def test_search_failure(self, mock_search, mock_fetch):
        """Search itself raises an exception."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_search.side_effect = Exception("Network error")

        result = search_and_fetch_impl("failing search")

        assert "错误" in result or "error" in result.lower() or "失败" in result

    @patch("backend.tools.search_and_fetch._do_search_internal")
    def test_mapped_searxng_failure_uses_search_failure_wrapper(self, mock_search):
        """Mapped search failures should surface as composite search failures."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_search.side_effect = RuntimeError("Search failed: SearXNG request timed out.")

        result = search_and_fetch_impl("timeout query")

        assert '=== 搜索结果: "timeout query" ===' in result
        assert "搜索失败: Search failed: SearXNG request timed out." in result
        assert "共找到 0 个结果" not in result
        assert "=== 搜索结束 ===" in result

    @patch("backend.tools.search_and_fetch._fetch_single")
    @patch("backend.tools.search_and_fetch._do_search_internal")
    @patch("backend.tools.search_and_fetch._get_fetch_content_fallback", return_value=None)
    def test_timeout_fetch(
        self,
        mock_get_fetch_content_fallback,
        mock_search,
        mock_fetch,
    ):
        """Fetch timeout is reported distinctly from generic failure."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_search.return_value = [
            {"title": "Slow Site", "url": "https://slow.com", "description": "Slow"},
        ]
        mock_fetch.side_effect = TimeoutError("Request timed out")

        result = search_and_fetch_impl("timeout query")

        assert "超时 1 个" in result
        assert "--- [1] Slow Site ---" in result

    @patch("backend.tools.search_and_fetch._fetch_single")
    @patch("backend.tools.search_and_fetch._do_search_internal")
    def test_max_results_respected(self, mock_search, mock_fetch):
        """max_results limits the number of fetched results."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_search.return_value = [
            {
                "title": f"Site {i}",
                "url": f"https://site{i}.com",
                "description": f"Desc {i}",
            }
            for i in range(10)
        ]
        mock_fetch.return_value = "content"

        result = search_and_fetch_impl("many results", max_results=3)

        assert "共找到 3 个结果" in result
        assert "--- [1] Site 0 ---" in result
        assert "--- [3] Site 2 ---" in result
        assert "--- [4]" not in result

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
            for i in range(12)
        ]
        mock_fetch.return_value = "content"

        result = search_and_fetch_impl("default results")

        assert mock_search.call_args.kwargs["max_results"] == 25
        assert mock_fetch.call_count == 12
        assert "共找到 12 个结果" in result
        assert "--- [12] Site 11 ---" in result

    @patch("backend.tools.search_and_fetch.wait")
    @patch("backend.tools.search_and_fetch.ThreadPoolExecutor")
    @patch("backend.tools.search_and_fetch._do_search_internal")
    @patch("backend.tools.search_and_fetch._get_fetch_max_workers", return_value=3, create=True)
    def test_fetch_parallelism_honors_configured_worker_cap(
        self,
        mock_get_fetch_max_workers,
        mock_search,
        mock_executor_cls,
        mock_wait,
    ):
        """search_and_fetch should cap workers by config while still submitting all results."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_search.return_value = [
            {
                "title": f"Site {i}",
                "url": f"https://site{i}.com",
                "description": f"Desc {i}",
            }
            for i in range(5)
        ]

        executor = MagicMock()
        executor.__enter__.return_value = executor
        executor.__exit__.return_value = False
        submitted = []

        def submit_side_effect(fn, url, timeout, check_abort):
            future = MagicMock()
            future.result.return_value = f"Content for {url}"
            submitted.append(future)
            return future

        executor.submit.side_effect = submit_side_effect
        mock_executor_cls.return_value = executor
        mock_wait.side_effect = lambda futures, timeout: (set(futures), set())

        result = search_and_fetch_impl("parallel config", max_results=5)

        mock_executor_cls.assert_called_once_with(max_workers=3)
        mock_get_fetch_max_workers.assert_called_once_with()
        assert executor.submit.call_count == 5
        assert "共找到 5 个结果，成功抓取 5 个" in result

    @patch("backend.tools.search_and_fetch._get_search_timeout")
    @patch("backend.tools.search_and_fetch._do_search_internal")
    def test_overall_search_timeout_returns_partial_results_after_fetch_workers_exit(
        self,
        mock_search,
        mock_get_search_timeout,
    ):
        """search_and_fetch should not return while timed-out fetch workers still run."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_get_search_timeout.return_value = 1
        mock_search.return_value = [
            {"title": "Fast Site", "url": "https://fast.com", "description": ""},
            {"title": "Slow Site", "url": "https://slow.com", "description": ""},
        ]

        def fetch_side_effect(url, timeout, check_abort):
            if "slow.com" in url:
                time.sleep(1.2)
                return "slow content"
            return "fast content"

        started_at = time.perf_counter()
        with patch(
            "backend.tools.search_and_fetch._fetch_single",
            side_effect=fetch_side_effect,
        ):
            result = search_and_fetch_impl("deadline partial", max_results=2)
        elapsed = time.perf_counter() - started_at

        # A deliberately non-cooperative test double still must be joined.
        # Production native calls have a supervised cancellation grace.
        assert elapsed >= 1.1
        assert elapsed < 1.6
        assert "成功抓取 1 个" in result
        assert "超时 1 个" in result
        assert "fast content" in result
        assert "Slow Site" in result

    @patch("backend.tools.search_and_fetch.time.monotonic")
    @patch("backend.tools.search_and_fetch._fetch_single")
    @patch("backend.tools.search_and_fetch._do_search_internal")
    @patch("backend.tools.search_and_fetch._get_search_timeout")
    def test_overall_search_timeout_caps_late_fetches(
        self,
        mock_get_search_timeout,
        mock_search,
        mock_fetch,
        mock_monotonic,
    ):
        """search_timeout should cap the whole composite operation while all fetches start together."""
        from backend.tools.search_and_fetch import search_and_fetch_impl

        mock_get_search_timeout.return_value = 10
        mock_search.return_value = [
            {"title": "Site 1", "url": "https://site1.com", "description": ""},
            {"title": "Site 2", "url": "https://site2.com", "description": ""},
        ]
        mock_fetch.return_value = "content"
        mock_monotonic.return_value = 0.0
        def finish_search(*args, **kwargs):
            mock_monotonic.return_value = 9.5
            return mock_search.return_value
        mock_search.side_effect = finish_search

        result = search_and_fetch_impl("deadline query", max_results=2)

        assert mock_fetch.call_count == 2
        assert mock_fetch.call_args_list[0].args[1] == 0.5
        assert mock_fetch.call_args_list[1].args[1] == 0.5
        assert "共找到 2 个结果，成功抓取 2 个" in result


# ---------------------------------------------------------------------------
# Wikipedia Search-and-Fetch composite tool tests
# ---------------------------------------------------------------------------

MOCK_WIKI_SEARCH_OUTPUT = """\
## Wikipedia Search Results for 'Python'

### 1. Python (programming language)
   - Page ID: 23862
   - **Python** is a high-level programming language

### 2. Monty Python
   - Page ID: 18942
   - **Monty** **Python** was a British comedy group

### 3. Ball python
   - Page ID: 54321
   - The ball **python** is a species of snake
"""

MOCK_WIKI_SEARCH_EMPTY = "No Wikipedia articles found for 'xyznonexistent'"

MOCK_WIKI_SEARCH_ERROR = "Error: Connection failed - timeout"


class TestWikipediaParseSearchResults:
    """Tests for the internal _parse_search_results helper in wikipedia_search_and_fetch."""

    def test_parse_normal_results(self):
        from backend.tools.wikipedia_search_and_fetch import _parse_search_results

        results = _parse_search_results(MOCK_WIKI_SEARCH_OUTPUT)
        assert len(results) == 3

        assert results[0]["title"] == "Python (programming language)"
        assert results[0]["page_id"] == "23862"

        assert results[1]["title"] == "Monty Python"
        assert results[1]["page_id"] == "18942"

        assert results[2]["title"] == "Ball python"
        assert results[2]["page_id"] == "54321"

    def test_parse_empty_results(self):
        from backend.tools.wikipedia_search_and_fetch import _parse_search_results

        results = _parse_search_results(MOCK_WIKI_SEARCH_EMPTY)
        assert results == []

    def test_parse_error_string(self):
        from backend.tools.wikipedia_search_and_fetch import _parse_search_results

        results = _parse_search_results(MOCK_WIKI_SEARCH_ERROR)
        assert results == []


class TestWikipediaSearchAndFetch:
    """Tests for wikipedia_search_and_fetch_impl composite function."""

    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_summary_by_page_id_impl")
    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_search_impl")
    @patch("backend.tools.wikipedia_search_and_fetch._get_search_timeout")
    def test_basic_search_and_fetch(
        self, mock_get_search_timeout, mock_search, mock_summary_by_page_id
    ):
        """Successful search + fetch for all articles."""
        from backend.tools.wikipedia_search_and_fetch import (
            wikipedia_search_and_fetch_impl,
        )

        mock_get_search_timeout.return_value = 15
        mock_search.return_value = MOCK_WIKI_SEARCH_OUTPUT
        mock_summary_by_page_id.side_effect = [
            "## Python (programming language)\n\nPython is a high-level language.",
            "## Monty Python\n\nMonty Python were a comedy group.",
            "## Ball python\n\nThe ball python is a snake.",
        ]

        result = wikipedia_search_and_fetch_impl("Python", max_results=5)

        # Verify structure
        assert '=== Wikipedia 搜索结果: "Python" ===' in result
        assert "共找到 3 个条目" in result
        assert "成功获取 3 个摘要" in result
        assert "--- [1] Python (programming language) ---" in result
        assert "--- [2] Monty Python ---" in result
        assert "--- [3] Ball python ---" in result
        assert "状态: 成功" in result
        assert "=== 搜索结束 ===" in result

        # Verify URL generation
        assert "https://en.wikipedia.org/wiki/Python_(programming_language)" in result
        assert "https://en.wikipedia.org/wiki/Monty_Python" in result

        # Verify summaries are included
        assert "Python is a high-level language." in result
        assert "Monty Python were a comedy group." in result

        mock_search.assert_called_once_with("Python", 5, 15)
        assert mock_summary_by_page_id.call_count == 3

    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_summary_by_page_id_impl")
    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_search_impl")
    def test_empty_search_results(self, mock_search, mock_summary_by_page_id):
        """Search returns no results."""
        from backend.tools.wikipedia_search_and_fetch import (
            wikipedia_search_and_fetch_impl,
        )

        mock_search.return_value = MOCK_WIKI_SEARCH_EMPTY

        result = wikipedia_search_and_fetch_impl("xyznonexistent")

        assert "共找到 0 个条目" in result
        assert "=== 搜索结束 ===" in result
        mock_summary_by_page_id.assert_not_called()

    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_summary_by_page_id_impl")
    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_search_impl")
    def test_error_in_summary(self, mock_search, mock_summary_by_page_id):
        """Some summaries return error strings."""
        from backend.tools.wikipedia_search_and_fetch import (
            wikipedia_search_and_fetch_impl,
        )

        mock_search.return_value = MOCK_WIKI_SEARCH_OUTPUT
        mock_summary_by_page_id.side_effect = [
            "## Python (programming language)\n\nPython is a high-level language.",
            "Error: Connection failed - timeout",
            "No Wikipedia article found for 'Ball python'",
        ]

        result = wikipedia_search_and_fetch_impl("Python")

        assert "成功获取 1 个摘要" in result
        assert "失败 2 个" in result

        # First article should be successful
        assert "状态: 成功" in result
        # Second and third should show failure
        assert "状态: 失败" in result

    def test_empty_query(self):
        """Empty query returns error message without calling search."""
        from backend.tools.wikipedia_search_and_fetch import (
            wikipedia_search_and_fetch_impl,
        )

        result = wikipedia_search_and_fetch_impl("")
        assert "错误" in result or "Error" in result

        result2 = wikipedia_search_and_fetch_impl("   ")
        assert "错误" in result2 or "Error" in result2

    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_summary_by_page_id_impl")
    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_search_impl")
    def test_summary_truncation(self, mock_search, mock_summary_by_page_id):
        """Summaries longer than 6000 chars are truncated."""
        from backend.tools.wikipedia_search_and_fetch import (
            wikipedia_search_and_fetch_impl,
        )

        single_result = """\
## Wikipedia Search Results for 'Test'

### 1. Test Article
   - Page ID: 99999
   - A test article snippet
"""
        mock_search.return_value = single_result
        long_summary = "A" * 8000
        mock_summary_by_page_id.return_value = f"## Test Article\n\n{long_summary}"

        result = wikipedia_search_and_fetch_impl("Test")

        assert "状态: 成功" in result
        # 6000 chars + truncation marker
        assert "...[截断]" in result

    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_summary_by_page_id_impl")
    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_search_impl")
    def test_search_failure(self, mock_search, mock_summary_by_page_id):
        """Search itself returns an error string."""
        from backend.tools.wikipedia_search_and_fetch import (
            wikipedia_search_and_fetch_impl,
        )

        mock_search.return_value = MOCK_WIKI_SEARCH_ERROR

        result = wikipedia_search_and_fetch_impl("Python")

        assert "共找到 0 个条目" in result or "Error" in result or "错误" in result
        mock_summary_by_page_id.assert_not_called()

    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_summary_by_page_id_impl")
    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_search_impl")
    def test_check_abort(self, mock_search, mock_summary_by_page_id):
        """check_abort callback causes early return."""
        from backend.tools.wikipedia_search_and_fetch import (
            wikipedia_search_and_fetch_impl,
        )

        abort_fn = MagicMock(return_value=True)
        result = wikipedia_search_and_fetch_impl("Python", check_abort=abort_fn)

        assert "中止" in result
        mock_search.assert_not_called()

    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_summary_by_page_id_impl")
    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_search_impl")
    def test_reuses_page_id_for_summary_fetch(
        self, mock_search, mock_summary_by_page_id
    ):
        """Composite tool should reuse page IDs instead of re-searching each title."""
        from backend.tools.wikipedia_search_and_fetch import (
            wikipedia_search_and_fetch_impl,
        )

        mock_search.return_value = MOCK_WIKI_SEARCH_OUTPUT
        mock_summary_by_page_id.side_effect = [
            "## Python (programming language)\n\nPython is a high-level language.",
            "## Monty Python\n\nMonty Python were a comedy group.",
            "## Ball python\n\nThe ball python is a snake.",
        ]

        result = wikipedia_search_and_fetch_impl("Python")

        assert "成功获取 3 个摘要" in result
        assert mock_summary_by_page_id.call_count == 3

    @patch("backend.tools.wikipedia_search_and_fetch.as_completed")
    @patch("backend.tools.wikipedia_search_and_fetch.ThreadPoolExecutor")
    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_summary_by_page_id_impl")
    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_search_impl")
    def test_uses_full_parallelism_for_default_result_limit(
        self,
        mock_search,
        mock_summary_by_page_id,
        mock_executor_cls,
        mock_as_completed,
    ):
        """Default 20-result fetches should not be split into 8-worker batches."""
        from backend.tools.wikipedia_search_and_fetch import (
            wikipedia_search_and_fetch_impl,
        )

        articles = []
        for idx in range(20):
            articles.append(
                f"### {idx + 1}. Article {idx}\n"
                f"   - Page ID: {1000 + idx}\n"
                f"   - Snippet {idx}\n"
            )
        mock_search.return_value = (
            "## Wikipedia Search Results for 'Python'\n\n" + "\n".join(articles)
        )

        executor = MagicMock()
        executor.__enter__.return_value = executor
        executor.__exit__.return_value = False

        def submit_side_effect(fn, title, page_id, timeout, check_abort):
            future = MagicMock()
            future.result.return_value = (
                title,
                "成功",
                f"## {title}\n\nSummary for {page_id}",
            )
            return future

        executor.submit.side_effect = submit_side_effect
        mock_executor_cls.return_value = executor
        mock_as_completed.side_effect = lambda future_to_idx: list(future_to_idx.keys())

        wikipedia_search_and_fetch_impl("Python", max_results=20)

        mock_executor_cls.assert_called_once_with(max_workers=20)

    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_summary_by_page_id_impl")
    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_search_impl")
    @patch("backend.tools.wikipedia_search_and_fetch._get_search_timeout")
    def test_uses_dedicated_wikipedia_timeout(
        self,
        mock_get_search_timeout,
        mock_search,
        mock_summary_by_page_id,
    ):
        """Wikipedia composite tool should use its own config timeout."""
        from backend.tools.wikipedia_search_and_fetch import (
            wikipedia_search_and_fetch_impl,
        )

        mock_get_search_timeout.return_value = 17
        mock_search.return_value = MOCK_WIKI_SEARCH_OUTPUT
        mock_summary_by_page_id.side_effect = [
            "## Python (programming language)\n\nPython is a high-level language.",
            "## Monty Python\n\nMonty Python were a comedy group.",
            "## Ball python\n\nThe ball python is a snake.",
        ]

        wikipedia_search_and_fetch_impl("Python", max_results=5)

        mock_search.assert_called_once_with("Python", 5, 17)

    @patch("backend.tools.wikipedia_search_and_fetch.as_completed")
    @patch("backend.tools.wikipedia_search_and_fetch.ThreadPoolExecutor")
    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_summary_by_page_id_impl")
    @patch("backend.tools.wikipedia_search_and_fetch.wikipedia_search_impl")
    @patch("backend.tools.wikipedia_search_and_fetch._get_search_timeout")
    def test_max_results_is_clamped_to_wikipedia_api_limit(
        self,
        mock_get_search_timeout,
        mock_search,
        mock_summary_by_page_id,
        mock_executor_cls,
        mock_as_completed,
    ):
        """Wikipedia composite tool should clamp max_results to the API limit."""
        from backend.tools.wikipedia_search_and_fetch import (
            wikipedia_search_and_fetch_impl,
        )

        mock_get_search_timeout.return_value = 15
        articles = []
        for idx in range(50):
            articles.append(
                f"### {idx + 1}. Article {idx}\n"
                f"   - Page ID: {1000 + idx}\n"
                f"   - Snippet {idx}\n"
            )
        mock_search.return_value = (
            "## Wikipedia Search Results for 'Python'\n\n" + "\n".join(articles)
        )

        executor = MagicMock()
        executor.__enter__.return_value = executor
        executor.__exit__.return_value = False

        def submit_side_effect(fn, title, page_id, timeout, check_abort):
            future = MagicMock()
            future.result.return_value = (
                title,
                "成功",
                f"## {title}\n\nSummary for {page_id}",
            )
            return future

        executor.submit.side_effect = submit_side_effect
        mock_executor_cls.return_value = executor
        mock_as_completed.side_effect = lambda future_to_idx: list(future_to_idx.keys())

        wikipedia_search_and_fetch_impl("Python", max_results=100)

        mock_search.assert_called_once_with("Python", 50, 15)
        mock_executor_cls.assert_called_once_with(max_workers=50)


class _DummyWikipediaResponse:
    def __init__(self, payload: str):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload.encode("utf-8")


class TestWikipediaHttpBehavior:
    @patch(
        "backend.tools.wikipedia_tools._get_fetch_proxy",
        return_value="http://127.0.0.1:10808",
    )
    @patch("backend.tools.wikipedia_tools.urllib.request.build_opener")
    def test_make_request_prefers_configured_fetch_proxy(
        self, mock_build_opener, mock_get_fetch_proxy
    ):
        from backend.tools.wikipedia_tools import _make_request

        opener = MagicMock()
        opener.open.return_value = _DummyWikipediaResponse('{"query": {}}')
        mock_build_opener.return_value = opener

        result = _make_request(
            "https://en.wikipedia.org/w/api.php?action=query", timeout=10
        )

        assert result == '{"query": {}}'
        assert mock_build_opener.call_count == 1
        assert len(mock_build_opener.call_args.args) == 1
        assert isinstance(
            mock_build_opener.call_args.args[0], urllib.request.ProxyHandler
        )

    @patch(
        "backend.tools.wikipedia_tools._get_fetch_proxy",
        return_value="http://127.0.0.1:10808",
    )
    @patch("backend.tools.wikipedia_tools.urllib.request.build_opener")
    def test_make_request_does_not_fallback_to_direct_when_fetch_proxy_configured(
        self, mock_build_opener, mock_get_fetch_proxy
    ):
        from backend.tools.wikipedia_tools import _make_request

        proxy_opener = MagicMock()
        proxy_opener.open.side_effect = OSError("proxy down")
        direct_opener = MagicMock()
        direct_opener.open.return_value = _DummyWikipediaResponse('{"query": {}}')
        mock_build_opener.side_effect = [proxy_opener, direct_opener]

        with pytest.raises(ConnectionError):
            _make_request("https://en.wikipedia.org/w/api.php?action=query", timeout=10)

        assert mock_build_opener.call_count == 1


class TestFetchProxySource:
    @patch.dict(
        "os.environ",
        {
            "HTTP_PROXY": "http://env-proxy:1080",
            "HTTPS_PROXY": "http://env-proxy:1080",
        },
        clear=False,
    )
    @patch("backend.config.ConfigLoader")
    def test_wikipedia_fetch_proxy_does_not_fallback_to_environment(self, mock_loader_cls):
        from backend.tools.wikipedia_tools import _get_fetch_proxy

        loader = MagicMock()
        loader.load.return_value.web_tools.fetch_proxy = None
        mock_loader_cls.return_value = loader

        assert _get_fetch_proxy() is None
