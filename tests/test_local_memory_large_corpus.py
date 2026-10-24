"""Large synthetic memory documents must survive native write-batch boundaries."""
import asyncio
from dataclasses import replace
import json

import pytest

from backend.retrieval.config import LocalMemoryConfig
from backend.retrieval.corpus import MemoryCorpus
from backend.retrieval.local_memory import LocalMemoryService


class CharacterTokenizer:
    def encode(self, text, add_special_tokens=True):
        return list(range(len(text)))


class KeyedEncoder:
    tokenizer = CharacterTokenizer()
    fingerprint = 'large-synthetic-corpus-v1'

    def __init__(self):
        self.encoded = 0

    def encode_query(self, text):
        return [1.0] + [0.0] * 383

    def encode_documents(self, texts):
        self.encoded += len(texts)
        return [([1.0, 0.0] if 'COPIEDPASSAGE' in text else [0.0, 1.0]) + [0.0] * 382
                for text in texts]


@pytest.fixture(autouse=True)
def isolated_startup(monkeypatch):
    monkeypatch.delenv('LOCAL_MEMORY_STARTUP_GATE', raising=False)
    monkeypatch.delenv('LOCAL_MEMORY_STARTUP_NONCE', raising=False)
    monkeypatch.setenv('WEB_CONCURRENCY', '1')
    monkeypatch.setenv('UVICORN_WORKERS', '1')


def memory_config():
    base = LocalMemoryConfig(source_dir='synthetic-notes', state_dir='synthetic-index')
    return replace(base, chunking=replace(base.chunking, max_chars=200, overlap_chars=16),
        retrieval=replace(base.retrieval, min_similarity=.82, timeout_ms=4000))


async def start_service(tmp_path, encoder):
    service = LocalMemoryService(memory_config(), project_root=tmp_path, encoder=encoder)
    await service.start()
    await asyncio.wait_for(asyncio.shield(service._startup_task), timeout=30)
    return service


def synthetic_novel():
    excerpt = ('COPIEDPASSAGE星河旅人踏上远航途中记录每一颗恒星的颜色和位置' * 5)[:100]
    pieces = []
    for index in range(1600):
        pieces.append((excerpt + ' ' + 'q' * 50 if index == 800 else
            f'Chapter{index:05d} ' + 'ab' * 80 + '星河远航记忆') + '\n\n')
    data = ''.join(pieces).encode('utf-8')[:291 * 1024]
    text = data.decode('utf-8', errors='ignore')
    text += 'x' * (291 * 1024 - len(text.encode('utf-8')))
    assert excerpt in text
    return text, excerpt


@pytest.mark.parametrize('suffix', ['md', 'txt'])
def test_second_added_291kib_novel_indexes_and_copied_excerpt_is_searchable(tmp_path, suffix):
    async def scenario():
        source = tmp_path / 'synthetic-notes'
        source.mkdir()
        (source / 'first.md').write_text('A small initial preference note.', encoding='utf-8')
        encoder = KeyedEncoder()
        first = await start_service(tmp_path, encoder)
        try:
            assert first.status_snapshot()['indexed_files'] == 1
        finally:
            await first.shutdown()

        novel, excerpt = synthetic_novel()
        filename = f'novel.{suffix}'
        (source / filename).write_text(novel, encoding='utf-8')
        assert (source / filename).stat().st_size == 291 * 1024
        second = await start_service(tmp_path, encoder)
        try:
            status = second.status_snapshot()
            assert status['status'] == 'ready', status
            assert status['indexed_files'] == 2
            records = second._corpus.manifest['files'].values()
            novel_record = next(item for item in records if item['source_path'] == filename)
            assert len(novel_record['chunk_ids']) > 1024
            assert encoder.encoded == 1 + len(novel_record['chunk_ids'])
            assert set(novel_record['chunk_ids']) <= second._corpus.store.all_ids()
            result = await second.prepare_user_turn(excerpt, enabled=True, context_validator=lambda _: True)
            assert result.status == 'matched'
            assert any('COPIEDPASSAGE' in text for text in result.memory_texts)
            assert {item.source for item in result.sources} == {filename}
            before_reopen = encoder.encoded
        finally:
            await second.shutdown()

        reopened = await start_service(tmp_path, encoder)
        try:
            assert reopened.status_snapshot()['indexed_files'] == 2
            assert encoder.encoded == before_reopen
            result = await reopened.prepare_user_turn(excerpt, enabled=True, context_validator=lambda _: True)
            assert result.status == 'matched'
            assert any('COPIEDPASSAGE' in text for text in result.memory_texts)
        finally:
            await reopened.shutdown()
    asyncio.run(scenario())


