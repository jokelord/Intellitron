"""Installed Crawl4AI arun_many and dispatcher, with only browser/network faked."""
import asyncio
from types import SimpleNamespace
import pytest

@pytest.fixture
def browser_layer(monkeypatch):
    import crawl4ai
    import crawl4ai.async_dispatcher as dispatch_module
    import crawl4ai.utils as utils
    from crawl4ai.models import CrawlResult, MarkdownGenerationResult
    official_many = crawl4ai.AsyncWebCrawler.arun_many
    state = SimpleNamespace(crawlers=[], configs=[], batches=[], started=[], contexts=[], dispatchers=[],
                            memory=10, hold=False, held_urls=set(), active=0, peak=0,
                            startup_hold=False, startup_entered=False)
    monkeypatch.setattr(utils, 'get_true_memory_usage_percent', lambda: state.memory)
    monkeypatch.setattr(dispatch_module, 'get_true_memory_usage_percent', lambda: state.memory)
    class Context:
        def __init__(self):
            self.closed = False
            self.number = len(state.contexts)
            state.contexts.append(self)
        async def new_page(self): return SimpleNamespace(context=self)
        async def close(self):
            await asyncio.sleep(.02)
            self.closed = True
    class Manager:
        def __init__(self): self.sessions = {}
        async def create_browser_context(self, config): return Context()
        async def setup_context(self, context, config): pass
    class Crawler:
        def __init__(self, **kwargs):
            state.configs.append(kwargs['config'])
            self.browser_type = kwargs['config'].browser_type
            self.crawler_strategy = SimpleNamespace(browser_manager=Manager())
            self.closed = False
            state.crawlers.append(self)
        async def start(self):
            state.startup_entered = True
            while state.startup_hold: await asyncio.sleep(.005)
        async def close(self): self.closed = True
        async def arun_many(self, urls, config, dispatcher):
            state.batches.append(list(urls))
            state.dispatchers.append(dispatcher)
            return await official_many(self, urls, config=config, dispatcher=dispatcher)
        async def arun(self, url, config):
            context = self.crawler_strategy.browser_manager.sessions[config.session_id][0]
            state.started.append((url, context))
            state.active += 1
            state.peak = max(state.active, state.peak)
            try:
                while state.hold or url in state.held_urls: await asyncio.sleep(.005)
                await asyncio.sleep(.01)
                return CrawlResult(url='https://same-redirect.example/', html='<p>article</p>',
                    success=True, markdown=MarkdownGenerationResult(
                        raw_markdown=f'{url} context={context.number}', markdown_with_citations='',
                        references_markdown=''), status_code=200)
            finally: state.active -= 1
    monkeypatch.setattr(crawl4ai, 'AsyncWebCrawler', Crawler)
    return state

def payload(url, **cfg):
    return {'url': url, 'timeout': 2, 'cfg': {'crawl4ai_batch_wait_seconds': .005,
        'crawl4ai_memory_check_interval_seconds': .01, 'crawl4ai_memory_threshold_percent': 85,
        'fetch_crawl4ai_max_concurrency': 50, **cfg}}

async def until(predicate):
    async def wait():
        while not predicate(): await asyncio.sleep(.005)
    await asyncio.wait_for(wait(), 1)

def test_official_multi_url_dispatch_is_concurrent_and_reuses_crawler(browser_layer):
    from backend.tools.fetch_worker_runtime import CrawlRuntime
    state = browser_layer
    async def run():
        runtime = CrawlRuntime()
        try:
            values = await asyncio.gather(*(runtime.fetch(payload(f'https://site{i}.example')) for i in range(3)))
            assert all(value['final_url'] == 'https://same-redirect.example/' for value in values)
            assert all(f'https://site{i}.example' in values[i]['body'] for i in range(3))
            assert state.peak == 3
            assert len(state.batches[0]) == 3
            await runtime.fetch(payload('https://later.example'))
            assert len(state.crawlers) == 1
            assert all(context.closed for context in state.contexts)
        finally: await runtime.close()
    asyncio.run(run())

def test_browser_config_receives_proxy_and_identity_does_not_cross_proxies(browser_layer):
    from backend.tools.fetch_worker_runtime import CrawlRuntime
    state = browser_layer
    async def run():
        runtime = CrawlRuntime()
        try:
            await runtime.fetch(payload('https://proxy-one.example', fetch_proxy=' http://proxy.example:8080 '))
            await runtime.fetch(payload('https://proxy-two.example', fetch_proxy='http://other-proxy.example:8080'))
            assert len(state.configs) == 2
            assert state.configs[0].proxy_config.server == 'http://proxy.example:8080'
            assert state.configs[1].proxy_config.server == 'http://other-proxy.example:8080'
            assert all(config.enable_stealth is True for config in state.configs)
        finally:
            await runtime.close()
    asyncio.run(run())

