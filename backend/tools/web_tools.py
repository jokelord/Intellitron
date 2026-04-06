"""
Web search and fetch tools with multi-engine support.
"""

import os
import re
import subprocess
import urllib.parse
from typing import List, Optional

from smolagents import tool

from backend.config import ConfigLoader


class AbortError(Exception):
    """Raised when operation is aborted by user."""

    pass


ENGINE_DEFAULTS = {
    "duckduckgo": {
        "endpoint": "https://html.duckduckgo.com/html/",
        "protocol": "curl",
        "method": "POST",
        "param_name": "q",
    },
    "bing": {
        "endpoint": "https://www.bing.com/search",
        "protocol": "curl",
        "method": "GET",
        "param_name": "q",
    },
    "sogou": {
        "endpoint": "https://www.sogou.com/web",
        "protocol": "curl",
        "method": "GET",
        "param_name": "query",
    },
    "360": {
        "endpoint": "https://www.so.com/s",
        "protocol": "curl",
        "method": "GET",
        "param_name": "q",
    },
}


def _get_config() -> dict:
    """Get web_tools config from config file."""
    try:
        config = ConfigLoader().load()
        return {
            "search_engine": config.web_tools.search_engine,
            "protocol": config.web_tools.protocol,
            "method": config.web_tools.method,
            "fallback_engine": config.web_tools.fallback_engine,
            "fallback_protocol": config.web_tools.fallback_protocol,
            "fallback_method": config.web_tools.fallback_method,
            "proxy": config.web_tools.proxy,
        }
    except Exception:
        pass
    return {
        "search_engine": "duckduckgo",
        "protocol": "curl",
        "method": "POST",
        "fallback_engine": "bing",
        "fallback_protocol": "curl",
        "fallback_method": "GET",
        "proxy": None,
    }


