# Curl Subprocess Hedging Design

## Goal

Add header-aware hedging to the process-isolated `curl` fetch transport while
preserving the existing `search_and_fetch` pipeline and Crawl4AI fallback.
The curl stage starts one request immediately, starts a second request only
when no usable response headers arrive within three seconds, and gives both
attempts one shared six-second deadline.

This change must also remove the current large-response pipe deadlock. The
existing subprocess helper leaves `stdout=PIPE` unread until curl exits, so a
response larger than the OS pipe buffer can block curl and be misreported as a
timeout.

## Scope

- Add hedging only to the subprocess `curl` transport.
- Keep `curl_cffi` disabled in the active configuration and leave its existing
  hedge implementation unchanged.
- Keep SearXNG streaming result discovery, per-URL fetch concurrency, Crawl4AI
  fallback, snippet fallback, and output formatting unchanged.
- Do not introduce frontend, SSE, agent-memory, prompt, or dependency-source
  changes.
- Do not refactor Crawl4AI into `arun_many()` as part of this change.

## Configuration

Add this field through `config/config-misc.yaml`, `WebToolsConfig`, config
parsing, and `_get_fetch_config()`:

```yaml
fetch_curl_hedge_delay_seconds: 3
```

Existing fields retain these meanings:

- `fetch_curl_timeout_seconds: 6` is the total curl-stage deadline shared by
  both attempts.
- `fetch_curl_cffi_hedge_delay_seconds: 3` applies only to `curl_cffi`.
- `fetch_crawl4ai_timeout_seconds: 15` applies only after the curl stage.
- `search_timeout: 45` remains the outer composite deadline.

The subprocess curl path no longer uses `fetch_retry_count` for a sequential
retry. The hedge is its only second attempt. The existing field remains in the
configuration to keep this change focused; removing it is separate cleanup.

## Request Timeline

For each discovered URL:

1. At T=0, start curl attempt A.
2. Poll process state, complete response-header blocks, abort state, and the
   shared curl deadline without waiting for the process to exit.
3. If A fails at the connection/process level before T=3, start B immediately.
4. If no final usable response headers have arrived by T=3, start B with only
   the time remaining before T=6.
5. Ignore informational 1xx responses and follow redirect 3xx responses.
6. A final 2xx response selects that attempt as the winner. Terminate and reap
   the loser immediately.
7. Any final non-2xx response ends the curl stage immediately and enters
   Crawl4AI. It does not start or wait for another curl attempt.
8. The winner may continue downloading its body, but only until the shared
   T=6 deadline.
9. If no winner completes by T=6, terminate and reap all curl processes, then
   enter Crawl4AI.
10. A completed winner still passes through existing block-page, empty-body,
    extraction, and low-quality checks. Failure enters Crawl4AI without
    reviving the loser or performing a sequential retry.

SearXNG result streaming remains unchanged. With 25 results and enough fetch
workers, initial curl requests still start in parallel as results are found.
A single query can briefly have up to 50 curl subprocesses when every URL
needs a hedge.

## Subprocess I/O

Each attempt uses unique temporary files under `/tmp/opencode` for response
headers, body, stderr, and cookies. Curl writes the body directly to its body
file, so large responses cannot fill an unread stdout pipe. Standard output
contains only the bounded final write-out metadata and is read after process
exit; stderr is also file-backed.

The header poller parses complete HTTP header blocks as curl follows redirects.
It must not mistake proxy CONNECT acknowledgement headers for the final origin
response. Only a final origin 2xx block is usable.

All temporary files are deleted after process cleanup, including exceptions,
abort, timeout, non-2xx responses, and loser cancellation.

## Cookie Handling

Both attempts begin from independent snapshots of the stable per-host cookie
jar. They never write the same cookie file concurrently.

Only a winner that completes and passes existing response-quality validation
may publish its cookie file. Publication uses a per-host lock and atomic
replacement so concurrent URLs cannot corrupt the stable jar. Loser and failed
attempt cookies are discarded.

The curl response carries an internal winner-workspace reference into the
existing validation block. Immediately before `_fetch_webpage_content()`
returns validated text, it commits that workspace's cookie candidate. A
`finally` block discards the workspace on every fallback or exception path.
The internal workspace reference never enters tool output or model context.

## Abort And Cleanup

The hedge coordinator checks `check_abort` throughout polling. On abort it
terminates every active curl process and raises `AbortError`; it must not enter
Crawl4AI.

Every exit path uses the same cleanup sequence:

1. Send terminate to active processes.
2. Wait for a bounded two-second grace period.
3. Kill processes that did not exit.
4. Wait for all process handles to reach a terminal state.
5. Close handles and remove temporary files.

No curl process or helper worker may survive after the tool returns. This
preserves the project's Stop/Answer Now and native-worker lifecycle contracts.

## Error Semantics

- Final 2xx headers: select the attempt and continue its body download.
- Final non-2xx headers: stop curl and enter Crawl4AI immediately.
- Connection/process failure before T=3: launch B immediately.
- Both attempts fail: enter Crawl4AI immediately.
- Shared six-second deadline: terminate both and enter Crawl4AI.
- User/session abort: terminate both and propagate `AbortError` without
  fallback.
- Completed but blocked, empty, or low-quality body: enter Crawl4AI through
  existing validation.

The model-visible result format and success/timeout/snippet accounting remain
unchanged.

## Tests

Use a local HTTP server and deterministic process timing rather than external
websites for automated assertions.

Required regression coverage:

1. A body larger than the OS pipe buffer completes without false timeout.
2. A final 2xx header before T=3 causes exactly one curl request.
3. No usable headers by T=3 starts exactly one hedge request.
4. A connection failure before T=3 starts the hedge immediately.
5. Either attempt can win after T=3, and the loser is terminated and reaped.
6. A final non-2xx response enters Crawl4AI without starting or waiting for a
   second curl request.
7. Both attempts share one six-second deadline rather than receiving six
   seconds each.
8. A winner whose body does not finish by T=6 enters Crawl4AI.
9. Abort terminates and reaps all active curl processes and does not execute
   Crawl4AI.
10. Winner cookies publish atomically; loser and failed cookies do not.
11. Curl no longer performs the old sequential retry.
12. Configuration parsing and defaults expose
    `fetch_curl_hedge_delay_seconds`.

Run the existing web fetch, search-and-fetch, timeout, worker-shutdown, and
configuration suites as regression coverage. A real query may be used for a
manual diagnostic comparison, but external result counts are not stable test
assertions.

## Success Criteria

- The active curl path follows the approved T=0/T=3/T=6 timeline.
- Response headers, not full-body completion, control whether B starts.
- Final non-2xx responses switch to Crawl4AI immediately.
- Large curl bodies no longer deadlock on an unread pipe.
- No loser, timeout, or aborted curl process survives the tool call.
- Existing search output shape and fallback behavior remain compatible.
