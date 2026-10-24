# SearXNG Pagination Design

## Context

The current SearXNG integration exposes `max_results` on `web_search` and `search_and_fetch`, but `web_search` only sends a single JSON search request to SearXNG. As a result, `max_results=20` means "return up to 20 items from page 1" rather than "aggregate up to 20 search results across multiple SearXNG result pages".

In practice, this causes a common failure mode:

- SearXNG page 1 returns only a small number of usable results after deduping/engine merging
- `search_and_fetch` can only fetch the URLs it was given
- users perceive low fetch coverage even when the fetch layer itself is healthy

The requested behavior is to adapt both `web_search` and `search_and_fetch` to SearXNG's pagination model while keeping the LLM-facing interface simple.

## Goals

- Keep `web_search` and `search_and_fetch` externally driven by `query` + `max_results`.
- Implement SearXNG pagination internally with `pageno=1,2,3...`.
- Aggregate results across pages until enough unique URLs are collected, or until pagination should stop.
- Teach the LLM that `max_results` controls the desired aggregated result count, and that pagination is automatic.

## Non-Goals

- Exposing `pageno` or page controls directly to the LLM.
- Adding new search engines for this change.
- Per-engine timeout tuning.
- Changing fetch transport headers/UA as the primary solution path.

## Desired Behavior

### 1. Interface Stability

`web_search` and `search_and_fetch` must keep the current public calling shape:

- `web_search(query, max_results=..., timeout=...)`
- `search_and_fetch(query, max_results=...)`

No LLM-visible pagination argument is added.

### 2. Internal Pagination

`web_search` must internally request paginated SearXNG JSON responses using `pageno`.

The flow is:

1. request page 1
2. extract usable results
3. dedupe by normalized URL
4. if still below `max_results`, request page 2
5. continue until stop condition

### 3. Stop Conditions

Pagination should stop when any of the following becomes true:

- unique usable results count reaches `max_results`
- current page yields zero usable results
- current page yields no new unique URLs compared with previous pages
- an internal page cap is reached

The page cap should be configuration-backed rather than hardcoded where practical.

### 4. Deduping

Deduping must happen before truncation to `max_results`.

At minimum, dedupe by URL string after trimming whitespace. Prefer a small normalization step that avoids obvious duplicates caused by trailing slashes or fragments without becoming overly invasive.

### 5. Error Semantics

Single-page search errors should preserve the current mapped failure messages.

For paginated search:

- if page 1 fails, the whole search fails exactly as today
- if page 1 succeeds but a later page fails, return the results already collected rather than discarding them

This keeps the system robust while preserving useful partial search coverage.

### 6. `search_and_fetch` Behavior

`search_and_fetch` should continue to depend on `web_search` output rather than re-implementing pagination itself.

This ensures one search policy for:

- direct `web_search` usage
- composite `search_and_fetch`

### 7. Prompt Guidance

The active agent prompt must explain:

- `max_results` means the desired final number of aggregated search results
- the backend will automatically paginate SearXNG as needed
- the model should not try to emulate pagination manually
- broad or time-sensitive searches can benefit from `max_results=10..20`

## Configuration

Add a configuration field under `web_tools.searxng` for internal page limiting, for example:

- `max_pages`

This is backend-controlled infrastructure behavior, not an LLM-visible runtime argument.

## Tests

Add or update tests for:

1. search request builder includes `pageno`
2. pagination aggregates across multiple pages
3. duplicate URLs across pages are removed
4. later-page failures still preserve earlier successful results
5. `search_and_fetch` benefits from paginated `web_search`
6. prompt text explains the `max_results` semantics and automatic pagination

## Acceptance Criteria

- `web_search(max_results=20)` can collect results from multiple SearXNG pages.
- `search_and_fetch(max_results=20)` receives those expanded results without changing its public API.
- LLM-facing prompts describe `max_results` correctly and do not expose `pageno`.
- Existing error mapping remains intact for first-page failures.
