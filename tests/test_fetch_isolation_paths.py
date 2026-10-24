"""Production facade isolation and end-to-end deadline guarantees."""
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


def test_production_parser_facade_does_not_import_native_parser_in_parent():
    code = '''
import sys
from backend.tools.web_tools import _extract_response_text, _transport_is_available
from backend.tools.fetch_process_pool import shutdown_fetch_pool
assert _transport_is_available('crawl4ai')
assert _transport_is_available('curl_cffi')
assert 'lxml.etree' not in sys.modules
try:
    result = _extract_response_text('<html><body><article>' + 'Article content. '*100 + '</article></body></html>', 'text/html', 10, True)
    assert 'Article content.' in result
    assert 'lxml.etree' not in sys.modules
    assert 'curl_cffi' not in sys.modules
    assert 'crawl4ai' not in sys.modules
finally:
    shutdown_fetch_pool()
'''
    subprocess.run([sys.executable, '-c', code], check=True, timeout=30)


def test_crawl_slot_is_held_until_worker_acknowledges_cleanup(monkeypatch):
    from backend.tools import web_tools as w
    entered = threading.Event()
    cleanup = threading.Event()
    monkeypatch.setattr(w, '_get_fetch_config', lambda: {'fetch_crawl4ai_max_concurrency':1})
    def worker(*args, **kwargs):
        entered.set()
        assert cleanup.wait(2)
        raise TimeoutError('cancelled after cleanup')
    monkeypatch.setattr(w, '_worker_call', worker)
    with ThreadPoolExecutor(max_workers=1) as pool:
        f = pool.submit(w._fetch_with_crawl4ai, 'https://example.test', 1, {})
        assert entered.wait(1)
        try:
            with pytest.raises(TimeoutError): w._acquire_crawl4ai_slot(timeout=.02)
            assert w._crawl4ai_active == 1
        finally: cleanup.set()
        with pytest.raises(TimeoutError): f.result()
    assert w._crawl4ai_active == 0


def test_queued_global_fetch_is_abortable_and_does_not_start(monkeypatch):
    from backend.tools import search_and_fetch as s
    monkeypatch.setattr(s, '_get_fetch_max_workers', lambda: 1)
    monkeypatch.setattr(s, '_fetch_budget_active', 1)
    with pytest.raises(s.AbortError):
        s._fetch_single_with_global_budget('https://example.test', 30, lambda: True)
    assert s._fetch_budget_active == 1


def test_zero_budget_never_starts_transport(monkeypatch):
    from backend.tools import web_tools as w
    monkeypatch.setattr(w, '_get_fetch_config', lambda: pytest.fail('No work after deadline'))
    with pytest.raises(TimeoutError): w._fetch_webpage_content('https://example.test', timeout=0)


def test_known_blocked_page_skips_native_parser_and_falls_back(monkeypatch):
    from backend.tools import web_tools as w
    monkeypatch.setattr(w, '_get_fetch_config', lambda: {'fetch_transport':'curl','fetch_fallback_transport':'crawl4ai'})
    monkeypatch.setattr(w, '_fetch_with_curl_hedged', lambda **kw: {'status_code':403,'body':'blocked'})
    monkeypatch.setattr(w, '_fetch_with_crawl4ai', lambda **kw: {'status_code':200,'body':'good '*300,'content_type':'text/plain'})
    bodies=[]
    def extract(body, *args, **kwargs):
        bodies.append(body)
        return body
    monkeypatch.setattr(w, '_extract_response_text', extract)
    assert 'good' in w._fetch_webpage_content('https://example.test')
    assert bodies == ['good '*300]