def test_large_file_batch_failure_does_not_publish_and_restart_recovers(tmp_path):
    async def scenario():
        source = tmp_path / 'synthetic-notes'
        source.mkdir()
        (source / 'first.md').write_text('A small initial preference note.', encoding='utf-8')
        encoder = KeyedEncoder()
        first = await start_service(tmp_path, encoder)
        await first.shutdown()
        manifest_path = tmp_path / 'synthetic-index/manifest.json'
        before_manifest = manifest_path.read_bytes()
        novel, excerpt = synthetic_novel()
        (source / 'novel.txt').write_text(novel, encoding='utf-8')
        corpus = MemoryCorpus(memory_config(), tmp_path, encoder)
        corpus.recover()
        corpus.set_encoder(encoder)
        snapshots, complete = corpus.scan()
        assert complete
        insert = corpus.store.collection.insert
        calls = 0
        def fail_second_batch(docs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('synthetic second native batch failure')
            return insert(docs)
        corpus.store.collection.insert = fail_second_batch
        try:
            with pytest.raises(RuntimeError, match='synthetic second native batch'):
                corpus.index_file('novel.txt', snapshots['novel.txt'])
            assert manifest_path.read_bytes() == before_manifest
            assert len(corpus.manifest['files']) == 1
            assert len(corpus.store.all_ids()) == 1025  # First note + one staged batch.
        finally:
            corpus.store.collection.insert = insert
            corpus.close()
        recovered = await start_service(tmp_path, encoder)
        try:
            assert recovered.status_snapshot()['status'] == 'ready'
            active = {key for item in recovered._corpus.manifest['files'].values() for key in item['chunk_ids']}
            assert recovered._corpus.store.all_ids() == active
            assert recovered._corpus.store.fetch_ids(sorted(active)) == active
            assert {row['id'] for row in recovered._corpus.store.fetch_rows(sorted(active))} == active
            assert not list((tmp_path / 'synthetic-index/journal').glob('*.json'))
            result = await recovered.prepare_user_turn(excerpt, enabled=True, context_validator=lambda _: True)
            assert result.status == 'matched'
            assert any('COPIEDPASSAGE' in text for text in result.memory_texts)
        finally:
            await recovered.shutdown()
    asyncio.run(scenario())


def test_modified_large_file_replaces_exact_revision_and_deletion_clears_all_batches(tmp_path):
    async def scenario():
        source = tmp_path / 'synthetic-notes'
        source.mkdir()
        (source / 'first.md').write_text('A small initial preference note.', encoding='utf-8')
        novel, excerpt = synthetic_novel()
        (source / 'novel.txt').write_text(novel, encoding='utf-8')
        encoder = KeyedEncoder()
        first = await start_service(tmp_path, encoder)
        try:
            assert first.status_snapshot()['status'] == 'ready'
            previous = json.loads((tmp_path / 'synthetic-index/manifest.json').read_text())
            old_records = {item['source_path']: item for item in previous['files'].values()}
            old_ids = set(old_records['novel.txt']['chunk_ids'])
            assert len(old_ids) > 1024
        finally:
            await first.shutdown()
        (source / 'novel.txt').write_text(novel.replace('星河', '蓝海'), encoding='utf-8')
        modified = await start_service(tmp_path, encoder)
        try:
            assert modified.status_snapshot()['status'] == 'ready'
            current = {item['source_path']: item for item in modified._corpus.manifest['files'].values()}
            assert current['first.md'] == old_records['first.md']
            new_ids = set(current['novel.txt']['chunk_ids'])
            assert old_ids.isdisjoint(new_ids)
            assert modified._corpus.store.all_ids() == new_ids | set(current['first.md']['chunk_ids'])
            result = await modified.prepare_user_turn(excerpt.replace('星河', '蓝海'), enabled=True,
                context_validator=lambda _: True)
            assert result.status == 'matched'
            assert any('COPIEDPASSAGE' in text for text in result.memory_texts)
            before_delete_encoding = encoder.encoded
        finally:
            await modified.shutdown()
        (source / 'novel.txt').unlink()
        deleted = await start_service(tmp_path, encoder)
        try:
            assert deleted.status_snapshot()['indexed_files'] == 1
            assert encoder.encoded == before_delete_encoding
            assert deleted._corpus.store.all_ids() == set(current['first.md']['chunk_ids'])
        finally:
            await deleted.shutdown()
    asyncio.run(scenario())
