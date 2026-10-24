#!/usr/bin/env python
"""Detailed live diagnostics for SearXNG engine search and page fetch.

This script intentionally talks to the real local SearXNG instance. It is a
diagnostic tool, not a mocked unit test.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_QUERY = "RLHF tool framework HuggingFace TRL"
DEFAULT_ENGINES = ["google", "duckduckgo", "brave", "startpage", "mojeek"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnose SearXNG search engines and fetch the returned URLs."
    )
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Search query.")
    parser.add_argument(
        "--max-results",
        type=int,
        default=20,
        help="Maximum search results to collect per engine.",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=3,
        help="Maximum SearXNG pages to request per engine.",
    )
    parser.add_argument(
        "--fetch-limit",
        type=int,
        default=3,
        help="Maximum URLs to fetch per engine. Use 0 to skip fetch.",
    )
    parser.add_argument(
        "--engines",
        default=",".join(DEFAULT_ENGINES),
        help="Comma-separated engine names.",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8080",
        help="SearXNG base URL.",
    )
    parser.add_argument("--language", default="all", help="SearXNG language.")
    parser.add_argument("--safe-search", type=int, default=0, help="SearXNG safesearch.")
    parser.add_argument("--timeout", type=int, default=30, help="Request/fetch timeout seconds.")
    parser.add_argument(
        "--preview-chars",
        type=int,
        default=500,
        help="Characters of fetched text preview to print.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full diagnostic report as JSON instead of text.",
    )
    return parser.parse_args(argv)


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 3)


def search_engine_page(
    engine: str,
    query: str,
    pageno: int,
    timeout: int,
    base_url: str,
    language: str,
    safe_search: int,
) -> dict[str, Any]:
    search_url = urllib.parse.urljoin(base_url.rstrip("/") + "/", "search")
    params = {
        "q": query,
        "format": "json",
        "engines": engine,
        "language": language,
        "safesearch": str(safe_search),
        "pageno": str(max(1, pageno)),
    }
    url = f"{search_url}?{urllib.parse.urlencode(params)}"
    started = time.monotonic()
    try:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 SearXNG-Diagnostics/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
            return {
                "status": "success",
                "http_status": getattr(response, "status", None) or response.getcode(),
                "elapsed_seconds": _elapsed(started),
                "request_url": url,
                "results": payload.get("results") or [],
                "answers": payload.get("answers") or [],
                "corrections": payload.get("corrections") or [],
                "infoboxes": payload.get("infoboxes") or [],
                "suggestions": payload.get("suggestions") or [],
                "unresponsive_engines": payload.get("unresponsive_engines") or [],
            }
    except urllib.error.HTTPError as exc:
        return {
            "status": "http_error",
            "http_status": exc.code,
            "elapsed_seconds": _elapsed(started),
            "request_url": url,
            "results": [],
            "unresponsive_engines": [],
            "error": str(exc),
        }
    except (socket.timeout, TimeoutError) as exc:
        return {
            "status": "timeout",
            "http_status": None,
            "elapsed_seconds": _elapsed(started),
            "request_url": url,
            "results": [],
            "unresponsive_engines": [],
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "status": "error",
            "http_status": None,
            "elapsed_seconds": _elapsed(started),
            "request_url": url,
            "results": [],
            "unresponsive_engines": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def fetch_url(url: str, timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        from backend.tools.web_tools import _fetch_webpage_content

        content = _fetch_webpage_content(url, timeout=timeout)
        return {
            "url": url,
            "status": "success",
            "elapsed_seconds": _elapsed(started),
            "content_length": len(content),
            "error": None,
            "preview": content,
        }
    except (socket.timeout, TimeoutError) as exc:
        return {
            "url": url,
            "status": "timeout",
            "elapsed_seconds": _elapsed(started),
            "content_length": 0,
            "error": str(exc),
            "preview": "",
        }
    except Exception as exc:
        return {
            "url": url,
            "status": "error",
            "elapsed_seconds": _elapsed(started),
            "content_length": 0,
            "error": f"{type(exc).__name__}: {exc}",
            "preview": "",
        }


def _result_key(item: dict[str, Any]) -> str:
    return str(item.get("url") or item.get("title") or "").strip()


def diagnose_engine(
    engine: str,
    query: str,
    max_results: int,
    pages: int,
    fetch_limit: int,
    timeout: int,
    base_url: str,
    language: str,
    safe_search: int,
) -> dict[str, Any]:
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    page_reports: list[dict[str, Any]] = []

    for pageno in range(1, max(1, pages) + 1):
        page = search_engine_page(
            engine=engine,
            query=query,
            pageno=pageno,
            timeout=timeout,
            base_url=base_url,
            language=language,
            safe_search=safe_search,
        )
        page_results = page.get("results") or []
        for item in page_results:
            if not isinstance(item, dict):
                continue
            key = _result_key(item)
            if not key or key in seen:
                continue
            seen.add(key)
            collected.append(item)
            if len(collected) >= max_results:
                break
        page["new_unique_results"] = len(collected) - sum(
            len(p.get("unique_results_after_page", [])) for p in []
        )
        page["collected_results_after_page"] = len(collected)
        page_reports.append(page)
        if len(collected) >= max_results:
            break

    fetches: list[dict[str, Any]] = []
    for item in collected[: max(0, fetch_limit)]:
        url = str(item.get("url") or "").strip()
        if not url:
            fetches.append(
                {
                    "url": "",
                    "status": "skipped",
                    "elapsed_seconds": 0,
                    "content_length": 0,
                    "error": "missing url",
                    "preview": "",
                }
            )
            continue
        fetches.append(fetch_url(url, timeout=timeout))

    summary = {
        "search_results": len(collected),
        "pages_requested": len(page_reports),
        "fetch_total": len(fetches),
        "fetch_success": sum(1 for item in fetches if item.get("status") == "success"),
        "fetch_timeout": sum(1 for item in fetches if item.get("status") == "timeout"),
        "fetch_error": sum(1 for item in fetches if item.get("status") == "error"),
        "unresponsive_events": sum(len(page.get("unresponsive_engines") or []) for page in page_reports),
    }
    return {
        "engine": engine,
        "query": query,
        "pages": page_reports,
        "results": collected,
        "fetches": fetches,
        "summary": summary,
    }


def _one_line(text: Any, limit: int = 180) -> str:
    compact = " ".join(str(text or "").split())
    return compact[:limit] + ("..." if len(compact) > limit else "")


def print_text_report(report: dict[str, Any], preview_chars: int) -> None:
    print("=" * 100)
    print(f"SearXNG Engine Diagnostics | query={report['query']!r}")
    print("=" * 100)
    for engine_report in report["engines"]:
        summary = engine_report["summary"]
        print()
        print(f"## Engine: {engine_report['engine']}")
        print(
            "Summary: "
            f"search_results={summary['search_results']}, "
            f"pages_requested={summary['pages_requested']}, "
            f"unresponsive_events={summary['unresponsive_events']}, "
            f"fetch_success={summary['fetch_success']}, "
            f"fetch_timeout={summary['fetch_timeout']}, "
            f"fetch_error={summary['fetch_error']}"
        )

        print("\n### Search Pages")
        for index, page in enumerate(engine_report["pages"], start=1):
            print(
                f"[page {index}] status={page.get('status')} "
                f"http={page.get('http_status')} "
                f"elapsed={page.get('elapsed_seconds')}s "
                f"results={len(page.get('results') or [])} "
                f"collected={page.get('collected_results_after_page')}"
            )
            print(f"  request_url: {page.get('request_url')}")
            if page.get("unresponsive_engines"):
                print(f"  unresponsive_engines: {page.get('unresponsive_engines')}")
            if page.get("error"):
                print(f"  error: {page.get('error')}")
            for result_index, item in enumerate((page.get("results") or [])[:10], start=1):
                print(
                    f"  - [{result_index}] engine={item.get('engine')} "
                    f"category={item.get('category')} score={item.get('score')}"
                )
                print(f"      title: {_one_line(item.get('title'))}")
                print(f"      url: {item.get('url')}")
                if item.get("content"):
                    print(f"      content: {_one_line(item.get('content'))}")

        print("\n### Fetch Results")
        if not engine_report["fetches"]:
            print("  (fetch skipped or no results)")
        for index, item in enumerate(engine_report["fetches"], start=1):
            print(
                f"[{index}] status={item.get('status')} "
                f"elapsed={item.get('elapsed_seconds')}s "
                f"content_length={item.get('content_length')}"
            )
            print(f"  url: {item.get('url')}")
            if item.get("error"):
                print(f"  error: {item.get('error')}")
            preview = _one_line(item.get("preview"), preview_chars)
            if preview:
                print(f"  preview: {preview}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    engines = [item.strip() for item in args.engines.split(",") if item.strip()]
    report = {
        "query": args.query,
        "base_url": args.base_url,
        "language": args.language,
        "max_results": args.max_results,
        "pages": args.pages,
        "fetch_limit": args.fetch_limit,
        "engines": [],
    }

    for engine in engines:
        report["engines"].append(
            diagnose_engine(
                engine=engine,
                query=args.query,
                max_results=args.max_results,
                pages=args.pages,
                fetch_limit=args.fetch_limit,
                timeout=args.timeout,
                base_url=args.base_url,
                language=args.language,
                safe_search=args.safe_search,
            )
        )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text_report(report, preview_chars=args.preview_chars)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
