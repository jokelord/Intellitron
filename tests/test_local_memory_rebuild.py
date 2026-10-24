"""Index-generation contracts using synthetic files and real native zvec only."""
import asyncio
from dataclasses import replace
import json

from backend.retrieval.config import ChunkingConfig, LocalMemoryConfig
from backend.retrieval.local_memory import LocalMemoryService


class CharacterTokenizer:
    def encode(self, text, add_special_tokens=True):
        return list(range(len(text) + (2 if add_special_tokens else 0)))


class RecordingEncoder:
    tokenizer = CharacterTokenizer()
    fingerprint = 'rebuild-contract-encoder-v1'

    def __init__(self):
        self.documents = []
        self.before_encode = None

    def encode_documents(self, texts):
        if self.before_encode:
            self.before_encode(texts)
        self.documents.extend(texts)
        return [[1.0] + [0.0] * 383 for _ in texts]

    def encode_query(self, text):
        return [1.0] + [0.0] * 383


def synthetic_corpus(tmp_path):
    # These paths always belong to pytest's temporary directory, never the repo.
    source = tmp_path / 'synthetic-notes'
    source.mkdir()
    (source / 'a.md').write_text('A' * 300, encoding='utf-8')
    (source / 'b.md').write_text('B' * 180, encoding='utf-8')
    return LocalMemoryConfig(
        source_dir='synthetic-notes', state_dir='synthetic-index',
        chunking=ChunkingConfig(max_chars=150, overlap_chars=10, max_tokens=320),
    )


async def start_service(tmp_path, config, encoder):
    service = LocalMemoryService(config, project_root=tmp_path, encoder=encoder)
    await service.start()
    await asyncio.wait_for(asyncio.shield(service._startup_task), timeout=10)
    return service


def manifest_bytes(tmp_path):
    return (tmp_path / 'synthetic-index' / 'manifest.json').read_bytes()


def isolate_startup(monkeypatch):
    monkeypatch.delenv('LOCAL_MEMORY_STARTUP_GATE', raising=False)
    monkeypatch.delenv('LOCAL_MEMORY_STARTUP_NONCE', raising=False)
    monkeypatch.setenv('WEB_CONCURRENCY', '1')
    monkeypatch.setenv('UVICORN_WORKERS', '1')


def test_max_chars_change_reencodes_every_file_and_publishes_new_generation(tmp_path, monkeypatch):
    """Ignoring max_chars in the generation fingerprint would reuse stale chunks."""
    isolate_startup(monkeypatch)

    async def scenario():
        original = synthetic_corpus(tmp_path)
        encoder = RecordingEncoder()
        first = await start_service(tmp_path, original, encoder)
        try:
            assert first.status_snapshot()['status'] == 'ready'
            assert len(encoder.documents) == 5  # A: 3 chunks, B: 2 chunks.
            previous_bytes = manifest_bytes(tmp_path)
            previous = json.loads(previous_bytes)
            previous_ids = first._corpus.store.all_ids()
        finally:
            await first.shutdown()

        observations = []

        def observe_publication(_texts):
            # A complete old generation stays durable even after the first new
            # file has finished encoding and staged its replacement rows.
            observations.append(manifest_bytes(tmp_path))

        encoder.before_encode = observe_publication
        changed = replace(original, chunking=replace(original.chunking, max_chars=90))
        second = await start_service(tmp_path, changed, encoder)
        try:
            assert second.status_snapshot()['status'] == 'ready'
            assert len(encoder.documents) == 12  # New A: 4 chunks, new B: 3.
            assert observations == [previous_bytes, previous_bytes]
            current = json.loads(manifest_bytes(tmp_path))
            assert current['fingerprint'] != previous['fingerprint']
            collections = tmp_path / 'synthetic-index' / 'collections'
            assert (collections / previous['fingerprint']).is_dir()
            assert (collections / current['fingerprint']).is_dir()
            assert len(current['files']) == 2
            assert previous_ids.isdisjoint(second._corpus.store.all_ids())
            assert second._corpus.store.all_ids() == {
                chunk_id for item in current['files'].values() for chunk_id in item['chunk_ids']
            }
            old_by_source = {item['source_path']: item for item in previous['files'].values()}
            new_by_source = {item['source_path']: item for item in current['files'].values()}
            assert len(new_by_source['a.md']['chunk_ids']) == 4
            assert len(new_by_source['b.md']['chunk_ids']) == 3
            for path in ('a.md', 'b.md'):
                old, new = old_by_source[path], new_by_source[path]
                assert old['sha256'] == new['sha256']
                assert old['mtime_ns'] == new['mtime_ns']
                assert old['revision_id'] != new['revision_id']
                assert set(old['chunk_ids']).isdisjoint(new['chunk_ids'])
        finally:
            await second.shutdown()

        encoder.before_encode = None
        third = await start_service(tmp_path, changed, encoder)
        try:
            assert third.status_snapshot()['status'] == 'ready'
            assert len(encoder.documents) == 12
            assert json.loads(manifest_bytes(tmp_path)) == current
        finally:
            await third.shutdown()

    asyncio.run(scenario())


