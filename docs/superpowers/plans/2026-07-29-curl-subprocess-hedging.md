# Curl Subprocess Hedging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add response-header-aware T=0/T=3/T=6 hedging to the active subprocess curl transport, eliminate large-body pipe deadlocks, and preserve clean Crawl4AI fallback and process shutdown.

**Architecture:** Keep the existing SearXNG-to-fetch pipeline and implement hedging inside `backend/tools/web_tools.py`. Each curl attempt writes headers, body, and cookies to an isolated workspace; a two-future coordinator polls complete header blocks, selects the first final 2xx response, and cancels/reaps the loser under one shared six-second deadline. Existing response validation remains authoritative, and only a validated winner publishes cookies.

**Tech Stack:** Python 3.12, `subprocess`, `ThreadPoolExecutor`, `threading.Event`, `tempfile`, FastAPI backend configuration dataclasses, pytest, local `ThreadingHTTPServer`, system curl 8.x.

## Global Constraints

- Do not run or include git commands; the user explicitly declined git usage for this work.
- Do not modify dependency source under `.sglang`.
- Keep active `fetch_transport: curl`; do not re-enable `curl_cffi`.
- `fetch_curl_timeout_seconds: 6` is one shared curl-stage deadline, not six seconds per attempt.
- Add `fetch_curl_hedge_delay_seconds: 3` for subprocess curl only.
- Final origin 2xx headers select a winner; any final non-2xx response switches to Crawl4AI immediately.
- Curl hedging replaces the old sequential curl retry.
- Stop/abort must terminate and reap all active curl subprocesses before returning.
- Keep frontend/SSE, agent memory, model prompts, result formatting, Crawl4AI behavior, and prefix-cache behavior unchanged.
- Keep implementation in the existing `backend/tools/web_tools.py`; do not mix in an `arun_many()` Crawl4AI refactor.

---

## File Structure

- Modify `backend/config.py`: expose and parse `fetch_curl_hedge_delay_seconds`.
- Modify `config/config-misc.yaml`: set the active three-second curl hedge delay and clarify retry semantics.
- Modify `backend/tools/web_tools.py`: add isolated curl workspaces, header parsing, hedge coordination, cookie publication, and no-retry integration.
- Modify `tests/test_web_tools_config.py`: lock configuration parsing and active YAML values.
- Modify `tests/test_web_fetch.py`: cover large bodies, header timing, status handling, deadlines, cookies, fallback, and abort cleanup.
- Reuse `tests/test_search_and_fetch.py`: verify existing composite deadline and worker-lifecycle contracts remain green.

---

### Task 1: Curl Hedge Configuration Contract

**Files:**
- Modify: `backend/config.py:461-482,1523-1575`
- Modify: `backend/tools/web_tools.py:97-149`
- Modify: `config/config-misc.yaml:311-330`
- Test: `tests/test_web_tools_config.py`
- Test: `tests/test_web_fetch.py`

**Interfaces:**
- Produces: `WebToolsConfig.fetch_curl_hedge_delay_seconds: int` with default `3`.
- Produces: `_get_fetch_config()["fetch_curl_hedge_delay_seconds"] -> int`.
- Consumes: existing `fetch_curl_timeout_seconds` as the total stage budget.

- [ ] **Step 1: Add failing configuration parser assertions**

Extend `test_parse_nested_searxng_config_and_fetch_settings`:

```python
"fetch_curl_timeout_seconds": 7,
"fetch_curl_hedge_delay_seconds": 2,
"fetch_curl_cffi_timeout_seconds": 6,
```

and assert:

```python
assert cfg.fetch_curl_timeout_seconds == 7
assert cfg.fetch_curl_hedge_delay_seconds == 2
assert cfg.fetch_curl_cffi_timeout_seconds == 6
```

Extend `test_explicit_null_non_optional_fields_fall_back_to_defaults`:

```python
assert cfg.fetch_curl_hedge_delay_seconds == 3
```

Extend `test_config_misc_uses_searxng_provider_block`:

```python
assert web_tools["fetch_curl_hedge_delay_seconds"] == 3
```

- [ ] **Step 2: Run the parser tests and verify RED**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_web_tools_config.py::TestParseWebToolsConfig::test_parse_nested_searxng_config_and_fetch_settings \
  tests/test_web_tools_config.py::TestParseWebToolsConfig::test_explicit_null_non_optional_fields_fall_back_to_defaults \
  tests/test_web_tools_config.py::TestParseWebToolsConfig::test_config_misc_uses_searxng_provider_block -q
