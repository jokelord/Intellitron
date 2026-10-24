"""Tests for config parsing defaults."""

import yaml

from backend.config import AgentConfig, ConfigLoader, PROJECT_ROOT, WebToolsConfig


class TestParseWebToolsConfig:
    def _make_loader_with_data(self, web_tools_data: dict) -> ConfigLoader:
        loader = ConfigLoader.__new__(ConfigLoader)
        loader._data = {"web_tools": web_tools_data}
        loader._agent_prompts_data = {}
        return loader

    def test_parse_nested_searxng_config_and_fetch_settings(self):
        loader = self._make_loader_with_data(
            {
                "provider": "searxng",
                "enable_wikipedia": False,
                "fetch_proxy": "http://127.0.0.1:10808",
                "search_timeout": 10,
                "wikipedia_search_timeout": 23,
                "searxng": {
                    "base_url": "https://searx.example.com",
                    "search_path": "/search",
                    "timeout": 17,
                    "format": "json",
                    "language": "zh-CN",
                    "safe_search": 1,
                    "max_pages": 5,
                    "categories": ["general", "science"],
                    "engines": ["engine-a", "engine-b"],
                    "api_token": "secret-token",
                    "verify_tls": False,
                },
                "fetch_transport": "curl_cffi",
                "fetch_fallback_transport": "crawl4ai",
                "fetch_content_fallback": "searxng_snippet",
                "fetch_impersonate": "chrome",
                "fetch_max_workers": 4,
                "fetch_retry_count": 3,
                "fetch_retry_backoff_seconds": 1.5,
                "fetch_min_text_chars": 180,
                "fetch_quality_min_text_chars": 321,
                "fetch_quality_min_text_lines": 9,
                "fetch_enable_readability_fallback": True,
                "fetch_block_markers": ["captcha", "just a moment"],
                "crawl4ai_browser_type": "chromium",
                "crawl4ai_headless": True,
                "crawl4ai_wait_until": "domcontentloaded",
                "crawl4ai_page_timeout_ms": 15000,
                "crawl4ai_word_count_threshold": 10,
                "crawl4ai_bypass_cache": True,
                "fetch_curl_timeout_seconds": 7,
                "fetch_curl_hedge_delay_seconds": 2,
                "fetch_curl_max_response_bytes": 123456,
                "fetch_curl_cffi_timeout_seconds": 6,
                "fetch_curl_cffi_hedge_delay_seconds": 3,
                "fetch_crawl4ai_timeout_seconds": 15,
                "crawl4ai_retry_count": 0,
            }
        )

        cfg = loader._parse_web_tools_config()

        assert isinstance(cfg, WebToolsConfig)
        assert cfg.provider == "searxng"
        assert cfg.enable_wikipedia is False
        assert cfg.fetch_proxy == "http://127.0.0.1:10808"
        assert cfg.search_timeout == 10
        assert cfg.wikipedia_search_timeout == 23
        assert cfg.searxng_base_url == "https://searx.example.com"
        assert cfg.searxng_search_path == "/search"
        assert cfg.searxng_timeout == 17
        assert cfg.searxng_format == "json"
        assert cfg.searxng_language == "zh-CN"
        assert cfg.searxng_safe_search == 1
        assert cfg.searxng_max_pages == 5
        assert cfg.searxng_categories == ["general", "science"]
        assert cfg.searxng_engines == ["engine-a", "engine-b"]
        assert cfg.searxng_api_token == "secret-token"
        assert cfg.searxng_verify_tls is False
        assert cfg.fetch_transport == "curl_cffi"
        assert cfg.fetch_fallback_transport == "crawl4ai"
        assert cfg.fetch_content_fallback == "searxng_snippet"
        assert cfg.fetch_impersonate == "chrome"
        assert cfg.fetch_max_workers == 4
        assert cfg.fetch_retry_count == 3
        assert cfg.fetch_retry_backoff_seconds == 1.5
        assert cfg.fetch_min_text_chars == 180
        assert cfg.fetch_quality_min_text_chars == 321
        assert cfg.fetch_quality_min_text_lines == 9
        assert cfg.fetch_enable_readability_fallback is True
        assert cfg.fetch_block_markers == ["captcha", "just a moment"]
        assert cfg.crawl4ai_browser_type == "chromium"
        assert cfg.crawl4ai_headless is True
        assert cfg.crawl4ai_wait_until == "domcontentloaded"
        assert cfg.crawl4ai_page_timeout_ms == 15000
        assert cfg.crawl4ai_word_count_threshold == 10
        assert cfg.crawl4ai_bypass_cache is True
        assert cfg.fetch_curl_timeout_seconds == 7
        assert cfg.fetch_curl_hedge_delay_seconds == 2
        assert cfg.fetch_curl_max_response_bytes == 123456
        assert cfg.fetch_curl_cffi_timeout_seconds == 6
        assert cfg.fetch_curl_cffi_hedge_delay_seconds == 3
        assert cfg.fetch_crawl4ai_timeout_seconds == 15
        assert cfg.crawl4ai_retry_count == 0

    def test_wikipedia_search_timeout_falls_back_to_search_timeout(self):
        loader = self._make_loader_with_data(
            {
                "provider": "searxng",
                "search_timeout": 14,
                "searxng": {},
            }
        )

        cfg = loader._parse_web_tools_config()

        assert cfg.wikipedia_search_timeout == 14

    def test_parse_nested_searxng_string_values(self):
        loader = self._make_loader_with_data(
            {
                "provider": "searxng",
                "searxng": {
                    "timeout": "21",
                    "safe_search": "2",
                    "verify_tls": "false",
                    "categories": "general, science",
                    "engines": "engine-a, engine-b",
                },
            }
        )

        cfg = loader._parse_web_tools_config()

        assert cfg.searxng_timeout == 21
        assert cfg.searxng_safe_search == 2
        assert cfg.searxng_verify_tls is False
        assert cfg.searxng_categories == ["general", "science"]
        assert cfg.searxng_engines == ["engine-a", "engine-b"]

    def test_invalid_nested_searxng_values_fall_back_to_defaults(self):
        loader = self._make_loader_with_data(
            {
                "provider": "searxng",
                "searxng": {
                    "timeout": "not-a-number",
                    "safe_search": "invalid",
                    "verify_tls": "maybe",
                },
            }
        )

        cfg = loader._parse_web_tools_config()

        assert cfg.searxng_timeout == 10
        assert cfg.searxng_safe_search == 0
        assert cfg.searxng_verify_tls is True

    def test_malformed_nested_searxng_block_falls_back_to_defaults(self):
        loader = self._make_loader_with_data(
            {
                "provider": "searxng",
                "searxng": "not-a-dict",
            }
        )

        cfg = loader._parse_web_tools_config()

        assert cfg.provider == "searxng"
        assert cfg.searxng_base_url == "http://127.0.0.1:8080"
        assert cfg.searxng_search_path == "/search"
        assert cfg.searxng_timeout == 10
        assert cfg.searxng_format == "json"
        assert cfg.searxng_language == "all"
        assert cfg.searxng_safe_search == 0
        assert cfg.searxng_max_pages == 3
        assert cfg.searxng_categories == []
        assert cfg.searxng_engines == []
        assert cfg.searxng_api_token is None
        assert cfg.searxng_verify_tls is True

    def test_rejects_unsupported_provider(self):
        loader = self._make_loader_with_data({"provider": "google"})

        try:
            loader._parse_web_tools_config()
            assert False, "expected ValueError"
        except ValueError as exc:
            assert "web_tools.provider" in str(exc)
            assert "searxng" in str(exc)

    def test_null_or_empty_base_url_falls_back_to_default(self):
        for value in (None, "", "   "):
            loader = self._make_loader_with_data(
                {
                    "provider": "searxng",
                    "searxng": {"base_url": value},
                }
            )

            cfg = loader._parse_web_tools_config()

            assert cfg.searxng_base_url == "http://127.0.0.1:8080"

    def test_explicit_null_non_optional_fields_fall_back_to_defaults(self):
        loader = self._make_loader_with_data(
            {
                "provider": None,
                "search_timeout": None,
                "wikipedia_search_timeout": None,
                "fetch_transport": None,
                "fetch_max_workers": None,
                "fetch_retry_count": None,
                "fetch_retry_backoff_seconds": None,
                "fetch_curl_max_response_bytes": None,
                "fetch_min_text_chars": None,
                "fetch_enable_readability_fallback": None,
                "searxng": {
                    "base_url": None,
                    "timeout": None,
                    "safe_search": None,
                },
            }
        )

        cfg = loader._parse_web_tools_config()

        assert cfg.provider == "searxng"
        assert cfg.search_timeout == 10
        assert cfg.wikipedia_search_timeout == 10
        assert cfg.fetch_transport == "auto"
        assert cfg.fetch_max_workers == 3
        assert cfg.fetch_retry_count == 2
        assert cfg.fetch_retry_backoff_seconds == 1.0
        assert cfg.fetch_min_text_chars == 200
        assert cfg.fetch_quality_min_text_chars == 1000
        assert cfg.fetch_quality_min_text_lines == 6
        assert cfg.fetch_enable_readability_fallback is True
        assert cfg.fetch_content_fallback is None
        assert cfg.crawl4ai_browser_type == "chromium"
        assert cfg.crawl4ai_headless is True
        assert cfg.crawl4ai_wait_until == "domcontentloaded"
        assert cfg.crawl4ai_page_timeout_ms == 15000
        assert cfg.crawl4ai_word_count_threshold == 10
        assert cfg.crawl4ai_bypass_cache is True
        assert cfg.fetch_curl_hedge_delay_seconds == 3
        assert cfg.fetch_curl_max_response_bytes == 8_388_608
        assert cfg.fetch_curl_cffi_timeout_seconds == 6
        assert cfg.fetch_curl_cffi_hedge_delay_seconds == 3

    def test_quality_thresholds_reject_invalid_values(self):
        loader = self._make_loader_with_data(
            {
                "fetch_quality_min_text_chars": -1,
                "fetch_quality_min_text_lines": 2.5,
            }
        )

        cfg = loader._parse_web_tools_config()

        assert cfg.fetch_quality_min_text_chars == 1000
        assert cfg.fetch_quality_min_text_lines == 6
        assert cfg.fetch_crawl4ai_timeout_seconds == 15
        assert cfg.crawl4ai_retry_count == 0
        assert cfg.searxng_base_url == "http://127.0.0.1:8080"
        assert cfg.searxng_timeout == 10
        assert cfg.searxng_safe_search == 0
        assert cfg.searxng_max_pages == 3

    def test_config_agent_operation_uses_searxng_provider_block(self):
        config_path = PROJECT_ROOT / "config" / "config-agent-operation.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        web_tools = config["web_tools"]
        searxng = web_tools["searxng"]

        assert web_tools["provider"] == "searxng"
        assert "searxng" in web_tools
        assert "search_engine" not in web_tools
        assert "fallback_engine" not in web_tools
        assert "protocol" not in web_tools
        assert "fallback_protocol" not in web_tools
        assert searxng["base_url"]
        assert searxng["search_path"]
        assert searxng["timeout"] > 0
        assert searxng["format"] == "json"
        assert web_tools["fetch_proxy"] is None
        assert web_tools["fetch_transport"] == "curl"
        assert web_tools["fetch_fallback_transport"] == "crawl4ai"
        assert web_tools["fetch_content_fallback"] == "searxng_snippet"
        assert web_tools["crawl4ai_browser_type"] == "chromium"
        assert web_tools["crawl4ai_enable_stealth"] is True
        assert web_tools["crawl4ai_headless"] is True
        assert web_tools["crawl4ai_wait_until"] == "domcontentloaded"
        assert web_tools["crawl4ai_page_timeout_ms"] > 0
        assert web_tools["crawl4ai_word_count_threshold"] > 0
        assert web_tools["crawl4ai_bypass_cache"] is True
        assert web_tools["crawl4ai_magic"] is False
        assert web_tools["fetch_curl_timeout_seconds"] == 6
        assert web_tools["fetch_curl_hedge_delay_seconds"] == 3
        assert web_tools["fetch_curl_max_response_bytes"] == 8_388_608
        assert web_tools["fetch_curl_cffi_timeout_seconds"] == 6
        assert web_tools["fetch_curl_cffi_hedge_delay_seconds"] == 3
        assert web_tools["fetch_crawl4ai_timeout_seconds"] > 0
        assert web_tools["crawl4ai_retry_count"] == 0

    def test_config_agent_operation_uses_process_isolated_primary_fetch_transport(self):
        config_path = PROJECT_ROOT / "config" / "config-agent-operation.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        assert config["web_tools"]["fetch_transport"] == "curl"

    def test_config_agent_operation_allows_the_full_fetch_fallback_chain_to_finish(self):
        config_path = PROJECT_ROOT / "config" / "config-agent-operation.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        assert config["web_tools"]["search_timeout"] == 45


class TestParseAgentConfig:
    def _make_loader_with_data(self, agent_data: dict) -> ConfigLoader:
        loader = ConfigLoader.__new__(ConfigLoader)
        loader._data = {"agent": agent_data}
        loader._agent_prompts_data = {}
        return loader

    def test_code_agent_mode_maps_to_tool_calling(self):
        loader = self._make_loader_with_data({"agent_mode": "code"})

        cfg = loader._parse_agent_config()

        assert isinstance(cfg, AgentConfig)
        assert cfg.agent_mode == "tool_calling"

    def test_parse_agent_python_executable(self):
        loader = self._make_loader_with_data(
            {"python_executable": "/opt/custom-agent/bin/python"}
        )

        cfg = loader._parse_agent_config()

        assert cfg.python_executable == "/opt/custom-agent/bin/python"

    def test_agent_python_executable_defaults_to_agent_uv_path(self):
        loader = self._make_loader_with_data({})

        cfg = loader._parse_agent_config()

        assert cfg.python_executable == "~/.agent/bin/python"