def test_failed_chunk_rebuild_keeps_old_manifest_and_disables_retrieval(tmp_path, monkeypatch):
    """Publishing per-file progress during rebuild would replace valid durable authority."""
    isolate_startup(monkeypatch)

    async def scenario():
        original = synthetic_corpus(tmp_path)
        encoder = RecordingEncoder()
        first = await start_service(tmp_path, original, encoder)
        try:
            assert first.status_snapshot()['status'] == 'ready'
            previous_bytes = manifest_bytes(tmp_path)
        finally:
            await first.shutdown()

        def fail_second_file(texts):
            assert manifest_bytes(tmp_path) == previous_bytes
            if texts[0].startswith('B'):
                raise RuntimeError('synthetic second-file encoder failure')

        encoder.before_encode = fail_second_file
        changed = replace(original, chunking=replace(original.chunking, max_chars=90))
        failed = await start_service(tmp_path, changed, encoder)
        try:
            status = failed.status_snapshot()
            assert status['status'] == 'degraded'
            assert status['available'] is False
            assert status['error_code'] == 'generation_incomplete'
            assert manifest_bytes(tmp_path) == previous_bytes
            result = await failed.prepare_user_turn(
                'synthetic memory question', enabled=True, context_validator=lambda _: True,
            )
            assert result.status == 'unready'
            assert result.model_user_content == 'synthetic memory question'
        finally:
            await failed.shutdown()

        encoder.before_encode = None
        recovered = await start_service(tmp_path, changed, encoder)
        try:
            assert recovered.status_snapshot()['status'] == 'ready'
            current = json.loads(manifest_bytes(tmp_path))
            assert current['fingerprint'] != json.loads(previous_bytes)['fingerprint']
            assert len(current['files']) == 2
            assert len(recovered._corpus.store.all_ids()) == 7
        finally:
            await recovered.shutdown()

    asyncio.run(scenario())


def test_adjacent_distance_change_reuses_existing_vectors(tmp_path, monkeypatch):
    """A retrieval-only radius must not become part of index identity."""
    isolate_startup(monkeypatch)

    async def scenario():
        original = synthetic_corpus(tmp_path)
        original = replace(original, retrieval=replace(original.retrieval, adjacent_distance=0))
        encoder = RecordingEncoder()
        first = await start_service(tmp_path, original, encoder)
        try:
            assert first.status_snapshot()['status'] == 'ready'
            previous_bytes = manifest_bytes(tmp_path)
            previous_ids = first._corpus.store.all_ids()
            assert len(encoder.documents) == 5
        finally:
            await first.shutdown()

        expanded = replace(original, retrieval=replace(original.retrieval, adjacent_distance=2))
        second = await start_service(tmp_path, expanded, encoder)
        try:
            assert second.status_snapshot()['status'] == 'ready'
            assert len(encoder.documents) == 5
            assert manifest_bytes(tmp_path) == previous_bytes
            assert second._corpus.store.all_ids() == previous_ids
        finally:
            await second.shutdown()

    asyncio.run(scenario())
