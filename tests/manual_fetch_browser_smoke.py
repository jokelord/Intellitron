"""Opt-in real Chromium smoke test; requires local TCP sockets and browser deps.

Run from repository root:
    ./.sglang/bin/python tests/manual_fetch_browser_smoke.py

No external sites are contacted. Ordinary pytest does not collect this script.
"""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.tools.web_tools import _fetch_with_crawl4ai, _get_fetch_config
from backend.tools.fetch_process_pool import shutdown_fetch_pool


class Page(BaseHTTPRequestHandler):
    seen_cookies = []
    def do_GET(self):
        if self.path == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
            return
        self.seen_cookies.append(self.headers.get('Cookie', ''))
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Set-Cookie', 'isolated=private; Path=/')
        self.end_headers()
        text = ('Browser smoke article content for ' + self.path + '. ') * 80
        self.wfile.write(('<html><body><article>' + text + '</article></body></html>').encode())
    def log_message(self, *_):
        pass


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = dict(_get_fetch_config(), fetch_worker_processes=1,
               crawl4ai_wait_until='domcontentloaded')
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda n: _fetch_with_crawl4ai(
                base + '/' + str(n), 30, cfg), range(3)))
        results.append(_fetch_with_crawl4ai(base + '/next', 30, cfg))
        assert all('Browser smoke article' in result['body'] for result in results)
        # Ignore favicon/background requests: all pages have fresh contexts.
        assert all(not value for value in Page.seen_cookies), Page.seen_cookies
        print('PASS: four real browser page results, parallel batch and next request, no cookie carryover')
    finally:
        shutdown_fetch_pool()
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    main()
