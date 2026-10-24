"""Native fetch operations. Imported only by the disposable fetch worker.

Crawl4AI's dispatcher owns scheduling; this adapter adds request identity and
cancellation, and gives each request a fresh browser context (cookies/storage).
The browser and crawler stay alive across batches. No dependency source edits.
"""
import asyncio
from collections import deque
import inspect
import time
import uuid


def _normalise_fetch_proxy(value):
    """Return an explicit proxy URL or None; never inherit whitespace values."""
    if value is None:
        return None
    proxy = str(value).strip()
    return proxy or None


async def _close_stream(stream):
    """Close an explicit Crawl4AI stream when the public object supports it."""
    closer = getattr(stream, 'aclose', None) or getattr(stream, 'close', None)
    if closer is None:
        return
    result = closer()
    if inspect.isawaitable(result):
        await result


class CrawlRuntime:
    def __init__(self):
        self.pending = deque()
        self.runner = None
        self.crawlers = {}
        self.browser_lock = asyncio.Lock()
        self.batches = set()

    async def fetch(self, payload):
        loop = asyncio.get_running_loop()
        entry = {'payload': payload, 'future': loop.create_future(),
                 'done': asyncio.Event(), 'task': None, 'cancelled': False, 'ready': False}
        self.pending.append(entry)
        if self.runner is None or self.runner.done():
            self.runner = asyncio.create_task(self._drain())
        try:
            return await asyncio.shield(entry['future'])
        except asyncio.CancelledError:
            entry['cancelled'] = True
            if entry['task'] is not None:
                entry['task'].cancel()
                # ACK cancellation only after the browser context is closed.
                await entry['done'].wait()
            elif entry in self.pending:
                self.pending.remove(entry)
                entry['done'].set()
            elif entry['ready']:
                # Waiting in memory dispatch owns no URL resources. The flag
                # prevents any future admission, so targeted ACK is safe now.
                entry['done'].set()
            else:
                # Selected batch may still be starting its browser. Do not ACK
                # while startup owns resources; a hang triggers supervisor kill.
                await entry['done'].wait()
            # Selected but not dispatched entries cannot start after this flag.
            if not entry['future'].done():
                entry['future'].cancel()
            raise

    async def _drain(self):
        while self.pending:
            cfg = self.pending[0]['payload']['cfg']
            await asyncio.sleep(float(cfg.get('crawl4ai_batch_wait_seconds', .01)))
            if not self.pending:
                continue
            first = self.pending.popleft()
            cfg = first['payload']['cfg']
            batch = [first]
            # Matching config only, unique URLs per official dispatch batch.
            # A later duplicate gets an independent context in another batch.
            seen = {first['payload']['url']}
            for entry in list(self.pending):
                p = entry['payload']
                if p['cfg'] == cfg and p['url'] not in seen:
                    batch.append(entry); seen.add(p['url']); self.pending.remove(entry)
            task = asyncio.create_task(self._run_batch(batch, cfg))
            self.batches.add(task)
            task.add_done_callback(self.batches.discard)

    async def _run_batch(self, batch, cfg):
        try:
            await self._batch(batch, cfg)
        except Exception as exc:
            for entry in batch:
                if not entry['future'].done(): entry['future'].set_exception(exc)
        finally:
            for entry in batch: entry['done'].set()

    async def _batch(self, batch, cfg):
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode, ProxyConfig
        from crawl4ai.async_dispatcher import MemoryAdaptiveDispatcher
        from backend.tools.web_tools import _build_crawl4ai_run_config_kwargs
        proxy = _normalise_fetch_proxy(cfg.get('fetch_proxy'))
        enable_stealth = bool(cfg.get('crawl4ai_enable_stealth', True))
        key = (
            str(cfg.get('crawl4ai_browser_type') or 'chromium'),
            bool(cfg.get('crawl4ai_headless', True)),
            proxy,
            enable_stealth,
        )
        async with self.browser_lock:
            crawler = self.crawlers.get(key)
            if crawler is None:
                proxy_config = ProxyConfig(server=proxy) if proxy else None
                crawler = AsyncWebCrawler(config=BrowserConfig(
                    browser_type=key[0], headless=key[1], verbose=False,
                    proxy_config=proxy_config, enable_stealth=enable_stealth,
                ))
                try:
                    await crawler.start()
                except BaseException:
                    try: await crawler.close()
                    except BaseException:
                        import os
                        os._exit(70)
                    raise
                self.crawlers[key] = crawler
        for entry in batch: entry['ready'] = True
        by_url = {e['payload']['url']: e for e in batch}

        async def fetch_entry(url):
            entry = by_url[url]
            if entry['cancelled']:
                entry['done'].set()
                return None
            entry['task'] = asyncio.current_task()
            context = None
            session_id = uuid.uuid4().hex
            manager = getattr(getattr(crawler, 'crawler_strategy', None), 'browser_manager', None)
            try:
                kwargs = _build_crawl4ai_run_config_kwargs(cfg, entry['payload']['timeout'], CacheMode.BYPASS)
                kwargs.update(session_id=session_id, verbose=False)
                config = CrawlerRunConfig(**kwargs)
                if manager is not None:
                    # Crawl4AI 0.9 pools equal-config contexts. Explicitly own an
                    # isolated context so concurrent users never share cookies.
                    context = await manager.create_browser_context(config)
                    await manager.setup_context(context, config)
                    page = await context.new_page()
                    manager.sessions[session_id] = (context, page, time.time())
                result = await crawler.arun(url=url, config=config)
                if not getattr(result, 'success', True):
                    raise RuntimeError(str(getattr(result, 'error_message', 'crawl4ai fetch failed')))
                value = _response(result, url, key[0])
                return result, value
            finally:
                if manager is not None:
                    manager.sessions.pop(session_id, None)
                if context is not None:
                    try: await context.close()
                    except BaseException:
                        # A context that cannot close poisons this worker. The
                        # supervisor reaps its entire process group.
                        import os
                        os._exit(70)
                entry['done'].set()

        class RequestDispatcher(MemoryAdaptiveDispatcher):
            async def _fetch_request(self, url):
                # Also used by crawl_url below: routing is by original URL,
                # never result.url, which can be changed by a redirect.
                entry = by_url[url]
                try:
                    fetched = await fetch_entry(url)
                    if fetched is None: return None
                    result, value = fetched
                    if not entry['future'].done(): entry['future'].set_result(value)
                    return result
                except asyncio.CancelledError:
                    # Local cancellation must not stop sibling dispatcher jobs.
                    return None
                except Exception as exc:
                    if not entry['future'].done(): entry['future'].set_exception(exc)
                    return None
                finally:
                    entry['done'].set()

            async def crawl_url(self, url, config, task_id, retry_count=0):
                # Keep the official dispatcher's memory scheduling, replace only
                # the per-URL invocation to own cancellation/context cleanup.
                from crawl4ai.models import CrawlerTaskResult, CrawlResult
                started = time.time()
                result = await self._fetch_request(url)
                if result is None:
                    result = CrawlResult(url=url, html='', success=False, error_message='Fetch cancelled or failed')
                return CrawlerTaskResult(task_id=task_id, url=url, result=result,
                    memory_usage=0, peak_memory=0, start_time=started,
                    end_time=time.time(), error_message='', retry_count=retry_count)

        threshold = float(cfg.get('crawl4ai_memory_threshold_percent', 85))
        dispatcher = RequestDispatcher(
            memory_threshold_percent=threshold,
            recovery_threshold_percent=max(0, threshold-5),
            critical_threshold_percent=min(100, threshold+5),
            check_interval=float(cfg.get('crawl4ai_memory_check_interval_seconds', .1)),
            max_session_permit=int(cfg.get('fetch_crawl4ai_max_concurrency', 50)),
            memory_wait_timeout=max(e['payload']['timeout'] for e in batch),
        )
        config = CrawlerRunConfig(stream=True, verbose=False)
        # The official dispatcher starts its monitor asynchronously. Sample
        # before the first admission so the initial batch respects pressure too.
        from crawl4ai.utils import get_true_memory_usage_percent
        dispatcher.current_memory_percent = get_true_memory_usage_percent()
        dispatcher.memory_pressure_mode = dispatcher.current_memory_percent >= threshold
        completed = False
        try:
            stream = await crawler.arun_many(urls=list(by_url), config=config, dispatcher=dispatcher)
            try:
                async for _ in stream:
                    pass
            finally:
                await _close_stream(stream)
            completed = True
        finally:
            for entry in batch:
                if completed and not entry['future'].done() and not entry['cancelled']:
                    entry['future'].set_exception(RuntimeError('Crawl dispatcher returned no result'))
                entry['cancelled'] = True
            tasks = {e['task'] for e in batch if e['task'] is not None and e['task'] is not asyncio.current_task() and not e['task'].done()}
            for task in tasks: task.cancel()
            if tasks: await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        if self.runner is not None and not self.runner.done():
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        tasks = list(self.batches)
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        for crawler in self.crawlers.values(): await crawler.close()


