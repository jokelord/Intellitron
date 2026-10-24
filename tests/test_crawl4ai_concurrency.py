"""Tests for Crawl4AI global page budget (crash containment is tested in process pool tests)."""

import asyncio
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest


def _make_loader_with_data(web_tools_data: dict):
    from backend.config import ConfigLoader

    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {"web_tools": web_tools_data}
    loader._agent_prompts_data = {}
    return loader


class TestCrawl4aiMaxConcurrencyConfig:
    def test_default_is_fifty(self):
        from backend.config import WebToolsConfig

        assert WebToolsConfig().fetch_crawl4ai_max_concurrency == 50

    def test_loader_parses_explicit_value(self):
        loader = _make_loader_with_data(
            {"provider": "searxng", "fetch_crawl4ai_max_concurrency": 3}
        )
        cfg = loader._parse_web_tools_config()
        assert cfg.fetch_crawl4ai_max_concurrency == 3

    def test_loader_falls_back_to_fifty_when_missing_or_invalid(self):
        for data in (
            {"provider": "searxng"},
            {"provider": "searxng", "fetch_crawl4ai_max_concurrency": None},
            {"provider": "searxng", "fetch_crawl4ai_max_concurrency": 0},
            {"provider": "searxng", "fetch_crawl4ai_max_concurrency": -1},
            {"provider": "searxng", "fetch_crawl4ai_max_concurrency": "bad"},
        ):
            loader = _make_loader_with_data(data)
            cfg = loader._parse_web_tools_config()
            assert cfg.fetch_crawl4ai_max_concurrency == 50

    def test_get_fetch_config_includes_concurrency(self):
        from backend.tools import web_tools

        class WebTools:
            proxy = None
            fetch_transport = "curl"
            fetch_fallback_transport = "crawl4ai"
            fetch_content_fallback = "searxng_snippet"
            fetch_impersonate = "chrome"
            fetch_max_workers = 3
            fetch_retry_count = 0
            fetch_retry_backoff_seconds = 0.0
            fetch_curl_timeout_seconds = 6
            fetch_curl_hedge_delay_seconds = 3
            fetch_curl_max_response_bytes = 8_388_608
            fetch_curl_cffi_timeout_seconds = 6
            fetch_curl_cffi_hedge_delay_seconds = 3
            fetch_crawl4ai_timeout_seconds = 15
            fetch_crawl4ai_max_concurrency = 50
            fetch_min_text_chars = 200
            fetch_enable_readability_fallback = True
            fetch_block_markers = []
            crawl4ai_browser_type = "chromium"
            crawl4ai_headless = True
            crawl4ai_wait_until = "domcontentloaded"
            crawl4ai_page_timeout_ms = 15000
            crawl4ai_word_count_threshold = 10
            crawl4ai_bypass_cache = True
            crawl4ai_retry_count = 0
            crawl4ai_enable_stealth = True
            crawl4ai_magic = False

        class Config:
            web_tools = WebTools()

        with patch("backend.tools.web_tools.get_config", return_value=Config()):
            cfg = web_tools._get_fetch_config()

        assert cfg["fetch_crawl4ai_max_concurrency"] == 50
        assert cfg["crawl4ai_enable_stealth"] is True
        assert cfg["crawl4ai_magic"] is False

    def test_crawl4ai_runtime_defaults_are_configurable(self):
        from backend.config import WebToolsConfig

        assert WebToolsConfig().crawl4ai_enable_stealth is True
        assert WebToolsConfig().crawl4ai_magic is False
        loader = _make_loader_with_data({
            "crawl4ai_enable_stealth": False,
            "crawl4ai_magic": True,
        })
        cfg = loader._parse_web_tools_config()
        assert cfg.crawl4ai_enable_stealth is False
        assert cfg.crawl4ai_magic is True

    def test_crawl4ai_magic_is_opt_in_run_config(self):
        from backend.tools.web_tools import _build_crawl4ai_run_config_kwargs

        assert "magic" not in _build_crawl4ai_run_config_kwargs(
            {"crawl4ai_magic": False}, 2
        )
        assert _build_crawl4ai_run_config_kwargs(
            {"crawl4ai_magic": True}, 2
        )["magic"] is True

    def test_get_fetch_config_fallback_includes_concurrency(self):
        from backend.tools import web_tools

        with patch(
            "backend.tools.web_tools.get_config",
            side_effect=RuntimeError("no config"),
        ):
            cfg = web_tools._get_fetch_config()

        assert cfg["fetch_crawl4ai_max_concurrency"] == 50


