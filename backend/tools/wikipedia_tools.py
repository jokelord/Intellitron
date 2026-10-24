"""
Wikipedia search tool with robust anti-crawling handling.
Provides a reliable Wikipedia search capability for the Agent system.
"""

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Optional

from smolagents import tool

logger = logging.getLogger(__name__)


def _get_fetch_proxy() -> Optional[str]:
    """Get the explicitly configured proxy for direct Wikipedia fetching."""
    try:
        from backend.config import ConfigLoader

        config = ConfigLoader().load()
        if config.web_tools and config.web_tools.fetch_proxy:
            return config.web_tools.fetch_proxy
    except Exception:
        pass
    return None


def _make_request(url: str, timeout: int = 15) -> str:
    """Make a direct Wikipedia request with optional configured fetch proxy."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }

    proxy = _get_fetch_proxy()
    start_time = time.monotonic()

    def _remaining_timeout() -> float:
        elapsed = time.monotonic() - start_time
        remaining = timeout - elapsed
        if remaining <= 0:
            raise TimeoutError("Wikipedia request timed out")
        return remaining

    def _try_request(proxy_url: Optional[str] = None):
        req = urllib.request.Request(url, headers=headers)
        request_timeout = _remaining_timeout()
        if proxy_url:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
        else:
            # Bypass environment proxies when explicitly trying a direct request.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=request_timeout)

    if proxy:
        try:
            logger.debug(f"Trying with proxy: {proxy}")
            response = _try_request(proxy)
            return response.read().decode("utf-8")
        except Exception as proxy_error:
            logger.debug(f"Proxy connection failed: {proxy_error}")
            if isinstance(proxy_error, TimeoutError):
                raise
            raise ConnectionError(
                f"Failed to connect to Wikipedia API via configured proxy: {proxy_error}"
            )

    try:
        response = _try_request()
        return response.read().decode("utf-8")
    except TimeoutError:
        raise
    except Exception as direct_error:
        logger.debug(f"Direct connection failed: {direct_error}")
        raise ConnectionError(f"Failed to connect to Wikipedia API: {direct_error}")


def wikipedia_summary_by_page_id_impl(
    page_id: str | int,
    title: str,
    timeout: int = 15,
) -> str:
    """Fetch a Wikipedia summary directly from a known page ID."""
    if page_id in (None, "", 0, "0"):
        return f"Error: Invalid Wikipedia page ID for '{title}'"

    try:
        summary_url = (
            f"https://en.wikipedia.org/w/api.php?"
            f"action=query&"
            f"prop=extracts&"
            f"exintro&"
            f"explaintext&"
            f"pageids={page_id}&"
            f"format=json"
        )

        response_text = _make_request(summary_url, timeout=timeout)
        summary_data = json.loads(response_text)

        pages = summary_data.get("query", {}).get("pages", {})
        page_data = pages.get(str(page_id), {})

        if not page_data:
            return f"Error: Could not retrieve content for '{title}'"

        resolved_title = page_data.get("title", title) or title
        extract = page_data.get("extract", "No content available.")

        if not extract or not extract.strip():
            return f"No summary available for '{resolved_title}'"

        return f"## {resolved_title}\n\n{extract.strip()}"

    except json.JSONDecodeError as e:
        return f"Error: Failed to parse Wikipedia API response: {e}"
    except ConnectionError as e:
        return f"Error: Connection failed - {e}"
    except Exception as e:
        return f"Error fetching Wikipedia summary for '{title}': {str(e)}"


def wikipedia_search_impl(query: str, max_results: int = 5, timeout: int = 15) -> str:
    """
    Search Wikipedia and return formatted results.
    注意使用该工具后一般要再用 wikipedia_summary 工具针对搜索结果中的具体文章进行内容获取，不要使用 web_fetch 抓取 Wikipedia 页面内容，因为 Wikipedia 的反爬虫机制会导致 web_fetch 失败

    Args:
        query: The search query
        max_results: Maximum number of results to return
        timeout: Request timeout in seconds (default: 15)

    Returns:
        Formatted Wikipedia search results as a string
    """
    if not query or not query.strip():
        return "Error: Search query cannot be empty."

    query = query.strip()

    try:
        search_url = (
            f"https://en.wikipedia.org/w/api.php?"
            f"action=query&"
            f"list=search&"
            f"srsearch={urllib.parse.quote(query)}&"
            f"format=json&"
            f"srlimit={max_results}"
        )

        response_text = _make_request(search_url, timeout=timeout)
        search_data = json.loads(response_text)

        if "query" not in search_data:
            return f"Error: Invalid response from Wikipedia API"

        search_results = search_data["query"].get("search", [])

        if not search_results:
            return f"No Wikipedia articles found for '{query}'"

        output = f"## Wikipedia Search Results for '{query}'\n\n"

        for idx, result in enumerate(search_results[:max_results], 1):
            title = result.get("title", "Unknown")
            page_id = result.get("pageid", 0)
            snippet = result.get("snippet", "")
            snippet = snippet.replace('<span class="searchmatch">', "**")
            snippet = snippet.replace("</span>", "**")

            output += f"### {idx}. {title}\n"
            output += f"   - Page ID: {page_id}\n"
            output += f"   - {snippet}\n\n"

        return output

    except json.JSONDecodeError as e:
        return f"Error: Failed to parse Wikipedia API response: {e}"
    except ConnectionError as e:
        return f"Error: Connection failed - {e}"
    except Exception as e:
        return f"Error searching Wikipedia for '{query}': {str(e)}"


def wikipedia_summary_impl(query: str, timeout: int = 15) -> str:
    """
    Get a detailed summary from Wikipedia for a specific topic.

    Args:
        query: The topic to search for
        timeout: Request timeout in seconds (default: 15)

    Returns:
        Wikipedia summary as a string
    """
    if not query or not query.strip():
        return "Error: Search query cannot be empty."

    query = query.strip()

    try:
        search_url = (
            f"https://en.wikipedia.org/w/api.php?"
            f"action=query&"
            f"list=search&"
            f"srsearch={urllib.parse.quote(query)}&"
            f"format=json&"
            f"srlimit=1"
        )

        response_text = _make_request(search_url, timeout=timeout)
        search_data = json.loads(response_text)

        if "query" not in search_data:
            return f"Error: Invalid response from Wikipedia API"

        search_results = search_data["query"].get("search", [])

        if not search_results:
            return f"No Wikipedia article found for '{query}'"

        first_result = search_results[0]
        title = first_result.get("title", query)
        page_id = first_result.get("pageid", 0)
        return wikipedia_summary_by_page_id_impl(page_id, title=title, timeout=timeout)

    except json.JSONDecodeError as e:
        return f"Error: Failed to parse Wikipedia API response: {e}"
    except ConnectionError as e:
        return f"Error: Connection failed - {e}"
    except Exception as e:
        return f"Error fetching Wikipedia summary for '{query}': {str(e)}"


@tool
def wikipedia_search(
    query: str, max_results: int = 5, timeout: int = 15, check_abort: callable = None
) -> str:
    """
    Search Wikipedia for articles matching the given query.

    This tool searches Wikipedia's database and returns a list of matching
    articles with their titles, page IDs, and snippets. Use this when you
    want to find relevant Wikipedia articles on a topic.
    注意使用该工具后一般要再用 wikipedia_summary 工具针对搜索结果中的具体文章进行内容获取，不要使用 web_fetch 抓取 Wikipedia 页面内容，因为 Wikipedia 的反爬虫机制会导致 web_fetch 失败


    Args:
        query: The search query string (e.g., "artificial intelligence", "Python programming")
        max_results: Maximum number of results to return (default: 5, max: 20)
        timeout: Request timeout in seconds (default: 15). Increase for slow networks.
        check_abort: Optional callback to check if operation should be aborted.

    Returns:
        Formatted search results with article titles, URLs, and descriptions
    """
    if check_abort and check_abort():
        return "Wikipedia 搜索已被用户中止"

    timeout = timeout or 15
    max_results = max_results if max_results is not None else 5
    max_results = min(max_results, 20)

    result = wikipedia_search_impl(query, max_results, timeout)

    return result


@tool
def wikipedia_summary(
    query: str, timeout: int = 15, check_abort: callable = None
) -> str:
    """
    Get a detailed summary from Wikipedia for a specific topic.

    This tool retrieves a comprehensive summary/extract from the most relevant
    Wikipedia article for the given query. Use this when you want detailed
    information about a specific subject.

    Args:
        query: The topic to search for (e.g., "Machine Learning", "NVIDIA Corporation")
        timeout: Request timeout in seconds (default: 15). Increase for slow networks.
        check_abort: Optional callback to check if operation should be aborted.

    Returns:
        A detailed summary from Wikipedia as a formatted string
    """
    if check_abort and check_abort():
        return "Wikipedia 摘要已被用户中止"

    timeout = timeout or 15
    result = wikipedia_summary_impl(query, timeout)

    return result
