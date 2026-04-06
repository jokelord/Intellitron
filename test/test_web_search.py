#!/usr/bin/env python
"""
Test web search using the custom web_search tool.
Reads configuration from config/config-misc.yaml
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.tools.web_tools import web_search, web_fetch


def test_web_search():
    print("=== Testing web_search ===")
    result = web_search("Python tutorial", max_results=3, use_fallback=False)
    print(result)
    assert "## Search Results" in result
    assert "Python" in result
    print("✓ web_search test passed")


def test_web_search_chinese():
    print("=== Testing web_search (Chinese) ===")
    result = web_search("张雪峰 逝世消息", max_results=3, use_fallback=False)
    print(result)
    assert "## Search Results" in result
    print("✓ web_search Chinese test passed")


def test_web_fetch():
    print("=== Testing web_fetch ===")
    result = web_fetch("https://example.com")
    print(result[:500] + "..." if len(result) > 500 else result)
    assert "# Content from https://example.com" in result
    assert "Example Domain" in result
    print("✓ web_fetch test passed")


if __name__ == "__main__":
    test_web_search()
    test_web_search_chinese()
    test_web_fetch()
    print("All tests passed!")