```

Expected: FAIL because `fetch_curl_hedge_delay_seconds` is absent.

- [ ] **Step 3: Add the configuration field and parser**

Add beside the curl timeout field in `WebToolsConfig`:

```python
fetch_curl_timeout_seconds: int = 6
fetch_curl_hedge_delay_seconds: int = 3
fetch_curl_cffi_timeout_seconds: int = 6
```

Add beside `fetch_curl_timeout_seconds` in `_parse_web_tools_config()`:

```python
fetch_curl_hedge_delay_seconds=(
    3
    if self._get_int("web_tools", "fetch_curl_hedge_delay_seconds", 3) is None
    else self._get_int("web_tools", "fetch_curl_hedge_delay_seconds", 3)
),
```

Add to both success and fallback dictionaries in `_get_fetch_config()`:

```python
"fetch_curl_hedge_delay_seconds": web_tools.fetch_curl_hedge_delay_seconds,
```

and:

```python
"fetch_curl_hedge_delay_seconds": 3,
```

- [ ] **Step 4: Add the active YAML value**

Place directly after `fetch_curl_timeout_seconds`:

```yaml
  # curl 第 2 个 hedged 请求的启动延迟；两个请求共享上面的 6 秒总预算。
  fetch_curl_hedge_delay_seconds: 3
```

Revise the generic retry comment so it does not claim to control curl:

```yaml
  # 保留的通用重试配置；curl/curl_cffi 使用 hedge，Crawl4AI 使用独立 retry_count。
  fetch_retry_count: 1
