# Crawl4AI Fetch Transport Design

## Context

`search_and_fetch` currently searches with SearXNG, then fetches result pages through `backend.tools.web_tools._fetch_webpage_content()`.

The existing fetch transports are:

- `curl`: subprocess-based HTTP fetch with browser-like headers and cookie jar support
- `curl_cffi`: Python HTTP fetch with browser/TLS impersonation
- `auto`: resolves to `curl_cffi` when installed, otherwise `curl`

The current robustness path already includes:

- block-page markers such as CAPTCHA, Cloudflare, access denied, and JavaScript-required pages
- `trafilatura` and `readability-lxml` extraction
- low-quality text rejection for pages with `line_count <= 5` and `char_count < 1000`
- per-URL failure accounting in `search_and_fetch`

The requested next step is to add `crawl4ai` as a first-class fallback transport while keeping the lighter `curl_cffi` path first:

```yaml
web_tools:
  fetch_transport: curl_cffi
  fetch_fallback_transport: crawl4ai
  fetch_content_fallback: searxng_snippet
```

The user has Playwright tooling installed on the PC, which is relevant because Crawl4AI relies on browser automation for JavaScript-rendered pages.

The requested `search_and_fetch` default `max_results` must also be reduced to `25` because a higher page fetch success rate makes `80` too expensive and too verbose.

## Goals

- Add `crawl4ai` as a valid `web_tools.fetch_fallback_transport` value.
- Use `curl_cffi` as the primary lightweight transport.
- Support `web_tools.fetch_fallback_transport: crawl4ai` so failed HTTP/TLS impersonation fetches can escalate to browser-rendered crawling.
- Add `fetch_content_fallback: searxng_snippet` for `search_and_fetch` only, so both transport failures can still return the search result snippet as degraded content.
- Keep `curl` and `curl_cffi` behavior compatible with existing tests.
- Preserve existing page-quality validation: CAPTCHA/challenge pages and short stub pages must count as fetch failures, not successes.
- Reduce `search_and_fetch_impl()` default `max_results` to `25` and update tests/docs accordingly.
- Keep all important knobs in config; do not hardcode browser behavior that users will need to tune.

## Non-Goals

- Do not add CAPTCHA-solving services.
- Do not introduce proxy pool management.
- Do not make Crawl4AI mandatory for environments that do not install it.
- Do not replace SearXNG search.
- Do not expose raw Crawl4AI debug output to the LLM observation.
- Do not change frontend protocol or Agent SSE behavior.

## Proposed Approach

### Recommended approach: transport plugin plus snippet content fallback

Add Crawl4AI behind the same internal transport contract used by `curl` and `curl_cffi`:

```python
def _fetch_with_crawl4ai(...) -> dict:
    return {
        "status_code": 200,
        "final_url": ...,
        "content_type": "text/markdown",
        "body": ...,
        "transport": "crawl4ai",
        "profile": ...,
    }
```

`_fetch_webpage_content()` can then keep the same validation and extraction pipeline for real page fetches:

1. resolve primary and fallback transports
2. run the selected transport
3. detect block/challenge pages
4. extract or normalize text
5. reject empty or low-quality text
6. return formatted content or raise an exception

This is the smallest change for real fetches because `search_and_fetch` already treats any `_fetch_webpage_content()` exception as a per-result failure.

The SearXNG snippet fallback is intentionally not implemented as a transport inside `_fetch_webpage_content()` because a single URL fetch does not have access to the search result title/description. It belongs in `search_and_fetch`, where each result item already contains a `description` field from SearXNG.

### Rejected approach: separate `crawl_fetch` tool

A separate tool would make the LLM choose between fetch modes. That is worse for reliability and cache stability because the model would have to reason about transport selection. Transport choice should remain backend config, not prompt behavior.

### Rejected approach: Crawl4AI-only rewrite

Using Crawl4AI for every page may improve some JS-heavy pages, but it is more expensive than HTTP fetches and can require browser process lifecycle tuning. The implementation should support Crawl4AI as the configured fallback transport while keeping `curl_cffi` primary.

## Configuration Design

Extend the fetch config model to include `crawl4ai`:

```python
fetch_transport: str = "auto"  # auto | curl | curl_cffi | crawl4ai
fetch_fallback_transport: Optional[str] = "curl"
```

Target config for this feature:

```yaml
web_tools:
  fetch_transport: curl_cffi
  fetch_fallback_transport: crawl4ai
  fetch_content_fallback: searxng_snippet
```

Add Crawl4AI-specific config fields under `web_tools`:

```yaml
  # Crawl4AI browser fetch settings
  crawl4ai_browser_type: chromium
  crawl4ai_headless: true
  crawl4ai_wait_until: networkidle
  crawl4ai_page_timeout_ms: 15000
  crawl4ai_word_count_threshold: 10
  crawl4ai_bypass_cache: true
```

Field intent:

- `crawl4ai_browser_type`: browser engine, default `chromium`.
- `crawl4ai_headless`: run browser headless by default.
- `crawl4ai_wait_until`: page readiness strategy. Use Crawl4AI-supported values only.
- `crawl4ai_page_timeout_ms`: browser page timeout, derived from the tool timeout if lower-level API needs milliseconds.
- `crawl4ai_word_count_threshold`: lets Crawl4AI filter tiny blocks before backend quality checks.
- `crawl4ai_bypass_cache`: avoid stale challenge pages during agent runs.

Do not add a config field for CAPTCHA solving.

`fetch_content_fallback` is deliberately separate from `fetch_fallback_transport`:

- `fetch_transport` and `fetch_fallback_transport` choose how to fetch the full page.
- `fetch_content_fallback` chooses what content to return when full-page fetch fails.
- `searxng_snippet` is not a transport because it does not fetch the page; it reuses the search result title and description already returned by SearXNG.

Accepted values:

```python
fetch_content_fallback: Optional[str] = None  # null | searxng_snippet
```

## Dependency And Availability

`crawl4ai` should be an optional dependency at runtime:

- If `crawl4ai` appears in `fetch_transport` or `fetch_fallback_transport` and the package is missing, `_fetch_webpage_content()` should raise a clear transport error for that attempt and continue to the next configured fallback path when available.
- If fallback is also unavailable, return a normal per-URL fetch failure through `search_and_fetch`.
- `_transport_is_available("crawl4ai")` should check Python import availability, not Playwright CLI availability.

Playwright browser installation is still required by Crawl4AI. The implementation should not shell out to `playwright-cli` directly unless Crawl4AI requires no other viable path. Browser lifecycle should be delegated to Crawl4AI.

## Runtime Data Flow

For `search_and_fetch` with target config:

1. SearXNG returns deduped URLs.
2. `search_and_fetch` submits fetch tasks through the existing executor.
3. `_fetch_webpage_content()` resolves transports to `["curl_cffi", "crawl4ai"]`.
4. `curl_cffi` attempts the lightweight full-page fetch first.
5. If `curl_cffi` fails or returns unusable content, Crawl4AI fetches and renders the page.
6. Backend normalizes Crawl4AI output into the standard response dict.
7. Existing block and low-quality checks run for both transports.
8. If both transports fail and `fetch_content_fallback: searxng_snippet` is enabled, `search_and_fetch` emits a degraded snippet result from the SearXNG title/description.
9. If no snippet is available, the URL is counted as `状态: 失败`.

## Crawl4AI Output Handling

Crawl4AI may expose markdown, cleaned HTML, raw HTML, or structured extraction fields depending on API version.

Implementation should prefer outputs in this order:

1. markdown or fit markdown, if available and non-empty
2. cleaned HTML passed through the existing text extraction path
3. raw HTML passed through the existing text extraction path
4. plain text fields, if available

The returned dict should use:

- `content_type: "text/markdown"` when markdown is used
- `content_type: "text/html"` when HTML is used
- `status_code: 200` unless Crawl4AI exposes a real HTTP status
- `final_url`: Crawl4AI final URL if available, otherwise requested URL

The backend should not expose Crawl4AI internals directly to the LLM. It should return the same `# Content from ...` format used by existing fetch transports.

## Failure And Fallback Policy

Transport fallback from `curl_cffi` to `crawl4ai` should trigger on:

- `curl_cffi` import or request failure
- `curl_cffi` timeout
- empty content
- block/challenge detection
- low-quality content where `line_count <= 5` and `char_count < 1000`

Crawl4AI should then be considered failed on:

- import or initialization failure
- browser launch failure
- timeout
- Crawl4AI unsuccessful result flag
- empty content
- block/challenge detection
- low-quality content where `line_count <= 5` and `char_count < 1000`

The final exception after all transports fail should preserve a useful reason, for example:

- `crawl4ai is not installed`
- `crawl4ai fetch timed out`
- `low quality extracted content`
- `empty extracted content`

If both transports fail, `search_and_fetch` should apply `fetch_content_fallback`:

- when `fetch_content_fallback: searxng_snippet` and the search result has a non-empty title or description, return a degraded snippet content block
- when disabled or no snippet exists, classify the URL as `状态: 失败`

No new low-quality status should be added. The degraded snippet path should use `状态: 摘要兜底` or another clearly non-success label, and it must not increment the full-page `成功抓取` count.

Recommended counts:

- `成功抓取`: real page fetch succeeded through `curl_cffi` or `crawl4ai`
- `摘要兜底`: both fetch transports failed, but SearXNG snippet was returned
- `超时`: deadline/timeout without snippet fallback
- `失败`: all fetch and content fallback paths failed

## Timeout, Abort, And Concurrency

The existing `search_timeout` deadline remains the outer budget for `search_and_fetch`.

Both `curl_cffi` and Crawl4AI fetches must honor:

- the per-call `timeout` passed into `_fetch_webpage_content()`
- `check_abort`, as much as Crawl4AI allows
- `fetch_max_workers`, enforced by the existing `ThreadPoolExecutor`

If Crawl4AI does not support cooperative cancellation during an active browser navigation, the wrapper should check `check_abort` before launch and after completion. It must not leave long-running browser tasks behind after `search_and_fetch` returns.

The previous safety contract still applies: `search_and_fetch` must wait for running fetch workers to exit before returning on timeout or abort.

## SearXNG Snippet Fallback Output

When both page fetch transports fail but SearXNG snippet fallback is available, the per-result output should remain compact and honest:

```text
状态: 摘要兜底
内容:
[FULL_FETCH_FAILED - low quality extracted content]
Search result snippet:
<title>
<description>
```

This prevents the LLM from mistaking a short search snippet for fetched page content while still giving it useful context.

If the result has no useful title or description, do not synthesize content. Count it as `失败`.

## `max_results` Change

Change `search_and_fetch_impl()` default:

```python
def search_and_fetch_impl(query: str, max_results: int = 25, ...)
```

Update docstrings and tests that currently expect `40` or older values.

Rationale:

- Crawl4AI browser rendering is available as fallback and is heavier than HTTP fetch.
- Higher fetch success rates produce larger observations.
- `25` gives enough breadth for research tasks while controlling latency and prompt size.

The meaning of `max_results` remains unchanged: it is the maximum number of final deduped search-result URLs processed by `search_and_fetch`.

## Tests

Add or update tests for:

1. `_resolve_fetch_transport("crawl4ai")` returns `crawl4ai`.
2. `_transport_is_available("crawl4ai")` is true when import succeeds and false when import fails.
3. `_fetch_webpage_content()` calls `_fetch_with_curl_cffi()` first and `_fetch_with_crawl4ai()` second when configured as `curl_cffi -> crawl4ai`.
4. Crawl4AI success returns formatted content through the standard `# Content from ...` output.
5. `curl_cffi` blocked/empty/low-quality output falls back to Crawl4AI when configured.
6. Missing Crawl4AI package falls through to snippet fallback in `search_and_fetch` when `curl_cffi` also failed and snippet exists.
7. If both `curl_cffi` and Crawl4AI fail and snippet exists, `search_and_fetch` counts the URL under `摘要兜底`, not `成功抓取`.
8. If both transports fail and no snippet exists, `search_and_fetch` counts the URL as `失败`.
9. `search_and_fetch_impl()` default `max_results` is `25`.
10. Existing `curl` and `curl_cffi` tests still pass.

Mock Crawl4AI in unit tests. Do not require real browser launch in normal unit tests.

Optionally add an integration test guarded by an environment variable, for example:

```bash
RUN_CRAWL4AI_INTEGRATION=1 ./.sglang/bin/python -m pytest tests/test_crawl4ai_fetch_integration.py
```

The integration test can fetch a stable local HTML page or a local test server page, not an external website.

## Acceptance Criteria

- Config accepts `fetch_transport: curl_cffi`, `fetch_fallback_transport: crawl4ai`, and `fetch_content_fallback: searxng_snippet`.
- Effective runtime config can print those exact values from `get_config().web_tools`.
- `curl_cffi` is attempted before Crawl4AI when configured as primary.
- `curl_cffi` failures and unusable pages can fall back to Crawl4AI.
- Short challenge/stub content still becomes a fetch failure unless Crawl4AI or snippet fallback can recover useful content.
- SearXNG snippet fallback is only used after both real fetch transports fail.
- Snippet fallback is labeled separately and does not increment the full-page success count.
- `search_and_fetch` does not add a new low-quality status; such pages are counted under `失败`.
- `search_and_fetch_impl` default `max_results` is `25`.
- Unit tests for web fetch and search-and-fetch pass, except for explicitly unrelated pre-existing failures if any are documented.

## Open Review Questions

These do not block the first implementation, but they should be reviewed before expanding beyond the minimal transport:

1. Should snippet fallback text be included in compact summaries, or treated as lower-confidence source material by future compact logic?
2. Should a future provider fallback such as Jina Reader come before or after `searxng_snippet`?
3. Should `摘要兜底` count be shown in the top summary line by default, or only inside each affected result block?
