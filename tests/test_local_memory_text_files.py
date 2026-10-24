"""UTF-8 text-source contracts with synthetic temporary files and native zvec."""
import asyncio
from dataclasses import replace
import json

import pytest

from backend.retrieval.config import LocalMemoryConfig
from backend.retrieval.local_memory import LocalMemoryService
from backend.retrieval.state import require_source_path


class CharacterTokenizer:
    def encode(self, text, add_special_tokens=True):
        return list(range(len(text) + (2 if add_special_tokens else 0)))


class SourceEncoder:
    tokenizer = CharacterTokenizer()
    fingerprint = 'synthetic-text-file-contract-v1'

    def __init__(self):
        self.documents = []

    def encode_documents(self, texts):
        self.documents.extend(texts)
        return [([0.0, 1.0] if 'NOVEL' in text else [1.0, 0.0]) + [0.0] * 382 for text in texts]

    def encode_query(self, text):
        return [0.0, 1.0] + [0.0] * 382


def temporary_config(tmp_path, monkeypatch):
    monkeypatch.delenv('LOCAL_MEMORY_STARTUP_GATE', raising=False)
    monkeypatch.delenv('LOCAL_MEMORY_STARTUP_NONCE', raising=False)
    monkeypatch.setenv('WEB_CONCURRENCY', '1')
    monkeypatch.setenv('UVICORN_WORKERS', '1')
    (tmp_path / 'synthetic-notes').mkdir()
    config = LocalMemoryConfig(source_dir='synthetic-notes', state_dir='synthetic-index')
    return replace(config, retrieval=replace(config.retrieval, timeout_ms=5000))


async def start_service(tmp_path, config, encoder):
    service = LocalMemoryService(config, project_root=tmp_path, encoder=encoder)
    await service.start()
    await asyncio.wait_for(asyncio.shield(service._startup_task), timeout=10)
    return service


@pytest.mark.parametrize('filename', ['private.txt', 'PRIVATE.TXT', 'PRIVATE.MD', 'private.mD'])
def test_new_utf8_text_source_is_indexed_and_delivered_as_text_only(tmp_path, monkeypatch, filename):
    """Skipping supported suffixes would leave only the original unrelated note searchable."""
    async def scenario():
        config = temporary_config(tmp_path, monkeypatch)
        source = tmp_path / 'synthetic-notes'
        (source / 'original.md').write_text('ORIGINAL: unrelated synthetic preferences.', encoding='utf-8')
        encoder = SourceEncoder()
        first = await start_service(tmp_path, config, encoder)
        try:
            assert first.status_snapshot()['status'] == 'ready'
            assert first.status_snapshot()['indexed_files'] == 1
        finally:
            await first.shutdown()

        novel = 'NOVEL: 山谷中的旅人记得蓝色的灯塔。\n这里只是合成测试文本。'
        (source / filename).write_text(novel, encoding='utf-8')
        second = await start_service(tmp_path, config, encoder)
        try:
            assert second.status_snapshot()['status'] == 'ready'
            assert second.status_snapshot()['indexed_files'] == 2
            assert encoder.documents == ['ORIGINAL: unrelated synthetic preferences.', novel]
            raw = '请查找合成小说中的灯塔。'
            result = await second.prepare_user_turn(raw, enabled=True, context_validator=lambda _: True)
            assert result.status == 'matched'
            assert result.raw_user_text == raw
            assert result.memory_texts == (novel,)
            assert result.model_user_content.startswith(raw + '\n\n')
            payloads = [json.loads(line) for line in result.model_user_content.splitlines() if line.startswith('{')]
            assert payloads == [{'text': novel}]
            assert tuple(item['text'] for item in payloads) == result.memory_texts
            assert filename not in result.model_user_content
            assert 'synthetic-notes' not in result.model_user_content
            assert result.sources[0].source == filename  # Internal provenance is retained privately.
        finally:
            await second.shutdown()

    asyncio.run(scenario())


