"""Tests for robust webpage fetching behavior."""

import sys
import os
import socket
import stat
import signal
import subprocess
import threading
import time
import types
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class _FetchTestHandler(BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        handler = self.routes[self.path]
        try:
            handler(self)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        return


class _LocalFetchServer:
    def __init__(self, routes):
        handler = type("ConfiguredFetchHandler", (_FetchTestHandler,), {"routes": routes})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, exc_type, exc, tb):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _ImmediateFuture:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.result_calls = 0

    def done(self):
        return True

    def result(self):
        self.result_calls += 1
        if self._error is not None:
            raise self._error
        return self._result


class _ImmediateExecutor:
    def __init__(self, shutdown_error=None):
        self.futures = []
        self.shutdown_error = shutdown_error
        self.shutdown_calls = 0

    def submit(self, fn, *args, **kwargs):
        try:
            future = _ImmediateFuture(result=fn(*args, **kwargs))
        except BaseException as exc:
            future = _ImmediateFuture(error=exc)
        self.futures.append(future)
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdown_calls += 1
        if self.shutdown_error is not None:
            raise self.shutdown_error


class _PendingFuture(Future):
    pass


class _PendingExecutor:
    def __init__(self, shutdown_error=None):
        self.futures = []
        self.shutdown_error = shutdown_error
        self.shutdown_calls = 0

    def submit(self, fn, *args, **kwargs):
        future = _PendingFuture()
        self.futures.append(future)
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdown_calls += 1
        if self.shutdown_error is not None:
            raise self.shutdown_error


def _tracked_curl_workspace_factory():
    from backend.tools.web_tools import _CurlAttemptWorkspace

    created = []
    original_create = _CurlAttemptWorkspace.create

    def create(stable_cookie_path, attempt_name):
        workspace = original_create(stable_cookie_path, attempt_name)
        workspace.cleanup = MagicMock(wraps=workspace.cleanup)
        created.append(workspace)
        return workspace

    return created, create


def _curl_hedge_fetch_config():
    return {
        "fetch_proxy": None,
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
        "fetch_curl_max_response_bytes": 8_388_608,
        "fetch_crawl4ai_timeout_seconds": 15,
        "crawl4ai_retry_count": 0,
    }


