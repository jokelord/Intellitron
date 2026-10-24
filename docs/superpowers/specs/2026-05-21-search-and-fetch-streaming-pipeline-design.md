# Search-And-Fetch Streaming Pipeline Design

## Context

The current `search_and_fetch` flow still behaves in two large phases:

1. perform search and collect the full deduped result list
2. start concurrent webpage fetches only after search collection is complete

This means pagination improvements in `web_search` increase the final candidate set, but `search_and_fetch` still waits until all search pages are processed before any fetch begins.

For time-sensitive news queries and larger `max_results` values, this is suboptimal. The requested behavior is a pipeline:

- page N arrives
- normalize and dedupe URLs against all prior pages
- newly discovered unique URLs are submitted for fetch immediately
- page N+1 continues the same process while earlier fetches are already in flight

This pipeline is wanted only for `search_and_fetch`. It must not change the public behavior of `web_search` or `web_fetch`.

## Goals

- Make `search_and_fetch` fetch newly discovered URLs as soon as each SearXNG page is processed.
- Keep `max_results` defined against the final deduped URL count.
- Reuse the existing fetch concurrency cap via `fetch_max_workers`.
- Keep observation output concise: counts of total/fetched/timeout/failure only, without exposing pagination or dedupe internals.

## Non-Goals

- Turning `web_search` into a streaming tool.
- Changing `web_fetch` semantics.
- Exposing `pageno`, page counts, dedupe counts, or pipeline internals to the LLM.
- Adding new engines or per-engine timeouts.

## Desired Runtime Behavior

### 1. Pipeline Scope

Only `search_and_fetch` gets streaming search-to-fetch behavior.

- `web_search`: remains a pure search tool that returns aggregated final search results.
- `web_fetch`: remains a single-page fetch tool.
- `search_and_fetch`: becomes the only tool that overlaps search pagination with webpage fetching.

### 2. Per-Page Streaming Flow

For each SearXNG page in `search_and_fetch`:

1. request the next search page
2. extract usable results
3. normalize each URL
4. compare against `seen_urls` from prior pages
5. add only newly discovered unique URLs to the result set
6. immediately submit those new URLs to the fetch executor
7. continue to the next search page without waiting for all prior fetches to finish

Example target behavior:

- page 1 returns 6 results, 4 are unique -> submit 4 fetch tasks immediately
- page 2 returns 6 results, 2 are newly unique -> submit those 2 immediately
- page 3 repeats the same pattern until stop conditions trigger

### 3. `max_results` Semantics

`max_results` must continue to mean:

- the desired number of final deduped unique search-result URLs

It must not mean:

- per-page size
- raw pre-dedup result count
- number of fetch attempts before dedupe

If each page contains only 6 raw results and many duplicates, the pipeline should keep requesting more pages until it either:

- reaches `max_results` unique URLs
- stops seeing any new unique URLs
- reaches the configured page cap
- runs out of overall time budget

### 4. Concurrency Control

The pipeline must continue using `fetch_max_workers` as the global concurrent fetch cap.

- New fetches are queued immediately when new unique URLs are found.
- The executor enforces the maximum number of in-flight webpage fetches.
- The design must not bypass the worker cap just because a new search page arrives.

### 5. Output Constraints

The final observation returned by `search_and_fetch` should keep the current high-value summary only:

- total deduped results discovered
- successful fetch count
- timeout count
- failure count

It must not display:

- how many pages were searched
- dedupe counts
- internal pipeline state

So output stays user-task-relevant, for example:

- `共找到 20 个结果，成功抓取 6 个，超时 0 个，失败 14 个`

### 6. Error Handling

Search page failures should follow the current practical behavior:

- if page 1 fails before any usable results exist, return a search failure wrapper
- if a later page fails after some unique URLs were already discovered and/or submitted for fetch, keep the already collected results and finish the fetch side cleanly

Fetch failures remain per-URL outcomes just like today.

## Architecture

The streaming behavior should live in `backend/tools/search_and_fetch.py`, not in `web_search`.

Recommended split:

- `backend/tools/web_tools.py`
  - keep reusable single-page SearXNG search helpers and URL normalization helpers
- `backend/tools/search_and_fetch.py`
  - own the page-by-page pipeline orchestration
  - own dedupe state for this composite operation
  - own executor submission timing

This keeps responsibilities clear:

- `web_search` is still about search output
- `search_and_fetch` is where search and fetch are intentionally fused into one pipeline

## Tests

Add or update tests for:

1. page 1 new URLs are submitted before later pages finish
2. page 2 only submits URLs that are new relative to page 1
3. `max_results` is enforced on deduped URLs, not raw page counts
4. worker cap still uses `fetch_max_workers`
5. observation output remains concise and does not expose page/dedupe internals
6. `web_search` and `web_fetch` behavior remain unaffected

## Acceptance Criteria

- `search_and_fetch` begins fetching URLs as soon as each page yields new unique results.
- `max_results` is still measured against deduped unique URLs.
- `fetch_max_workers` still caps concurrent webpage fetches.
- Final observation remains concise and unchanged in style.
- `web_search` and `web_fetch` do not gain this streaming pipeline behavior.