def _response(result, url, profile):
    markdown = getattr(result, 'markdown', None) or getattr(result, 'fit_markdown', None)
    body, content_type = '', 'text/markdown'
    if markdown:
        body = str(next((getattr(markdown, k) for k in ('fit_markdown','raw_markdown','markdown') if getattr(markdown,k,None)), markdown))
    else:
        body = getattr(result, 'cleaned_html', None) or getattr(result, 'html', None)
        content_type = 'text/html'
        if not body:
            body = getattr(result, 'text', '') or ''
            content_type = 'text/plain'
    return {'status_code': int(getattr(result,'status_code',None) or 200),
            'final_url': str(getattr(result,'url',None) or url), 'body': str(body),
            'content_type': content_type, 'transport':'crawl4ai', 'profile':profile}


_runtime = None

async def handle(operation, payload):
    global _runtime
    if operation == 'crawl4ai':
        if _runtime is None: _runtime = CrawlRuntime()
        return await _runtime.fetch(payload)
    if operation == 'extract':
        from backend.tools.web_tools import _extract_response_text_local
        # Serial extraction in the worker avoids concurrent libxml2 calls.
        return _extract_response_text_local(**payload)
    if operation == 'curl_cffi':
        from curl_cffi.requests import AsyncSession
        async with AsyncSession() as session:
            response = await session.get(payload['url'], headers=payload['headers'],
                timeout=payload['timeout'], allow_redirects=True,
                impersonate=payload.get('impersonate') or 'chrome', proxy=payload.get('proxy'))
            return {'status_code': response.status_code, 'final_url': str(response.url),
                'content_type': response.headers.get('Content-Type',''), 'body': response.text or '',
                'transport':'curl_cffi','profile':'desktop'}
    raise ValueError(f'Unknown fetch operation: {operation}')

async def shutdown():
    if _runtime is not None: await _runtime.close()
