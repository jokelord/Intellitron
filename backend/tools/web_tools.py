"""
Web search and fetch tools.
"""

import json
import hashlib
import os
import re
import signal
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from smolagents import tool

from backend.config import get_config


class AbortError(Exception):
    """Raised when operation is aborted by user."""

    pass


_FETCH_META_MARKER = "__OPENCODE_FETCH_META__"
_MAX_FETCH_TEXT_LENGTH = 8000
_DEFAULT_CURL_MAX_RESPONSE_BYTES = 8_388_608
_DEFAULT_BLOCK_MARKERS = (
    "captcha",
    "just a moment",
    "enable javascript",
    "access denied",
    "verify you are human",
    "cf-browser-verification",
    "cloudflare",
    "bot check",
)


def _normalise_fetch_proxy(value):
    """Return an explicit proxy URL or None; never inherit whitespace values."""
    if value is None:
        return None
    proxy = str(value).strip()
    return proxy or None


def _validate_fetch_url(url: str) -> str:
    """Validate the URL contract before any transport or cookie work starts."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL is required")

    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise ValueError(f"invalid URL: {exc}") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"unsupported URL scheme: {parsed.scheme or '<empty>'}")
    if not parsed.hostname:
        raise ValueError("URL hostname is required")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid URL port: {exc}") from exc
    return url
_BROWSER_PROFILES = {
    "desktop": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    },
    "mobile": {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    },
}

_IMPERSONATION_FALLBACKS = {
    "desktop": ("chrome", "edge", "firefox"),
    "mobile": ("safari_ios", "chrome_android", "firefox"),
}


def _get_config() -> dict:
    """Get web_tools config from config file."""
    config = get_config()
    return {
        "provider": config.web_tools.provider,
        "searxng_base_url": config.web_tools.searxng_base_url,
        "searxng_search_path": config.web_tools.searxng_search_path,
        "searxng_timeout": config.web_tools.searxng_timeout,
        "searxng_format": config.web_tools.searxng_format,
        "searxng_language": config.web_tools.searxng_language,
        "searxng_safe_search": config.web_tools.searxng_safe_search,
        "searxng_max_pages": config.web_tools.searxng_max_pages,
        "searxng_categories": config.web_tools.searxng_categories,
        "searxng_engines": config.web_tools.searxng_engines,
        "searxng_api_token": config.web_tools.searxng_api_token,
        "searxng_verify_tls": config.web_tools.searxng_verify_tls,
    }


def _build_fetch_subprocess_environment() -> dict[str, str]:
    """Build the non-secret environment needed by the curl transport."""
    environment = {
        "PATH": os.environ.get("PATH") or "/usr/bin:/bin",
        "LANG": os.environ.get("LANG") or "C.UTF-8",
        "LC_ALL": os.environ.get("LC_ALL") or "C.UTF-8",
    }
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _get_fetch_config() -> dict:
    """Get robust fetch configuration from config file."""
    try:
        config = get_config()
        web_tools = config.web_tools
        try:
            _crawl4ai_max_concurrency = int(
                getattr(web_tools, "fetch_crawl4ai_max_concurrency", 50) or 0
            )
        except (TypeError, ValueError):
            _crawl4ai_max_concurrency = 50
        if _crawl4ai_max_concurrency <= 0:
            _crawl4ai_max_concurrency = 50
        return {
            "fetch_proxy": web_tools.fetch_proxy,
            "fetch_transport": web_tools.fetch_transport,
            "fetch_fallback_transport": web_tools.fetch_fallback_transport,
            "fetch_content_fallback": web_tools.fetch_content_fallback,
            "fetch_impersonate": web_tools.fetch_impersonate,
            "fetch_max_workers": web_tools.fetch_max_workers,
            "fetch_retry_count": web_tools.fetch_retry_count,
            "fetch_retry_backoff_seconds": web_tools.fetch_retry_backoff_seconds,
            "fetch_curl_timeout_seconds": web_tools.fetch_curl_timeout_seconds,
            "fetch_curl_hedge_delay_seconds": web_tools.fetch_curl_hedge_delay_seconds,
            "fetch_curl_max_response_bytes": web_tools.fetch_curl_max_response_bytes,
            "fetch_curl_cffi_timeout_seconds": web_tools.fetch_curl_cffi_timeout_seconds,
            "fetch_curl_cffi_hedge_delay_seconds": web_tools.fetch_curl_cffi_hedge_delay_seconds,
            "fetch_crawl4ai_timeout_seconds": web_tools.fetch_crawl4ai_timeout_seconds,
            "fetch_crawl4ai_max_concurrency": _crawl4ai_max_concurrency,
            "fetch_worker_processes": getattr(web_tools, "fetch_worker_processes", 4),
            "fetch_worker_cancel_grace_seconds": getattr(web_tools, "fetch_worker_cancel_grace_seconds", 1.0),
            "crawl4ai_memory_threshold_percent": getattr(web_tools, "crawl4ai_memory_threshold_percent", 85.0),
            "crawl4ai_memory_check_interval_seconds": getattr(web_tools, "crawl4ai_memory_check_interval_seconds", 0.1),
            "crawl4ai_batch_wait_seconds": getattr(web_tools, "crawl4ai_batch_wait_seconds", 0.01),
            "fetch_min_text_chars": web_tools.fetch_min_text_chars,
            "fetch_quality_min_text_chars": getattr(web_tools, "fetch_quality_min_text_chars", 1000),
            "fetch_quality_min_text_lines": getattr(web_tools, "fetch_quality_min_text_lines", 6),
            "fetch_enable_readability_fallback": web_tools.fetch_enable_readability_fallback,
            "fetch_block_markers": web_tools.fetch_block_markers,
            "crawl4ai_browser_type": web_tools.crawl4ai_browser_type,
            "crawl4ai_headless": web_tools.crawl4ai_headless,
            "crawl4ai_wait_until": web_tools.crawl4ai_wait_until,
            "crawl4ai_page_timeout_ms": web_tools.crawl4ai_page_timeout_ms,
            "crawl4ai_word_count_threshold": web_tools.crawl4ai_word_count_threshold,
            "crawl4ai_bypass_cache": web_tools.crawl4ai_bypass_cache,
            "crawl4ai_retry_count": web_tools.crawl4ai_retry_count,
            "crawl4ai_enable_stealth": getattr(web_tools, "crawl4ai_enable_stealth", True),
            "crawl4ai_magic": getattr(web_tools, "crawl4ai_magic", False),
        }
    except Exception:
        return {
            "fetch_proxy": None,
            "fetch_transport": "auto",
            "fetch_fallback_transport": "curl",
            "fetch_content_fallback": None,
            "fetch_impersonate": "chrome",
            "fetch_max_workers": 3,
            "fetch_retry_count": 2,
            "fetch_retry_backoff_seconds": 1.0,
            "fetch_curl_timeout_seconds": 6,
            "fetch_curl_hedge_delay_seconds": 3,
            "fetch_curl_max_response_bytes": _DEFAULT_CURL_MAX_RESPONSE_BYTES,
            "fetch_curl_cffi_timeout_seconds": 6,
            "fetch_curl_cffi_hedge_delay_seconds": 3,
            "fetch_crawl4ai_timeout_seconds": 15,
            "fetch_crawl4ai_max_concurrency": 50,
            "fetch_worker_processes": 4,
            "fetch_worker_cancel_grace_seconds": 1.0,
            "crawl4ai_memory_threshold_percent": 85.0,
            "crawl4ai_memory_check_interval_seconds": 0.1,
            "crawl4ai_batch_wait_seconds": 0.01,
            "fetch_min_text_chars": 200,
            "fetch_quality_min_text_chars": 1000,
            "fetch_quality_min_text_lines": 6,
            "fetch_enable_readability_fallback": True,
            "fetch_block_markers": [],
            "crawl4ai_browser_type": "chromium",
            "crawl4ai_headless": True,
            "crawl4ai_wait_until": "domcontentloaded",
            "crawl4ai_page_timeout_ms": 15000,
            "crawl4ai_word_count_threshold": 10,
            "crawl4ai_bypass_cache": True,
            "crawl4ai_retry_count": 0,
            "crawl4ai_enable_stealth": True,
            "crawl4ai_magic": False,
        }


def _build_searxng_search_request(
    query: str,
    cfg: dict,
    pageno: int = 1,
) -> urllib.request.Request:
    """Build a SearXNG JSON search request from config."""
    base_url = str(cfg.get("searxng_base_url") or "http://127.0.0.1:8080").rstrip("/")
    search_path = str(cfg.get("searxng_search_path") or "/search")
    search_url = urllib.parse.urljoin(base_url + "/", search_path.lstrip("/"))

    params: list[tuple[str, str]] = [
        ("q", query),
        ("format", str(cfg.get("searxng_format") or "json")),
        ("language", str(cfg.get("searxng_language") or "all")),
        ("safesearch", str(cfg.get("searxng_safe_search") or 0)),
        ("pageno", str(max(1, pageno))),
    ]

    categories = [str(item).strip() for item in (cfg.get("searxng_categories") or []) if str(item).strip()]
    if categories:
        params.append(("categories", ",".join(categories)))

    engines = [str(item).strip() for item in (cfg.get("searxng_engines") or []) if str(item).strip()]
    if engines:
        params.append(("engines", ",".join(engines)))

    request = urllib.request.Request(
        f"{search_url}?{urllib.parse.urlencode(params)}",
        headers={
            "Accept": "application/json",
            "User-Agent": _BROWSER_PROFILES["desktop"]["User-Agent"],
        },
    )
    api_token = cfg.get("searxng_api_token")
    if api_token:
        request.add_header("Authorization", f"Bearer {api_token}")
    return request


def _build_searxng_opener(verify_tls: bool):
    """Build a direct urllib opener for the local SearXNG service."""
    # SearXNG owns the upstream-engine proxy inside its container. Explicitly
    # disable urllib environment proxies for the backend-to-SearXNG hop so a
    # host proxy cannot accidentally proxy the local service request.
    proxy_handler = urllib.request.ProxyHandler({})
    ssl_context = None
    if not verify_tls:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    https_handler = urllib.request.HTTPSHandler(context=ssl_context)
    return urllib.request.build_opener(proxy_handler, https_handler)


def _extract_searxng_results(payload: dict) -> list[dict[str, str]]:
    """Extract usable result records from a SearXNG payload."""
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []

    results: list[dict[str, str]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue

        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url or not url.startswith(("http://", "https://")):
            continue

        description = str(item.get("content") or item.get("description") or "").strip()
        results.append({"title": title, "url": url, "description": description})
    return results


def _normalize_searxng_result_url(url: str) -> str:
    normalized = (url or "").strip()
    if not normalized:
        return ""
    parsed = urllib.parse.urlsplit(normalized)
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, "")
    )


def _fetch_searxng_page(
    query: str,
    timeout: int,
    cfg: dict,
    opener,
    pageno: int,
) -> list[dict[str, str]]:
    request = _build_searxng_search_request(query, cfg, pageno=pageno)

    try:
        with opener.open(request, timeout=timeout) as response:
            status_code = getattr(response, "status", None) or response.getcode()
            if status_code != 200:
                raise RuntimeError(f"Search failed: SearXNG returned HTTP {status_code}.")
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Search failed: SearXNG returned HTTP {exc.code}.") from exc
    except socket.timeout as exc:
        raise RuntimeError("Search failed: SearXNG request timed out.") from exc
    except TimeoutError as exc:
        raise RuntimeError("Search failed: SearXNG request timed out.") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, socket.timeout):
            raise RuntimeError("Search failed: SearXNG request timed out.") from exc
        raise RuntimeError("Search failed: SearXNG instance is unreachable.") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Search failed: SearXNG returned invalid JSON.") from exc

    return _extract_searxng_results(payload)


def _search_with_searxng(
    query: str,
    timeout: int,
    cfg: dict,
    max_results: int,
) -> list[dict[str, str]]:
    """Execute a JSON search against the configured SearXNG instance."""
    opener = _build_searxng_opener(
        verify_tls=bool(cfg.get("searxng_verify_tls", True)),
    )

    max_pages = max(1, int(cfg.get("searxng_max_pages") or 1))
    target_results = max(1, int(max_results or 1))
    seen_urls: set[str] = set()
    results: list[dict[str, str]] = []

    for pageno in range(1, max_pages + 1):
        try:
            page_results = _fetch_searxng_page(query, timeout, cfg, opener, pageno)
        except Exception:
            if pageno == 1:
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
            if len(results) >= target_results:
                return results[:target_results]

        if new_results_count == 0:
            break

    if not results:
        raise LookupError("No search results found. SearXNG returned no usable results.")
    return results[:target_results]


def _html_unescape(text: str) -> str:
    """Unescape HTML entities."""
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#\d+;", "", text)
    return text


def _normalize_transport(transport: Optional[str]) -> str:
    """Normalize transport names used by web fetch."""
    return (transport or "").strip().lower()


def _transport_is_available(transport: str) -> bool:
    """Check whether a fetch transport is available in current env."""
    if transport == "curl":
        return True
    if transport in {'curl_cffi', 'crawl4ai'}:
        import importlib.util
        try:
            return importlib.util.find_spec(transport) is not None
        except (ImportError, ValueError):
            return False
    return False


def _resolve_fetch_transport(transport: Optional[str]) -> Optional[str]:
    """Resolve the configured transport, including auto fallback."""
    normalized = _normalize_transport(transport)
    if not normalized or normalized == "none":
        return None
    if normalized == "auto":
        return "curl_cffi" if _transport_is_available("curl_cffi") else "curl"
    return normalized


def _normalize_impersonate(impersonate: Optional[str], profile_name: str) -> str:
    """Normalize curl_cffi impersonation preset for the chosen browser profile."""
    normalized = (impersonate or "").strip().lower()
    if normalized:
        return normalized
    return _IMPERSONATION_FALLBACKS.get(profile_name, ("chrome",))[0]


def _build_profile_attempts(primary_impersonate: Optional[str]) -> list[tuple[str, str]]:
    """Build browser-profile attempts from least to most invasive."""
    attempts: list[tuple[str, str]] = []
    primary = _normalize_impersonate(primary_impersonate, "desktop")
    attempts.append(("desktop", primary))
    attempts.append(("mobile", _normalize_impersonate("safari_ios", "mobile")))

    for candidate in _IMPERSONATION_FALLBACKS["desktop"]:
        if candidate != primary:
            attempts.append(("desktop", candidate))
    return attempts


def _build_cookie_jar_path(url: str) -> str:
    """Build a stable cookie-jar path per host."""
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname or "default"
    default_port = {"http": 80, "https": 443}.get(parsed.scheme.lower())
    port = parsed.port
    host_identity = (
        hostname if port is None or port == default_port else f"{hostname}:{port}"
    )
    safe_host = re.sub(r"[^A-Za-z0-9._-]", "_", host_identity)
    digest = hashlib.sha1(host_identity.encode("utf-8")).hexdigest()[:12]
    cookie_dir = os.path.join("/tmp/opencode", "web_fetch_cookies")
    os.makedirs(cookie_dir, mode=0o700, exist_ok=True)
    os.chmod(cookie_dir, 0o700)
    return os.path.join(cookie_dir, f"{safe_host}-{digest}.cookies")


_cookie_jar_locks_guard = threading.Lock()
_cookie_jar_locks: dict[str, threading.Lock] = {}


def _get_cookie_jar_lock(path: str) -> threading.Lock:
    with _cookie_jar_locks_guard:
        return _cookie_jar_locks.setdefault(path, threading.Lock())


def _publish_curl_cookie_candidate(candidate: str, stable: str) -> None:
    """Atomically replace a host's stable cookie jar with a validated winner."""
    stable_dir = os.path.dirname(stable) or "."
    stable_dir_existed = os.path.isdir(stable_dir)
    os.makedirs(stable_dir, mode=0o700, exist_ok=True)
    if not stable_dir_existed:
        os.chmod(stable_dir, 0o700)
    staged = f"{stable}.{os.getpid()}.{threading.get_ident()}.tmp"
    with _get_cookie_jar_lock(stable):
        try:
            shutil.copy2(candidate, staged)
            os.chmod(staged, 0o600)
            os.replace(staged, stable)
            os.chmod(stable, 0o600)
        finally:
            try:
                os.remove(staged)
            except FileNotFoundError:
                pass