class TestCrawl4aiConcurrencyLimit:
    def test_ten_concurrent_fetches_never_exceed_limit(self):
        from backend.tools import web_tools

        # reset process-level slot state
        with web_tools._crawl4ai_sem_condition:
            web_tools._crawl4ai_active = 0

        tracker = {"active": 0, "max_active": 0, "lock": threading.Lock()}
        def isolated_call(*args, **kwargs):
            with tracker['lock']:
                tracker['active'] += 1
                tracker['max_active'] = max(tracker['max_active'],tracker['active'])
            try:
                time.sleep(.02)
                return {'transport':'crawl4ai'}
            finally:
                with tracker['lock']: tracker['active'] -= 1
        cfg = {
            "crawl4ai_browser_type": "chromium",
            "crawl4ai_headless": True,
            "crawl4ai_wait_until": "domcontentloaded",
            "crawl4ai_page_timeout_ms": 5000,
            "crawl4ai_word_count_threshold": 10,
            "crawl4ai_bypass_cache": True,
        }

        with patch.object(web_tools, "_worker_call", side_effect=isolated_call), patch.object(
            web_tools,
            "_get_fetch_config",
            return_value={"fetch_crawl4ai_max_concurrency": 2},
        ):
            # re-read limit inside workers via _get_crawl4ai_max_concurrency
            assert web_tools._get_crawl4ai_max_concurrency() == 2

            def _call(idx: int):
                return web_tools._fetch_with_crawl4ai(
                    url=f"https://example.com/{idx}",
                    timeout=10,
                    cfg=dict(cfg),
                    check_abort=None,
                )

            with ThreadPoolExecutor(max_workers=10) as pool:
                results = list(pool.map(_call, range(10)))

        assert len(results) == 10
        assert all(r["transport"] == "crawl4ai" for r in results)
        assert tracker["max_active"] <= 2

        with web_tools._crawl4ai_sem_condition:
            assert web_tools._crawl4ai_active == 0

    def test_waiting_worker_exits_promptly_on_abort(self):
        from backend.tools import web_tools

        with web_tools._crawl4ai_sem_condition:
            web_tools._crawl4ai_active = 0

        with patch.object(
            web_tools,
            "_get_fetch_config",
            return_value={"fetch_crawl4ai_max_concurrency": 1},
        ):
            # occupy the only slot
            web_tools._acquire_crawl4ai_slot(check_abort=None, timeout=5)
            try:
                assert web_tools._get_crawl4ai_max_concurrency() == 1

                started = time.monotonic()
                with pytest.raises(web_tools.AbortError):
                    web_tools._acquire_crawl4ai_slot(
                        check_abort=lambda: True,
                        timeout=10,
                    )
                elapsed = time.monotonic() - started
                assert elapsed < 1.0
            finally:
                web_tools._release_crawl4ai_slot()

        with web_tools._crawl4ai_sem_condition:
            assert web_tools._crawl4ai_active == 0

    def test_acquire_timeout_raises_timeout_error(self):
        from backend.tools import web_tools

        with web_tools._crawl4ai_sem_condition:
            web_tools._crawl4ai_active = 0

        with patch.object(
            web_tools,
            "_get_fetch_config",
            return_value={"fetch_crawl4ai_max_concurrency": 1},
        ):
            web_tools._acquire_crawl4ai_slot(check_abort=None, timeout=5)
            try:
                started = time.monotonic()
                with pytest.raises(TimeoutError):
                    web_tools._acquire_crawl4ai_slot(
                        check_abort=None,
                        timeout=0.3,
                    )
                elapsed = time.monotonic() - started
                assert elapsed < 2.0
            finally:
                web_tools._release_crawl4ai_slot()