def _get_config_proxy() -> Optional[str]:
    """Get proxy URL from config, fallback to environment variables."""
    cfg = _get_config()
    if cfg.get("proxy"):
        return cfg["proxy"]
    return (
        os.environ.get("HTTP_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("https_proxy")
    )


def _html_unescape(text: str) -> str:
    """Unescape HTML entities."""
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#\d+;", "", text)
    return text


def parse_duckduckgo_html(html: str) -> List[dict]:
    """Parse DuckDuckGo HTML results."""
    results = []

    link_regex = re.compile(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]*)"[^>]*>([^<]*)</a>',
        re.IGNORECASE,
    )

    snippet_regex = re.compile(
        r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    links = []
    for match in link_regex.finditer(html):
        url = match.group(1)
        title = _html_unescape(match.group(2).strip())

        if "uddg=" in url:
            try:
                parsed_url = urllib.parse.urlparse(url, scheme="https://duckduckgo.com")
                real_url = urllib.parse.parse_qs(parsed_url.query).get("uddg", [url])[0]
                url = urllib.parse.unquote(real_url)
            except Exception:
                pass

        if url and title and url.startswith("http"):
            links.append({"url": url, "title": title})

    snippets = []
    for match in snippet_regex.finditer(html):
        snippet = re.sub(r"<[^>]*>", "", match.group(1)).strip()
        snippet = _html_unescape(snippet)
        snippet = re.sub(r"\s+", " ", snippet)
        snippets.append(snippet)

    for i, link in enumerate(links):
        results.append(
            {
                "title": link["title"],
                "url": link["url"],
                "description": snippets[i] if i < len(snippets) else "",
            }
        )

    return results


def parse_bing_html(html: str) -> List[dict]:
    """Parse Bing HTML results."""
    results = []

    li_regex = re.compile(
        r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>',
        re.IGNORECASE | re.DOTALL,
    )

    for match in li_regex.finditer(html):
        li_html = match.group(1)

        title_match = re.search(
            r'<div[^>]*class="b_algoheader"[^>]*>.*?<a[^>]*href="([^"]*)"[^>]*>.*?<h[23][^>]*>(.*?)</h[23]>',
            li_html,
            re.IGNORECASE | re.DOTALL,
        )
        if not title_match:
            title_match = re.search(
                r'<h2[^>]*><a[^>]*href="([^"]*)"[^>]*>(.*?)</a></h2>',
                li_html,
                re.IGNORECASE | re.DOTALL,
            )
        if not title_match:
            title_match = re.search(
                r'<h[23][^>]*><a[^>]*href="([^"]*)"[^>]*>(.*?)</a></h[23]>',
                li_html,
                re.IGNORECASE | re.DOTALL,
            )
        if not title_match:
            title_match = re.search(
                r'<a[^>]*href="(https?://[^"]*)"[^>]*>(.*?)</a>',
                li_html,
                re.IGNORECASE | re.DOTALL,
            )
        if not title_match:
            continue

        url = title_match.group(1)
        title = _html_unescape(re.sub(r"<[^>]*>", "", title_match.group(2)).strip())

        desc_match = re.search(
            r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>',
            li_html,
            re.IGNORECASE | re.DOTALL,
        )
        if not desc_match:
            desc_match = re.search(
                r'class="b_desc"[^>]*>(.*?)</', li_html, re.IGNORECASE | re.DOTALL
            )
        if not desc_match:
            desc_match = re.search(
                r'class="b_caption[^"]*"[^>]*>(.*?)</p>',
                li_html,
                re.IGNORECASE | re.DOTALL,
            )

        description = ""
        if desc_match:
            description = _html_unescape(
                re.sub(r"<[^>]*>", "", desc_match.group(1)).strip()
            )
            description = re.sub(r"\s+", " ", description)

        if url and title and url.startswith("http"):
            results.append(
                {
                    "title": title,
                    "url": url,
                    "description": description,
                }
            )

    return results


def parse_sogou_html(html: str) -> List[dict]:
    """Parse Sogou HTML results."""
    results = []

    vrwrap_regex = re.compile(
        r'<div[^>]*class="[^"]*vrwrap[^"]*"[^>]*>(.*?)</div>\s*</div>',
        re.IGNORECASE | re.DOTALL,
    )

    for match in vrwrap_regex.finditer(html):
        wrap_html = match.group(1)

        title_match = re.search(
            r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            wrap_html,
            re.IGNORECASE | re.DOTALL,
        )
        if not title_match:
            continue

        url = title_match.group(1)
        title = _html_unescape(re.sub(r"<[^>]*>", "", title_match.group(2)).strip())

        if not url.startswith("http"):
            if url.startswith("/"):
                url = "https://www.sogou.com" + url

        # Handle Sogou's URL encoding
        if "/link?url=" in url:
            try:
                parsed_url = urllib.parse.urlparse(url)
                real_url = urllib.parse.parse_qs(parsed_url.query).get("url", [url])[0]
                url = urllib.parse.unquote(real_url)
            except Exception:
                pass

        description = ""  # New structure often lacks descriptions

        if url and title and url.startswith("http"):
            results.append(
                {
                    "title": title,
                    "url": url,
                    "description": description,
                }
            )

    return results


def parse_360_html(html: str) -> List[dict]:
    """Parse 360搜索 HTML results."""
    results = []

    res_regex = re.compile(
        r'<li[^>]*class="[^"]*res-list[^"]*"[^>]*>(.*?)</li>',
        re.IGNORECASE | re.DOTALL,
    )

    for match in res_regex.finditer(html):
        res_html = match.group(1)

        title_match = re.search(
            r'<h3[^>]*class="[^"]*res-title[^"]*"[^>]*><a[^>]*data-mdurl="([^"]*)"[^>]*>(.*?)</a></h3>',
            res_html,
            re.IGNORECASE | re.DOTALL,
        )
        if not title_match:
            title_match = re.search(
                r'<h3[^>]*><a[^>]*data-mdurl="([^"]*)"[^>]*>(.*?)</a></h3>',
                res_html,
                re.IGNORECASE | re.DOTALL,
            )
        if not title_match:
            title_match = re.search(
                r'<h3[^>]*class="[^"]*res-title[^"]*"[^>]*><a[^>]*href="([^"]*)"[^>]*>(.*?)</a></h3>',
                res_html,
                re.IGNORECASE | re.DOTALL,
            )
            if not title_match:
                title_match = re.search(
                    r'<h3[^>]*><a[^>]*href="([^"]*)"[^>]*>(.*?)</a></h3>',
                    res_html,
                    re.IGNORECASE | re.DOTALL,
                )
        if not title_match:
            continue

        url = title_match.group(1)
        title = _html_unescape(re.sub(r"<[^>]*>", "", title_match.group(2)).strip())

        desc_match = re.search(
            r'<span[^>]*class="[^"]*res-list-summary[^"]*"[^>]*>(.*?)</span>',
            res_html,
            re.IGNORECASE | re.DOTALL,
        )
        if not desc_match:
            desc_match = re.search(
                r'class="res-abstract"[^>]*>(.*?)</',
                res_html,
                re.IGNORECASE | re.DOTALL,
            )

        description = ""
        if desc_match:
            description = _html_unescape(
                re.sub(r"<[^>]*>", "", desc_match.group(1)).strip()
            )
            description = re.sub(r"\s+", " ", description)

        if url and title and url.startswith("http"):
            results.append(
                {
                    "title": title,
                    "url": url,
                    "description": description,
                }
            )

    return results


def execute_curl_search(
    endpoint: str,
    query: str,
    method: str,
    proxy: Optional[str],
    engine: str = "",
    check_abort: callable = None,
    timeout: int = 30,
    param_name: str = "q",
) -> str:
    """Execute search using curl."""
    import time

    if engine == "bing":
        user_agent = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    else:
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

    curl_args = [
        "curl",
        "-s",
        "--max-time",
        str(timeout),
        "-L",
        "-H",
        f"User-Agent: {user_agent}",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    ]

    use_proxy = proxy

    if use_proxy:
        curl_args.extend(["-x", proxy])

        if method == "POST":
            curl_args.extend(
                [
                    "-X",
                    "POST",
                    "-H",
                    "Content-Type: application/x-www-form-urlencoded",
                ]
            )
            curl_args.extend(["-d", f"{param_name}={urllib.parse.quote(query)}"])
            curl_args.append(endpoint)
        else:
            curl_args.append(f"{endpoint}?{param_name}={urllib.parse.quote(query)}")

    try:
        proc = subprocess.Popen(
            curl_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        start_time = time.time()
        proc_timeout = timeout + 5  # 给 curl 多 5 秒的宽限

        while True:
            if check_abort and check_abort():
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise AbortError("Search aborted by user")

            retcode = proc.poll()
            if retcode is not None:
                stdout, stderr = proc.communicate()
                return stdout

            elapsed = time.time() - start_time
            if elapsed > proc_timeout:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise TimeoutError("Search timed out")

            time.sleep(0.5)
    except TimeoutError:
        raise
    except AbortError:
        raise
    except Exception as e:
        raise RuntimeError(f"Search failed: {e}")


def _detect_results(html: str, engine: str) -> bool:
    """Detect if search returned valid results."""
    if engine == "duckduckgo":
        return 'class="result__a"' in html
    elif engine == "bing":
        return 'class="b_algo"' in html or "b_algo" in html
    elif engine == "sogou":
        return 'class="vrwrap"' in html or 'class="vr-title"' in html
    elif engine == "360":
        return 'class="res-list"' in html or 'class="res-abstract"' in html
    return len(html) > 500


def _validate_results(results: List[dict], query: str) -> bool:
    """Validate that results actually match the query."""
    if not results:
        return False
    query_lower = query.lower()
    for r in results[:3]:
        title_lower = r.get("title", "").lower()
        desc_lower = r.get("description", "").lower()
        if query_lower in title_lower or query_lower in desc_lower:
            return True
    first_title = results[0].get("title", "").lower()
    if len(query) >= 2 and len(first_title) >= 2:
        if query[0] == first_title[0]:
            return True
    return False


def _do_search(
    engine: str,
    endpoint: str,
    protocol: str,
    method: str,
    query: str,
    proxy: Optional[str],
    check_abort: callable = None,
    timeout: int = 30,
) -> List[dict]:
    """Execute search with specified engine."""
    if protocol == "curl":
        param_name = ENGINE_DEFAULTS.get(engine, {}).get("param_name", "q")
        html = execute_curl_search(
            endpoint, query, method, proxy, engine, check_abort, timeout, param_name
        )
    else:
        raise ValueError(f"Unsupported protocol: {protocol}")

    if engine == "duckduckgo":
        return parse_duckduckgo_html(html)
    elif engine == "bing":
        return parse_bing_html(html)
    elif engine == "sogou":
        return parse_sogou_html(html)
    elif engine == "360":
        return parse_360_html(html)
    else:
        raise ValueError(f"Unknown engine: {engine}")


@tool
def web_search(
    query: str,
    max_results: int = 10,
    timeout: int = 30,
    use_fallback: bool = True,
    check_abort: callable = None,
) -> str:
    """Search the web using configured search engine.

    Args:
        query: The search query string.
        max_results: Maximum number of results to return (default: 10).
        timeout: Request timeout in seconds (default: 30). Increase for slow networks.
        use_fallback: Whether to use fallback engine on failure (default: True).
        check_abort: Optional callback to check if operation should be aborted.

    Returns:
        Formatted search results as markdown with titles, URLs, and descriptions.
    """
    timeout = timeout or 30
    cfg = _get_config()

    engine = cfg["search_engine"]
    protocol = cfg["protocol"]
    method = cfg["method"]
    proxy = cfg["proxy"] or _get_config_proxy()

    engines_to_try = [
        (engine, ENGINE_DEFAULTS.get(engine, {}).get("endpoint", ""), protocol, method),
    ]

    if use_fallback:
        fallback_engine = cfg["fallback_engine"]
        fallback_protocol = cfg["fallback_protocol"]
        fallback_method = cfg["fallback_method"]
        engines_to_try.append(
            (
                fallback_engine,
                ENGINE_DEFAULTS.get(fallback_engine, {}).get("endpoint", ""),
                fallback_protocol,
                fallback_method,
            ),
        )

    for eng, endpoint, prot, met in engines_to_try:
        if not endpoint:
            continue

        if check_abort and check_abort():
            return "搜索已被用户中止"

        try:
            results = _do_search(
                eng, endpoint, prot, met, query, proxy, check_abort, timeout
            )
            if results:
                results = results[:max_results]
                output = "## Search Results\n\n"
                for r in results:
                    output += f"- [{r['title']}]({r['url']})\n"
                    if r["description"]:
                        output += f"  {r['description']}\n"
                    output += "\n"
                return output
        except AbortError:
            return "搜索已被用户中止"
        except Exception as e:
            continue

    return "No search results found. The search service may be temporarily unavailable."


@tool
def web_fetch(url: str, timeout: int = 30, check_abort: callable = None) -> str:
    """Fetch a webpage and convert it to text.

    Args:
        url: The URL of the webpage to fetch.
        timeout: Request timeout in seconds (default: 30). Increase for slow networks.
        check_abort: Optional callback to check if operation should be aborted.

    Returns:
        The webpage content converted to text/markdown format.
    """
    import time

    timeout = timeout or 30

    curl_args = [
        "curl",
        "-s",
        "--max-time",
        str(timeout),
        "--compressed",
        "-L",
        "-H",
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "-H",
        "Accept-Language: en-US,en;q=0.9",
        "-H",
        "Accept-Encoding: gzip, deflate, br",
        "-H",
        "DNT: 1",
        "-H",
        "Connection: keep-alive",
        "-H",
        "Upgrade-Insecure-Requests: 1",
    ]

    proxy = _get_config_proxy()
    if proxy:
        curl_args.extend(["-x", proxy])

    try:
        proc = subprocess.Popen(
            curl_args + [url],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        start_time = time.time()
        proc_timeout = timeout + 5  # 给 curl 多 5 秒的宽限

        while True:
            if check_abort and check_abort():
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return "网页获取已被用户中止"

            retcode = proc.poll()
            if retcode is not None:
                stdout, stderr = proc.communicate()
                result_stdout = stdout
                break

            elapsed = time.time() - start_time
            if elapsed > proc_timeout:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return "Failed to fetch webpage: request timed out."

            time.sleep(0.5)

        if not result_stdout:
            return "Failed to fetch webpage: empty response."

        html = result_stdout

        output = f"# Content from {url}\n\n"

        text = re.sub(
            r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE
        )
        text = re.sub(
            r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE
        )
        text = re.sub(
            r"<header[^>]*>.*?</header>", "", text, flags=re.DOTALL | re.IGNORECASE
        )
        text = re.sub(
            r"<footer[^>]*>.*?</footer>", "", text, flags=re.DOTALL | re.IGNORECASE
        )
        text = re.sub(r"<nav[^>]*>.*?</nav>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(
            r"<iframe[^>]*>.*?</iframe>", "", text, flags=re.DOTALL | re.IGNORECASE
        )

        text = re.sub(
            r"<h[1-6][^>]*>(.*?)</h[1-6]>",
            r"\n\n## \1\n\n",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(
            r"<p[^>]*>(.*?)</p>", r"\1\n\n", text, flags=re.DOTALL | re.IGNORECASE
        )
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(
            r"<li[^>]*>(.*?)</li>", r"- \1\n", text, flags=re.DOTALL | re.IGNORECASE
        )
        text = re.sub(
            r"<a[^>]*href=\"([^\"]*)\"[^>]*>(.*?)</a>",
            r"[\2](\1)",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )

        text = re.sub(r"<[^>]+>", "", text)
        text = _html_unescape(text)

        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()

        max_length = 8000
        if len(text) > max_length:
            text = text[:max_length] + "\n\n...(content truncated)"

        return output + text
    except Exception as e:
        return f"Failed to fetch webpage: {str(e)}"