@dataclass
class _CurlAttemptWorkspace:
    root: str
    header_path: str
    body_path: str
    stderr_path: str
    cookie_path: str

    @classmethod
    def create(
        cls,
        stable_cookie_path: Optional[str],
        attempt_name: str,
    ) -> "_CurlAttemptWorkspace":
        root = tempfile.mkdtemp(prefix=f"curl-{attempt_name}-", dir="/tmp/opencode")
        try:
            os.chmod(root, 0o700)
            workspace = cls(
                root=root,
                header_path=os.path.join(root, "headers.txt"),
                body_path=os.path.join(root, "body.bin"),
                stderr_path=os.path.join(root, "stderr.txt"),
                cookie_path=os.path.join(root, "cookies.txt"),
            )
            for path in (
                workspace.header_path,
                workspace.body_path,
                workspace.stderr_path,
            ):
                Path(path).touch(mode=0o600)
                os.chmod(path, 0o600)
            if stable_cookie_path and os.path.exists(stable_cookie_path):
                shutil.copy2(stable_cookie_path, workspace.cookie_path)
            else:
                Path(workspace.cookie_path).touch(mode=0o600)
            os.chmod(workspace.cookie_path, 0o600)
            return workspace
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _cleanup_curl_workspaces(
    workspaces: list[_CurlAttemptWorkspace],
    keep_index: Optional[int] = None,
) -> None:
    """Attempt each coordinator-owned cleanup without masking its outcome."""
    for index, workspace in enumerate(workspaces):
        if index == keep_index:
            continue
        try:
            workspace.cleanup()
        except Exception:
            pass


