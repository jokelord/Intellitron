"""Wikipedia search-and-fetch composite tool.

Chains wikipedia_search_impl -> parse results -> concurrent
wikipedia_summary_by_page_id_impl fetches, then returns structured output
combining search results with article summaries.
"""

import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from backend.config import get_config
from backend.tools.wikipedia_tools import (
    wikipedia_search_impl,
    wikipedia_summary_by_page_id_impl,
)

# Maximum characters per summary before truncation
_MAX_SUMMARY_CHARS = 6000
_WIKIPEDIA_API_MAX_RESULTS = 50


def _get_search_timeout() -> int:
    """从配置读取 Wikipedia 复合搜索工具超时（秒）。"""
    try:
        config = get_config()
        return config.web_tools.wikipedia_search_timeout
    except Exception:
        return 10


def _parse_search_results(raw_text: str) -> list[dict]:
    """Parse wikipedia_search_impl output into structured results.

    Extracts article titles and page IDs from the markdown-formatted
    search output of ``wikipedia_search_impl``.

    Args:
        raw_text: Raw text output from wikipedia_search_impl.

    Returns:
        List of dicts with keys: ``title``, ``page_id``, ``url``.
        Returns empty list if the input is an error message or contains
        no parseable results.
    """
    if (
        not raw_text
        or raw_text.startswith("Error:")
        or raw_text.startswith("No Wikipedia")
    ):
        return []

    results: list[dict] = []

    # Pattern matches lines like: ### 1. Python (programming language)
    title_pattern = re.compile(r"^###\s+\d+\.\s+(.+)$", re.MULTILINE)
    # Pattern matches lines like:    - Page ID: 23862
    page_id_pattern = re.compile(r"^\s+-\s+Page ID:\s+(\d+)", re.MULTILINE)

    titles = title_pattern.findall(raw_text)
    page_ids = page_id_pattern.findall(raw_text)

    for i, title in enumerate(titles):
        title = title.strip()
        page_id = page_ids[i] if i < len(page_ids) else ""
        # Build Wikipedia URL from title
        url_title = urllib.parse.quote(title.replace(" ", "_"), safe="()")
        url = f"https://en.wikipedia.org/wiki/{url_title}"
        results.append(
            {
                "title": title,
                "page_id": page_id,
                "url": url,
            }
        )

    return results


def _fetch_summary(
    title: str,
    page_id: str,
    timeout: int,
    check_abort: Optional[Callable] = None,
) -> tuple[str, str, Optional[str]]:
    """Fetch a single Wikipedia summary.

    Args:
        title: Article title to look up.
        page_id: Wikipedia page ID resolved by the initial search.
        timeout: Request timeout in seconds.
        check_abort: Optional abort callback.

    Returns:
        Tuple of (title, status, summary_or_error).
        status is one of: "成功", "失败", "超时", "中止"
    """
    if check_abort and check_abort():
        return title, "中止", None

    try:
        summary = wikipedia_summary_by_page_id_impl(
            page_id, title=title, timeout=timeout
        )
    except TimeoutError:
        return title, "超时", None
    except Exception as exc:
        return title, "失败", str(exc)

    # wikipedia_summary_impl returns error strings rather than raising
    if summary.startswith("Error:") or summary.startswith("No Wikipedia"):
        return title, "失败", summary

    return title, "成功", summary


def wikipedia_search_and_fetch_impl(
    query: str,
    max_results: int = 20,
    check_abort: Optional[Callable] = None,
) -> str:
    """Search Wikipedia and fetch summaries for all results concurrently.

    This is the composite implementation that:
    1. Calls ``wikipedia_search_impl()`` to search.
    2. Parses the output to extract article titles.
    3. Uses ``ThreadPoolExecutor`` to concurrently fetch summaries.
    4. Returns structured text combining search results + summaries.

    Args:
        query: The search query.
        max_results: Maximum number of results (default 20).
        check_abort: Optional callback; if it returns True, abort early.

    Returns:
        Structured text with search results and article summaries.
    """
    # timeout 由配置控制，LLM 无法指定
    timeout = _get_search_timeout()

    if not query or not query.strip():
        return "错误: 搜索查询不能为空"

    max_results = max(1, min(max_results, _WIKIPEDIA_API_MAX_RESULTS))

    query = query.strip()

    if check_abort and check_abort():
        return "Wikipedia 搜索已被用户中止"

    # Step 1: Search
    raw_search = wikipedia_search_impl(query, max_results, timeout)

    # Step 2: Parse results
    articles = _parse_search_results(raw_search)

    # Step 3: Concurrent summary fetches
    # Maintain order by mapping futures to index
    summary_results: list[tuple[str, str, Optional[str]]] = [None] * len(articles)

    if articles:
        with ThreadPoolExecutor(max_workers=len(articles)) as pool:
            future_to_idx = {}
            for idx, article in enumerate(articles):
                future = pool.submit(
                    _fetch_summary,
                    article["title"],
                    article["page_id"],
                    timeout,
                    check_abort,
                )
                future_to_idx[future] = idx

            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    summary_results[idx] = future.result()
                except TimeoutError:
                    summary_results[idx] = (articles[idx]["title"], "超时", None)
                except Exception as exc:
                    summary_results[idx] = (articles[idx]["title"], "失败", str(exc))

    # Step 4: Build output
    n_total = len(articles)
    n_success = sum(1 for r in summary_results if r and r[1] == "成功")
    n_timeout = sum(1 for r in summary_results if r and r[1] == "超时")
    n_fail = n_total - n_success - n_timeout

    lines: list[str] = []
    lines.append(f'=== Wikipedia 搜索结果: "{query}" ===')
    lines.append(
        f"共找到 {n_total} 个条目，"
        f"成功获取 {n_success} 个摘要，"
        f"超时 {n_timeout} 个，"
        f"失败 {n_fail} 个"
    )
    lines.append("")

    for idx, article in enumerate(articles):
        title = article["title"]
        url = article["url"]
        _, status, content = summary_results[idx]

        lines.append(f"--- [{idx + 1}] {title} ---")
        lines.append(f"URL: {url}")
        if status == "成功":
            lines.append("状态: 成功")
            # Truncate long summaries
            if content and len(content) > _MAX_SUMMARY_CHARS:
                content = content[:_MAX_SUMMARY_CHARS] + "...[截断]"
            lines.append("摘要:")
            lines.append(content or "")
        elif status == "超时":
            lines.append("状态: 超时")
            lines.append("摘要: [超时 - 未获取到内容]")
        else:
            error_detail = content or "unknown error"
            lines.append(f"状态: 失败 ({error_detail})")
            lines.append(f"摘要: [ERROR - {error_detail}]")
        lines.append("")

    lines.append("=== 搜索结束 ===")

    return "\n".join(lines)