class TestWebFetchRobustness:
    @pytest.mark.parametrize("transport, mock_name", [
        ("curl", "_fetch_with_curl_hedged"),
        ("curl_cffi", "_fetch_with_curl_cffi_hedged"),
    ])
    @pytest.mark.parametrize("configured_proxy, expected_proxy", [
        ("   ", None),
        (" http://proxy.example:8080 ", "http://proxy.example:8080"),
    ])
    def test_transport_receives_normalized_fetch_proxy_without_network(
        self, transport, mock_name, configured_proxy, expected_proxy, monkeypatch
    ):
        from backend.tools import web_tools

        body = "usable article text " * 100
        config = _curl_hedge_fetch_config()
        config.update({
            "fetch_proxy": configured_proxy,
            "fetch_transport": transport,
            "fetch_fallback_transport": None,
        })
        monkeypatch.setattr(web_tools, "_get_fetch_config", lambda: config)
        calls = []

        def fake_fetch(**kwargs):
            calls.append(kwargs)
            return {
                "status_code": 200,
                "final_url": "https://example.com/article",
                "content_type": "text/plain",
                "body": body,
                "transport": transport,
                "profile": "desktop",
            }

        monkeypatch.setattr(web_tools, mock_name, fake_fetch)
        result = web_tools._fetch_webpage_content("https://example.com/article", timeout=5)

        assert "usable article text" in result
        assert len(calls) == 1
        assert calls[0]["proxy"] == expected_proxy

    def test_curl_subprocess_environment_excludes_application_secrets(self, monkeypatch):
        from backend.tools import web_tools

        monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")
        monkeypatch.setenv("HOME", "/tmp/secret-home")

        environment = web_tools._build_fetch_subprocess_environment()

        assert environment["PATH"]
        assert environment["LANG"]
        assert "OPENAI_API_KEY" not in environment
        assert "HOME" not in environment

    @pytest.mark.parametrize(
        "url",
        ["file:///etc/hostname", "ftp://example.com/resource", "javascript:alert(1)"],
    )
    def test_web_fetch_rejects_non_http_url_before_fetch(self, url):
        from backend.tools import web_tools

        with patch(
            "backend.tools.web_tools._fetch_webpage_content",
            side_effect=AssertionError("fetch must not start"),
        ):
            result = web_tools.web_fetch(url)

        assert "unsupported URL scheme" in result

    def test_web_fetch_rejects_url_without_hostname_before_fetch(self):
        from backend.tools import web_tools

        with patch(
            "backend.tools.web_tools._fetch_webpage_content",
            side_effect=AssertionError("fetch must not start"),
        ):
            result = web_tools.web_fetch("https:///missing-host")

        assert "URL hostname is required" in result

    def test_curl_argv_terminates_options_before_user_url(self):
        from backend.tools.web_tools import _build_curl_fetch_args

        url = "-K/attacker.conf"
        args = _build_curl_fetch_args(url=url, timeout=2, headers={})

        assert args[-2:] == ["--", url]
        assert args[args.index("--proto") + 1] == "=http,https"
        assert args[args.index("--proto-redir") + 1] == "=http,https"

    def test_curl_subprocess_starts_in_a_new_process_group(self):
        from backend.tools import web_tools

        proc = MagicMock()
        proc.returncode = 0
        proc.communicate.return_value = ("output", "")
        with patch(
            "backend.tools.web_tools.subprocess.Popen", return_value=proc
        ) as popen:
            web_tools._run_subprocess_with_abort(["curl", "https://example.com"], timeout=1)

        assert popen.call_args.kwargs["start_new_session"] is True

    def test_curl_cleanup_signals_the_process_group(self):
        from backend.tools import web_tools

        class HangingProcess:
            pid = 1234
            returncode = None

            def poll(self):
                return None

            def wait(self, timeout=None):
                if timeout is not None:
                    raise subprocess.TimeoutExpired(["curl"], timeout)
                self.returncode = -signal.SIGKILL
                return self.returncode

            def terminate(self):
                raise AssertionError("process-group termination must be used")

            def kill(self):
                raise AssertionError("process-group kill must be used")

        proc = HangingProcess()
        with patch("backend.tools.web_tools.os.getpgid", return_value=4321), patch(
            "backend.tools.web_tools.os.killpg"
        ) as killpg:
            web_tools._terminate_and_reap_process(proc)

        assert killpg.call_args_list == [
            ((4321, signal.SIGTERM),),
            ((4321, signal.SIGKILL),),
        ]

    def test_resolve_fetch_transport_accepts_crawl4ai(self):
        from backend.tools.web_tools import _resolve_fetch_transport

        assert _resolve_fetch_transport("crawl4ai") == "crawl4ai"

    def test_get_fetch_config_includes_crawl4ai_defaults(self):
        from backend.tools import web_tools

        class WebTools:
            fetch_proxy = None
            fetch_transport = "curl_cffi"
            fetch_fallback_transport = "crawl4ai"
            fetch_content_fallback = "searxng_snippet"
            fetch_impersonate = "chrome"
            fetch_max_workers = 3
            fetch_retry_count = 0
            fetch_retry_backoff_seconds = 0.0
            fetch_min_text_chars = 200
            fetch_quality_min_text_chars = 1000
            fetch_quality_min_text_lines = 6
            fetch_enable_readability_fallback = True
            fetch_block_markers = []
            crawl4ai_browser_type = "chromium"
            crawl4ai_headless = True
            crawl4ai_wait_until = "networkidle"
            crawl4ai_page_timeout_ms = 15000
            crawl4ai_word_count_threshold = 10
            crawl4ai_bypass_cache = True
            fetch_curl_timeout_seconds = 6
            fetch_curl_hedge_delay_seconds = 3
            fetch_curl_max_response_bytes = 8_388_608
            fetch_curl_cffi_timeout_seconds = 6
            fetch_curl_cffi_hedge_delay_seconds = 3
            fetch_crawl4ai_timeout_seconds = 15
            crawl4ai_retry_count = 0

        class Config:
            web_tools = WebTools()

        with patch("backend.tools.web_tools.get_config", return_value=Config()):
            cfg = web_tools._get_fetch_config()

        assert cfg["fetch_fallback_transport"] == "crawl4ai"
        assert cfg["fetch_content_fallback"] == "searxng_snippet"
        assert cfg["crawl4ai_browser_type"] == "chromium"
        assert cfg["fetch_curl_timeout_seconds"] == 6
        assert cfg["fetch_curl_hedge_delay_seconds"] == 3
        assert cfg["fetch_curl_max_response_bytes"] == 8_388_608
        assert cfg["fetch_curl_cffi_timeout_seconds"] == 6
        assert cfg["fetch_curl_cffi_hedge_delay_seconds"] == 3
        assert cfg["fetch_crawl4ai_timeout_seconds"] == 15
        assert cfg["crawl4ai_retry_count"] == 0
        assert cfg["fetch_quality_min_text_chars"] == 1000
        assert cfg["fetch_quality_min_text_lines"] == 6

    def test_low_quality_text_boundaries(self):
        from backend.tools.web_tools import _looks_like_low_quality_text

        assert _looks_like_low_quality_text("") is True
        assert _looks_like_low_quality_text("x" * 200) is True
        assert _looks_like_low_quality_text("x" * 1000) is False
        assert _looks_like_low_quality_text("x\n" * 5) is True
        assert _looks_like_low_quality_text("x" * 200 + "\ny\ny\ny\ny\ny") is False
        long_multi_line = "\n".join("x" * 200 for _ in range(6))
        assert len(long_multi_line) >= 1000
        assert len(long_multi_line.splitlines()) >= 6
        assert _looks_like_low_quality_text(long_multi_line) is False

    def test_low_quality_text_uses_configured_thresholds(self):
        from backend.tools.web_tools import _looks_like_low_quality_text

        assert _looks_like_low_quality_text("x" * 9, 10, 2) is True
        assert _looks_like_low_quality_text("x" * 10 + "\ny", 10, 2) is False

    @patch("backend.tools.web_tools._fetch_with_crawl4ai")
    @patch("backend.tools.web_tools._fetch_with_curl_hedged")
    @patch("backend.tools.web_tools._get_fetch_config")
    def test_low_quality_curl_content_invokes_crawl4ai_fallback(
        self, mock_get_fetch_config, mock_curl, mock_crawl4ai
    ):
        from backend.tools.web_tools import _fetch_webpage_content

        cfg = _curl_hedge_fetch_config()
        cfg["fetch_quality_min_text_chars"] = 1000
        cfg["fetch_quality_min_text_lines"] = 6
        mock_get_fetch_config.return_value = cfg
        mock_curl.return_value = {
            "status_code": 200,
            "final_url": "https://example.com/article",
            "content_type": "text/plain",
            "body": "too short",
            "transport": "curl",
            "profile": "desktop",
        }
        mock_crawl4ai.return_value = {
            "status_code": 200,
            "final_url": "https://example.com/article",
            "content_type": "text/plain",
            "body": "recovered article content " * 80,
            "transport": "crawl4ai",
            "profile": "chromium",
        }

        result = _fetch_webpage_content("https://example.com/article", timeout=10)

        assert "recovered article content" in result
        mock_crawl4ai.assert_called_once()

    def test_build_curl_fetch_args_include_cookie_jar_and_browser_headers(self):
        from backend.tools.web_tools import _BROWSER_PROFILES, _build_curl_fetch_args

        args = _build_curl_fetch_args(
            url="https://example.com/article",
            timeout=12,
            headers=_BROWSER_PROFILES["desktop"],
            proxy="http://127.0.0.1:1080",
            cookie_jar_path="/tmp/example.cookies",
        )

        assert "--compressed" in args
        assert "--suppress-connect-headers" in args
        assert args[args.index("--max-filesize") + 1] == "8388608"
        assert "-b" in args
        assert "/tmp/example.cookies" in args
        assert "-c" in args
        assert "-x" in args
        assert "http://127.0.0.1:1080" in args
        assert any(arg.startswith("User-Agent:") for arg in args)
        assert "Accept-Language: en-US,en;q=0.9" in args
        assert "Upgrade-Insecure-Requests: 1" in args

    def test_curl_cookie_path_creation_uses_private_permissions(self, tmp_path):
        from backend.tools.web_tools import _build_curl_fetch_args

        cookie_path = tmp_path / "cookies" / "stable.cookies"
        _build_curl_fetch_args(
            url="https://example.com/article",
            timeout=1,
            headers={},
            cookie_jar_path=str(cookie_path),
        )

        assert stat.S_IMODE(cookie_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(cookie_path.stat().st_mode) == 0o600

    def test_curl_workspace_uses_private_permissions_for_all_files(self, tmp_path):
        from backend.tools import web_tools

        with patch("backend.tools.web_tools.tempfile.mkdtemp") as mock_mkdtemp:
            root = tmp_path / "workspace"

            def create_root(**kwargs):
                root.mkdir(mode=0o755)
                return str(root)

            mock_mkdtemp.side_effect = create_root
            workspace = web_tools._CurlAttemptWorkspace.create(None, "private")

        try:
            assert stat.S_IMODE(root.stat().st_mode) == 0o700
            for path in (
                workspace.header_path,
                workspace.body_path,
                workspace.stderr_path,
                workspace.cookie_path,
            ):
                assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600
        finally:
            workspace.cleanup()

    def test_curl_workspace_create_removes_root_when_cookie_copy_fails(
        self,
        tmp_path,
    ):
        from backend.tools import web_tools

        stable = tmp_path / "stable.cookies"
        stable.write_text("stable", encoding="utf-8")
        created_roots = []

        def make_root(**kwargs):
            root = tmp_path / kwargs["prefix"].rstrip("-")
            root.mkdir()
            created_roots.append(root)
            return str(root)

        with patch(
            "backend.tools.web_tools.tempfile.mkdtemp",
            side_effect=make_root,
        ), patch(
            "backend.tools.web_tools.shutil.copy2",
            side_effect=OSError("cookie copy failed"),
        ):
            with pytest.raises(OSError, match="cookie copy failed"):
                web_tools._CurlAttemptWorkspace.create(str(stable), "copy-failure")

        assert len(created_roots) == 1
        assert not created_roots[0].exists()

    def test_curl_workspace_create_removes_root_when_cookie_touch_fails(
        self,
        tmp_path,
    ):
        from backend.tools import web_tools

        created_roots = []

        def make_root(**kwargs):
            root = tmp_path / kwargs["prefix"].rstrip("-")
            root.mkdir()
            created_roots.append(root)
            return str(root)

        with patch(
            "backend.tools.web_tools.tempfile.mkdtemp",
            side_effect=make_root,
        ), patch.object(
            web_tools.Path,
            "touch",
            side_effect=OSError("cookie touch failed"),
        ):
            with pytest.raises(OSError, match="cookie touch failed"):
                web_tools._CurlAttemptWorkspace.create(None, "touch-failure")

        assert len(created_roots) == 1
        assert not created_roots[0].exists()

    def test_subprocess_communicate_failure_reaps_process_and_preserves_primary(self):
        from backend.tools import web_tools

        proc = MagicMock()
        proc.returncode = None
        proc.communicate.side_effect = UnicodeError("decode failed")

        def fail_cleanup_after_reap(active_proc):
            active_proc.returncode = -9
            raise OSError("cleanup failed")

        with patch(
            "backend.tools.web_tools.subprocess.Popen",
            return_value=proc,
        ), patch(
            "backend.tools.web_tools._terminate_and_reap_process",
            side_effect=fail_cleanup_after_reap,
        ) as mock_reap:
            with pytest.raises(UnicodeError, match="decode failed"):
                web_tools._run_subprocess_with_abort(["curl"], timeout=1)

        mock_reap.assert_called_once_with(proc)
        assert proc.returncode == -9

    def test_subprocess_abort_callback_failure_reaps_process_and_preserves_primary(self):
        from backend.tools import web_tools

        proc = MagicMock()
        proc.returncode = None

        def fail_cleanup_after_reap(active_proc):
            active_proc.returncode = -15
            raise OSError("cleanup failed")

        with patch(
            "backend.tools.web_tools.subprocess.Popen",
            return_value=proc,
        ), patch(
            "backend.tools.web_tools._terminate_and_reap_process",
            side_effect=fail_cleanup_after_reap,
        ) as mock_reap:
            with pytest.raises(RuntimeError, match="abort callback failed"):
                web_tools._run_subprocess_with_abort(
                    ["curl"],
                    timeout=1,
                    check_abort=MagicMock(
                        side_effect=RuntimeError("abort callback failed")
                    ),
                )

        mock_reap.assert_called_once_with(proc)
        assert proc.returncode == -15

    def test_subprocess_cleanup_failure_surfaces_without_primary_exception(self):
        from backend.tools import web_tools

        proc = MagicMock()
        proc.returncode = None
        proc.communicate.return_value = ("metadata", "")

        def fail_cleanup_after_reap(active_proc):
            active_proc.returncode = 0
            raise OSError("cleanup failed")

        with patch(
            "backend.tools.web_tools.subprocess.Popen",
            return_value=proc,
        ), patch(
            "backend.tools.web_tools._terminate_and_reap_process",
            side_effect=fail_cleanup_after_reap,
        ):
            with pytest.raises(OSError, match="cleanup failed"):
                web_tools._run_subprocess_with_abort(["curl"], timeout=1)

        assert proc.returncode == 0

    def test_subprocess_poll_failure_still_reaps_and_preserves_decode_error(self):
        from backend.tools import web_tools

        class PollFaultProcess:
            returncode = None

            def __init__(self):
                self.alive = True
                self.terminate_calls = 0
                self.wait_calls = 0

            def communicate(self, timeout):
                raise UnicodeError("decode failed")

            def poll(self):
                raise RuntimeError("poll failed")

            def terminate(self):
                self.terminate_calls += 1
                self.alive = False
                self.returncode = -15

            def wait(self, timeout=None):
                self.wait_calls += 1
                return self.returncode

        proc = PollFaultProcess()
        with patch(
            "backend.tools.web_tools.subprocess.Popen",
            return_value=proc,
        ):
            with pytest.raises(UnicodeError, match="decode failed"):
                web_tools._run_subprocess_with_abort(["curl"], timeout=1)

        assert proc.alive is False
        assert proc.terminate_calls == 1
        assert proc.wait_calls == 1

    def test_cookie_jar_path_normalizes_equivalent_host_identity(self):
        from backend.tools.web_tools import _build_cookie_jar_path

        paths = {
            _build_cookie_jar_path("https://EXAMPLE.com/article"),
            _build_cookie_jar_path("https://user:pass@example.com/article"),
            _build_cookie_jar_path("https://example.com:443/article"),
            _build_cookie_jar_path("http://example.com:80/article"),
        }

        assert len(paths) == 1

    def test_curl_large_body_does_not_block_on_stdout_pipe(self, tmp_path):
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

    def test_parse_latest_curl_header_state_waits_through_redirect(self, tmp_path):
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

    def test_parse_latest_curl_header_state_rejects_final_non_2xx(self, tmp_path):
        from backend.tools.web_tools import _parse_latest_curl_header_state

        path = tmp_path / "headers.txt"
        path.write_text("HTTP/1.1 404 Not Found\r\n\r\n", encoding="iso-8859-1")
        assert _parse_latest_curl_header_state(str(path)) == ("failure", 404)

    def test_parse_latest_curl_header_state_ignores_partial_header_block(self, tmp_path):
        from backend.tools.web_tools import _parse_latest_curl_header_state

        path = tmp_path / "headers.txt"
        path.write_text(
            "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n",
            encoding="iso-8859-1",
        )
        assert _parse_latest_curl_header_state(str(path)) == ("pending", None)

    def test_parse_latest_curl_header_state_ignores_proxy_connect(self, tmp_path):
        from backend.tools.web_tools import _parse_latest_curl_header_state

        path = tmp_path / "headers.txt"
        path.write_text(
            "HTTP/1.1 200 Connection established\r\n\r\n",
            encoding="iso-8859-1",
        )
        assert _parse_latest_curl_header_state(str(path)) == ("pending", None)

    @pytest.mark.parametrize(
        "connect_block",
        [
            "HTTP/1.1 200 OK\r\n\r\n",
            "HTTP/1.1 200\r\n\r\n",
            "HTTP/2 200\r\n\r\n",
        ],
    )
    def test_parse_latest_curl_header_state_uses_origin_after_ambiguous_connect_200(
        self,
        tmp_path,
        connect_block,
    ):
        from backend.tools.web_tools import _parse_latest_curl_header_state

        path = tmp_path / "headers.txt"
        path.write_text(
            connect_block + "HTTP/2 404\r\nContent-Type: text/plain\r\n\r\n",
            encoding="iso-8859-1",
        )

        assert _parse_latest_curl_header_state(str(path)) == ("failure", 404)

    def test_parse_latest_curl_header_state_keeps_direct_origin_200(self, tmp_path):
        from backend.tools.web_tools import _parse_latest_curl_header_state

        path = tmp_path / "headers.txt"
        path.write_text(
            "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n",
            encoding="iso-8859-1",
        )

        assert _parse_latest_curl_header_state(str(path)) == ("success", 200)

    @patch("backend.tools.web_tools._publish_curl_cookie_candidate")
    @patch("backend.tools.web_tools._fetch_with_crawl4ai")
    @patch("backend.tools.web_tools._get_fetch_config")
    def test_truncated_nonzero_curl_with_long_partial_body_falls_back_without_cookie_publish(
        self,
        mock_get_fetch_config,
        mock_crawl4ai,
        mock_publish,
        tmp_path,
    ):
        from backend.tools import web_tools

        partial_body = b"long partial article body " * 200

        def truncated_response(request):
            request.send_response(200)
            request.send_header("Content-Type", "text/plain")
            request.send_header("Content-Length", str(len(partial_body) + 50_000))
            request.send_header("Set-Cookie", "partial=must-not-publish")
            request.end_headers()
            request.wfile.write(partial_body)
            request.wfile.flush()
            request.connection.shutdown(socket.SHUT_RDWR)
            request.close_connection = True

        cfg = _curl_hedge_fetch_config()
        cfg["fetch_curl_hedge_delay_seconds"] = 0.5
        cfg["fetch_curl_timeout_seconds"] = 2
        mock_get_fetch_config.return_value = cfg
        mock_crawl4ai.return_value = {
            "status_code": 200,
            "final_url": "https://fallback.example/article",
            "content_type": "text/plain",
            "body": "crawl4ai recovered article " * 80,
            "transport": "crawl4ai",
            "profile": "chromium",
        }
        stable = tmp_path / "cookies" / "stable.cookies"

        with _LocalFetchServer({"/truncated": truncated_response}) as base_url, patch(
            "backend.tools.web_tools._build_cookie_jar_path",
            return_value=str(stable),
        ):
            result = web_tools._fetch_webpage_content(
                f"{base_url}/truncated",
                timeout=4,
            )

        assert "crawl4ai recovered article" in result
        mock_crawl4ai.assert_called_once()
        mock_publish.assert_not_called()

    @patch("backend.tools.web_tools._publish_curl_cookie_candidate")
    @patch("backend.tools.web_tools._fetch_with_crawl4ai")
    @patch("backend.tools.web_tools._get_fetch_config")
    def test_curl_response_size_limit_falls_back_without_cookie_publish(
        self,
        mock_get_fetch_config,
        mock_crawl4ai,
        mock_publish,
        tmp_path,
    ):
        from backend.tools import web_tools

        body = b"oversized article body " * 500

        def oversized_response(request):
            request.send_response(200)
            request.send_header("Content-Type", "text/plain")
            request.send_header("Content-Length", str(len(body)))
            request.send_header("Set-Cookie", "oversized=must-not-publish")
            request.end_headers()
            request.wfile.write(body)

        cfg = _curl_hedge_fetch_config()
        cfg["fetch_curl_max_response_bytes"] = 1024
        mock_get_fetch_config.return_value = cfg
        mock_crawl4ai.return_value = {
            "status_code": 200,
            "final_url": "https://fallback.example/article",
            "content_type": "text/plain",
            "body": "size fallback article " * 80,
            "transport": "crawl4ai",
            "profile": "chromium",
        }

        with _LocalFetchServer({"/oversized": oversized_response}) as base_url, patch(
            "backend.tools.web_tools._build_cookie_jar_path",
            return_value=str(tmp_path / "cookies" / "stable.cookies"),
        ):
            result = web_tools._fetch_webpage_content(
                f"{base_url}/oversized",
                timeout=4,
            )

        assert "size fallback article" in result
        mock_crawl4ai.assert_called_once()
        mock_publish.assert_not_called()

    def test_curl_hedge_starts_second_request_after_header_delay(self, tmp_path):
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

        with _LocalFetchServer({"/hedge": hedged_response}) as base_url:
            response = _fetch_with_curl_hedged(
                url=f"{base_url}/hedge",
                timeout=1,
                proxy=None,
                cfg={"fetch_curl_hedge_delay_seconds": 0.15},
                headers={},
                cookie_jar_path=str(tmp_path / "cookies.txt"),
            )

        try:
            assert request_count == 2
            assert response["body"] == "attempt-2"
            assert response["_curl_cookie_candidate"] == response[
                "_curl_hedge_workspace"
            ].cookie_path
        finally:
            response["_curl_hedge_workspace"].cleanup()

    def test_curl_hedge_does_not_duplicate_after_timely_2xx_headers(self, tmp_path):
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
            response = _fetch_with_curl_hedged(
                f"{base_url}/timely",
                1,
                None,
                {"fetch_curl_hedge_delay_seconds": 0.15},
                {},
                str(tmp_path / "cookies.txt"),
            )

        try:
            assert request_count == 1
        finally:
            response["_curl_hedge_workspace"].cleanup()

    @patch("backend.tools.web_tools._fetch_with_curl")
    def test_curl_hedge_starts_second_attempt_immediately_after_connection_failure(
        self,
        mock_fetch_curl,
        tmp_path,
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

        try:
            assert response["body"] == "recovered body"
            assert mock_fetch_curl.call_count == 2
            assert time.monotonic() - started < 0.5
        finally:
            response["_curl_hedge_workspace"].cleanup()

    def test_curl_hedge_rejects_final_non_2xx_without_second_request(self, tmp_path):
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
                    f"{base_url}/missing",
                    1,
                    None,
                    {"fetch_curl_hedge_delay_seconds": 0.15},
                    {},
                    str(tmp_path / "cookies.txt"),
                )

        assert request_count == 1

    def test_curl_hedge_attempts_share_one_deadline(self, tmp_path):
        from backend.tools.web_tools import _fetch_with_curl_hedged

        def no_headers(request):
            time.sleep(1)

        started = time.monotonic()
        with _LocalFetchServer({"/slow": no_headers}) as base_url:
            with pytest.raises(TimeoutError):
                _fetch_with_curl_hedged(
                    f"{base_url}/slow",
                    0.4,
                    None,
                    {"fetch_curl_hedge_delay_seconds": 0.2},
                    {},
                    str(tmp_path / "cookies.txt"),
                )
        elapsed = time.monotonic() - started

        assert elapsed < 0.7

    def test_curl_hedge_times_out_winner_body_without_starting_second_request(
        self,
        tmp_path,
    ):
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

    def test_curl_hedge_abort_reaps_all_active_processes(self, tmp_path):
        from backend.tools import web_tools

        abort_event = threading.Event()
        reaped = []
        original_reap = web_tools._terminate_and_reap_process

        def no_headers(request):
            time.sleep(1)

        def traced_reap(proc):
            original_reap(proc)
            reaped.append(proc)

        timer = threading.Timer(0.3, abort_event.set)
        with _LocalFetchServer({"/abort": no_headers}) as base_url, patch(
            "backend.tools.web_tools._terminate_and_reap_process",
            side_effect=traced_reap,
        ):
            timer.start()
            try:
                with pytest.raises(web_tools.AbortError):
                    web_tools._fetch_with_curl_hedged(
                        f"{base_url}/abort",
                        timeout=2,
                        proxy=None,
                        cfg={"fetch_curl_hedge_delay_seconds": 0.1},
                        headers={},
                        cookie_jar_path=str(tmp_path / "cookies.txt"),
                        check_abort=abort_event.is_set,
                    )
            finally:
                timer.cancel()
                timer.join(timeout=1)

        assert len(reaped) == 2
        assert all(proc.poll() is not None for proc in reaped)

    @patch("backend.tools.web_tools._fetch_with_curl")
    def test_curl_hedge_propagates_worker_observed_abort(
        self,
        mock_fetch_curl,
        tmp_path,
    ):
        from backend.tools import web_tools

        mock_fetch_curl.side_effect = [
            web_tools.AbortError("Fetch aborted by user"),
            {
                "status_code": 200,
                "final_url": "https://example.com/article",
                "content_type": "text/plain",
                "body": "must not win",
                "transport": "curl",
                "profile": "desktop",
            },
        ]

        with pytest.raises(web_tools.AbortError):
            web_tools._fetch_with_curl_hedged(
                "https://example.com/article",
                1,
                None,
                {"fetch_curl_hedge_delay_seconds": 0.8},
                {},
                str(tmp_path / "cookies.txt"),
            )

        assert mock_fetch_curl.call_count == 1

    @patch("backend.tools.web_tools._parse_latest_curl_header_state")
    @patch("backend.tools.web_tools._fetch_with_curl")
    def test_curl_hedge_rechecks_final_non_2xx_after_completed_attempt_error(
        self,
        mock_fetch_curl,
        mock_parse_headers,
        tmp_path,
    ):
        from backend.tools import web_tools

        executor = _ImmediateExecutor()

        def fail_after_final_header(**kwargs):
            Path(kwargs["header_path"]).write_text(
                "HTTP/1.1 404 Not Found\r\n\r\n",
                encoding="iso-8859-1",
            )
            raise RuntimeError("empty response")

        mock_fetch_curl.side_effect = fail_after_final_header
        parse_calls = 0

        def parse_headers(path):
            nonlocal parse_calls
            parse_calls += 1
            if parse_calls == 1 or "/curl-b-" in path:
                return "pending", None
            return "failure", 404

        mock_parse_headers.side_effect = parse_headers

        with patch(
            "backend.tools.web_tools.ThreadPoolExecutor",
            return_value=executor,
        ):
            with pytest.raises(RuntimeError, match="HTTP 404"):
                web_tools._fetch_with_curl_hedged(
                    "https://example.com/missing",
                    1,
                    None,
                    {"fetch_curl_hedge_delay_seconds": 0.8},
                    {},
                    str(tmp_path / "cookies.txt"),
                )

        assert mock_fetch_curl.call_count == 1
        assert mock_parse_headers.call_count == 2
        assert executor.futures[0].result_calls == 1

    def test_curl_hedge_winner_promptly_cancels_and_reaps_loser(self, tmp_path):
        from backend.tools import web_tools

        lock = threading.Lock()
        request_count = 0
        reaped = []
        original_reap = web_tools._terminate_and_reap_process
        workspaces, create_workspace = _tracked_curl_workspace_factory()

        def slow_first_response(request):
            nonlocal request_count
            with lock:
                request_count += 1
                index = request_count
            if index == 1:
                time.sleep(1)
                return
            body = b"winner"
            request.send_response(200)
            request.send_header("Content-Length", str(len(body)))
            request.end_headers()
            request.wfile.write(body)

        def traced_reap(proc):
            original_reap(proc)
            reaped.append(proc)

        started = time.monotonic()
        with _LocalFetchServer({"/winner": slow_first_response}) as base_url, patch(
            "backend.tools.web_tools._terminate_and_reap_process",
            side_effect=traced_reap,
        ), patch.object(
            web_tools._CurlAttemptWorkspace,
            "create",
            side_effect=create_workspace,
        ):
            response = web_tools._fetch_with_curl_hedged(
                f"{base_url}/winner",
                2,
                None,
                {"fetch_curl_hedge_delay_seconds": 0.1},
                {},
                str(tmp_path / "cookies.txt"),
            )

        try:
            winner_workspace = response["_curl_hedge_workspace"]
            loser_workspace = next(
                workspace for workspace in workspaces if workspace is not winner_workspace
            )
            assert response["body"] == "winner"
            assert request_count == 2
            assert len(reaped) == 1
            assert reaped[0].poll() is not None
            assert loser_workspace.cleanup.call_count == 1
            assert winner_workspace.cleanup.call_count == 0
            assert time.monotonic() - started < 0.7
        finally:
            response["_curl_hedge_workspace"].cleanup()
        assert winner_workspace.cleanup.call_count == 1

    @patch("backend.tools.web_tools._fetch_with_curl")
    def test_curl_hedge_loser_cleanup_failure_preserves_validated_winner_handoff(
        self,
        mock_fetch_curl,
        tmp_path,
    ):
        from backend.tools import web_tools

        executor = _ImmediateExecutor()
        workspaces = []
        original_create = web_tools._CurlAttemptWorkspace.create

        def create_workspace(stable_cookie_path, attempt_name):
            workspace = original_create(stable_cookie_path, attempt_name)
            workspace.cleanup = MagicMock(wraps=workspace.cleanup)
            if attempt_name == "a":
                workspace.cleanup.side_effect = OSError("loser cleanup failed")
            workspaces.append(workspace)
            return workspace

        mock_fetch_curl.side_effect = [
            RuntimeError("attempt A failed"),
            {
                "status_code": 200,
                "final_url": "https://example.com/article",
                "content_type": "text/plain",
                "body": "validated winner body " * 80,
                "transport": "curl",
                "profile": "desktop",
            },
        ]
        cfg = _curl_hedge_fetch_config()
        cfg["fetch_fallback_transport"] = None

        with patch(
            "backend.tools.web_tools.ThreadPoolExecutor",
            return_value=executor,
        ), patch.object(
            web_tools._CurlAttemptWorkspace,
            "create",
            side_effect=create_workspace,
        ), patch(
            "backend.tools.web_tools._get_fetch_config",
            return_value=cfg,
        ), patch(
            "backend.tools.web_tools._build_cookie_jar_path",
            return_value=str(tmp_path / "stable.cookies"),
        ), patch(
            "backend.tools.web_tools._publish_curl_cookie_candidate",
        ):
            result = web_tools._fetch_webpage_content(
                "https://example.com/article",
                timeout=45,
            )

        assert "validated winner body" in result
        assert [workspace.cleanup.call_count for workspace in workspaces] == [1, 1]

    @patch("backend.tools.web_tools._fetch_with_curl")
    def test_curl_hedge_attempts_share_one_locked_cookie_snapshot(
        self,
        mock_fetch_curl,
        tmp_path,
    ):
        from backend.tools import web_tools

        class TrackedLock:
            def __init__(self):
                self.lock = threading.Lock()
                self.owner = None

            def __enter__(self):
                self.lock.acquire()
                self.owner = threading.get_ident()
                return self

            def __exit__(self, exc_type, exc, tb):
                self.owner = None
                self.lock.release()

            def owned_by_current_thread(self):
                return self.owner == threading.get_ident()

        stable = tmp_path / "stable.cookies"
        candidate = tmp_path / "published.cookies"
        stable.write_text("generation-a", encoding="utf-8")
        candidate.write_text("generation-b", encoding="utf-8")
        cookie_lock = TrackedLock()
        first_snapshot_copied = threading.Event()
        publication_done = threading.Event()
        original_copy = web_tools.shutil.copy2
        snapshot_copy_count = 0
        attempt_cookies = []

        def traced_copy(source, destination, *args, **kwargs):
            nonlocal snapshot_copy_count
            result = original_copy(source, destination, *args, **kwargs)
            if str(source) == str(stable) and Path(destination).name == "cookies.txt":
                snapshot_copy_count += 1
                if snapshot_copy_count == 1:
                    first_snapshot_copied.set()
                    if not cookie_lock.owned_by_current_thread():
                        assert publication_done.wait(timeout=2)
            return result

        def publish_new_generation():
            assert first_snapshot_copied.wait(timeout=2)
            web_tools._publish_curl_cookie_candidate(str(candidate), str(stable))
            publication_done.set()

        def fetch(**kwargs):
            attempt_cookies.append(
                Path(kwargs["cookie_read_path"]).read_text(encoding="utf-8")
            )
            if len(attempt_cookies) == 1:
                raise RuntimeError("start hedge B")
            return {
                "status_code": 200,
                "final_url": "https://example.com/article",
                "content_type": "text/plain",
                "body": "winner",
                "transport": "curl",
                "profile": "desktop",
            }

        mock_fetch_curl.side_effect = fetch
        publisher = threading.Thread(target=publish_new_generation)
        publisher.start()
        try:
            with patch(
                "backend.tools.web_tools._get_cookie_jar_lock",
                return_value=cookie_lock,
            ), patch(
                "backend.tools.web_tools.shutil.copy2",
                side_effect=traced_copy,
            ), patch(
                "backend.tools.web_tools.ThreadPoolExecutor",
                return_value=_ImmediateExecutor(),
            ):
                response = web_tools._fetch_with_curl_hedged(
                    "https://example.com/article",
                    1,
                    None,
                    {"fetch_curl_hedge_delay_seconds": 0.8},
                    {},
                    str(stable),
                )
        finally:
            publisher.join(timeout=2)

        try:
            assert not publisher.is_alive()
            assert attempt_cookies == ["generation-a", "generation-a"]
            assert stable.read_text(encoding="utf-8") == "generation-b"
        finally:
            response["_curl_hedge_workspace"].cleanup()

    @patch("backend.tools.web_tools._fetch_with_curl")
    def test_curl_hedge_handles_each_failed_future_and_cleans_workspace_once(
        self,
        mock_fetch_curl,
        tmp_path,
    ):
        from backend.tools import web_tools

        executor = _ImmediateExecutor()
        workspaces, create_workspace = _tracked_curl_workspace_factory()
        mock_fetch_curl.side_effect = [
            RuntimeError("attempt A failed"),
            RuntimeError("attempt B failed"),
        ]

        with patch(
            "backend.tools.web_tools.ThreadPoolExecutor",
            return_value=executor,
        ), patch.object(
            web_tools._CurlAttemptWorkspace,
            "create",
            side_effect=create_workspace,
        ):
            with pytest.raises(RuntimeError, match="attempt B failed"):
                web_tools._fetch_with_curl_hedged(
                    "https://example.com/failure",
                    1,
                    None,
                    {"fetch_curl_hedge_delay_seconds": 0.8},
                    {},
                    str(tmp_path / "cookies.txt"),
                )

        assert [future.result_calls for future in executor.futures] == [1, 1]
        assert [workspace.cleanup.call_count for workspace in workspaces] == [1, 1]

    @patch("backend.tools.web_tools._fetch_with_curl")
    def test_curl_hedge_second_attempt_receives_only_remaining_deadline(
        self,
        mock_fetch_curl,
        tmp_path,
    ):
        from backend.tools import web_tools

        class Clock:
            now = 100.0

            def __call__(self):
                return self.now

        clock = Clock()
        executor = _ImmediateExecutor()
        attempt_timeouts = []
        response = {
            "status_code": 200,
            "final_url": "https://example.com/article",
            "content_type": "text/plain",
            "body": "recovered",
            "transport": "curl",
            "profile": "desktop",
        }

        def fetch(**kwargs):
            attempt_timeouts.append(kwargs["timeout"])
            if len(attempt_timeouts) == 1:
                clock.now += 0.25
                raise RuntimeError("connection refused")
            return response

        mock_fetch_curl.side_effect = fetch
        with patch(
            "backend.tools.web_tools.ThreadPoolExecutor",
            return_value=executor,
        ), patch("backend.tools.web_tools.time.monotonic", side_effect=clock):
            result = web_tools._fetch_with_curl_hedged(
                "https://example.com/article",
                1,
                None,
                {"fetch_curl_hedge_delay_seconds": 0.8},
                {},
                str(tmp_path / "cookies.txt"),
            )

        try:
            assert attempt_timeouts == pytest.approx([1.0, 0.75])
        finally:
            result["_curl_hedge_workspace"].cleanup()

    def test_curl_hedge_cleans_workspaces_when_executor_construction_fails(
        self,
        tmp_path,
    ):
        from backend.tools import web_tools

        workspaces, create_workspace = _tracked_curl_workspace_factory()
        with patch.object(
            web_tools._CurlAttemptWorkspace,
            "create",
            side_effect=create_workspace,
        ), patch(
            "backend.tools.web_tools.ThreadPoolExecutor",
            side_effect=RuntimeError("executor unavailable"),
        ):
            with pytest.raises(RuntimeError, match="executor unavailable"):
                web_tools._fetch_with_curl_hedged(
                    "https://example.com/article",
                    1,
                    None,
                    {"fetch_curl_hedge_delay_seconds": 0.8},
                    {},
                    str(tmp_path / "cookies.txt"),
                )

        assert [workspace.cleanup.call_count for workspace in workspaces] == [1, 1]

    def test_curl_hedge_cleans_first_workspace_when_second_creation_fails(
        self,
        tmp_path,
    ):
        from backend.tools import web_tools

        first_workspace = MagicMock()
        first_workspace.cleanup.side_effect = OSError("workspace A cleanup failed")
        with patch.object(
            web_tools._CurlAttemptWorkspace,
            "create",
            side_effect=[first_workspace, OSError("workspace B failed")],
        ):
            with pytest.raises(OSError, match="workspace B failed"):
                web_tools._fetch_with_curl_hedged(
                    "https://example.com/article",
                    1,
                    None,
                    {"fetch_curl_hedge_delay_seconds": 0.8},
                    {},
                    str(tmp_path / "stable.cookies"),
                )

        first_workspace.cleanup.assert_called_once_with()

    @patch("backend.tools.web_tools._fetch_with_curl")
    def test_curl_hedge_cleans_workspaces_when_executor_shutdown_fails(
        self,
        mock_fetch_curl,
        tmp_path,
    ):
        from backend.tools import web_tools

        executor = _ImmediateExecutor(
            shutdown_error=RuntimeError("executor shutdown failed")
        )
        workspaces, create_workspace = _tracked_curl_workspace_factory()
        mock_fetch_curl.return_value = {
            "status_code": 200,
            "final_url": "https://example.com/article",
            "content_type": "text/plain",
            "body": "ok",
            "transport": "curl",
            "profile": "desktop",
        }

        with patch(
            "backend.tools.web_tools.ThreadPoolExecutor",
            return_value=executor,
        ), patch.object(
            web_tools._CurlAttemptWorkspace,
            "create",
            side_effect=create_workspace,
        ):
            with pytest.raises(RuntimeError, match="executor shutdown failed"):
                web_tools._fetch_with_curl_hedged(
                    "https://example.com/article",
                    1,
                    None,
                    {"fetch_curl_hedge_delay_seconds": 0.8},
                    {},
                    str(tmp_path / "cookies.txt"),
                )

        assert [workspace.cleanup.call_count for workspace in workspaces] == [1, 1]

    @patch("backend.tools.web_tools._fetch_with_curl")
    def test_curl_hedge_shutdown_failure_does_not_mask_abort_and_cleans_once(
        self,
        mock_fetch_curl,
        tmp_path,
    ):
        from backend.tools import web_tools

        executor = _ImmediateExecutor(
            shutdown_error=RuntimeError("executor shutdown failed")
        )
        workspaces, create_workspace = _tracked_curl_workspace_factory()
        mock_fetch_curl.return_value = {
            "status_code": 200,
            "final_url": "https://example.com/article",
            "content_type": "text/plain",
            "body": "unused",
            "transport": "curl",
            "profile": "desktop",
        }

        with patch(
            "backend.tools.web_tools.ThreadPoolExecutor",
            return_value=executor,
        ), patch.object(
            web_tools._CurlAttemptWorkspace,
            "create",
            side_effect=create_workspace,
        ):
            with pytest.raises(web_tools.AbortError, match="Fetch aborted by user"):
                web_tools._fetch_with_curl_hedged(
                    "https://example.com/article",
                    1,
                    None,
                    {"fetch_curl_hedge_delay_seconds": 0.8},
                    {},
                    str(tmp_path / "cookies.txt"),
                    check_abort=MagicMock(return_value=True),
                )

        assert executor.shutdown_calls == 1
        assert [workspace.cleanup.call_count for workspace in workspaces] == [1, 1]

    @patch("backend.tools.web_tools._fetch_with_curl")
    def test_curl_hedge_shutdown_failure_does_not_mask_timeout_and_cleans_once(
        self,
        mock_fetch_curl,
        tmp_path,
    ):
        from backend.tools import web_tools

        executor = _PendingExecutor(
            shutdown_error=RuntimeError("executor shutdown failed")
        )
        workspaces, create_workspace = _tracked_curl_workspace_factory()

        with patch(
            "backend.tools.web_tools.ThreadPoolExecutor",
            return_value=executor,
        ), patch.object(
            web_tools._CurlAttemptWorkspace,
            "create",
            side_effect=create_workspace,
        ):
            with pytest.raises(TimeoutError, match="Request timed out"):
                web_tools._fetch_with_curl_hedged(
                    "https://example.com/article",
                    0.1,
                    None,
                    {"fetch_curl_hedge_delay_seconds": 0.1},
                    {},
                    str(tmp_path / "cookies.txt"),
                )

        assert executor.shutdown_calls == 1
        assert [workspace.cleanup.call_count for workspace in workspaces] == [1, 1]

    @patch("backend.tools.web_tools._fetch_with_curl")
    def test_curl_hedge_shutdown_failure_does_not_mask_worker_error_and_cleans_once(
        self,
        mock_fetch_curl,
        tmp_path,
    ):
        from backend.tools import web_tools

        executor = _ImmediateExecutor(
            shutdown_error=RuntimeError("executor shutdown failed")
        )
        workspaces, create_workspace = _tracked_curl_workspace_factory()
        mock_fetch_curl.side_effect = [
            RuntimeError("attempt A failed"),
            RuntimeError("attempt B failed"),
        ]

        with patch(
            "backend.tools.web_tools.ThreadPoolExecutor",
            return_value=executor,
        ), patch.object(
            web_tools._CurlAttemptWorkspace,
            "create",
            side_effect=create_workspace,
        ):
            with pytest.raises(RuntimeError, match="attempt B failed"):
                web_tools._fetch_with_curl_hedged(
                    "https://example.com/article",
                    1,
                    None,
                    {"fetch_curl_hedge_delay_seconds": 0.8},
                    {},
                    str(tmp_path / "cookies.txt"),
                )

        assert executor.shutdown_calls == 1
        assert [workspace.cleanup.call_count for workspace in workspaces] == [1, 1]

    @patch("backend.tools.web_tools._fetch_with_curl")
    def test_curl_hedge_accepts_completed_before_deadline_and_strips_timestamp(
        self,
        mock_fetch_curl,
        tmp_path,
    ):
        from backend.tools import web_tools

        executor = _ImmediateExecutor()
        mock_fetch_curl.return_value = {
            "status_code": 200,
            "final_url": "https://example.com/article",
            "content_type": "text/plain",
            "body": "completed before deadline",
            "transport": "curl",
            "profile": "desktop",
            "_curl_completed_at": 100.5,
        }

        with patch(
            "backend.tools.web_tools.ThreadPoolExecutor",
            return_value=executor,
        ), patch(
            "backend.tools.web_tools.time.monotonic",
            side_effect=[100.0, 100.0, 101.1],
        ):
            response = web_tools._fetch_with_curl_hedged(
                "https://example.com/article",
                1,
                None,
                {"fetch_curl_hedge_delay_seconds": 0.8},
                {},
                str(tmp_path / "cookies.txt"),
            )

        try:
            assert response["body"] == "completed before deadline"
            assert "_curl_completed_at" not in response
        finally:
            response["_curl_hedge_workspace"].cleanup()

    def test_looks_like_block_page_detects_common_markers(self):
        from backend.tools.web_tools import _looks_like_block_page

        blocked_html = (
            "<html><head><title>Just a moment...</title></head>"
            "<body>Enable JavaScript and cookies to continue</body></html>"
        )
        normal_html = (
            "<html><body><article><h1>Normal article</h1><p>"
            + ("Useful article content. " * 30)
            + "</p></article></body></html>"
        )

        assert _looks_like_block_page(403, "text/html", blocked_html) is True
        assert _looks_like_block_page(200, "text/html", "<html>captcha required</html>")
        assert _looks_like_block_page(429, "text/html", normal_html) is True
        assert _looks_like_block_page(200, "text/html", normal_html) is False
        assert _looks_like_block_page(200, "text/markdown", "captcha required") is True

    @patch("backend.tools.web_tools._get_fetch_config")
    @patch("backend.tools.web_tools._fetch_with_curl")
    def test_web_fetch_rejects_short_extracted_content_as_failure(
        self,
        mock_fetch_curl,
        mock_get_fetch_config,
    ):
        from backend.tools.web_tools import _fetch_webpage_content

        mock_get_fetch_config.return_value = {
            "fetch_proxy": None,
            "fetch_transport": "curl",
            "fetch_fallback_transport": None,
            "fetch_impersonate": "chrome",
            "fetch_retry_count": 0,
            "fetch_retry_backoff_seconds": 0.0,
            "fetch_min_text_chars": 200,
            "fetch_enable_readability_fallback": True,
            "fetch_block_markers": None,
        }
        mock_fetch_curl.return_value = {
            "status_code": 200,
            "final_url": "https://example.com/challenge",
            "content_type": "text/html",
            "body": "<html><body>Please verify you are not a robot.</body></html>",
            "transport": "curl",
            "profile": "desktop",
        }

        with pytest.raises(RuntimeError, match="low quality extracted content"):
            _fetch_webpage_content("https://example.com/challenge", timeout=10)

    @patch("backend.tools.web_tools._extract_text_with_readability")
    @patch("backend.tools.web_tools._extract_text_with_trafilatura")
    def test_extract_response_text_uses_readability_only_when_primary_too_short(
        self,
        mock_trafilatura,
        mock_readability,
    ):
        from backend.tools.web_tools import _extract_response_text_local as _extract_response_text

        html = "<html><body><article><p>short text</p></article></body></html>"
        mock_trafilatura.return_value = "brief text"
        mock_readability.return_value = "expanded readable content " * 20

        result = _extract_response_text(
            body=html,
            content_type="text/html",
            min_text_chars=120,
            enable_readability_fallback=True,
        )

        assert "expanded readable content" in result
        mock_readability.assert_called_once()

    @patch("backend.tools.web_tools._get_fetch_config")
    @patch("backend.tools.web_tools._fetch_with_curl_hedged")
    @patch("backend.tools.web_tools._fetch_with_curl_cffi_hedged")
    def test_web_fetch_preserves_remaining_timeout_across_transport_attempts(
        self,
        mock_fetch_cffi,
        mock_fetch_curl,
        mock_get_fetch_config,
    ):
        from backend.tools.web_tools import web_fetch

        success_html = (
            "<html><body><article><h1>Recovered article</h1><p>"
            + ("Recovered content within shared time budget. " * 40)
            + "</p></article></body></html>"
        )

        mock_get_fetch_config.return_value = {
            "fetch_proxy": None,
            "fetch_transport": "curl_cffi",
            "fetch_fallback_transport": "curl",
            "fetch_impersonate": "chrome",
            "fetch_retry_count": 0,
            "fetch_retry_backoff_seconds": 0.0,
            "fetch_min_text_chars": 120,
            "fetch_enable_readability_fallback": True,
            "fetch_block_markers": None,
        }
        mock_fetch_cffi.side_effect = TimeoutError("primary timed out")
        mock_fetch_curl.return_value = {
            "status_code": 200,
            "final_url": "https://example.com/article",
            "content_type": "text/html",
            "body": success_html,
            "transport": "curl",
            "profile": "desktop",
        }

        result = web_fetch("https://example.com/article", timeout=10)

        assert "Recovered content within shared time budget." in result
        assert 0 < mock_fetch_cffi.call_args.kwargs["timeout"] <= 10
        assert 0 < mock_fetch_curl.call_args.kwargs["timeout"] < mock_fetch_cffi.call_args.kwargs["timeout"]

    @patch("backend.tools.web_tools._get_fetch_config")
    @patch("backend.tools.web_tools._fetch_with_curl")
    @patch("backend.tools.web_tools._fetch_with_curl_cffi")
    def test_web_fetch_falls_back_after_timeout_on_primary_transport(
        self,
        mock_fetch_cffi,
        mock_fetch_curl,
        mock_get_fetch_config,
    ):
        from backend.tools.web_tools import web_fetch

        success_html = (
            "<html><body><article><h1>Recovered article</h1><p>"
            + ("Recovered content after timeout fallback. " * 40)
            + "</p></article></body></html>"
        )

        mock_get_fetch_config.return_value = {
            "fetch_proxy": None,
            "fetch_transport": "curl_cffi",
            "fetch_fallback_transport": "curl",
            "fetch_impersonate": "chrome",
            "fetch_retry_count": 0,
            "fetch_retry_backoff_seconds": 0.0,
            "fetch_min_text_chars": 120,
            "fetch_enable_readability_fallback": True,
            "fetch_block_markers": None,
        }
        mock_fetch_cffi.side_effect = TimeoutError("primary timed out")
        mock_fetch_curl.return_value = {
            "status_code": 200,
            "final_url": "https://example.com/article",
            "content_type": "text/html",
            "body": success_html,
            "transport": "curl",
            "profile": "desktop",
        }

        result = web_fetch("https://example.com/article", timeout=12)

        assert "Recovered content after timeout fallback." in result
        assert mock_fetch_cffi.called
        assert mock_fetch_curl.called

    @patch("backend.tools.web_tools._get_fetch_config")
    @patch("backend.tools.web_tools._fetch_with_curl")
    @patch("backend.tools.web_tools._fetch_with_curl_cffi")
    def test_web_fetch_falls_back_after_blocked_primary_response(
        self,
        mock_fetch_cffi,
        mock_fetch_curl,
        mock_get_fetch_config,
    ):
        from backend.tools.web_tools import web_fetch

        blocked_html = (
            "<html><head><title>Just a moment...</title></head>"
            "<body>Enable JavaScript and cookies to continue</body></html>"
        )
        success_html = (
            "<html><body><article><h1>Recovered article</h1><p>"
            + ("Recovered content from fallback transport. " * 40)
            + "</p></article></body></html>"
        )

        mock_get_fetch_config.return_value = {
            "fetch_proxy": None,
            "fetch_transport": "curl_cffi",
            "fetch_fallback_transport": "curl",
            "fetch_impersonate": "chrome",
            "fetch_retry_count": 0,
            "fetch_retry_backoff_seconds": 0.0,
            "fetch_min_text_chars": 120,
            "fetch_enable_readability_fallback": True,
            "fetch_block_markers": None,
        }
        mock_fetch_cffi.return_value = {
            "status_code": 403,
            "final_url": "https://example.com/challenge",
            "content_type": "text/html",
            "body": blocked_html,
            "transport": "curl_cffi",
            "profile": "desktop",
        }
        mock_fetch_curl.return_value = {
            "status_code": 200,
            "final_url": "https://example.com/article",
            "content_type": "text/html",
            "body": success_html,
            "transport": "curl",
            "profile": "desktop",
        }

        result = web_fetch("https://example.com/article", timeout=12)

        assert "Recovered content from fallback transport." in result
        assert mock_fetch_cffi.called
        assert mock_fetch_curl.called

    @patch("backend.tools.web_tools._get_fetch_config")
    @patch("backend.tools.web_tools._fetch_with_curl")
    @patch("backend.tools.web_tools._fetch_with_curl_cffi")
    def test_web_fetch_falls_back_after_blocked_curl_cffi_feedback_without_profile_loop(
        self,
        mock_fetch_cffi,
        mock_fetch_curl,
        mock_get_fetch_config,
    ):
        from backend.tools.web_tools import web_fetch

        blocked_html = (
            "<html><head><title>Just a moment...</title></head>"
            "<body>Enable JavaScript and cookies to continue</body></html>"
        )
        success_html = (
            "<html><body><article><h1>Recovered article</h1><p>"
            + ("Recovered content after fallback transport. " * 40)
            + "</p></article></body></html>"
        )

        mock_get_fetch_config.return_value = {
            "fetch_proxy": None,
            "fetch_transport": "curl_cffi",
            "fetch_fallback_transport": "curl",
            "fetch_impersonate": "chrome",
            "fetch_retry_count": 0,
            "fetch_retry_backoff_seconds": 0.0,
            "fetch_min_text_chars": 120,
            "fetch_enable_readability_fallback": True,
            "fetch_block_markers": None,
        }
        mock_fetch_cffi.return_value = {
            "status_code": 403,
            "final_url": "https://example.com/challenge",
            "content_type": "text/html",
            "body": blocked_html,
            "transport": "curl_cffi",
            "profile": "desktop",
        }
        mock_fetch_curl.return_value = {
            "status_code": 200,
            "final_url": "https://example.com/article",
            "content_type": "text/html",
            "body": success_html,
            "transport": "curl",
            "profile": "desktop",
        }

        result = web_fetch("https://example.com/article", timeout=12)

        assert "Recovered content after fallback transport." in result
        assert mock_fetch_cffi.call_count == 1
        assert mock_fetch_curl.call_count == 1

    @patch("backend.tools.web_tools._get_fetch_config")
    @patch("backend.tools.web_tools._fetch_with_crawl4ai")
    @patch("backend.tools.web_tools._fetch_with_curl_hedged")
    def test_web_fetch_uses_curl_hedge_before_crawl4ai(
        self,
        mock_curl_hedged,
        mock_fetch_crawl4ai,
        mock_get_fetch_config,
    ):
        from backend.tools.web_tools import web_fetch

        mock_get_fetch_config.return_value = _curl_hedge_fetch_config()
        mock_curl_hedged.side_effect = TimeoutError("curl hedge timed out")
        mock_fetch_crawl4ai.side_effect = TimeoutError("crawl4ai timed out")

        result = web_fetch("https://example.com/article", timeout=45)

        assert "Failed to fetch webpage" in result
        mock_curl_hedged.assert_called_once()
        assert mock_curl_hedged.call_args.kwargs["timeout"] == 6
        assert mock_fetch_crawl4ai.call_args.kwargs["timeout"] == 15

    def test_curl_transport_retry_count_is_zero_when_hedging_is_enabled(self):
        from backend.tools.web_tools import _resolve_transport_retry_count

        assert _resolve_transport_retry_count("curl", {}, 3) == 0
        assert _resolve_transport_retry_count("curl_cffi", {}, 3) == 0

    @patch("backend.tools.web_tools._publish_curl_cookie_candidate")
    @patch("backend.tools.web_tools._fetch_with_curl_hedged")
    @patch("backend.tools.web_tools._get_fetch_config")
    def test_validated_curl_winner_publishes_cookie_and_cleans_workspace(
        self,
        mock_get_fetch_config,
        mock_curl_hedged,
        mock_publish,
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
        assert "_curl_hedge_workspace" not in result
        assert "_curl_cookie_candidate" not in result
        mock_publish.assert_called_once_with(candidate, _build_cookie_jar_path(url))
        workspace.cleanup.assert_called_once_with()

    @patch("backend.tools.web_tools._publish_curl_cookie_candidate")
    @patch("backend.tools.web_tools._fetch_with_crawl4ai")
    @patch("backend.tools.web_tools._fetch_with_curl_hedged")
    @patch("backend.tools.web_tools._get_fetch_config")
    def test_rejected_curl_winner_discards_cookie_before_crawl4ai(
        self,
        mock_get_fetch_config,
        mock_curl_hedged,
        mock_crawl4ai,
        mock_publish,
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

    def test_publish_curl_cookie_candidate_is_atomic_under_concurrency(self, tmp_path):
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

        assert all(not thread.is_alive() for thread in threads)
        assert stable.read_text(encoding="utf-8") in payloads
        assert list(tmp_path.glob("stable.cookies.*.tmp")) == []

    def test_publish_curl_cookie_candidate_uses_private_permissions(self, tmp_path):
        from backend.tools.web_tools import _publish_curl_cookie_candidate

        candidate = tmp_path / "candidate.cookies"
        candidate.write_text("private cookie", encoding="utf-8")
        candidate.chmod(0o644)
        stable = tmp_path / "cookie-dir" / "stable.cookies"

        _publish_curl_cookie_candidate(str(candidate), str(stable))

        assert stat.S_IMODE(stable.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(stable.stat().st_mode) == 0o600

    @patch("backend.tools.web_tools._fetch_with_curl_hedged")
    @patch("backend.tools.web_tools._get_fetch_config")
    def test_cookie_publication_copy_failure_cleans_stage_and_winner_workspace(
        self,
        mock_get_fetch_config,
        mock_curl_hedged,
        tmp_path,
    ):
        from backend.tools import web_tools

        stable = tmp_path / "stable.cookies"
        candidate = tmp_path / "candidate.cookies"
        stable.write_text("stable-generation", encoding="utf-8")
        candidate.write_text("invalid-generation", encoding="utf-8")
        workspace = MagicMock()
        cfg = _curl_hedge_fetch_config()
        cfg["fetch_fallback_transport"] = None
        mock_get_fetch_config.return_value = cfg
        mock_curl_hedged.return_value = {
            "status_code": 200,
            "final_url": "https://example.com/article",
            "content_type": "text/plain",
            "body": "validated article body " * 80,
            "transport": "curl",
            "profile": "desktop",
            "_curl_hedge_workspace": workspace,
            "_curl_cookie_candidate": str(candidate),
        }

        with patch(
            "backend.tools.web_tools._build_cookie_jar_path",
            return_value=str(stable),
        ), patch(
            "backend.tools.web_tools.shutil.copy2",
            side_effect=OSError("publication copy failed"),
        ):
            with pytest.raises(OSError, match="publication copy failed"):
                web_tools._fetch_webpage_content(
                    "https://example.com/article",
                    timeout=45,
                )

        assert stable.read_text(encoding="utf-8") == "stable-generation"
        assert list(tmp_path.glob("stable.cookies.*.tmp")) == []
        workspace.cleanup.assert_called_once_with()

    @patch("backend.tools.web_tools._fetch_with_curl_hedged")
    @patch("backend.tools.web_tools._get_fetch_config")
    def test_cookie_publication_replace_failure_cleans_stage_and_winner_workspace(
        self,
        mock_get_fetch_config,
        mock_curl_hedged,
        tmp_path,
    ):
        from backend.tools import web_tools

        stable = tmp_path / "stable.cookies"
        candidate = tmp_path / "candidate.cookies"
        stable.write_text("stable-generation", encoding="utf-8")
        candidate.write_text("invalid-generation", encoding="utf-8")
        workspace = MagicMock()
        cfg = _curl_hedge_fetch_config()
        cfg["fetch_fallback_transport"] = None
        mock_get_fetch_config.return_value = cfg
        mock_curl_hedged.return_value = {
            "status_code": 200,
            "final_url": "https://example.com/article",
            "content_type": "text/plain",
            "body": "validated article body " * 80,
            "transport": "curl",
            "profile": "desktop",
            "_curl_hedge_workspace": workspace,
            "_curl_cookie_candidate": str(candidate),
        }

        with patch(
            "backend.tools.web_tools._build_cookie_jar_path",
            return_value=str(stable),
        ), patch(
            "backend.tools.web_tools.os.replace",
            side_effect=OSError("publication replace failed"),
        ):
            with pytest.raises(OSError, match="publication replace failed"):
                web_tools._fetch_webpage_content(
                    "https://example.com/article",
                    timeout=45,
                )

        assert stable.read_text(encoding="utf-8") == "stable-generation"
        assert list(tmp_path.glob("stable.cookies.*.tmp")) == []
        workspace.cleanup.assert_called_once_with()

    @patch("backend.tools.web_tools._get_fetch_config")
    @patch("backend.tools.web_tools._fetch_with_crawl4ai")
    @patch("backend.tools.web_tools._fetch_with_curl_cffi")
    def test_web_fetch_falls_back_from_curl_cffi_to_crawl4ai(
        self,
        mock_fetch_cffi,
        mock_fetch_crawl4ai,
        mock_get_fetch_config,
    ):
        from backend.tools.web_tools import web_fetch

        mock_get_fetch_config.return_value = {
            "fetch_proxy": None,
            "fetch_transport": "curl_cffi",
            "fetch_fallback_transport": "crawl4ai",
            "fetch_content_fallback": "searxng_snippet",
            "fetch_impersonate": "chrome",
            "fetch_retry_count": 0,
            "fetch_retry_backoff_seconds": 0.0,
            "fetch_min_text_chars": 120,
            "fetch_enable_readability_fallback": True,
            "fetch_block_markers": None,
            "crawl4ai_browser_type": "chromium",
            "crawl4ai_headless": True,
            "crawl4ai_wait_until": "networkidle",
            "crawl4ai_page_timeout_ms": 15000,
            "crawl4ai_word_count_threshold": 10,
            "crawl4ai_bypass_cache": True,
        }
        mock_fetch_cffi.side_effect = TimeoutError("primary timed out")
        mock_fetch_crawl4ai.return_value = {
            "status_code": 200,
            "final_url": "https://example.com/article",
            "content_type": "text/markdown",
            "body": "# Recovered article\n\n" + ("Recovered through Crawl4AI transport=crawl4ai. " * 60),
            "transport": "crawl4ai",
            "profile": "chromium",
        }

        result = web_fetch("https://example.com/article", timeout=12)

        assert "Recovered through Crawl4AI transport=crawl4ai" in result
        assert mock_fetch_cffi.called
        assert mock_fetch_crawl4ai.called
        assert mock_fetch_crawl4ai.return_value["transport"] == "crawl4ai"

    @patch("backend.tools.web_tools._get_fetch_config")
    @patch("backend.tools.web_tools._fetch_with_crawl4ai")
    @patch("backend.tools.web_tools._fetch_with_curl_cffi")
    def test_web_fetch_uses_transport_specific_timeouts_and_crawl4ai_retry_count(
        self,
        mock_fetch_cffi,
        mock_fetch_crawl4ai,
        mock_get_fetch_config,
    ):
        from backend.tools.web_tools import web_fetch

        mock_get_fetch_config.return_value = {
            "fetch_proxy": None,
            "fetch_transport": "curl_cffi",
            "fetch_fallback_transport": "crawl4ai",
            "fetch_content_fallback": "searxng_snippet",
            "fetch_impersonate": "chrome",
            "fetch_retry_count": 1,
            "fetch_retry_backoff_seconds": 0.0,
            "fetch_min_text_chars": 120,
            "fetch_enable_readability_fallback": True,
            "fetch_block_markers": None,
            "fetch_curl_cffi_timeout_seconds": 6,
            "fetch_curl_cffi_hedge_delay_seconds": 3,
            "fetch_crawl4ai_timeout_seconds": 15,
            "crawl4ai_retry_count": 0,
            "crawl4ai_browser_type": "chromium",
            "crawl4ai_headless": True,
            "crawl4ai_wait_until": "domcontentloaded",
            "crawl4ai_page_timeout_ms": 15000,
            "crawl4ai_word_count_threshold": 10,
            "crawl4ai_bypass_cache": True,
        }
        mock_fetch_cffi.side_effect = TimeoutError("primary timed out")
        mock_fetch_crawl4ai.side_effect = TimeoutError("crawl4ai timed out")

        result = web_fetch("https://example.com/article", timeout=45)

        assert "Failed to fetch webpage" in result
        assert mock_fetch_cffi.call_count == 2
        assert all(
            1 <= call.kwargs["timeout"] <= 6
            for call in mock_fetch_cffi.call_args_list
        )
        assert {call.kwargs["timeout"] for call in mock_fetch_crawl4ai.call_args_list} == {15}
        assert mock_fetch_crawl4ai.call_count == 1

    def test_crawl4ai_page_timeout_honors_per_call_timeout(self):
        from backend.tools.web_tools import _build_crawl4ai_run_config_kwargs
        kwargs = _build_crawl4ai_run_config_kwargs({'crawl4ai_page_timeout_ms':15000}, 3.25)
        assert kwargs['page_timeout'] == 3250

    @patch("backend.tools.web_tools._get_fetch_config")
    @patch("backend.tools.web_tools._fetch_with_crawl4ai")
    @patch("backend.tools.web_tools._fetch_with_curl_cffi")
    def test_web_fetch_rejects_blocked_final_crawl4ai_response(
        self,
        mock_fetch_cffi,
        mock_fetch_crawl4ai,
        mock_get_fetch_config,
    ):
        from backend.tools.web_tools import web_fetch

        mock_get_fetch_config.return_value = {
            "fetch_proxy": None,
            "fetch_transport": "curl_cffi",
            "fetch_fallback_transport": "crawl4ai",
            "fetch_content_fallback": "searxng_snippet",
            "fetch_impersonate": "chrome",
            "fetch_retry_count": 0,
            "fetch_retry_backoff_seconds": 0.0,
            "fetch_min_text_chars": 120,
            "fetch_enable_readability_fallback": True,
            "fetch_block_markers": None,
            "crawl4ai_browser_type": "chromium",
            "crawl4ai_headless": True,
            "crawl4ai_wait_until": "networkidle",
            "crawl4ai_page_timeout_ms": 15000,
            "crawl4ai_word_count_threshold": 10,
            "crawl4ai_bypass_cache": True,
        }
        mock_fetch_cffi.side_effect = TimeoutError("primary timed out")
        mock_fetch_crawl4ai.return_value = {
            "status_code": 200,
            "final_url": "https://example.com/challenge",
            "content_type": "text/markdown",
            "body": "captcha " + ("challenge page filler " * 80),
            "transport": "crawl4ai",
            "profile": "chromium",
        }

        result = web_fetch("https://example.com/article", timeout=12)

        assert result.startswith("Failed to fetch webpage:")
        assert "captcha" not in result
        assert mock_fetch_cffi.called
        assert mock_fetch_crawl4ai.called

    @patch("backend.tools.web_tools._get_fetch_config")
    @patch("backend.tools.web_tools._fetch_with_curl_cffi")
    def test_web_fetch_returns_curl_cffi_transport_marker(
        self,
        mock_fetch_cffi,
        mock_get_fetch_config,
    ):
        from backend.tools.web_tools import web_fetch

        mock_get_fetch_config.return_value = {
            "fetch_proxy": None,
            "fetch_transport": "curl_cffi",
            "fetch_fallback_transport": None,
            "fetch_impersonate": "chrome",
            "fetch_retry_count": 0,
            "fetch_retry_backoff_seconds": 0.0,
            "fetch_min_text_chars": 120,
            "fetch_enable_readability_fallback": True,
            "fetch_block_markers": None,
        }
        mock_fetch_cffi.return_value = {
            "status_code": 200,
            "final_url": "https://example.com/article",
            "content_type": "text/plain",
            "body": "transport=curl_cffi " + ("successful fetch body " * 80),
            "transport": "curl_cffi",
            "profile": "desktop",
        }

        result = web_fetch("https://example.com/article", timeout=12)

        assert "transport=curl_cffi" in result