def _create_curl_hedge_workspaces(
    stable_cookie_path: Optional[str],
) -> list[_CurlAttemptWorkspace]:
    """Create A/B from one stable-cookie generation and roll back partial state."""
    workspaces: list[_CurlAttemptWorkspace] = []

    def create_both() -> None:
        workspaces.append(_CurlAttemptWorkspace.create(stable_cookie_path, "a"))
        workspaces.append(_CurlAttemptWorkspace.create(stable_cookie_path, "b"))

    try:
        if stable_cookie_path:
            with _get_cookie_jar_lock(stable_cookie_path):
                create_both()
        else:
            create_both()
        return workspaces
    except BaseException:
        _cleanup_curl_workspaces(workspaces)
        raise


def _parse_latest_curl_header_state(path: str) -> tuple[str, Optional[int]]:
    """Return the state of the latest complete terminal curl header block."""
    try:
        raw = Path(path).read_text(encoding="iso-8859-1")
    except (FileNotFoundError, OSError):
        return "pending", None

    complete_blocks = re.split(r"\r?\n\r?\n", raw)[:-1]
    origin_blocks = []
    for block in complete_blocks:
        match = re.search(
            r"^HTTP/\S+\s+(\d{3})\s*([^\r\n]*)",
            block,
            re.MULTILINE,
        )
        if not match:
            continue

        status = int(match.group(1))
        reason = match.group(2).strip().lower()
        if status == 200 and reason == "connection established":
            continue
        if 100 <= status < 200:
            continue
        origin_blocks.append((status, block))

    if not origin_blocks:
        return "pending", None

    status, block = origin_blocks[-1]
    if 300 <= status < 400 and re.search(
        r"^Location:",
        block,
        re.MULTILINE | re.IGNORECASE,
    ):
        return "pending", status
    if 200 <= status < 300:
        return "success", status
    return "failure", status


