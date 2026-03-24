#!/usr/bin/env python
"""
Test web search using the custom web_search tool.
Reads configuration from config/config-misc.yaml
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.tools.web_tools import web_search, web_fetch

print("=== Testing web_search ===")
result = web_search("Python tutorial", max_results=3, use_fallback=False)
print(result)

print("=== Testing web_search (Chinese) ===")
result = web_search("张雪峰 逝世消息", max_results=3, use_fallback=False)
print(result)

print("=== Testing web_fetch ===")
result = web_fetch("https://example.com")
print(result[:500] if len(result) > 500 else result)
