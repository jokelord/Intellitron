"""Tests for SearXNG-backed web search behavior."""

import socket
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import backend.tools.web_tools as web_tools
import pytest


def _make_web_tools_config(**overrides):
    values = {
        "provider": "searxng",
        "searxng_base_url": "https://searx.example.com",
        "searxng_search_path": "/search",
        "searxng_timeout": 17,
        "searxng_format": "json",
        "searxng_language": "zh-CN",
        "searxng_safe_search": 1,
        "searxng_max_pages": 3,
        "searxng_categories": ["general", "science"],
        "searxng_engines": ["engine-a", "engine-b"],
        "searxng_api_token": "secret-token",
        "searxng_verify_tls": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _DummyResponse:
    def __init__(self, body: str, status: int = 200):
        self._body = body.encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TestSearXNGWebSearch:
    def test_build_search_request_includes_pageno(self):
        request = web_tools._build_searxng_search_request(
            "llm deployment",
            {
                "searxng_base_url": "https://searx.example.com",
                "searxng_search_path": "/search",
                "searxng_format": "json",
                "searxng_language": "zh-CN",
                "searxng_safe_search": 0,
                "searxng_categories": [],
                "searxng_engines": [],
            },
            pageno=3,
        )

        assert "pageno=3" in request.full_url

    def test_web_search_does_not_swallow_config_validation_errors(self):
        with patch.object(
            web_tools,
            "get_config",
            side_effect=ValueError(
                "Unsupported web_tools.provider='google'; expected 'searxng'"
            ),
        ), patch.object(
            web_tools,
            "_search_with_searxng",
        ) as mock_search:
            with pytest.raises(ValueError) as exc:
                web_tools.web_search("llm deployment")

        assert str(exc.value) == "Unsupported web_tools.provider='google'; expected 'searxng'"
        mock_search.assert_not_called()

    def test_web_search_uses_searxng_json_and_skips_bad_results(self):
        response = _DummyResponse(
            """
            {
              "results": [
                {"title": "Valid A", "url": "https://a.example.com", "content": "Alpha snippet"},
                {"title": "", "url": "https://skip-empty-title.example.com", "content": "bad"},
                {"title": "Missing URL", "content": "bad"},
                {"url": "https://skip-missing-title.example.com", "content": "bad"},
                {"title": "Bad Scheme", "url": "ftp://files.example.com", "content": "bad"},
                "not-a-dict",
                {"title": "Valid B", "url": "https://b.example.com", "content": "Beta snippet"}
              ]
            }
            """
        )
        opener = MagicMock()
        opener.open.return_value = response

        with patch.object(
            web_tools,
            "get_config",
            return_value=SimpleNamespace(web_tools=_make_web_tools_config()),
        ), patch("urllib.request.build_opener", return_value=opener):
            result = web_tools.web_search("llm deployment", max_results=5)

        assert "## Search Results" in result
        assert "[Valid A](https://a.example.com)" in result
        assert "Alpha snippet" in result
        assert "[Valid B](https://b.example.com)" in result
        assert "Beta snippet" in result
        assert "skip-empty-title" not in result
        assert "files.example.com" not in result
        assert "Missing URL" not in result
        assert "not-a-dict" not in result
        request = opener.open.call_args.args[0]
        assert request.full_url.startswith("https://searx.example.com/search?")
        assert "q=llm+deployment" in request.full_url
        assert "format=json" in request.full_url
        assert "language=zh-CN" in request.full_url
        assert "safesearch=1" in request.full_url
        assert "categories=general%2Cscience" in request.full_url
        assert "engines=engine-a%2Cengine-b" in request.full_url
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert opener.open.call_args.kwargs["timeout"] == 17

    def test_searxng_opener_always_bypasses_backend_proxy(self):
        with patch("urllib.request.build_opener") as mock_build_opener:
            web_tools._build_searxng_opener(verify_tls=True)

        proxy_handler = mock_build_opener.call_args.args[0]
        assert isinstance(proxy_handler, urllib.request.ProxyHandler)
        assert proxy_handler.proxies == {}

    def test_web_search_aggregates_multiple_pages_until_max_results(self):
        page_1 = _DummyResponse(
            '{"results": ['
            '{"title": "Result A", "url": "https://a.example.com", "content": "Alpha"},'
            '{"title": "Result B", "url": "https://b.example.com", "content": "Beta"}'
            ']}'
        )
        page_2 = _DummyResponse(
            '{"results": ['
            '{"title": "Result B duplicate", "url": "https://b.example.com", "content": "Beta dupe"},'
            '{"title": "Result C", "url": "https://c.example.com", "content": "Gamma"}'
            ']}'
        )
        opener = MagicMock()
        opener.open.side_effect = [page_1, page_2]

        with patch.object(
            web_tools,
            "get_config",
            return_value=SimpleNamespace(
                web_tools=_make_web_tools_config(searxng_max_pages=4)
            ),
        ), patch("urllib.request.build_opener", return_value=opener):
            result = web_tools.web_search("llm deployment", max_results=3)

        assert "[Result A](https://a.example.com)" in result
        assert "[Result B](https://b.example.com)" in result
        assert "[Result C](https://c.example.com)" in result
        assert "Result B duplicate" not in result
        assert opener.open.call_count == 2
        first_request = opener.open.call_args_list[0].args[0]
        second_request = opener.open.call_args_list[1].args[0]
        assert "pageno=1" in first_request.full_url
        assert "pageno=2" in second_request.full_url

    def test_web_search_returns_partial_results_when_later_page_fails(self):
        page_1 = _DummyResponse(
            '{"results": ['
            '{"title": "Result A", "url": "https://a.example.com", "content": "Alpha"}'
            ']}'
        )
        opener = MagicMock()
        opener.open.side_effect = [
            page_1,
            urllib.error.HTTPError(
                url="https://searx.example.com/search?pageno=2",
                code=503,
                msg="Service Unavailable",
                hdrs=None,
                fp=None,
            ),
        ]

        with patch.object(
            web_tools,
            "get_config",
            return_value=SimpleNamespace(
                web_tools=_make_web_tools_config(searxng_max_pages=4)
            ),
        ), patch("urllib.request.build_opener", return_value=opener):
            result = web_tools.web_search("llm deployment", max_results=3)

        assert "[Result A](https://a.example.com)" in result
        assert opener.open.call_count == 2

    def test_web_search_maps_unreachable_instance(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError(OSError("connection refused"))

        with patch.object(
            web_tools,
            "get_config",
            return_value=SimpleNamespace(web_tools=_make_web_tools_config()),
        ), patch("urllib.request.build_opener", return_value=opener):
            result = web_tools.web_search("llm deployment")

        assert result == "Search failed: SearXNG instance is unreachable."

    def test_web_search_maps_timeout(self):
        opener = MagicMock()
        opener.open.side_effect = socket.timeout("timed out")

        with patch.object(
            web_tools,
            "get_config",
            return_value=SimpleNamespace(web_tools=_make_web_tools_config()),
        ), patch("urllib.request.build_opener", return_value=opener):
            result = web_tools.web_search("llm deployment")

        assert result == "Search failed: SearXNG request timed out."

    def test_web_search_maps_invalid_json(self):
        opener = MagicMock()
        opener.open.return_value = _DummyResponse("not-json")

        with patch.object(
            web_tools,
            "get_config",
            return_value=SimpleNamespace(web_tools=_make_web_tools_config()),
        ), patch("urllib.request.build_opener", return_value=opener):
            result = web_tools.web_search("llm deployment")

        assert result == "Search failed: SearXNG returned invalid JSON."

    def test_web_search_maps_non_200_status(self):
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(
            url="https://searx.example.com/search",
            code=503,
            msg="Service Unavailable",
            hdrs=None,
            fp=None,
        )
        with patch.object(
            web_tools,
            "get_config",
            return_value=SimpleNamespace(web_tools=_make_web_tools_config()),
        ), patch("urllib.request.build_opener", return_value=opener):
            result = web_tools.web_search("llm deployment")

        assert result == "Search failed: SearXNG returned HTTP 503."

    def test_web_search_maps_empty_usable_results(self):
        opener = MagicMock()
        opener.open.return_value = _DummyResponse(
            '{"results": [{"title": "", "url": "https://bad.example.com"}, {"url": "https://still-bad.example.com"}]}'
        )
        with patch.object(
            web_tools,
            "get_config",
            return_value=SimpleNamespace(web_tools=_make_web_tools_config()),
        ), patch("urllib.request.build_opener", return_value=opener):
            result = web_tools.web_search("llm deployment")

        assert result == "No search results found. SearXNG returned no usable results."