def _build_curl_fetch_args(
    url: str,
    timeout: int,
    headers: dict[str, str],
    proxy: Optional[str] = None,
    cookie_jar_path: Optional[str] = None,
    header_path: Optional[str] = None,
    body_path: Optional[str] = None,
    cookie_read_path: Optional[str] = None,
    cookie_write_path: Optional[str] = None,
    max_response_bytes: int = _DEFAULT_CURL_MAX_RESPONSE_BYTES,
) -> list[str]:
    """Build curl args for webpage fetching with browser-like headers."""
    curl_args = [
        "curl",
        "-sS",
        "--max-time",
        str(timeout),
        "--compressed",
        "--suppress-connect-headers",
        "--max-filesize",
        str(max(1, int(max_response_bytes or _DEFAULT_CURL_MAX_RESPONSE_BYTES))),
        "-L",
        "--proto",
        "=http,https",
        "--proto-redir",
        "=http,https",
    ]

    if cookie_jar_path and not (cookie_read_path or cookie_write_path):
        cookie_dir = os.path.dirname(cookie_jar_path) or "."
        cookie_dir_existed = os.path.isdir(cookie_dir)
        os.makedirs(cookie_dir, mode=0o700, exist_ok=True)
        if not cookie_dir_existed:
            os.chmod(cookie_dir, 0o700)
        fd = os.open(cookie_jar_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        os.chmod(cookie_jar_path, 0o600)
        curl_args.extend(["-b", cookie_jar_path, "-c", cookie_jar_path])

    if header_path:
        curl_args.extend(["--dump-header", header_path])
    if body_path:
        curl_args.extend(["--output", body_path])
    if cookie_read_path:
        curl_args.extend(["-b", cookie_read_path])
    if cookie_write_path:
        curl_args.extend(["-c", cookie_write_path])

    if proxy:
        curl_args.extend(["-x", proxy])

    for key, value in headers.items():
        curl_args.extend(["-H", f"{key}: {value}"])

    curl_args.extend(
        [
            "-w",
            (
                f"\n{_FETCH_META_MARKER}"
                '{"status_code":%{http_code},"final_url":"%{url_effective}","content_type":"%{content_type}"}'
                "\n"
            ),
            "--",
            url,
        ]
    )
    return curl_args


def _run_subprocess_with_abort(
    args: list[str],
    timeout: int,
    check_abort: Optional[Callable] = None,
    abort_message: str = "Operation aborted by user",
    stderr_path: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> tuple[str, str, int]:
    """Run a subprocess while honoring timeout and abort callback."""
    stderr_file = open(stderr_path, "w+", encoding="utf-8") if stderr_path else None
    proc = None
    primary_error = None
    try:
        proc = subprocess.Popen(
            args,
        stdout=subprocess.PIPE,
            stderr=stderr_file or subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            **({"start_new_session": True} if os.name == "posix" else {}),
        )
        started_at = time.monotonic()
        while True:
            if check_abort and check_abort():
                raise AbortError(abort_message)

            remaining = timeout - (time.monotonic() - started_at)
            if remaining <= 0:
                raise TimeoutError("request timed out")

            try:
                stdout, stderr = proc.communicate(timeout=min(0.1, remaining))
                if stderr_file:
                    stderr_file.flush()
                    stderr_file.seek(0)
                    stderr = stderr_file.read()
                return stdout or "", stderr or "", proc.returncode
            except subprocess.TimeoutExpired:
                continue
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error = None
        if proc is not None and getattr(proc, "returncode", None) is None:
            try:
                _terminate_and_reap_process(proc)
            except BaseException as exc:
                cleanup_error = exc
        if stderr_file:
            try:
                stderr_file.close()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error


def _terminate_and_reap_process(proc: subprocess.Popen) -> None:
    """Terminate a process and wait for it, escalating to kill if needed."""
    cleanup_error = None
    process_group_id = None
    if os.name == "posix":
        pid = getattr(proc, "pid", None)
        if isinstance(pid, int) and pid > 0:
            try:
                process_group_id = os.getpgid(pid)
            except (OSError, ProcessLookupError):
                process_group_id = None

    def signal_process(sig: int) -> None:
        nonlocal cleanup_error
        if process_group_id is not None:
            try:
                os.killpg(process_group_id, sig)
                return
            except ProcessLookupError:
                return
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        try:
            if sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except BaseException as exc:
            cleanup_error = cleanup_error or exc

    try:
        process_done = proc.poll() is not None
    except BaseException as exc:
        cleanup_error = exc
        process_done = False

    if process_done:
        try:
            proc.wait()
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
    else:
        signal_process(signal.SIGTERM)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            signal_process(signal.SIGKILL)
            try:
                proc.wait()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
            try:
                proc.kill()
            except BaseException as kill_exc:
                cleanup_error = cleanup_error or kill_exc
            try:
                proc.wait()
            except BaseException as wait_exc:
                cleanup_error = cleanup_error or wait_exc

    if cleanup_error is not None:
        raise cleanup_error


def _parse_curl_fetch_output(
    stdout: str,
    requested_url: str,
    body: Optional[str] = None,
) -> dict:
    """Parse curl body + write-out metadata payload."""
    meta = {
        "status_code": 0,
        "final_url": requested_url,
        "content_type": "",
        "body": body if body is not None else stdout,
    }
    if _FETCH_META_MARKER in stdout:
        _, raw_meta = stdout.rsplit(_FETCH_META_MARKER, 1)
        try:
            parsed_meta = json.loads(raw_meta.strip())
        except Exception:
            parsed_meta = {}
        meta.update(parsed_meta)
    if body is None and _FETCH_META_MARKER in stdout:
        captured_body, _ = stdout.rsplit(_FETCH_META_MARKER, 1)
        meta["body"] = captured_body.rstrip()
    if not meta.get("final_url"):
        meta["final_url"] = requested_url
    return meta


def _fetch_with_curl(
    url: str,
    timeout: int,
    proxy: Optional[str],
    headers: dict[str, str],
    cookie_jar_path: Optional[str],
    check_abort: Optional[Callable] = None,
    header_path: Optional[str] = None,
    body_path: Optional[str] = None,
    stderr_path: Optional[str] = None,
    cookie_read_path: Optional[str] = None,
    cookie_write_path: Optional[str] = None,
    max_response_bytes: int = _DEFAULT_CURL_MAX_RESPONSE_BYTES,
) -> dict:
    """Fetch a webpage via curl subprocess."""
    workspace = None
    if not header_path or not body_path or not stderr_path:
        workspace = _CurlAttemptWorkspace.create(cookie_jar_path, "single")
        header_path = header_path or workspace.header_path
        body_path = body_path or workspace.body_path
        stderr_path = stderr_path or workspace.stderr_path
        cookie_read_path = cookie_read_path or workspace.cookie_path
        cookie_write_path = cookie_write_path or workspace.cookie_path
    else:
        cookie_read_path = cookie_read_path or cookie_jar_path
        cookie_write_path = cookie_write_path or cookie_jar_path

    try:
        curl_args = _build_curl_fetch_args(
            url=url,
            timeout=timeout,
            headers=headers,
            proxy=proxy,
            cookie_jar_path=cookie_jar_path,
            header_path=header_path,
            body_path=body_path,
            cookie_read_path=cookie_read_path,
            cookie_write_path=cookie_write_path,
            max_response_bytes=max_response_bytes,
        )
        stdout, stderr, retcode = _run_subprocess_with_abort(
            curl_args,
            timeout,
            check_abort,
            abort_message="Fetch aborted by user",
            stderr_path=stderr_path,
            env=_build_fetch_subprocess_environment(),
        )
        try:
            body = Path(body_path).read_bytes().decode("utf-8", errors="replace")
        except (FileNotFoundError, OSError):
            body = ""
        response = _parse_curl_fetch_output(stdout, url, body=body)

        if retcode != 0:
            raise RuntimeError(stderr.strip() or f"curl exited with status {retcode}")
        if not response.get("body"):
            raise RuntimeError("empty response")

        if workspace and cookie_jar_path and os.path.exists(workspace.cookie_path):
            cookie_dir = os.path.dirname(cookie_jar_path) or "."
            cookie_dir_existed = os.path.isdir(cookie_dir)
            os.makedirs(cookie_dir, mode=0o700, exist_ok=True)
            if not cookie_dir_existed:
                os.chmod(cookie_dir, 0o700)
            shutil.copy2(workspace.cookie_path, cookie_jar_path)
            os.chmod(cookie_jar_path, 0o600)

        response["transport"] = "curl"
        response["profile"] = "desktop"
        response["_curl_completed_at"] = time.monotonic()
        return response
    finally:
        if workspace:
            workspace.cleanup()


def _fetch_with_curl_hedged(
    url: str,
    timeout: int,
    proxy: Optional[str],
    cfg: dict,
    headers: dict[str, str],
    cookie_jar_path: Optional[str],
    check_abort: Optional[Callable] = None,
) -> dict:
    """Run at most two header-aware curl attempts within one total budget."""
    total_timeout = float(timeout)
    if total_timeout <= 0: raise TimeoutError("Fetch deadline exceeded")
    try:
        hedge_delay = float(cfg.get("fetch_curl_hedge_delay_seconds") or 0)
    except (TypeError, ValueError):
        hedge_delay = 0.0
    hedge_delay = min(max(0.0, hedge_delay), total_timeout)
    try:
        max_response_bytes = int(
            cfg.get("fetch_curl_max_response_bytes")
            or _DEFAULT_CURL_MAX_RESPONSE_BYTES
        )
    except (TypeError, ValueError):
        max_response_bytes = _DEFAULT_CURL_MAX_RESPONSE_BYTES
    max_response_bytes = max(1, max_response_bytes)

    started_at = time.monotonic()
    deadline = started_at + total_timeout
    workspaces = _create_curl_hedge_workspaces(cookie_jar_path)

    cancel_events = [threading.Event(), threading.Event()]
    futures = {}
    handled_done = set()
    errors = {}
    winner_index = None
    winner_response = None
    second_started = False
    handed_off_workspace = False

    def combined_abort(index: int) -> bool:
        return cancel_events[index].is_set() or bool(check_abort and check_abort())

    def submit(pool: ThreadPoolExecutor, index: int) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Request timed out for {url}")
        workspace = workspaces[index]
        futures[index] = pool.submit(
            _fetch_with_curl,
            url=url,
            timeout=remaining,
            proxy=proxy,
            headers=headers,
            cookie_jar_path=workspace.cookie_path,
            check_abort=lambda: combined_abort(index),
            header_path=workspace.header_path,
            body_path=workspace.body_path,
            stderr_path=workspace.stderr_path,
            cookie_read_path=workspace.cookie_path,
            cookie_write_path=workspace.cookie_path,
            max_response_bytes=max_response_bytes,
        )

    def select_winner(index: int) -> None:
        nonlocal winner_index
        winner_index = index
        for other_index in futures:
            if other_index != index:
                cancel_events[other_index].set()

    pool = None
    primary_error = None
    try:
        pool = ThreadPoolExecutor(max_workers=2)
        submit(pool, 0)
        while True:
            if check_abort and check_abort():
                raise AbortError("Fetch aborted by user")

            now = time.monotonic()

            if winner_index is None:
                terminal_headers = []
                for index in futures:
                    state, status = _parse_latest_curl_header_state(
                        workspaces[index].header_path
                    )
                    if state == "pending":
                        continue
                    try:
                        observed_at = os.stat(workspaces[index].header_path).st_mtime_ns
                    except OSError:
                        observed_at = 0
                    terminal_headers.append((observed_at, index, state, status))

                if terminal_headers:
                    _, index, state, status = min(terminal_headers)
                    if state == "failure":
                        raise RuntimeError(f"HTTP {status} returned for {url}")
                    select_winner(index)

            for index, future in list(futures.items()):
                if not future.done() or index in handled_done:
                    continue
                handled_done.add(index)
                try:
                    response = future.result()
                except AbortError:
                    if not cancel_events[index].is_set():
                        raise
                except Exception as exc:
                    if index == winner_index:
                        raise
                    if not cancel_events[index].is_set():
                        state, status = _parse_latest_curl_header_state(
                            workspaces[index].header_path
                        )
                        if state == "failure":
                            raise RuntimeError(
                                f"HTTP {status} returned for {url}"
                            ) from exc
                        if state == "success":
                            select_winner(index)
                            raise
                        errors[index] = exc
                        if index == 0 and not second_started:
                            submit(pool, 1)
                            second_started = True
                else:
                    if index != winner_index and winner_index is not None:
                        continue
                    completed_at = response.pop("_curl_completed_at", None)
                    if completed_at is not None and float(completed_at) > deadline:
                        raise TimeoutError(f"Request timed out for {url}")
                    if completed_at is None and now >= deadline:
                        raise TimeoutError(f"Request timed out for {url}")
                    status_code = int(response.get("status_code") or 0)
                    if not 200 <= status_code < 300:
                        raise RuntimeError(
                            f"HTTP {status_code} returned for {url}"
                        )
                    if winner_index is None:
                        select_winner(index)
                    winner_response = response

            if winner_response is not None:
                if check_abort and check_abort():
                    raise AbortError("Fetch aborted by user")
                winner_response["_curl_hedge_workspace"] = workspaces[winner_index]
                winner_response["_curl_cookie_candidate"] = workspaces[
                    winner_index
                ].cookie_path
                handed_off_workspace = True
                return winner_response

            if now >= deadline:
                raise TimeoutError(f"Request timed out for {url}")

            if (
                winner_index is None
                and not second_started
                and time.monotonic() - started_at >= hedge_delay
            ):
                submit(pool, 1)
                second_started = True

            if (
                winner_index is None
                and second_started
                and all(future.done() for future in futures.values())
                and all(index in handled_done for index in futures)
            ):
                raise errors.get(1) or errors.get(0) or RuntimeError(
                    f"curl failed for {url}"
                )

            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError(f"Request timed out for {url}")
            wake_at = deadline
            if winner_index is None and not second_started:
                wake_at = min(wake_at, started_at + hedge_delay)
            wait_timeout = min(0.02, max(0.0, wake_at - now))
            unfinished = {
                future for future in futures.values() if not future.done()
            }
            if unfinished and wait_timeout > 0:
                wait(unfinished, timeout=wait_timeout, return_when=FIRST_COMPLETED)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        shutdown_error = None
        try:
            for event in cancel_events:
                event.set()
            if pool is not None:
                try:
                    pool.shutdown(wait=True, cancel_futures=True)
                except BaseException as exc:
                    shutdown_error = exc
        finally:
            if shutdown_error is not None:
                handed_off_workspace = False
            keep_index = winner_index if handed_off_workspace else None
            _cleanup_curl_workspaces(workspaces, keep_index=keep_index)
        if primary_error is None and shutdown_error is not None:
            raise shutdown_error


def _worker_call(operation, payload, timeout, check_abort=None, cfg=None):
    from backend.tools.fetch_process_pool import get_fetch_pool, FetchAbortedError
    try:
        return get_fetch_pool(cfg or _get_fetch_config()).call(
            operation, payload, timeout=timeout, check_abort=check_abort)
    except FetchAbortedError as exc:
        raise AbortError("Fetch aborted by user") from exc


def _fetch_with_curl_cffi(url, timeout, proxy, headers, impersonate, check_abort=None):
    """Run native libcurl in a supervised child process."""
    return _worker_call('curl_cffi', dict(url=url, timeout=timeout, proxy=proxy,
        headers=headers, impersonate=impersonate), timeout, check_abort)


def _fetch_with_curl_cffi_hedged(url, timeout, proxy, cfg, profile_attempts, check_abort=None):
    """Hedge isolated requests, cancel the loser and await acknowledged cleanup."""
    deadline = time.monotonic() + float(timeout)
    delay = float(cfg.get('fetch_curl_cffi_hedge_delay_seconds') or float(timeout)/2)
    profiles = (profile_attempts or [('desktop', 'chrome')])[:2]
    if len(profiles) == 1: profiles = profiles * 2
    cancel = threading.Event()
    def aborted():
        return cancel.is_set() or bool(check_abort and check_abort())
    executor = ThreadPoolExecutor(max_workers=2)
    futures = []
    def submit(profile):
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError('Fetch deadline exceeded')
        name, impersonate = profile
        futures.append(executor.submit(_fetch_with_curl_cffi, url=url,
            timeout=remaining, proxy=proxy,
            headers=dict(_BROWSER_PROFILES.get(name, _BROWSER_PROFILES['desktop'])),
            impersonate=impersonate, check_abort=aborted))
    second_at = time.monotonic() + delay
    last_error = None
    try:
        submit(profiles[0])
        second_started = False
        while futures:
            if check_abort and check_abort(): raise AbortError('Fetch aborted by user')
            now = time.monotonic()
            if now >= deadline: raise TimeoutError(f'Request timed out for {url}')
            if not second_started and now >= second_at:
                submit(profiles[1]); second_started = True
            done, _ = wait(futures, timeout=min(.05, deadline-now), return_when=FIRST_COMPLETED)
            for future in done:
                futures.remove(future)
                try: return future.result()
                except Exception as exc:
                    last_error = exc
                    if not second_started:
                        submit(profiles[1]); second_started = True
        raise last_error or RuntimeError('No fetch response')
    finally:
        cancel.set()
        # Each child cancellation has a bounded cleanup grace then process kill.
        executor.shutdown(wait=True, cancel_futures=True)


def _resolve_crawl4ai_page_timeout_ms(cfg: dict, timeout: int) -> int:
    """Resolve Crawl4AI page timeout without exceeding the per-call budget."""
    try:
        timeout_ms = int(float(timeout) * 1000)
    except (TypeError, ValueError):
        timeout_ms = 0
    timeout_ms = max(1, timeout_ms)

    try:
        configured_ms = int(cfg.get("crawl4ai_page_timeout_ms"))
    except (AttributeError, TypeError, ValueError):
        configured_ms = 0

    if configured_ms > 0:
        return max(1, min(configured_ms, timeout_ms))
    return timeout_ms


def _build_crawl4ai_run_config_kwargs(cfg: dict, timeout: int, cache_mode=None) -> dict:
    """Build Crawl4AI run config kwargs across old and new cache APIs."""
    kwargs = {
        "word_count_threshold": int(cfg.get("crawl4ai_word_count_threshold") or 10),
        "wait_until": str(cfg.get("crawl4ai_wait_until") or "networkidle"),
        "page_timeout": _resolve_crawl4ai_page_timeout_ms(cfg, timeout),
    }
    if bool(cfg.get("crawl4ai_bypass_cache", True)):
        if cache_mode is not None:
            kwargs["cache_mode"] = cache_mode
        else:
            kwargs["bypass_cache"] = True
    if bool(cfg.get("crawl4ai_magic", False)):
        kwargs["magic"] = True
    return kwargs


_DEFAULT_CRAWL4AI_MAX_CONCURRENCY = 50

_crawl4ai_sem_condition = threading.Condition(threading.Lock())
_crawl4ai_active = 0


def _get_crawl4ai_max_concurrency() -> int:
    """Return the configured max concurrent Crawl4AI page tasks (fallback 50)."""
    try:
        cfg = _get_fetch_config()
        value = int(cfg.get("fetch_crawl4ai_max_concurrency") or 0)
    except Exception:
        return _DEFAULT_CRAWL4AI_MAX_CONCURRENCY
    if value <= 0:
        return _DEFAULT_CRAWL4AI_MAX_CONCURRENCY
    return value


def _acquire_crawl4ai_slot(
    check_abort: Optional[Callable] = None,
    timeout: Optional[float] = None,
) -> None:
    """Acquire a Crawl4AI browser slot, abort/deadline aware.

    Polls every 0.1s so Stop/abort callbacks stay responsive. Raises
    AbortError when check_abort fires, TimeoutError when the wait exceeds
    timeout seconds.
    """
    global _crawl4ai_active
    try:
        wait_budget = float(timeout) if timeout is not None else None
    except (TypeError, ValueError):
        wait_budget = None
    if wait_budget is not None and wait_budget <= 0:
        raise TimeoutError("Timed out waiting for Crawl4AI browser slot")
    deadline = None if wait_budget is None else time.monotonic() + wait_budget
    with _crawl4ai_sem_condition:
        while True:
            if check_abort and check_abort():
                raise AbortError("Fetch aborted by user")
            try:
                limit_cfg = _get_fetch_config()
                limit = int(limit_cfg.get("fetch_crawl4ai_max_concurrency") or 0)
            except Exception:
                limit = _DEFAULT_CRAWL4AI_MAX_CONCURRENCY
            if limit <= 0:
                limit = _DEFAULT_CRAWL4AI_MAX_CONCURRENCY
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for Crawl4AI browser slot")
            if _crawl4ai_active < limit:
                _crawl4ai_active += 1
                return
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                raise TimeoutError("Timed out waiting for Crawl4AI browser slot")
            if deadline is None:
                wait_for = 0.1
            else:
                wait_for = min(0.1, max(0.0, deadline - now))
                if wait_for <= 0:
                    raise TimeoutError(
                        "Timed out waiting for Crawl4AI browser slot"
                    )
            _crawl4ai_sem_condition.wait(timeout=wait_for)


def _release_crawl4ai_slot() -> None:
    """Release a previously acquired Crawl4AI browser slot."""
    global _crawl4ai_active
    with _crawl4ai_sem_condition:
        if _crawl4ai_active > 0:
            _crawl4ai_active -= 1
        _crawl4ai_sem_condition.notify_all()


def _fetch_with_crawl4ai(url, timeout, cfg, check_abort=None):
    """Share the global page budget until worker cleanup is acknowledged."""
    budget = _resolve_transport_timeout_seconds('crawl4ai', cfg, timeout)
    deadline = time.monotonic() + budget
    _acquire_crawl4ai_slot(check_abort=check_abort, timeout=budget)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError("Crawl4AI deadline exceeded")
        return _worker_call('crawl4ai', dict(url=url, timeout=remaining, cfg=cfg),
            remaining, check_abort, cfg)
    finally:
        # Pool.call returns only after cancellation cleanup or process death.
        _release_crawl4ai_slot()


def _normalize_content_type(content_type: Optional[str]) -> str:
    """Normalize content-type header to the MIME portion only."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def _merge_block_markers(extra_markers: Optional[list[str]]) -> list[str]:
    """Merge built-in block markers with config-supplied markers."""
    markers = list(_DEFAULT_BLOCK_MARKERS)
    for marker in extra_markers or []:
        normalized = str(marker).strip().lower()
        if normalized and normalized not in markers:
            markers.append(normalized)
    return markers


def _looks_like_block_page(
    status_code: int,
    content_type: str,
    body: str,
    extra_markers: Optional[list[str]] = None,
) -> bool:
    """Detect common anti-bot block/challenge pages."""
    if status_code in {401, 403, 429, 503}:
        return True

    normalized_content_type = _normalize_content_type(content_type)
    if (
        normalized_content_type
        and "html" not in normalized_content_type
        and "text" not in normalized_content_type
        and "markdown" not in normalized_content_type
    ):
        return False

    lowered = (body or "").lower()
    return any(marker in lowered for marker in _merge_block_markers(extra_markers))


def _extract_text_with_trafilatura(html: str) -> str:
    """Try extracting main text with trafilatura when available."""
    try:
        import trafilatura
    except Exception:
        return ""

    try:
        text = trafilatura.extract(html, include_links=True, include_formatting=True)
    except Exception:
        return ""
    return (text or "").strip()


def _extract_text_with_readability(html: str) -> str:
    """Try extracting readable article content when readability is available."""
    try:
        from readability import Document
    except Exception:
        return ""

    try:
        summary_html = Document(html).summary()
    except Exception:
        return ""
    return _html_to_text(summary_html)


def _select_primary_html_fragment(html: str) -> str:
    """Prefer article/main-like fragments before falling back to body/full HTML."""
    fragment_patterns = [
        r"<article\b[^>]*>(.*?)</article>",
        r"<main\b[^>]*>(.*?)</main>",
        r"<body\b[^>]*>(.*?)</body>",
    ]
    for pattern in fragment_patterns:
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if match and match.group(1).strip():
            return match.group(1)
    return html


def _html_to_text(html: str) -> str:
    """Convert HTML to readable plain text with lightweight heuristics."""
    text = html
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<noscript[^>]*>.*?</noscript>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<header[^>]*>.*?</header>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<footer[^>]*>.*?</footer>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<nav[^>]*>.*?</nav>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<iframe[^>]*>.*?</iframe>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(
        r"<h[1-6][^>]*>(.*?)</h[1-6]>",
        r"\n\n## \1\n\n",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<p[^>]*>(.*?)</p>", r"\1\n\n", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(
        r"<a[^>]*href=\"([^\"]*)\"[^>]*>(.*?)</a>",
        r"\2 (\1)",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", "", text)
    text = _html_unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_response_text(body, content_type, min_text_chars,
                           enable_readability_fallback, timeout=30, check_abort=None):
    """Keep every native HTML parser out of the API process."""
    return _worker_call('extract', dict(body=body, content_type=content_type,
        min_text_chars=min_text_chars, enable_readability_fallback=enable_readability_fallback),
        timeout, check_abort)


def _extract_response_text_local(
    body: str,
    content_type: str,
    min_text_chars: int,
    enable_readability_fallback: bool,
) -> str:
    """Extract the most useful textual content from a fetched response."""
    normalized_content_type = _normalize_content_type(content_type)
    raw_body = (body or "").strip()
    if not raw_body:
        return ""

    if "json" in normalized_content_type:
        try:
            return json.dumps(json.loads(raw_body), ensure_ascii=False, indent=2)
        except Exception:
            return raw_body

    if normalized_content_type.startswith("text/plain"):
        return raw_body

    looks_like_html = (
        "html" in normalized_content_type
        or raw_body.lower().startswith("<!doctype html")
        or "<html" in raw_body.lower()
        or "<body" in raw_body.lower()
    )
    if not looks_like_html:
        return raw_body

    candidates = []
    trafilatura_text = _extract_text_with_trafilatura(raw_body)
    if trafilatura_text:
        candidates.append(trafilatura_text)

    readability_text = ""
    if enable_readability_fallback:
        readability_text = _extract_text_with_readability(raw_body).strip()
        if readability_text:
            candidates.append(readability_text)

    primary_fragment = _select_primary_html_fragment(raw_body)
    primary_text = _html_to_text(primary_fragment)
    if primary_text:
        candidates.append(primary_text)

    full_text = _html_to_text(raw_body)
    if full_text:
        candidates.append(full_text)

    candidates = [candidate.strip() for candidate in candidates if candidate and candidate.strip()]
    if not candidates:
        return ""

    def _candidate_rank(candidate: str) -> tuple[int, int]:
        if candidate == trafilatura_text:
            return (3, len(candidate))
        if readability_text and candidate == readability_text:
            return (2, len(candidate))
        return (1, len(candidate))

    candidates.sort(key=_candidate_rank, reverse=True)
    best = candidates[0]
    if len(best) >= min_text_chars:
        return best

    if readability_text:
        return readability_text

    return best


def _truncate_extracted_text(text: str, max_length: int = _MAX_FETCH_TEXT_LENGTH) -> str:
    """Truncate extracted text to a bounded size."""
    if len(text) <= max_length:
        return text
    return text[:max_length] + "\n\n...(content truncated)"


def _looks_like_low_quality_text(
    text: str, min_text_chars: int = 1000, min_text_lines: int = 6
) -> bool:
    """Reject very short extracted pages that are usually challenges or stubs."""
    stripped = (text or "").strip()
    if not stripped:
        return True
    line_count = len(stripped.splitlines())
    return len(stripped) < min_text_chars and line_count < min_text_lines


def _sleep_with_abort(seconds: float, check_abort: Optional[Callable] = None) -> None:
    """Sleep in short intervals so abort callbacks remain responsive."""
    end_time = time.time() + max(0.0, seconds)
    while time.time() < end_time:
        if check_abort and check_abort():
            raise AbortError("Fetch aborted by user")
        time.sleep(min(0.2, end_time - time.time()))


def _resolve_transport_timeout_seconds(transport: str, cfg: dict, timeout: int) -> int:
    """Cap each transport attempt without exceeding the caller's remaining budget."""
    try:
        call_timeout = float(timeout)
    except (TypeError, ValueError):
        call_timeout = 30
    if call_timeout <= 0:
        raise TimeoutError("Fetch deadline exceeded")

    config_key = None
    if transport == "curl":
        config_key = "fetch_curl_timeout_seconds"
    elif transport == "curl_cffi":
        config_key = "fetch_curl_cffi_timeout_seconds"
    elif transport == "crawl4ai":
        config_key = "fetch_crawl4ai_timeout_seconds"

    if not config_key:
        return call_timeout

    try:
        configured = int(cfg.get(config_key) or 0)
    except (TypeError, ValueError):
        configured = 0
    if configured <= 0:
        return call_timeout
    return min(configured, call_timeout)


def _resolve_transport_retry_count(transport: str, cfg: dict, default_retry_count: int) -> int:
    """Use a separate retry budget for heavy browser fallback."""
    if transport in {"curl", "curl_cffi"}:
        return 0
    if transport != "crawl4ai":
        return default_retry_count
    try:
        return max(0, int(cfg.get("crawl4ai_retry_count") or 0))
    except (TypeError, ValueError):
        return 0


def _format_fetch_output(final_url: str, text: str) -> str:
    """Format extracted text for tool output."""
    return f"# Content from {final_url}\n\n{text}"


def _fetch_webpage_content(
    url: str,
    timeout: int = 30,
    check_abort: callable = None,
) -> str:
    """Internal fetch path that raises structured exceptions for callers."""
    _validate_fetch_url(url)
    timeout = 30 if timeout is None else timeout
    if timeout <= 0: raise TimeoutError("Fetch deadline exceeded")
    deadline = time.monotonic() + float(timeout)
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0: raise TimeoutError("Fetch deadline exceeded")
        return value
    cfg = dict(_get_fetch_config())
    fetch_proxy = _normalise_fetch_proxy(cfg.get("fetch_proxy"))
    cfg["fetch_proxy"] = fetch_proxy
    primary_transport = _resolve_fetch_transport(cfg.get("fetch_transport")) or "curl"
    fallback_transport = _resolve_fetch_transport(cfg.get("fetch_fallback_transport"))
    cookie_jar_path = _build_cookie_jar_path(url)
    retry_count = max(0, int(cfg.get("fetch_retry_count") or 0))
    retry_backoff = float(cfg.get("fetch_retry_backoff_seconds") or 0.0)
    min_text_chars = max(0, int(cfg.get("fetch_min_text_chars") or 0))
    quality_min_text_chars = max(
        0, int(cfg.get("fetch_quality_min_text_chars", 1000) or 0)
    )
    quality_min_text_lines = max(
        0, int(cfg.get("fetch_quality_min_text_lines", 6) or 0)
    )
    enable_readability_fallback = bool(
        cfg.get("fetch_enable_readability_fallback", True)
    )
    block_markers = cfg.get("fetch_block_markers") or []
    transports = [primary_transport]
    if fallback_transport and fallback_transport not in transports:
        transports.append(fallback_transport)
    profile_attempts = _build_profile_attempts(cfg.get("fetch_impersonate"))

    last_error: Optional[Exception] = None
    for transport_index, transport in enumerate(transports):
        transport_profiles = (
            [("desktop", "")] if transport == "curl_cffi" else [("desktop", "")]
        )
        transport_timeout = _resolve_transport_timeout_seconds(transport, cfg, remaining())
        transport_retry_count = _resolve_transport_retry_count(
            transport, cfg, retry_count
        )
        should_fallback_transport = False
        for profile_name, impersonate in transport_profiles:
            profile_blocked = False
            headers = dict(_BROWSER_PROFILES[profile_name])
            for attempt in range(transport_retry_count + 1):
                if check_abort and check_abort():
                    raise AbortError("Fetch aborted by user")

                try:
                    transport_timeout = _resolve_transport_timeout_seconds(transport, cfg, remaining())
                    if transport == "crawl4ai":
                        response = _fetch_with_crawl4ai(
                            url=url,
                            timeout=transport_timeout,
                            cfg=cfg,
                            check_abort=check_abort,
                        )
                    elif transport == "curl_cffi":
                        response = _fetch_with_curl_cffi_hedged(
                            url=url,
                            timeout=transport_timeout,
                            proxy=fetch_proxy,
                            cfg=cfg,
                            profile_attempts=profile_attempts,
                            check_abort=check_abort,
                        )
                    elif transport == "curl":
                        response = _fetch_with_curl_hedged(
                            url=url,
                            timeout=transport_timeout,
                            proxy=fetch_proxy,
                            cfg=cfg,
                            headers=headers,
                            cookie_jar_path=cookie_jar_path,
                            check_abort=check_abort,
                        )
                    else:
                        raise RuntimeError(f"unsupported fetch transport: {transport}")

                    curl_workspace = response.pop("_curl_hedge_workspace", None)
                    curl_cookie_candidate = response.pop("_curl_cookie_candidate", None)
                    response.pop("_curl_completed_at", None)
                    try:
                        blocked = _looks_like_block_page(
                            int(response.get("status_code") or 0),
                            response.get("content_type", ""),
                            response.get("body", ""),
                            block_markers,
                        )

                        if blocked:
                            last_error = RuntimeError("blocked or challenge page")
                            if transport == "curl_cffi":
                                profile_blocked = True
                                break
                            if transport_index < len(transports) - 1:
                                should_fallback_transport = True
                                break
                            break
                        extracted_text = _extract_response_text(
                            response.get("body", ""),
                            response.get("content_type", ""),
                            min_text_chars,
                            enable_readability_fallback,
                            timeout=remaining(), check_abort=check_abort,
                        )

                        if not extracted_text:
                            raise RuntimeError("empty extracted content")
                        if _looks_like_low_quality_text(
                            extracted_text,
                            quality_min_text_chars,
                            quality_min_text_lines,
                        ):
                            raise RuntimeError("low quality extracted content")

                        extracted_text = _truncate_extracted_text(extracted_text)
                        final_url = response.get("final_url") or url
                        if (
                            curl_workspace is not None
                            and curl_cookie_candidate
                            and cookie_jar_path
                        ):
                            _publish_curl_cookie_candidate(
                                curl_cookie_candidate,
                                cookie_jar_path,
                            )
                        return _format_fetch_output(final_url, extracted_text)
                    finally:
                        if curl_workspace is not None:
                            curl_workspace.cleanup()
                except AbortError:
                    raise
                except TimeoutError as exc:
                    last_error = exc
                    if attempt >= transport_retry_count:
                        break
                    if retry_backoff > 0:
                        _sleep_with_abort(
                            min(retry_backoff * (2**attempt), remaining()),
                            check_abort=check_abort,
                        )
                except Exception as exc:
                    last_error = exc
                    if attempt >= transport_retry_count:
                        break
                    if retry_backoff > 0:
                        _sleep_with_abort(
                            min(retry_backoff * (2**attempt), remaining()),
                            check_abort=check_abort,
                        )

            if should_fallback_transport:
                break
            if transport == "curl_cffi" and profile_blocked:
                continue
        if should_fallback_transport:
            continue

    if last_error is None:
        raise RuntimeError("empty response")
    raise last_error


@tool
def web_search(
    query: str,
    max_results: int = 80,
    timeout: int = 30,
    use_fallback: bool = True,
    check_abort: callable = None,
) -> str:
    """Search the external web for current information.

    Args:
        query: The search query string.
        max_results: Maximum number of results to return (default: 80).
        timeout: Request timeout in seconds. Omit to use the configured search
            timeout; the value 30 also selects that configured timeout.
            Other explicit values override it. Increase for slow networks.
        use_fallback: Deprecated compatibility arg. No legacy provider fallback is used.
        check_abort: Optional callback to check if operation should be aborted.

    Returns:
        Formatted search results as markdown with titles, URLs, and descriptions.
    """
    timeout = timeout or 30
    cfg = _get_config()
    resolved_timeout = cfg.get("searxng_timeout", 10)
    if timeout and timeout != 30:
        resolved_timeout = timeout

    if check_abort and check_abort():
        return "搜索已被用户中止"

    try:
        results = _search_with_searxng(
            query,
            int(resolved_timeout),
            cfg,
            max_results=max_results,
        )[:max_results]
    except AbortError:
        return "搜索已被用户中止"
    except LookupError as exc:
        return str(exc)
    except Exception as exc:
        return str(exc)

    output = "## Search Results\n\n"
    for r in results:
        output += f"- [{r['title']}]({r['url']})\n"
        if r["description"]:
            output += f"  {r['description']}\n"
        output += "\n"
    return output


@tool
def web_fetch(url: str, timeout: int = 30, check_abort: callable = None) -> str:
    """Fetch and convert a known webpage URL to readable text.

    Args:
        url: The URL of the webpage to fetch.
        timeout: Request timeout in seconds (default: 30). Increase for slow networks.
        check_abort: Optional callback to check if operation should be aborted.

    Returns:
        The webpage content converted to text/markdown format.
    """
    try:
        _validate_fetch_url(url)
        return _fetch_webpage_content(url, timeout=timeout, check_abort=check_abort)
    except AbortError:
        return "网页获取已被用户中止"
    except TimeoutError:
        return "Failed to fetch webpage: request timed out."
    except Exception as e:
        return f"Failed to fetch webpage: {str(e)}"