def test_null_empty_and_whitespace_proxy_create_direct_browser_config(browser_layer):
    from backend.tools.fetch_worker_runtime import CrawlRuntime
    state = browser_layer
    async def run():
        runtime = CrawlRuntime()
        try:
            await runtime.fetch(payload('https://null-proxy.example', fetch_proxy=None))
            await runtime.fetch(payload('https://empty-proxy.example', fetch_proxy='   '))
            assert len(state.configs) == 1
            assert state.configs[0].proxy_config is None
        finally:
            await runtime.close()
    asyncio.run(run())

def test_supported_stream_close_is_awaited():
    from backend.tools.fetch_worker_runtime import _close_stream

    class Stream:
        def __init__(self): self.closed = False
        def __aiter__(self): return self
        async def __anext__(self): raise StopAsyncIteration
        async def aclose(self): self.closed = True

    stream = Stream()
    asyncio.run(_close_stream(stream))
    assert stream.closed is True

def test_supported_arbitrary_awaitable_stream_close_is_awaited():
    from backend.tools.fetch_worker_runtime import _close_stream

    class Awaitable:
        def __init__(self): self.done = False
        def __await__(self):
            async def finish(): self.done = True
            return finish().__await__()
    class Stream:
        def __init__(self): self.result = Awaitable()
        def close(self): return self.result

    stream = Stream()
    asyncio.run(_close_stream(stream))
    assert stream.result.done is True

def test_initial_memory_pressure_blocks_admission_then_recovers(browser_layer):
    from backend.tools.fetch_worker_runtime import CrawlRuntime
    state = browser_layer
    state.memory = 95
    async def run():
        runtime = CrawlRuntime()
        task = asyncio.create_task(runtime.fetch(payload('https://memory.example')))
        try:
            await until(lambda: state.dispatchers)
            await asyncio.sleep(.05)
            assert not state.started
            assert state.dispatchers[0].memory_pressure_mode
            state.memory = 50
            assert (await asyncio.wait_for(task, 1))['status_code'] == 200
            assert not state.dispatchers[0].memory_pressure_mode
        finally: await runtime.close()
    asyncio.run(run())

def test_target_cancel_waits_context_cleanup_and_preserves_sibling(browser_layer):
    from backend.tools.fetch_worker_runtime import CrawlRuntime
    state = browser_layer
    state.hold = True
    async def run():
        runtime = CrawlRuntime()
        target = asyncio.create_task(runtime.fetch(payload('https://target.example')))
        sibling = asyncio.create_task(runtime.fetch(payload('https://sibling.example')))
        try:
            await until(lambda: len(state.started) == 2)
            context = next(c for url, c in state.started if 'target' in url)
            target.cancel()
            with pytest.raises(asyncio.CancelledError): await target
            assert context.closed
            assert not sibling.done()
            assert not state.crawlers[0].closed
            state.hold = False
            assert 'sibling' in (await asyncio.wait_for(sibling, 1))['body']
        finally: await runtime.close()
    asyncio.run(run())

def test_duplicate_urls_use_distinct_contexts_and_redirect_keeps_identity(browser_layer):
    from backend.tools.fetch_worker_runtime import CrawlRuntime
    state = browser_layer
    async def run():
        runtime = CrawlRuntime()
        try:
            urls = ['https://duplicate.example', 'https://different.example', 'https://duplicate.example']
            values = await asyncio.gather(*(runtime.fetch(payload(url)) for url in urls))
            assert len(state.batches) == 2
            assert values[0]['body'] != values[2]['body']
            for url, value in zip(urls, values):
                assert url in value['body']
                assert value['final_url'] == 'https://same-redirect.example/'
            assert len(state.contexts) == 3
            assert all(context.closed for context in state.contexts)
        finally: await runtime.close()
    asyncio.run(run())

def test_official_dispatcher_enforces_page_limit(browser_layer):
    from backend.tools.fetch_worker_runtime import CrawlRuntime
    state = browser_layer
    async def run():
        runtime = CrawlRuntime()
        try:
            values = await asyncio.gather(*(runtime.fetch(payload(f'https://limit{i}.example',
                fetch_crawl4ai_max_concurrency=2)) for i in range(5)))
            assert len(values) == 5
            assert state.peak == 2
        finally: await runtime.close()
    asyncio.run(run())

def test_late_arrival_does_not_wait_for_earlier_slow_batch(browser_layer):
    from backend.tools.fetch_worker_runtime import CrawlRuntime
    state = browser_layer
    state.held_urls.add('https://slow.example')
    async def run():
        runtime = CrawlRuntime()
        slow = asyncio.create_task(runtime.fetch(payload('https://slow.example')))
        try:
            await until(lambda: state.started)
            fast = await asyncio.wait_for(runtime.fetch(payload('https://fast.example')), .5)
            assert 'fast' in fast['body']
            assert not slow.done()
            state.held_urls.clear()
            await asyncio.wait_for(slow, 1)
        finally: await runtime.close()
    asyncio.run(run())