def test_md_to_txt_rename_replaces_identity_and_delete_removes_exact_rows(tmp_path, monkeypatch):
    """A renamed text source must not reuse the old file identity or leave stale rows."""
    async def scenario():
        config = temporary_config(tmp_path, monkeypatch)
        source = tmp_path / 'synthetic-notes'
        content = 'NOVEL: synthetic text survives a Markdown-to-text rename.'
        (source / 'chapter.md').write_text(content, encoding='utf-8')
        encoder = SourceEncoder()
        first = await start_service(tmp_path, config, encoder)
        try:
            assert first.status_snapshot()['status'] == 'ready'
            old_ids = first._corpus.store.all_ids()
            old_file_ids = set(first._corpus.manifest['files'])
            assert len(old_ids) == 1
        finally:
            await first.shutdown()

        (source / 'chapter.md').rename(source / 'chapter.txt')
        second = await start_service(tmp_path, config, encoder)
        try:
            assert second.status_snapshot()['status'] == 'ready'
            assert second.status_snapshot()['indexed_files'] == 1
            assert len(encoder.documents) == 2
            assert old_file_ids.isdisjoint(second._corpus.manifest['files'])
            assert old_ids.isdisjoint(second._corpus.store.all_ids())
            assert len(second._corpus.store.all_ids()) == 1
            result = await second.prepare_user_turn('synthetic novel', enabled=True, context_validator=lambda _: True)
            assert result.memory_texts == (content,)
            assert result.sources[0].source == 'chapter.txt'
        finally:
            await second.shutdown()

        (source / 'chapter.txt').unlink()
        third = await start_service(tmp_path, config, encoder)
        try:
            assert third.status_snapshot()['status'] == 'ready'
            assert third.status_snapshot()['indexed_files'] == 0
            assert third._corpus.store.all_ids() == set()
            assert third._corpus.manifest['files'] == {}
            assert len(encoder.documents) == 2
        finally:
            await third.shutdown()

    asyncio.run(scenario())


def test_unsupported_files_and_symlink_text_sources_never_enter_index(tmp_path, monkeypatch):
    """Adding .txt support must retain the bounded regular-file read scope."""
    async def scenario():
        config = temporary_config(tmp_path, monkeypatch)
        source = tmp_path / 'synthetic-notes'
        (source / 'valid.md').write_text('NOVEL: safe synthetic source.', encoding='utf-8')
        encoder = SourceEncoder()
        first = await start_service(tmp_path, config, encoder)
        try:
            assert first.status_snapshot()['status'] == 'ready'
        finally:
            await first.shutdown()

        (source / 'unsupported.pdf').write_text('DO NOT ENCODE unsupported PDF', encoding='utf-8')
        (source / 'unsupported.json').write_text('DO NOT ENCODE unsupported JSON', encoding='utf-8')
        outside = tmp_path / 'outside.txt'
        outside.write_text('DO NOT ENCODE symlink target', encoding='utf-8')
        (source / 'linked.TXT').symlink_to(outside)
        second = await start_service(tmp_path, config, encoder)
        try:
            assert second.status_snapshot()['status'] == 'degraded'
            assert second.status_snapshot()['available'] is True
            assert second.status_snapshot()['indexed_files'] == 1
            assert encoder.documents == ['NOVEL: safe synthetic source.']
            assert {item['source_path'] for item in second._corpus.manifest['files'].values()} == {'valid.md'}
            result = await second.prepare_user_turn('synthetic novel', enabled=True, context_validator=lambda _: True)
            assert result.memory_texts == ('NOVEL: safe synthetic source.',)
        finally:
            await second.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize('path', ['../outside.txt', '/outside.TXT', 'folder\\outside.txt',
    'folder/../outside.txt', 'unsupported.pdf', 'unsupported.json'])
def test_text_source_paths_still_reject_unsupported_or_escaping_names(path):
    with pytest.raises(ValueError):
        require_source_path(path)
