"""
Search-and-fetch composite tool: search the web, then concurrently fetch all result pages.
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Optional

from backend.config import get_config
from backend.tools.web_tools import (
    AbortError,
    _build_searxng_opener,
    _fetch_searxng_page,
    _fetch_webpage_content,
    _get_config,
    _normalize_searxng_result_url,
    web_search,
)

# Maximum characters to keep per fetched page
_MAX_CONTENT_LENGTH = 8000

_fetch_budget_condition = threading.Condition()
_fetch_budget_active = 0


def _get_search_timeout() -> int:
    """从配置读取搜索工具超时（秒）。"""
    try:
        config = get_config()
        return config.web_tools.search_timeout
    except Exception:
        return 15


def _get_fetch_max_workers() -> int:
    """Read fetch worker cap from config."""
    try:
        config = get_config()
        return max(1, int(config.web_tools.fetch_max_workers))
    except Exception:
        return 3


def _acquire_global_fetch_slot(check_abort=None, deadline=None) -> None:
    """Acquire one process-wide fetch slot shared across sessions."""
    global _fetch_budget_active
    with _fetch_budget_condition:
        while True:
            if check_abort and check_abort(): raise AbortError("Fetch aborted by user")
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for global fetch slot")
            if _fetch_budget_active < _get_fetch_max_workers():
                _fetch_budget_active += 1
                return
            _fetch_budget_condition.wait(timeout=0.05)


def _release_global_fetch_slot() -> None:
    global _fetch_budget_active
    with _fetch_budget_condition:
        _fetch_budget_active = max(0, _fetch_budget_active - 1)
        _fetch_budget_condition.notify_all()


def _fetch_single_with_global_budget(url: str, timeout: int, check_abort=None) -> str:
    deadline = time.monotonic() + float(timeout)
    _acquire_global_fetch_slot(check_abort, deadline)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError("Fetch deadline exceeded")
        return _fetch_single(url, remaining, check_abort)
    finally:
        _release_global_fetch_slot()


def _get_fetch_content_fallback() -> Optional[str]:
    """Read degraded content fallback mode from config."""
    try:
        value = getattr(get_config().web_tools, "fetch_content_fallback", None)
    except Exception:
        return None

    if not isinstance(value, str):
        return None

    value = value.strip()
    return value or None


def _build_searxng_snippet_content(item: dict, error: str) -> Optional[str]:
    """Build compact degraded content from a SearXNG result snippet."""
    title = str(item.get("title") or "").strip()
    description = str(item.get("description") or "").strip()

    if not title and not description:
        return None

    lines = ["source=searxng_snippet"]
    if title:
        lines.append(f"标题: {title}")
    if description:
        lines.append(f"摘要: {description}")
    if error:
        lines.append(f"原始抓取错误: {error}")
    return "\n".join(lines)


def _build_fetch_error_outcome(
    item: dict,
    error: str,
    content_fallback: Optional[str],
    default_status: str,
) -> dict:
    snippet_content = None
    if content_fallback == "searxng_snippet":
        snippet_content = _build_searxng_snippet_content(item, error)

    if snippet_content:
        return {
            "title": item["title"],
            "url": item["url"],
            "content": snippet_content,
            "status": "摘要兜底",
            "error": error,
        }

    return {
        "title": item["title"],
        "url": item["url"],
        "content": None,
        "status": default_status,
        "error": error,
    }


def _parse_search_results(raw_text: str) -> list[dict]:
    """Parse markdown search results into structured dicts.

    Expected input format (produced by web_search):
        ## Search Results

        - [Title](https://url)
          Description text

    Returns:
        List of {"title": str, "url": str, "description": str}
    """
    results: list[dict] = []
    # Match markdown link lines: "- [title](url)"
    pattern = re.compile(r"^- \[(.+?)\]\((.+?)\)\s*$", re.MULTILINE)
    lines = raw_text.split("\n")

    i = 0
    while i < len(lines):
        m = pattern.match(lines[i])
        if m:
            title = m.group(1)
            url = m.group(2)
            description = ""
            # Next line might be an indented description
            if i + 1 < len(lines) and lines[i + 1].startswith("  "):
                description = lines[i + 1].strip()
                i += 1
            results.append({"title": title, "url": url, "description": description})
        i += 1

    return results


def _do_search_internal(
    query: str,
    max_results: int = 80,
    timeout: int = 15,
    check_abort: Optional[callable] = None,
    on_result: Optional[callable] = None,
) -> list[dict]:
    """Call web_search and parse the markdown output into structured results.

    Returns:
        List of {"title": str, "url": str, "description": str}
    """
    if on_result is not None:
        search_deadline = time.monotonic() + float(timeout)
        cfg = _get_config()
        max_pages = max(1, int(cfg.get("searxng_max_pages") or 1))
        opener = _build_searxng_opener(
            verify_tls=bool(cfg.get("searxng_verify_tls", True)),
        )
        results: list[dict] = []
        seen_urls: set[str] = set()

        for pageno in range(1, max_pages + 1):
            if check_abort and check_abort():
                break

            try:
                page_timeout = search_deadline - time.monotonic()
                if page_timeout <= 0: break
                page_results = _fetch_searxng_page(query, page_timeout, cfg, opener, pageno)
            except Exception:
                if not results:
                    raise
                break

            if not page_results:
                break

            new_results_count = 0
            for item in page_results:
                normalized_url = _normalize_searxng_result_url(item.get("url", ""))
                if not normalized_url or normalized_url in seen_urls:
                    continue

                seen_urls.add(normalized_url)
                results.append(item)
                new_results_count += 1
                on_result(item)

                if len(results) >= max_results:
                    return results[:max_results]

            if new_results_count == 0:
                break

        return results[:max_results]

    raw = web_search(
        query, max_results=max_results, timeout=timeout, check_abort=check_abort
    )
    if isinstance(raw, str) and raw.startswith("Search failed:"):
        raise RuntimeError(raw)
    return _parse_search_results(raw)


def _fetch_single(
    url: str,
    timeout: int = 15,
    check_abort: Optional[callable] = None,
) -> str:
    """Fetch a single URL using web_fetch.

    Returns:
        Fetched text content.

    Raises:
        TimeoutError: on timeout
        Exception: on other failures
    """
    try:
        return _fetch_webpage_content(url, timeout=timeout, check_abort=check_abort)
    except AbortError as exc:
        raise TimeoutError(f"Request timed out for {url}") from exc
    except TimeoutError as exc:
        raise TimeoutError(f"Request timed out for {url}") from exc


def _make_deadline_abort(
    base_check_abort: Optional[callable],
    deadline: float,
) -> callable:
    """Return an abort checker that also stops once the composite deadline expires."""

    def _deadline_abort() -> bool:
        if base_check_abort and base_check_abort():
            return True
        return time.monotonic() >= deadline

    return _deadline_abort


def search_and_fetch_impl(
    query: str,
    max_results: int = 25,
    check_abort: Optional[callable] = None,
) -> str:
    """Search the web and concurrently fetch all result pages.

    Args:
        query: Search query string.
        max_results: Maximum number of search results to process (default 25).
        check_abort: Optional callback; if it returns True, abort.

    Returns:
        Structured text combining search results and fetched page contents.
    """
    # timeout 由配置控制，LLM 无法指定
    timeout = _get_search_timeout()
    started_at = time.monotonic()
    deadline = started_at + timeout

    # --- Empty query guard ---
    if not query or not query.strip():
        return "错误: 搜索查询不能为空"

    # --- Step 1 & 2: Stream search results into the fetch pool ---
    search_results: list[dict] = []
    fetch_outcomes: list[dict] = []
    indexed_results: dict[int, dict] = {}
    future_to_idx: dict = {}
    content_fallback = _get_fetch_content_fallback()
    fetch_stop = threading.Event()
    deadline_abort = _make_deadline_abort(
        lambda: fetch_stop.is_set() or bool(check_abort and check_abort()), deadline)
    remaining = max(0.0, deadline - time.monotonic())

    if remaining <= 0:
        total = 0
    else:
        executor = ThreadPoolExecutor(
            max_workers=min(max_results, _get_fetch_max_workers())
        )
        try:
            def _submit_result(item: dict) -> None:
                fetch_timeout = max(0.0, deadline - time.monotonic())
                idx = len(search_results)
                search_results.append(item)
                future = executor.submit(
                    _fetch_single_with_global_budget,
                    item["url"],
                    fetch_timeout,
                    deadline_abort,
                )
                future_to_idx[future] = idx

            try:
                returned_results = _do_search_internal(
                    query,
                    max_results=max_results,
                    timeout=timeout,
                    check_abort=deadline_abort,
                    on_result=_submit_result,
                )
            except Exception as exc:
                return f'=== 搜索结果: "{query}" ===\n搜索失败: {exc}\n=== 搜索结束 ==='

            if not future_to_idx:
                # Legacy/mock path: submit the returned batch together with one shared
                # timeout budget so all fetches can start immediately.
                submit_timeout = max(0.0, deadline - time.monotonic())
                for item in returned_results[:max_results]:
                    idx = len(search_results)
                    search_results.append(item)
                    future = executor.submit(
                        _fetch_single_with_global_budget,
                        item["url"],
                        submit_timeout,
                        deadline_abort,
                    )
                    future_to_idx[future] = idx

            if future_to_idx:
                done, not_done = wait(
                    set(future_to_idx),
                    timeout=max(0.0, deadline - time.monotonic()),
                )
            else:
                done, not_done = set(), set()

            for future in done:
                idx = future_to_idx[future]
                item = search_results[idx]
                try:
                    content = future.result()
                    indexed_results[idx] = {
                        "title": item["title"],
                        "url": item["url"],
                        "content": content,
                        "status": "成功",
                        "error": None,
                    }
                except TimeoutError as e:
                    indexed_results[idx] = _build_fetch_error_outcome(
                        item,
                        str(e),
                        content_fallback,
                        "超时",
                    )
                except Exception as e:
                    error = str(e)
                    indexed_results[idx] = _build_fetch_error_outcome(
                        item,
                        error,
                        content_fallback,
                        "失败",
                    )

            for future in not_done:
                idx = future_to_idx[future]
                item = search_results[idx]
                future.cancel()
                indexed_results[idx] = {
                    "title": item["title"],
                    "url": item["url"],
                    "content": None,
                    "status": "超时",
                    "error": f"Request timed out for {item['url']}",
                }
        finally:
            fetch_stop.set()
            # Requests observe deadline/Stop even while queued. Native requests
            # return after worker cleanup ACK or bounded process-group recovery.
            executor.shutdown(wait=True, cancel_futures=True)

        total = len(search_results)

        for idx in range(total):
            fetch_outcomes.append(indexed_results[idx])

    # --- Step 4: Build output ---
    success_count = sum(1 for o in fetch_outcomes if o["status"] == "成功")
    timeout_count = sum(1 for o in fetch_outcomes if o["status"] == "超时")
    fail_count = sum(1 for o in fetch_outcomes if o["status"] == "失败")
    snippet_fallback_count = sum(1 for o in fetch_outcomes if o["status"] == "摘要兜底")

    lines: list[str] = []
    lines.append(f'=== 搜索结果: "{query}" ===')
    lines.append(
        f"共找到 {total} 个结果，成功抓取 {success_count} 个，"
        f"超时 {timeout_count} 个，失败 {fail_count} 个，"
        f"摘要兜底 {snippet_fallback_count} 个"
    )
    lines.append("")

    for i, outcome in enumerate(fetch_outcomes, start=1):
        lines.append(f"--- [{i}] {outcome['title']} ---")
        lines.append(f"URL: {outcome['url']}")

        if outcome["status"] == "成功":
            lines.append("状态: 成功")
            content = outcome["content"] or ""
            if len(content) > _MAX_CONTENT_LENGTH:
                content = content[:_MAX_CONTENT_LENGTH] + "\n...(内容已截断)"
            lines.append("内容:")
            lines.append(content)
        elif outcome["status"] == "超时":
            lines.append(f"状态: 超时 ({outcome['error']})")
            lines.append(f"内容: [TIMEOUT - {outcome['error']}]")
        elif outcome["status"] == "摘要兜底":
            lines.append(f"状态: 摘要兜底 ({outcome['error']})")
            content = outcome["content"] or ""
            if len(content) > _MAX_CONTENT_LENGTH:
                content = content[:_MAX_CONTENT_LENGTH] + "\n...(内容已截断)"
            lines.append("内容:")
            lines.append(content)
        else:
            lines.append(f"状态: 失败 ({outcome['error']})")
            lines.append(f"内容: [ERROR - {outcome['error']}]")

        lines.append("")

    lines.append("=== 搜索结束 ===")
    return "\n".join(lines)
