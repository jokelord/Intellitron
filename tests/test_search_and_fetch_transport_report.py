"""End-to-end style transport/source report for concrete URLs.

Run with:
    ./.sglang/bin/python -m pytest tests/test_search_and_fetch_transport_report.py -s -q

This diagnostic test fixes search results to three real URLs, then lets the
real fetch pipeline run: curl_cffi -> crawl4ai -> searxng_snippet. The printed
tables show which path ultimately produced each result.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
import time
from unittest.mock import patch


REPORT_URLS = [
    {
        "title": "dji-4g-vohive-mac GitHub repository",
        "url": "https://github.com/wlzh/dji-4g-vohive-mac",
        "description": "GitHub repository page for wlzh/dji-4g-vohive-mac.",
    },
    {
        "title": "Zhihu article p/48876718",
        "url": "https://zhuanlan.zhihu.com/p/48876718",
        "description": "Zhihu column article that may require anti-bot handling.",
    },
    {
        "title": "Generalist AI about page",
        "url": "https://www.figure.ai/news/introducing-figure-03",
        "description": "figure ai",
    },
]


@dataclass
class TransportTrace:
    attempts: list[str]
    successful_responses: list[str]
    attempt_details: list[dict]


def _extract_result_blocks(output: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in output.splitlines():
        if line.startswith("--- ["):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _extract_field(block: str, prefix: str) -> str:
    for line in block.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def _extract_content_chars(block: str) -> int:
    marker = "内容:\n"
    if marker not in block:
        return 0
    return len(block.split(marker, 1)[1].strip())


def _detect_final_source(block: str, trace: TransportTrace) -> str:
    status = _extract_field(block, "状态:")
    if "source=searxng_snippet" in block:
        return "searxng_snippet"
    if status.startswith("成功") and trace.successful_responses:
        return trace.successful_responses[-1]
    if status.startswith("超时"):
        return "timeout"
    if status.startswith("失败"):
        return "failed"
    return "unknown"


@patch("backend.tools.search_and_fetch._get_search_timeout", return_value=45)
@patch(
    "backend.tools.search_and_fetch._get_fetch_content_fallback",
    return_value="searxng_snippet",
)
@patch("backend.tools.search_and_fetch._do_search_internal")
def test_search_and_fetch_prints_real_url_transport_report(
    mock_search,
    mock_content_fallback,
    mock_search_timeout,
):
    """Print a concrete URL-by-URL report for curl_cffi/crawl4ai/snippet."""
    from backend.tools import web_tools
    from backend.tools.search_and_fetch import search_and_fetch_impl

    traces: dict[str, TransportTrace] = defaultdict(
        lambda: TransportTrace(attempts=[], successful_responses=[], attempt_details=[])
    )
    original_curl_cffi = web_tools._fetch_with_curl_cffi
    original_crawl4ai = web_tools._fetch_with_crawl4ai

    def traced_curl_cffi(*args, **kwargs):
        url = kwargs.get("url") or args[0]
        started_at = time.monotonic()
        traces[url].attempts.append("curl_cffi")
        timeout = kwargs.get("timeout", "")
        try:
            response = original_curl_cffi(*args, **kwargs)
            traces[url].successful_responses.append("curl_cffi")
            traces[url].attempt_details.append(
                {
                    "transport": "curl_cffi",
                    "timeout": timeout,
                    "elapsed": time.monotonic() - started_at,
                    "result": "ok",
                    "error": "",
                }
            )
            return response
        except Exception as exc:
            traces[url].attempt_details.append(
                {
                    "transport": "curl_cffi",
                    "timeout": timeout,
                    "elapsed": time.monotonic() - started_at,
                    "result": "error",
                    "error": str(exc).splitlines()[0][:120],
                }
            )
            raise

    def traced_crawl4ai(*args, **kwargs):
        url = kwargs.get("url") or args[0]
        started_at = time.monotonic()
        traces[url].attempts.append("crawl4ai")
        timeout = kwargs.get("timeout", "")
        try:
            response = original_crawl4ai(*args, **kwargs)
            traces[url].successful_responses.append("crawl4ai")
            traces[url].attempt_details.append(
                {
                    "transport": "crawl4ai",
                    "timeout": timeout,
                    "elapsed": time.monotonic() - started_at,
                    "result": "ok",
                    "error": "",
                }
            )
            return response
        except Exception as exc:
            traces[url].attempt_details.append(
                {
                    "transport": "crawl4ai",
                    "timeout": timeout,
                    "elapsed": time.monotonic() - started_at,
                    "result": "error",
                    "error": str(exc).splitlines()[0][:120],
                }
            )
            raise

    mock_search.return_value = REPORT_URLS
    with patch("backend.tools.web_tools._fetch_with_curl_cffi", traced_curl_cffi), patch(
        "backend.tools.web_tools._fetch_with_crawl4ai", traced_crawl4ai
    ):
        output = search_and_fetch_impl("transport report concrete urls", max_results=3)

    rows = []
    for block in _extract_result_blocks(output):
        url = _extract_field(block, "URL:")
        status = _extract_field(block, "状态:")
        trace = traces[url]
        rows.append(
            {
                "url": url,
                "status": status,
                "attempts": " -> ".join(trace.attempts) or "none",
                "attempt_details": trace.attempt_details,
                "final_source": _detect_final_source(block, trace),
                "content_chars": _extract_content_chars(block),
            }
        )

    counts = Counter(row["final_source"] for row in rows)

    print("\n\n=== concrete URL fetch source summary ===")
    print("| final source | count |")
    print("|---|---:|")
    for source in ("curl_cffi", "crawl4ai", "searxng_snippet", "timeout", "failed", "unknown"):
        if counts[source]:
            print(f"| {source} | {counts[source]} |")

    print("\n=== concrete URL-by-URL fetch source report ===")
    print("| URL | attempted transports | final status | final source | content chars |")
    print("|---|---|---|---|---:|")
    for row in rows:
        print(
            "| {url} | {attempts} | {status} | {final_source} | {content_chars} |".format(
                **row
            )
        )

    print("\n=== concrete URL fetch attempt details ===")
    print("| URL | transport | timeout_s | elapsed_s | result | error |")
    print("|---|---|---:|---:|---|---|")
    for row in rows:
        for detail in row["attempt_details"]:
            print(
                "| {url} | {transport} | {timeout} | {elapsed:.2f} | {result} | {error} |".format(
                    url=row["url"],
                    **detail,
                )
            )
    print("")

    assert [row["url"] for row in rows] == [item["url"] for item in REPORT_URLS]
    assert all(row["final_source"] != "unknown" for row in rows)