def test_memory_waiting_target_cancels_without_killing_sibling(browser_layer):
    from backend.tools.fetch_worker_runtime import CrawlRuntime
    state = browser_layer
    state.memory = 95
    async def run():
        runtime = CrawlRuntime()
        target = asyncio.create_task(runtime.fetch(payload('https://cancel-queued.example')))
        sibling = asyncio.create_task(runtime.fetch(payload('https://keep-queued.example')))
        try:
            await until(lambda: state.dispatchers)
            assert not state.started
            target.cancel()
            done, _ = await asyncio.wait({target}, timeout=.15)
            assert target in done, 'memory-waiting URL owns no page and should cancel promptly'
            with pytest.raises(asyncio.CancelledError): await target
            assert not sibling.done()
            state.memory = 50
            assert 'keep-queued' in (await asyncio.wait_for(sibling, 1))['body']
        finally:
            state.memory = 50
            await runtime.close()
            await asyncio.gather(target, sibling, return_exceptions=True)
    asyncio.run(run())

def test_startup_cancellation_does_not_ack_before_startup_finishes(browser_layer):
    from backend.tools.fetch_worker_runtime import CrawlRuntime
    state = browser_layer
    state.startup_hold = True
    async def run():
        runtime = CrawlRuntime()
        target = asyncio.create_task(runtime.fetch(payload('https://startup.example')))
        try:
            await until(lambda: state.startup_entered)
            target.cancel()
            await asyncio.sleep(.05)
            assert not target.done(), 'startup can still own live browser resources'
            state.startup_hold = False
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(target, 1)
            assert not state.started
        finally:
            state.startup_hold = False
            await runtime.close()
            await asyncio.gather(target, return_exceptions=True)
    asyncio.run(run())

def test_cancelled_first_pending_url_does_not_lend_its_config_to_survivor(browser_layer):
    from backend.tools.fetch_worker_runtime import CrawlRuntime
    state = browser_layer
    async def run():
        runtime = CrawlRuntime()
        first = asyncio.create_task(runtime.fetch(payload('https://first.example',
            crawl4ai_batch_wait_seconds=.05, crawl4ai_browser_type='chromium')))
        survivor = asyncio.create_task(runtime.fetch(payload('https://survivor.example',
            crawl4ai_batch_wait_seconds=.05, crawl4ai_browser_type='firefox')))
        try:
            await asyncio.sleep(.02)
            first.cancel()
            with pytest.raises(asyncio.CancelledError): await first
            await asyncio.wait_for(survivor, 1)
            assert [crawler.browser_type for crawler in state.crawlers] == ['firefox']
        finally:
            await runtime.close()
            await asyncio.gather(first, survivor, return_exceptions=True)
    asyncio.run(run())

def test_hung_real_runtime_startup_is_killed_and_worker_recovers(tmp_path, monkeypatch):
    """Real supervisor + real runtime; replace only browser launch in the child."""
    import os
    from backend.tools.fetch_process_pool import FetchProcessPool
    helper = tmp_path / 'hung_crawler_fixture.py'
    marker = tmp_path / 'startup_entered'
    helper.write_text('''import asyncio
import os
import crawl4ai
from backend.tools import fetch_worker_runtime as runtime
class Crawler:
    def __init__(self, **kwargs): pass
    async def start(self):
        with open(os.environ['FETCH_STARTUP_MARKER'], 'w') as marker:
            marker.write('started')
        await asyncio.Event().wait()
    async def close(self): pass
crawl4ai.AsyncWebCrawler = Crawler
async def handle(operation, payload):
    if operation == 'pid': return os.getpid()
    return await runtime.handle(operation, payload)
async def shutdown(): await runtime.shutdown()
''')
    monkeypatch.setenv('PYTHONPATH', str(tmp_path) + os.pathsep + os.environ.get('PYTHONPATH', ''))
    monkeypatch.setenv('FETCH_STARTUP_MARKER', str(marker))
    pool = FetchProcessPool(worker_count=1, cancel_grace_seconds=.1,
                           handler_module='hung_crawler_fixture')
    try:
        old = pool.call('pid', {}, 10)
        with pytest.raises(TimeoutError):
            pool.call('crawl4ai', payload('https://hung-startup.example'), .3)
        assert marker.read_text() == 'started'
        with pytest.raises(ProcessLookupError): os.kill(old, 0)
        assert pool.call('pid', {}, 10) != old
    finally:
        pool.close()
