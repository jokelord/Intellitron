"""Decoding policies exercised through temporary corpora and native zvec."""
import asyncio
import hashlib
import json

import pytest
import yaml

from backend.retrieval.config import load_local_memory_config
from backend.retrieval.local_memory import LocalMemoryService


class CharacterTokenizer:
    def encode(self, text, add_special_tokens=True):
        return list(range(len(text) + (2 if add_special_tokens else 0)))


class RecordingEncoder:
    tokenizer = CharacterTokenizer()
    fingerprint = 'synthetic-encoding-contract-v1'

    def __init__(self):
        self.documents = []

    def encode_documents(self, texts):
        self.documents.extend(texts)
        return [[1.0] + [0.0] * 383 for _ in texts]

    def encode_query(self, text):
        return [1.0] + [0.0] * 383


@pytest.fixture(autouse=True)
def isolate_startup(monkeypatch):
    monkeypatch.delenv('LOCAL_MEMORY_STARTUP_GATE', raising=False)
    monkeypatch.delenv('LOCAL_MEMORY_STARTUP_NONCE', raising=False)
    monkeypatch.setenv('WEB_CONCURRENCY', '1')
    monkeypatch.setenv('UVICORN_WORKERS', '1')


def config_for(tmp_path, *, encoding='auto', overrides=None):
    config_path = tmp_path / 'synthetic-config.yaml'
    config_path.write_text(yaml.safe_dump({'local_memory': {
        'source_dir': 'synthetic-notes', 'state_dir': 'synthetic-index',
        'decoding': {'encoding': encoding, 'overrides': {} if overrides is None else overrides},
        'retrieval': {'timeout_ms': 5000},
    }}, allow_unicode=True), encoding='utf-8')
    return load_local_memory_config(config_path, tmp_path)


async def start_service(tmp_path, config, encoder):
    service = LocalMemoryService(config, project_root=tmp_path, encoder=encoder)
    await service.start()
    await asyncio.wait_for(asyncio.shield(service._startup_task), timeout=10)
    return service


def read_manifest(tmp_path):
    return json.loads((tmp_path / 'synthetic-index' / 'manifest.json').read_bytes())


def test_decoding_configuration_defaults_aliases_and_exact_relative_overrides(tmp_path):
    config = config_for(tmp_path, encoding='UTF_8', overrides={'nested/PRIVATE.TXT': 'SHIFT-JIS'})
    assert config.decoding.encoding == 'utf-8'
    assert config.decoding.overrides == {'nested/PRIVATE.TXT': 'shift_jis'}
    default = config_for(tmp_path)
    assert default.decoding.encoding == 'auto'
    assert default.decoding.overrides == {}


@pytest.mark.parametrize(('encoding', 'overrides'), [
    ('rot13', {}), ('utf-32', {}), (True, {}), (0, {}), ('', {}),
    ('auto', []), ('auto', {'../outside.txt': 'gbk'}),
    ('auto', {'/outside.txt': 'gbk'}), ('auto', {'nested\\outside.txt': 'gbk'}),
    ('auto', {'unsupported.pdf': 'gbk'}), ('auto', {'note.txt': 'auto'}),
    ('auto', {'note.txt': True}), ('auto', {1: 'gbk'}),
    ('auto', {'*.txt': 'gbk'}), ('auto', {'note?.txt': 'gbk'}),
    ('auto', {'notes/[ab].txt': 'gbk'}),
])
def test_decoding_policy_rejects_invalid_codecs_and_override_paths(tmp_path, encoding, overrides):
    with pytest.raises(ValueError):
        config_for(tmp_path, encoding=encoding, overrides=overrides)