```

- [ ] **Step 5: Extend `_get_fetch_config()` coverage and verify GREEN**

Add `fetch_curl_hedge_delay_seconds = 3` to the test `WebTools` fake and assert:

```python
assert cfg["fetch_curl_hedge_delay_seconds"] == 3
```

Run:

```bash
./.sglang/bin/python -m pytest tests/test_web_tools_config.py tests/test_web_fetch.py::TestWebFetchRobustness::test_get_fetch_config_includes_crawl4ai_defaults -q
```

Expected: all selected tests PASS.

---

### Task 2: File-Backed Curl Attempt And Header Parser

**Files:**
- Modify: `backend/tools/web_tools.py:5-16,385-538`
- Test: `tests/test_web_fetch.py`

**Interfaces:**
- Produces: `_CurlAttemptWorkspace` with per-attempt header, body, stderr, and cookie paths.
- Produces: `_parse_latest_curl_header_state(path: str) -> tuple[str, Optional[int]]`, where state is `pending`, `success`, or `failure`.
- Produces: `_fetch_with_curl(url, timeout, proxy, headers, cookie_jar_path, check_abort=None, header_path=None, body_path=None, stderr_path=None, cookie_read_path=None, cookie_write_path=None) -> dict`.
- Preserves: `_fetch_with_curl()` response keys `status_code`, `final_url`, `content_type`, `body`, `transport`, and `profile`.

- [ ] **Step 1: Add a local HTTP test server helper**

Add imports to `tests/test_web_fetch.py`:

```python
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from unittest.mock import MagicMock, patch
```

Add a context helper near the top of the file:

```python
class _FetchTestHandler(BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        handler = self.routes[self.path]
        handler(self)

    def log_message(self, format, *args):
        return


class _LocalFetchServer:
    def __init__(self, routes):
        handler = type("ConfiguredFetchHandler", (_FetchTestHandler,), {"routes": routes})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
```

- [ ] **Step 2: Add a failing large-body regression test**

```python
def test_curl_large_body_does_not_block_on_stdout_pipe(tmp_path):
    from backend.tools.web_tools import _fetch_with_curl

    body = b"x" * 1_000_000

    def large_response(request):
        request.send_response(200)
        request.send_header("Content-Type", "text/plain")
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    with _LocalFetchServer({"/large": large_response}) as base_url:
        response = _fetch_with_curl(
            url=f"{base_url}/large",
            timeout=2,
            proxy=None,
            headers={},
            cookie_jar_path=str(tmp_path / "cookies.txt"),
        )

    assert len(response["body"]) == len(body)
```

- [ ] **Step 3: Run the large-body test and verify RED**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_web_fetch.py::TestWebFetchRobustness::test_curl_large_body_does_not_block_on_stdout_pipe -q
```

Expected: FAIL with `TimeoutError: request timed out` because the current parent does not drain the body pipe.

- [ ] **Step 4: Add complete-header parser tests**

```python
def test_parse_latest_curl_header_state_waits_through_redirect(tmp_path):
    from backend.tools.web_tools import _parse_latest_curl_header_state

    path = tmp_path / "headers.txt"
    path.write_text(
        "HTTP/1.1 302 Found\r\nLocation: /final\r\n\r\n",
        encoding="iso-8859-1",
    )
    assert _parse_latest_curl_header_state(str(path)) == ("pending", 302)

    path.write_text(
        "HTTP/1.1 302 Found\r\nLocation: /final\r\n\r\n"
        "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n",
        encoding="iso-8859-1",
    )
    assert _parse_latest_curl_header_state(str(path)) == ("success", 200)


def test_parse_latest_curl_header_state_rejects_final_non_2xx(tmp_path):
    from backend.tools.web_tools import _parse_latest_curl_header_state

    path = tmp_path / "headers.txt"
    path.write_text("HTTP/1.1 404 Not Found\r\n\r\n", encoding="iso-8859-1")
    assert _parse_latest_curl_header_state(str(path)) == ("failure", 404)


def test_parse_latest_curl_header_state_ignores_partial_header_block(tmp_path):
    from backend.tools.web_tools import _parse_latest_curl_header_state

    path = tmp_path / "headers.txt"
    path.write_text(
        "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n",
        encoding="iso-8859-1",
    )
    assert _parse_latest_curl_header_state(str(path)) == ("pending", None)


def test_parse_latest_curl_header_state_ignores_proxy_connect(tmp_path):
    from backend.tools.web_tools import _parse_latest_curl_header_state

    path = tmp_path / "headers.txt"
    path.write_text(
        "HTTP/1.1 200 Connection established\r\n\r\n",
        encoding="iso-8859-1",
    )
    assert _parse_latest_curl_header_state(str(path)) == ("pending", None)
```

- [ ] **Step 5: Implement isolated paths and header parsing**

Add imports:

```python
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
```

Add the workspace and parser before `_build_curl_fetch_args`:

```python
@dataclass
class _CurlAttemptWorkspace:
    root: str
    header_path: str
    body_path: str
    stderr_path: str
    cookie_path: str

    @classmethod
    def create(cls, stable_cookie_path: Optional[str], attempt_name: str):
        root = tempfile.mkdtemp(prefix=f"curl-{attempt_name}-", dir="/tmp/opencode")
        workspace = cls(
            root=root,
            header_path=os.path.join(root, "headers.txt"),
            body_path=os.path.join(root, "body.bin"),
            stderr_path=os.path.join(root, "stderr.txt"),
            cookie_path=os.path.join(root, "cookies.txt"),
        )
        if stable_cookie_path and os.path.exists(stable_cookie_path):
            shutil.copy2(stable_cookie_path, workspace.cookie_path)
        else:
            Path(workspace.cookie_path).touch()
        return workspace

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def _parse_latest_curl_header_state(path: str) -> tuple[str, Optional[int]]:
    try:
        raw = Path(path).read_text(encoding="iso-8859-1")
    except (FileNotFoundError, OSError):
        return "pending", None

    blocks = re.split(r"\r?\n\r?\n", raw)[:-1]
    latest_status = None
    for block in blocks:
        match = re.search(r"^HTTP/\S+\s+(\d{3})\s*([^\r\n]*)", block, re.MULTILINE)
        if not match:
            continue
        status = int(match.group(1))
        reason = match.group(2).strip().lower()
        if status == 200 and reason == "connection established":
            continue
        if 100 <= status < 200:
            continue
        latest_status = status
        if 300 <= status < 400 and re.search(r"^Location:", block, re.MULTILINE | re.IGNORECASE):
            continue
        return ("success", status) if 200 <= status < 300 else ("failure", status)
    return "pending", latest_status
```

- [ ] **Step 6: Make curl body and headers file-backed**

Extend `_build_curl_fetch_args()` with optional `header_path`, `body_path`, `cookie_read_path`, and `cookie_write_path`. Add:

```python
if header_path:
    curl_args.extend(["--dump-header", header_path])
if body_path:
    curl_args.extend(["--output", body_path])
if cookie_read_path:
    curl_args.extend(["-b", cookie_read_path])
if cookie_write_path:
    curl_args.extend(["-c", cookie_write_path])
```

Do not add the old combined `-b/-c` pair when separate cookie paths are supplied.

Extend `_fetch_with_curl()` with optional attempt paths, including `stderr_path`. When paths are absent, create one `_CurlAttemptWorkspace`; when supplied, use the caller workspace. Pass `stderr_path` to `_run_subprocess_with_abort()`. After curl exits, read `body_path` as bytes and decode with `utf-8`, `errors="replace"`, then pass the text to an extended parser:

```python
def _parse_curl_fetch_output(stdout: str, requested_url: str, body: Optional[str] = None) -> dict:
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
```

Ensure locally-created workspaces are cleaned in `finally` after body content has been copied into the response dictionary.

- [ ] **Step 7: Drain bounded process output while polling**

Extend `_run_subprocess_with_abort()` with `stderr_path: Optional[str] = None`. Open that path in `w+` mode when supplied, pass the handle through the `stderr` keyword shown below, and otherwise retain `subprocess.PIPE`. Replace the poll-without-read loop with repeated bounded `communicate()` calls:

```python
stderr_file = open(stderr_path, "w+", encoding="utf-8") if stderr_path else None
try:
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=stderr_file or subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    started_at = time.monotonic()
    while True:
        if check_abort and check_abort():
            _terminate_and_reap_process(proc)
            raise AbortError(abort_message)
        remaining = timeout - (time.monotonic() - started_at)
        if remaining <= 0:
            _terminate_and_reap_process(proc)
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
finally:
    if stderr_file:
        stderr_file.close()
```

Add one shared helper used here and by the hedge coordinator:

```python
def _terminate_and_reap_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        proc.wait()
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
```

- [ ] **Step 8: Run file-I/O and parser tests and verify GREEN**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_web_fetch.py -k "large_body or latest_curl_header_state or build_curl_fetch_args" -q
```

Expected: all selected tests PASS.

---

### Task 3: Header-Aware Curl Hedge Coordinator

**Files:**
- Modify: `backend/tools/web_tools.py:505-658`
- Test: `tests/test_web_fetch.py`

**Interfaces:**
- Consumes: `_CurlAttemptWorkspace.create(stable_cookie_path, attempt_name)` from Task 2.
- Consumes: `_parse_latest_curl_header_state(path)` from Task 2.
- Produces: `_fetch_with_curl_hedged(url, timeout, proxy, cfg, headers, cookie_jar_path, check_abort=None) -> dict`.
- Produces: response-private `_curl_hedge_workspace` and `_curl_cookie_candidate` keys for Task 4.

- [ ] **Step 1: Add deterministic hedge timing tests**

Use `threading.Lock()` and a request counter so the first request delays headers while the second responds immediately:

```python
def test_curl_hedge_starts_second_request_after_header_delay(tmp_path):
    from backend.tools.web_tools import _fetch_with_curl_hedged

    lock = threading.Lock()
    request_count = 0

    def hedged_response(request):
        nonlocal request_count
        with lock:
            request_count += 1
            index = request_count
        if index == 1:
            time.sleep(0.35)
        body = f"attempt-{index}".encode()
        request.send_response(200)
        request.send_header("Content-Type", "text/plain")
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    cfg = {"fetch_curl_hedge_delay_seconds": 0.15}
    with _LocalFetchServer({"/hedge": hedged_response}) as base_url:
        response = _fetch_with_curl_hedged(
            url=f"{base_url}/hedge",
            timeout=1,
            proxy=None,
            cfg=cfg,
            headers={},
            cookie_jar_path=str(tmp_path / "cookies.txt"),
        )

    assert request_count == 2
    assert response["body"] == "attempt-2"
```

Add a one-request test where headers arrive before the hedge delay:

```python
def test_curl_hedge_does_not_duplicate_after_timely_2xx_headers(tmp_path):
    from backend.tools.web_tools import _fetch_with_curl_hedged

    request_count = 0

    def timely_response(request):
        nonlocal request_count
        request_count += 1
        body = b"ok"
        request.send_response(200)
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        time.sleep(0.2)
        request.wfile.write(body)

    with _LocalFetchServer({"/timely": timely_response}) as base_url:
        _fetch_with_curl_hedged(
            f"{base_url}/timely", 1, None,
            {"fetch_curl_hedge_delay_seconds": 0.15}, {},
            str(tmp_path / "cookies.txt"),
        )

    assert request_count == 1


@patch("backend.tools.web_tools._fetch_with_curl")
def test_curl_hedge_starts_second_attempt_immediately_after_connection_failure(
    mock_fetch_curl, tmp_path
):
    from backend.tools.web_tools import _fetch_with_curl_hedged

    mock_fetch_curl.side_effect = [
        RuntimeError("connection refused"),
        {
            "status_code": 200,
            "final_url": "https://example.com/article",
            "content_type": "text/plain",
            "body": "recovered body",
            "transport": "curl",
            "profile": "desktop",
        },
    ]

    started = time.monotonic()
    response = _fetch_with_curl_hedged(
        "https://example.com/article",
        1,
        None,
        {"fetch_curl_hedge_delay_seconds": 0.8},
        {},
        str(tmp_path / "cookies.txt"),
    )

    assert response["body"] == "recovered body"
    assert mock_fetch_curl.call_count == 2
    assert time.monotonic() - started < 0.5
```

- [ ] **Step 2: Add non-2xx and shared-deadline tests**

```python
def test_curl_hedge_rejects_final_non_2xx_without_second_request(tmp_path):
    from backend.tools.web_tools import _fetch_with_curl_hedged

    request_count = 0

    def not_found(request):
        nonlocal request_count
        request_count += 1
        request.send_response(404)
        request.end_headers()

    with _LocalFetchServer({"/missing": not_found}) as base_url:
        with pytest.raises(RuntimeError, match="HTTP 404"):
            _fetch_with_curl_hedged(
                f"{base_url}/missing", 1, None,
                {"fetch_curl_hedge_delay_seconds": 0.15}, {},
                str(tmp_path / "cookies.txt"),
            )

    assert request_count == 1


def test_curl_hedge_attempts_share_one_deadline(tmp_path):
    from backend.tools.web_tools import _fetch_with_curl_hedged

    def no_headers(request):
        time.sleep(1)

    started = time.monotonic()
    with _LocalFetchServer({"/slow": no_headers}) as base_url:
        with pytest.raises(TimeoutError):
            _fetch_with_curl_hedged(
                f"{base_url}/slow", 0.4, None,
                {"fetch_curl_hedge_delay_seconds": 0.2}, {},
                str(tmp_path / "cookies.txt"),
            )
    elapsed = time.monotonic() - started

    assert elapsed < 0.7


def test_curl_hedge_times_out_winner_body_without_starting_second_request(tmp_path):
    from backend.tools.web_tools import _fetch_with_curl_hedged

    request_count = 0

    def headers_then_slow_body(request):
        nonlocal request_count
        request_count += 1
        body = b"slow body"
        request.send_response(200)
        request.send_header("Content-Length", str(len(body)))
        request.end_headers()
        time.sleep(1)
        request.wfile.write(body)

    started = time.monotonic()
    with _LocalFetchServer({"/slow-body": headers_then_slow_body}) as base_url:
        with pytest.raises(TimeoutError):
            _fetch_with_curl_hedged(
                f"{base_url}/slow-body",
                0.4,
                None,
                {"fetch_curl_hedge_delay_seconds": 0.2},
                {},
                str(tmp_path / "cookies.txt"),
            )

    assert request_count == 1
    assert time.monotonic() - started < 0.7
```

- [ ] **Step 3: Run hedge tests and verify RED**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_web_fetch.py -k "curl_hedge" -q
```

Expected: FAIL because `_fetch_with_curl_hedged` does not exist.

- [ ] **Step 4: Implement the coordinator**

Add a coordinator that normalizes the configured delay and owns both attempt cancellation events:

```python
def _fetch_with_curl_hedged(
    url: str,
    timeout: int,
    proxy: Optional[str],
    cfg: dict,
    headers: dict[str, str],
    cookie_jar_path: Optional[str],
    check_abort: Optional[Callable] = None,
) -> dict:
    total_timeout = max(0.1, float(timeout or 0.1))
    try:
        hedge_delay = float(cfg.get("fetch_curl_hedge_delay_seconds") or 0)
    except (TypeError, ValueError):
        hedge_delay = 0.0
    hedge_delay = min(max(0.0, hedge_delay), total_timeout)
    started_at = time.monotonic()
    deadline = started_at + total_timeout
    workspaces = [
        _CurlAttemptWorkspace.create(cookie_jar_path, "a"),
        _CurlAttemptWorkspace.create(cookie_jar_path, "b"),
    ]
    cancel_events = [threading.Event(), threading.Event()]
    futures = {}
    handled_done = set()
    errors = {}
    winner_index = None
    handed_off_workspace = False

    def combined_abort(index):
        return cancel_events[index].is_set() or bool(check_abort and check_abort())

    def submit(pool, index):
        remaining = max(0.1, deadline - time.monotonic())
        futures[index] = pool.submit(
            _fetch_with_curl,
            url=url,
            timeout=remaining,
            proxy=proxy,
            headers=headers,
            cookie_jar_path=workspaces[index].cookie_path,
            check_abort=lambda: combined_abort(index),
            header_path=workspaces[index].header_path,
            body_path=workspaces[index].body_path,
            stderr_path=workspaces[index].stderr_path,
            cookie_read_path=workspaces[index].cookie_path,
            cookie_write_path=workspaces[index].cookie_path,
        )

    pool = ThreadPoolExecutor(max_workers=2)
    try:
        submit(pool, 0)
        while True:
            if check_abort and check_abort():
                raise AbortError("Fetch aborted by user")

            now = time.monotonic()
            for index, future in list(futures.items()):
                state, status = _parse_latest_curl_header_state(workspaces[index].header_path)
                if state == "failure":
                    raise RuntimeError(f"HTTP {status} returned for {url}")
                if state == "success" and winner_index is None:
                    winner_index = index
                    for other in futures:
                        if other != index:
                            cancel_events[other].set()

                if future.done() and winner_index is None and index not in handled_done:
                    handled_done.add(index)
                    try:
                        response = future.result()
                    except AbortError as exc:
                        if cancel_events[index].is_set():
                            continue
                        errors[index] = exc
                    except Exception as exc:
                        errors[index] = exc
                    else:
                        status_code = int(response.get("status_code") or 0)
                        if not 200 <= status_code < 300:
                            raise RuntimeError(f"HTTP {status_code} returned for {url}")
                        winner_index = index
                        for other in futures:
                            if other != index:
                                cancel_events[other].set()

                    if index in errors:
                        if 1 not in futures:
                            submit(pool, 1)
                            continue
                        if all(item.done() for item in futures.values()) and all(
                            item in handled_done for item in futures
                        ):
                            raise errors.get(1) or errors[0]

            if winner_index is not None and futures[winner_index].done():
                response = futures[winner_index].result()
                response["_curl_hedge_workspace"] = workspaces[winner_index]
                response["_curl_cookie_candidate"] = workspaces[winner_index].cookie_path
                handed_off_workspace = True
                return response

            if 1 not in futures and winner_index is None and now - started_at >= hedge_delay:
                submit(pool, 1)

            if now >= deadline:
                raise TimeoutError(f"Request timed out for {url}")
            time.sleep(min(0.05, max(0.0, deadline - now)))
    finally:
        for event in cancel_events:
            event.set()
        pool.shutdown(wait=True, cancel_futures=True)
        for index, workspace in enumerate(workspaces):
            if not handed_off_workspace or index != winner_index:
                workspace.cleanup()
```

- [ ] **Step 5: Add abort cleanup coverage**

Use a local endpoint that never sends headers, trigger abort after both attempts start, and wrap the real process-reaping helper:

```python
@patch("backend.tools.web_tools._get_fetch_config")
@patch("backend.tools.web_tools._fetch_with_crawl4ai")
def test_web_fetch_abort_reaps_all_curl_hedge_processes(
    mock_crawl4ai, mock_get_fetch_config
):
    from backend.tools import web_tools

    mock_get_fetch_config.return_value = {
        "proxy": None,
        "fetch_transport": "curl",
        "fetch_fallback_transport": "crawl4ai",
        "fetch_impersonate": "chrome",
        "fetch_retry_count": 1,
        "fetch_retry_backoff_seconds": 1.0,
        "fetch_min_text_chars": 120,
        "fetch_enable_readability_fallback": True,
        "fetch_block_markers": None,
        "fetch_curl_timeout_seconds": 2,
        "fetch_curl_hedge_delay_seconds": 0.1,
        "fetch_crawl4ai_timeout_seconds": 1,
        "crawl4ai_retry_count": 0,
    }
    abort_event = threading.Event()
    reaped = []
    original_reap = web_tools._terminate_and_reap_process

    def no_headers(request):
        time.sleep(2)

    def traced_reap(proc):
        original_reap(proc)
        reaped.append(proc)

    timer = threading.Timer(0.3, abort_event.set)
    with _LocalFetchServer({"/abort": no_headers}) as base_url, patch(
        "backend.tools.web_tools._terminate_and_reap_process",
        side_effect=traced_reap,
    ):
        timer.start()
        with pytest.raises(web_tools.AbortError):
            web_tools._fetch_webpage_content(
                f"{base_url}/abort",
                timeout=3,
                check_abort=abort_event.is_set,
            )
        timer.join(timeout=1)

    assert len(reaped) == 2
    assert all(proc.poll() is not None for proc in reaped)
    mock_crawl4ai.assert_not_called()
```

- [ ] **Step 6: Run coordinator tests and verify GREEN**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_web_fetch.py -k "curl_hedge" -q
```

Expected: all selected tests PASS with no live curl child left after each test.

---

### Task 4: Validation, Cookie Publication, And Fallback Integration

**Files:**
- Modify: `backend/tools/web_tools.py:385-393,1067-1208`
- Test: `tests/test_web_fetch.py`
- Test: `tests/test_search_and_fetch.py`

**Interfaces:**
- Consumes: `_fetch_with_curl_hedged(url, timeout, proxy, cfg, headers, cookie_jar_path, check_abort=None)` from Task 3.
- Produces: `_publish_curl_cookie_candidate(candidate: str, stable: str) -> None`.
- Preserves: `_fetch_webpage_content(url, timeout=30, check_abort=None) -> str` and all model-visible output.

- [ ] **Step 1: Add failing fallback integration test**

Replace the current curl-specific timeout test with hedge-level expectations:

```python
@patch("backend.tools.web_tools._get_fetch_config")
@patch("backend.tools.web_tools._fetch_with_crawl4ai")
@patch("backend.tools.web_tools._fetch_with_curl_hedged")
def test_web_fetch_uses_curl_hedge_before_crawl4ai(
    self, mock_curl_hedged, mock_crawl4ai, mock_get_fetch_config
):
    from backend.tools.web_tools import web_fetch

    mock_get_fetch_config.return_value = {
        "proxy": None,
        "fetch_transport": "curl",
        "fetch_fallback_transport": "crawl4ai",
        "fetch_impersonate": "chrome",
        "fetch_retry_count": 1,
        "fetch_retry_backoff_seconds": 1.0,
        "fetch_min_text_chars": 120,
        "fetch_enable_readability_fallback": True,
        "fetch_block_markers": None,
        "fetch_curl_timeout_seconds": 6,
        "fetch_curl_hedge_delay_seconds": 3,
        "fetch_crawl4ai_timeout_seconds": 15,
        "crawl4ai_retry_count": 0,
    }
    mock_curl_hedged.side_effect = TimeoutError("curl hedge timed out")
    mock_crawl4ai.side_effect = TimeoutError("crawl4ai timed out")

    result = web_fetch("https://example.com/article", timeout=45)

    assert "Failed to fetch webpage" in result
    mock_curl_hedged.assert_called_once()
    assert mock_curl_hedged.call_args.kwargs["timeout"] == 6
    mock_crawl4ai.assert_called_once()
    assert mock_crawl4ai.call_args.kwargs["timeout"] == 15
```

- [ ] **Step 2: Add failing no-sequential-retry assertion**

```python
def test_curl_transport_retry_count_is_zero_when_hedging_is_enabled():
    from backend.tools.web_tools import _resolve_transport_retry_count

    assert _resolve_transport_retry_count("curl", {}, 3) == 0
    assert _resolve_transport_retry_count("curl_cffi", {}, 3) == 0
```

- [ ] **Step 3: Run integration tests and verify RED**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_web_fetch.py -k "curl_hedge_before or retry_count_is_zero" -q
```

Expected: FAIL because the transport loop still calls `_fetch_with_curl` and curl still inherits the generic retry count.

- [ ] **Step 4: Route curl through the hedge coordinator**

Change `_resolve_transport_retry_count()`:

```python
if transport in {"curl", "curl_cffi"}:
    return 0
```

Replace the curl branch in `_fetch_webpage_content()`:

```python
elif transport == "curl":
    response = _fetch_with_curl_hedged(
        url=url,
        timeout=transport_timeout,
        proxy=proxy,
        cfg=cfg,
        headers=headers,
        cookie_jar_path=cookie_jar_path,
        check_abort=check_abort,
    )
else:
    response = _fetch_with_curl(
        url=url,
        timeout=transport_timeout,
        proxy=proxy,
        headers=headers,
        cookie_jar_path=cookie_jar_path,
        check_abort=check_abort,
    )
```

Retain the final `else` only for compatibility with a resolved non-special curl-like transport; the active `curl` path must use the hedge branch.

- [ ] **Step 5: Add atomic cookie publication**

Add process-wide cookie locks near the existing cookie helpers:

```python
_cookie_jar_locks_guard = threading.Lock()
_cookie_jar_locks: dict[str, threading.Lock] = {}


def _get_cookie_jar_lock(path: str) -> threading.Lock:
    with _cookie_jar_locks_guard:
        return _cookie_jar_locks.setdefault(path, threading.Lock())


def _publish_curl_cookie_candidate(candidate: str, stable: str) -> None:
    os.makedirs(os.path.dirname(stable), exist_ok=True)
    staged = f"{stable}.{threading.get_ident()}.tmp"
    with _get_cookie_jar_lock(stable):
        shutil.copy2(candidate, staged)
        os.replace(staged, stable)
```

Wrap response validation so private workspace state is always removed and cleaned:

```python
curl_workspace = response.pop("_curl_hedge_workspace", None)
curl_cookie_candidate = response.pop("_curl_cookie_candidate", None)
try:
    blocked = _looks_like_block_page(
        int(response.get("status_code") or 0),
        response.get("content_type", ""),
        response.get("body", ""),
        block_markers,
    )
    extracted_text = _extract_response_text(
        response.get("body", ""),
        response.get("content_type", ""),
        min_text_chars,
        enable_readability_fallback,
    )
    if blocked:
        raise RuntimeError("blocked or challenge page")
    if not extracted_text:
        raise RuntimeError("empty extracted content")
    if _looks_like_low_quality_text(extracted_text):
        raise RuntimeError("low quality extracted content")
    extracted_text = _truncate_extracted_text(extracted_text)
    final_url = response.get("final_url") or url
    if curl_workspace and curl_cookie_candidate and cookie_jar_path:
        _publish_curl_cookie_candidate(curl_cookie_candidate, cookie_jar_path)
    return _format_fetch_output(final_url, extracted_text)
finally:
    if curl_workspace is not None:
        curl_workspace.cleanup()
```

Place publication only after all existing validation checks and immediately before the successful return.

- [ ] **Step 6: Add cookie winner/loser tests**

Add a shared complete config fixture in the test module:

```python
def _curl_hedge_fetch_config():
    return {
        "proxy": None,
        "fetch_transport": "curl",
        "fetch_fallback_transport": "crawl4ai",
        "fetch_impersonate": "chrome",
        "fetch_retry_count": 1,
        "fetch_retry_backoff_seconds": 1.0,
        "fetch_min_text_chars": 120,
        "fetch_enable_readability_fallback": True,
        "fetch_block_markers": None,
        "fetch_curl_timeout_seconds": 6,
        "fetch_curl_hedge_delay_seconds": 3,
        "fetch_crawl4ai_timeout_seconds": 15,
        "crawl4ai_retry_count": 0,
    }
```

Add validated-winner publication coverage:

```python
@patch("backend.tools.web_tools._publish_curl_cookie_candidate")
@patch("backend.tools.web_tools._fetch_with_curl_hedged")
@patch("backend.tools.web_tools._get_fetch_config")
def test_validated_curl_winner_publishes_cookie_and_cleans_workspace(
    mock_get_fetch_config, mock_curl_hedged, mock_publish
):
    from backend.tools.web_tools import _build_cookie_jar_path, _fetch_webpage_content

    workspace = MagicMock()
    candidate = "/tmp/opencode/winner-cookies.txt"
    url = "https://example.com/article"
    mock_get_fetch_config.return_value = _curl_hedge_fetch_config()
    mock_curl_hedged.return_value = {
        "status_code": 200,
        "final_url": url,
        "content_type": "text/plain",
        "body": "validated article body " * 80,
        "transport": "curl",
        "profile": "desktop",
        "_curl_hedge_workspace": workspace,
        "_curl_cookie_candidate": candidate,
    }

    result = _fetch_webpage_content(url, timeout=45)

    assert "validated article body" in result
    mock_publish.assert_called_once_with(candidate, _build_cookie_jar_path(url))
    workspace.cleanup.assert_called_once_with()
```

Add rejected-winner cleanup and fallback coverage:

```python
@patch("backend.tools.web_tools._publish_curl_cookie_candidate")
@patch("backend.tools.web_tools._fetch_with_crawl4ai")
@patch("backend.tools.web_tools._fetch_with_curl_hedged")
@patch("backend.tools.web_tools._get_fetch_config")
def test_rejected_curl_winner_discards_cookie_before_crawl4ai(
    mock_get_fetch_config, mock_curl_hedged, mock_crawl4ai, mock_publish
):
    from backend.tools.web_tools import _fetch_webpage_content

    workspace = MagicMock()
    mock_get_fetch_config.return_value = _curl_hedge_fetch_config()
    mock_curl_hedged.return_value = {
        "status_code": 200,
        "final_url": "https://example.com/article",
        "content_type": "text/plain",
        "body": "short",
        "transport": "curl",
        "profile": "desktop",
        "_curl_hedge_workspace": workspace,
        "_curl_cookie_candidate": "/tmp/opencode/rejected-cookies.txt",
    }
    mock_crawl4ai.return_value = {
        "status_code": 200,
        "final_url": "https://example.com/article",
        "content_type": "text/markdown",
        "body": "recovered browser article " * 80,
        "transport": "crawl4ai",
        "profile": "chromium",
    }

    result = _fetch_webpage_content("https://example.com/article", timeout=45)

    assert "recovered browser article" in result
    mock_publish.assert_not_called()
    workspace.cleanup.assert_called_once_with()
    mock_crawl4ai.assert_called_once()


def test_publish_curl_cookie_candidate_is_atomic_under_concurrency(tmp_path):
    from backend.tools.web_tools import _publish_curl_cookie_candidate

    stable = tmp_path / "stable.cookies"
    candidates = [tmp_path / "a.cookies", tmp_path / "b.cookies"]
    payloads = ["a" * 100_000, "b" * 100_000]
    for path, payload in zip(candidates, payloads):
        path.write_text(payload, encoding="utf-8")

    barrier = threading.Barrier(2)

    def publish(path):
        barrier.wait()
        _publish_curl_cookie_candidate(str(path), str(stable))

    threads = [threading.Thread(target=publish, args=(path,)) for path in candidates]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert stable.read_text(encoding="utf-8") in payloads
    assert list(tmp_path.glob("stable.cookies.*.tmp")) == []
```

- [ ] **Step 7: Verify integration GREEN**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_web_fetch.py tests/test_search_and_fetch.py -q
```

Expected: all tests PASS, including executor shutdown and overall deadline tests.

---

### Task 5: Full Regression And Real-Query Diagnostic

**Files:**
- Verify only; do not add production behavior.

**Interfaces:**
- Verifies active runtime config and the complete tool pipeline.

- [ ] **Step 1: Run configuration and fetch regression suites**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_web_tools_config.py \
  tests/test_web_fetch.py \
  tests/test_search_and_fetch.py -q
```

Expected: zero failures and zero errors.

- [ ] **Step 2: Run agent lifecycle regressions related to tool shutdown**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_agent_session_context.py \
  -k "generator_close_keeps_run_busy_until_worker_exits or stop or abort" -q
```

Expected: all selected tests PASS; no backend `rich.Live` or run-scope lifecycle regression.

- [ ] **Step 3: Confirm active runtime values**

Run:

```bash
./.sglang/bin/python -c 'from backend.config import ConfigLoader; c=ConfigLoader().load(reload=True).web_tools; print(c.fetch_transport, c.fetch_curl_timeout_seconds, c.fetch_curl_hedge_delay_seconds, c.fetch_crawl4ai_timeout_seconds, c.search_timeout)'
```

Expected:

```text
curl 6 3 15 45
```

- [ ] **Step 4: Run the same real query once as a diagnostic**

Run:

```bash
./.sglang/bin/python -c 'import time; from backend.tools.search_and_fetch import search_and_fetch_impl; s=time.monotonic(); r=search_and_fetch_impl("US ban import Chinese AI robots humanoid all types 2025 2026", max_results=25); print(next(x for x in r.splitlines() if x.startswith("共找到"))); print(f"elapsed={time.monotonic()-s:.2f}s")'
```

Expected invariants rather than fixed external counts:

- No process crash, double free, or heap corruption.
- The command returns after all curl/Crawl4AI workers exit.
- Curl never spends more than six seconds per URL before fallback.
- URLs with timely origin 2xx headers do not start a second curl request.
- External success/snippet counts may vary with live search results and site behavior.

---

## Completion Checklist

- [ ] The config chain exposes curl hedge delay `3`.
- [ ] Large bodies use file-backed output and do not deadlock on PIPE capacity.
- [ ] The second curl starts only when no final origin 2xx headers arrive by T=3, or immediately after an early connection/process failure.
- [ ] Final non-2xx status enters Crawl4AI immediately.
- [ ] Both attempts share one T=6 deadline.
- [ ] Losers, timeouts, and aborts terminate and reap every subprocess.
- [ ] Curl performs no additional sequential retry.
- [ ] Only a validated winner publishes cookies atomically.
- [ ] Existing result formatting, search concurrency, and lifecycle tests remain green.
- [ ] No git command was run.