@pytest.mark.parametrize(('encoding', 'text'), [
    ('ascii', 'Synthetic ASCII preferences.\r\nSecond line.'),
    ('utf-8', '旅人记得山谷的灯塔。\r\nCafé と星空。'),
    ('utf-8-sig', '旅人记得山谷的灯塔。\r\n保留换行。'),
    ('gbk', '旅人经过山谷，记录蓝色的灯塔。'),
    ('gb18030', '旅人记得𠀀字与山谷的灯塔。'),
    ('big5', '旅人經過山谷，記錄藍色的燈塔。'),
    ('iso-8859-1', 'Café déjà vu: naïve façade.\r\nVoilà.'),
    ('shift_jis', '旅人は青い灯台と山の谷を覚えています。'),
    ('euc_kr', '여행자는 산골짜기의 푸른 등대를 기억합니다.'),
    ('utf-16', '旅人记得山谷的灯塔。\r\n별빛。'),
    ('utf-16-le', '旅人记得山谷的灯塔。\r\n별빛。'),
    ('utf-16-be', '旅人记得山谷的灯塔。\r\n별빛。'),
])
def test_explicit_source_encodings_preserve_exact_injected_text_and_raw_hash(tmp_path, encoding, text):
    """UTF-8-only or replacement decoding would lose these original characters."""
    async def scenario():
        source = tmp_path / 'synthetic-notes'
        source.mkdir()
        data = text.encode(encoding)
        (source / 'private.txt').write_bytes(data)
        config = config_for(tmp_path, overrides={'private.txt': encoding})
        encoder = RecordingEncoder()
        service = await start_service(tmp_path, config, encoder)
        try:
            assert service.status_snapshot()['status'] == 'ready'
            assert encoder.documents == [text]
            result = await service.prepare_user_turn('synthetic memory', enabled=True, context_validator=lambda _: True)
            assert result.status == 'matched'
            assert result.memory_texts == (text,)
            payloads = [json.loads(line) for line in result.model_user_content.splitlines() if line.startswith('{')]
            assert payloads == [{'text': text}]
            assert 'private.txt' not in result.model_user_content
            assert encoding not in result.model_user_content
            item = next(iter(read_manifest(tmp_path)['files'].values()))
            assert item['sha256'] == hashlib.sha256(data).hexdigest()
            assert (source / 'private.txt').read_bytes() == data
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_auto_bom_encoding_change_reindexes_only_changed_raw_file(tmp_path):
    """Raw bytes changing encoding must invalidate that source even if decoded text agrees."""
    async def scenario():
        source = tmp_path / 'synthetic-notes'
        source.mkdir()
        content = '旅人记得山谷的蓝色灯塔。\r\n第二行保留原样。'
        (source / 'first.md').write_text('Stable synthetic note.', encoding='utf-8')
        (source / 'novel.txt').write_bytes(content.encode('utf-8-sig'))
        config = config_for(tmp_path)
        encoder = RecordingEncoder()
        first = await start_service(tmp_path, config, encoder)
        try:
            assert first.status_snapshot()['status'] == 'ready'
            previous = read_manifest(tmp_path)
            assert encoder.documents == ['Stable synthetic note.', content]
        finally:
            await first.shutdown()

        new_bytes = content.encode('utf-16')
        (source / 'novel.txt').write_bytes(new_bytes)
        second = await start_service(tmp_path, config, encoder)
        try:
            assert second.status_snapshot()['status'] == 'ready'
            assert encoder.documents == ['Stable synthetic note.', content, content]
            current = read_manifest(tmp_path)
            assert current['fingerprint'] == previous['fingerprint']
            old_records = {item['source_path']: item for item in previous['files'].values()}
            new_records = {item['source_path']: item for item in current['files'].values()}
            assert old_records['first.md'] == new_records['first.md']
            assert new_records['novel.txt']['sha256'] == hashlib.sha256(new_bytes).hexdigest()
            assert old_records['novel.txt']['revision_id'] != new_records['novel.txt']['revision_id']
        finally:
            await second.shutdown()

        third = await start_service(tmp_path, config, encoder)
        try:
            assert third.status_snapshot()['status'] == 'ready'
            assert len(encoder.documents) == 3
            assert read_manifest(tmp_path) == current
        finally:
            await third.shutdown()

    asyncio.run(scenario())


def test_decoding_policy_change_rebuilds_all_sources_but_aliases_reuse_vectors(tmp_path):
    """Missing decoding fingerprint would reuse vectors from the previous interpretation."""
    async def scenario():
        source = tmp_path / 'synthetic-notes'
        source.mkdir()
        (source / 'first.md').write_text('First synthetic note.', encoding='utf-8')
        (source / 'second.txt').write_text('Second synthetic note.', encoding='utf-8')
        encoder = RecordingEncoder()
        first = await start_service(tmp_path, config_for(tmp_path, encoding='utf8'), encoder)
        try:
            assert first.status_snapshot()['status'] == 'ready'
            previous = read_manifest(tmp_path)
            old_ids = first._corpus.store.all_ids()
        finally:
            await first.shutdown()

        equivalent = await start_service(tmp_path, config_for(tmp_path, encoding='UTF_8'), encoder)
        try:
            assert equivalent.status_snapshot()['status'] == 'ready'
            assert len(encoder.documents) == 2
            assert read_manifest(tmp_path) == previous
        finally:
            await equivalent.shutdown()

        changed = await start_service(tmp_path, config_for(tmp_path, encoding='utf-8-sig'), encoder)
        try:
            assert changed.status_snapshot()['status'] == 'ready'
            assert len(encoder.documents) == 4
            assert read_manifest(tmp_path)['fingerprint'] != previous['fingerprint']
            assert old_ids.isdisjoint(changed._corpus.store.all_ids())
        finally:
            await changed.shutdown()

    asyncio.run(scenario())


def test_failed_decoding_policy_rebuild_keeps_previous_manifest_atomically(tmp_path):
    """A later decoding failure must not publish the first file of a new generation."""
    async def scenario():
        source = tmp_path / 'synthetic-notes'
        source.mkdir()
        (source / 'first.md').write_text('First synthetic ASCII note.', encoding='utf-8')
        (source / 'second.txt').write_text('第二份合成记忆。', encoding='utf-8')
        encoder = RecordingEncoder()
        first = await start_service(tmp_path, config_for(tmp_path, encoding='utf-8'), encoder)
        try:
            assert first.status_snapshot()['status'] == 'ready'
            manifest_path = tmp_path / 'synthetic-index' / 'manifest.json'
            previous_bytes = manifest_path.read_bytes()
        finally:
            await first.shutdown()

        failed = await start_service(tmp_path, config_for(tmp_path, encoding='ascii'), encoder)
        try:
            status = failed.status_snapshot()
            assert status['status'] == 'degraded'
            assert status['available'] is False
            assert status['error_code'] == 'generation_incomplete'
            assert manifest_path.read_bytes() == previous_bytes
            result = await failed.prepare_user_turn('synthetic question', enabled=True, context_validator=lambda _: True)
            assert result.model_user_content == 'synthetic question'
            assert result.memory_texts == ()
        finally:
            await failed.shutdown()

    asyncio.run(scenario())
