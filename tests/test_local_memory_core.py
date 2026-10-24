"""Behavioral contracts for local Markdown memory; encoder is deterministic and offline."""
import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from backend.retrieval.config import LocalMemoryConfig, load_local_memory_config
from backend.retrieval.markdown import split_markdown
from backend.retrieval.local_memory import LocalMemoryService


class CharacterTokenizer:
    def encode(self, text, add_special_tokens=True):
        return list(range(len(text) + (2 if add_special_tokens else 0)))


def test_configuration_defaults_and_strict_validation(tmp_path):
    path = tmp_path / 'memory.yaml'
    path.write_text('local_memory:\n  chunking:\n    max_chars: 1000\n    overlap_chars: 120\n    max_tokens: 320\n')
    config = load_local_memory_config(path, tmp_path)
    assert config.chunking.max_chars == 1000
    assert config.chunking.max_tokens == 320
    assert config.retrieval.min_similarity == .8
    path.write_text('local_memory:\n  enabled: "false"\n')
    with pytest.raises(ValueError):
        load_local_memory_config(path, tmp_path)
    path.write_text('local_memory:\n  state_dir: memory/agent-local/generated\n')
    with pytest.raises(ValueError):
        load_local_memory_config(path, tmp_path)


def test_splitter_enforces_unicode_and_token_caps_without_loss():
    text = '# 习惯\n\n' + ('中文 text ' * 100) + '\n```python\n' + 'x = 1\n' * 100 + '```\n'
    config = LocalMemoryConfig().chunking
    chunks = split_markdown(text, source_path='notes.md', tokenizer=CharacterTokenizer(), config=config)
    assert len(chunks) > 5
    assert all(len(c.text) <= 1000 for c in chunks)
    assert all(len(CharacterTokenizer().encode('passage: ' + c.text)) <= 320 for c in chunks)
    assert all(c.start_line >= 1 and c.end_line >= c.start_line for c in chunks)
    # Every original character occurs in a chunk; overlap can repeat but never remove input.
    cursor = 0
    for chunk in chunks:
        start = text.find(chunk.text, max(0, cursor - config.overlap_chars))
        assert start >= 0
        cursor = max(cursor, start + len(chunk.text))
    assert cursor == len(text)


def test_disabled_preparation_is_raw_without_loading_any_dependencies(tmp_path):
    service = LocalMemoryService(LocalMemoryConfig(), project_root=tmp_path)
    result = asyncio.run(service.prepare_user_turn('  raw\n', enabled=False))
    assert result.model_user_content == '  raw\n'
    assert result.sources == ()
    assert result.status == 'disabled'
    assert service.status_snapshot()['default_enabled'] is False


def test_artifact_manifest_rejects_replaced_weights(tmp_path):
    from backend.retrieval.embedding import artifact_digest, write_artifact_manifest
    (tmp_path / 'config.json').write_text('{}')
    (tmp_path / 'tokenizer.json').write_text('{}')
    (tmp_path / 'model.safetensors').write_bytes(b'weights')
    write_artifact_manifest(tmp_path, model_id='m', revision='r')
    assert len(artifact_digest(tmp_path, model_id='m', revision='r')) == 64
    (tmp_path / 'model.safetensors').write_bytes(b'changed')
    with pytest.raises(ValueError, match='checksum'):
        artifact_digest(tmp_path, model_id='m', revision='r')


class OfflineEncoder:
    tokenizer = CharacterTokenizer()
    fingerprint = 'offline-test-encoder-v1'
    calls = 0
    def encode_documents(self, texts):
        self.calls += len(texts)
        return [[1.0] + [0.0] * 383 for _ in texts]
    def encode_query(self, text):
        return [1.0] + [0.0] * 383


async def started_service(tmp_path, encoder):
    service = LocalMemoryService(LocalMemoryConfig(), project_root=tmp_path, encoder=encoder)
    await service.start()
    for _ in range(500):
        if service.status_snapshot()['status'] in ('ready', 'degraded'):
            return service
        await asyncio.sleep(.01)
    raise AssertionError('local memory initialization did not finish')


def test_real_zvec_incremental_same_content_files_and_no_stale_delivery(tmp_path):
    async def scenario():
        root = tmp_path / 'memory/agent-local'
        root.mkdir(parents=True)
        (root / 'a.md').write_text('# Memory\nPreferences: concise answers.\n')
        (root / 'b.md').write_text('# Memory\nPreferences: concise answers.\n')
        encoder = OfflineEncoder()
        service = await started_service(tmp_path, encoder)
        assert service.status_snapshot()['available'], service.status_snapshot()
        result = await service.prepare_user_turn('preferences?', enabled=True, context_validator=lambda text: True)
        assert result.status == 'matched'
        assert result.model_user_content.startswith('preferences?\n\n')
        assert encoder.calls == 2
        (root / 'a.md').unlink()
        result = await service.prepare_user_turn('preferences?', enabled=True, context_validator=lambda text: True)
        assert result.status == 'matched'
        assert all(s.source == 'b.md' for s in result.sources)
        await service.shutdown()
        service = await started_service(tmp_path, encoder)
        assert encoder.calls == 2  # deleted file needs no model encoding; unchanged file reused
        assert service.status_snapshot()['indexed_files'] == 1
        (root / 'b.md').write_text('Edited preferences that are not indexed yet.')
        result = await service.prepare_user_turn('preferences?', enabled=True, context_validator=lambda text: True)
        assert result.model_user_content == 'preferences?'
        await service.shutdown()
    asyncio.run(scenario())


def test_context_budget_and_no_validator_never_inject(tmp_path):
    async def scenario():
        root = tmp_path / 'memory/agent-local'
        root.mkdir(parents=True)
        (root / 'a.md').write_text('Preferences: concise answers.')
        service = await started_service(tmp_path, OfflineEncoder())
        for validator in (None, lambda text: False):
            result = await service.prepare_user_turn('question', enabled=True, context_validator=validator)
            assert result.model_user_content == 'question'
        await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['partial_insert', 'before_publish', 'after_publish', 'old_gc'])
def test_journal_recovery_never_exposes_partial_or_duplicate_file(tmp_path, failure, monkeypatch):
    from backend.retrieval.corpus import MemoryCorpus
    async def scenario():
        source = tmp_path / 'memory/agent-local'
        source.mkdir(parents=True)
        (source / 'a.md').write_text('old memory ' * 50)
        encoder = OfflineEncoder()
        service = await started_service(tmp_path, encoder)
        await service.shutdown()
        (source / 'a.md').write_text('new memory ' * 70)
        corpus = MemoryCorpus(LocalMemoryConfig(), tmp_path, encoder)
        corpus.recover()
        corpus.set_encoder(encoder)
        snapshots, complete = corpus.scan()
        assert complete
        original_insert, original_publish, original_delete = corpus.store.insert, corpus._publish, corpus.store.delete
        published = False
        def insert(rows):
            if failure == 'partial_insert':
                original_insert(rows[:1])
                corpus.store.flush()
                raise RuntimeError('injected partial native batch')
            return original_insert(rows)
        def publish(manifest):
            nonlocal published
            if failure == 'before_publish':
                raise RuntimeError('injected before manifest publication')
            original_publish(manifest)
            published = True
            if failure == 'after_publish':
                raise RuntimeError('injected after manifest publication')
        def delete(ids):
            if failure == 'old_gc' and published:
                raise RuntimeError('injected old revision cleanup failure')
            return original_delete(ids)
        corpus.store.insert, corpus._publish, corpus.store.delete = insert, publish, delete
        with pytest.raises(RuntimeError, match='injected'):
            corpus.index_file('a.md', snapshots['a.md'])
        corpus.close()
        # Recovery uses only durable state and the actual on-disk zvec collection.
        recovered = await started_service(tmp_path, encoder)
        assert recovered.status_snapshot()['available'], recovered.status_snapshot()
        manifest = json.loads((tmp_path / 'data/agent-local-memory/manifest.json').read_text())
        active_ids = {key for item in manifest['files'].values() for key in item['chunk_ids']}
        assert recovered._corpus.store.all_ids() == active_ids
        assert not list((tmp_path / 'data/agent-local-memory/journal').glob('*.json'))
        turn = await recovered.prepare_user_turn('memory?', enabled=True, context_validator=lambda _: True)
        assert 'new memory' in turn.model_user_content and 'old memory' not in turn.model_user_content
        await recovered.shutdown()
    asyncio.run(scenario())


def test_deletion_commits_even_when_encoder_cannot_initialize(tmp_path, monkeypatch):
    async def scenario():
        source = tmp_path / 'memory/agent-local'
        source.mkdir(parents=True)
        (source / 'a.md').write_text('memory')
        first = await started_service(tmp_path, OfflineEncoder())
        await first.shutdown()
        (source / 'a.md').unlink()
        import backend.retrieval.local_memory as module
        def unavailable(*args):
            raise FileNotFoundError('prepared encoder absent')
        monkeypatch.setattr(module, 'LocalE5Encoder', unavailable)
        second = await started_service(tmp_path, None)
        assert second.status_snapshot()['status'] == 'degraded'
        manifest = json.loads((tmp_path / 'data/agent-local-memory/manifest.json').read_text())
        assert manifest['files'] == {}
        assert second._corpus.store.all_ids() == set()
        await second.shutdown()
    asyncio.run(scenario())


def test_incomplete_scan_cannot_mass_delete_old_vectors(tmp_path):
    async def scenario():
        source = tmp_path / 'memory/agent-local'
        source.mkdir(parents=True)
        (source / 'a.md').write_text('memory')
        first = await started_service(tmp_path, OfflineEncoder())
        await first.shutdown()
        moved = source.with_name('temporarily-away')
        source.rename(moved)
        second = await started_service(tmp_path, OfflineEncoder())
        manifest = json.loads((tmp_path / 'data/agent-local-memory/manifest.json').read_text())
        assert len(manifest['files']) == 1
        assert second.status_snapshot()['status'] == 'degraded'
        turn = await second.prepare_user_turn('memory?', enabled=True, context_validator=lambda _: True)
        assert turn.model_user_content == 'memory?'
        await second.shutdown()
    asyncio.run(scenario())


def test_private_gate_nonce_delays_background_index_only(tmp_path, monkeypatch):
    async def scenario():
        source = tmp_path / 'memory/agent-local'
        source.mkdir(parents=True)
        (source / 'a.md').write_text('memory')
        gate_dir = tmp_path / 'private-run'
        gate_dir.mkdir(mode=0o700)
        gate = gate_dir / 'gate'
        monkeypatch.setenv('LOCAL_MEMORY_STARTUP_GATE', str(gate))
        monkeypatch.setenv('LOCAL_MEMORY_STARTUP_NONCE', 'expected')
        service = LocalMemoryService(LocalMemoryConfig(), project_root=tmp_path, encoder=OfflineEncoder())
        await service.start()
        await asyncio.sleep(.02)
        assert service.status_snapshot()['status'] == 'waiting_startup'
        gate.write_text('stale-other-run')
        await asyncio.sleep(.06)
        assert service.status_snapshot()['status'] == 'waiting_startup'
        gate.write_text('expected')
        for _ in range(100):
            if service.status_snapshot()['status'] == 'ready':
                break
            await asyncio.sleep(.01)
        assert service.status_snapshot()['available']
        await service.shutdown()
    asyncio.run(scenario())


def test_timeout_does_not_free_actual_worker_or_allow_late_injection(tmp_path):
    from dataclasses import replace
    import threading
    async def scenario():
        root = tmp_path / 'memory/agent-local'
        root.mkdir(parents=True)
        (root / 'a.md').write_text('memory')
        encoder = OfflineEncoder()
        service = await started_service(tmp_path, encoder)
        service.config = replace(service.config, retrieval=replace(service.config.retrieval, timeout_ms=30, max_pending_queries=1))
        entered, release = threading.Event(), threading.Event()
        original = encoder.encode_query
        def blocked(text):
            entered.set()
            release.wait(3)
            return original(text)
        encoder.encode_query = blocked
        result = await service.prepare_user_turn('memory?', enabled=True, context_validator=lambda _: True)
        assert entered.is_set() and result.status == 'timeout'
        result = await service.prepare_user_turn('memory?', enabled=True, context_validator=lambda _: True)
        assert result.status == 'queue_full'
        assert service._pending == 1
        release.set()
        await service.shutdown()
    asyncio.run(scenario())


def test_missing_active_chunks_are_repaired_even_with_surviving_duplicate_ids(tmp_path):
    async def scenario():
        source = tmp_path / 'memory/agent-local'
        source.mkdir(parents=True)
        (source / 'a.md').write_text('memory line\n' * 100)
        encoder = OfflineEncoder()
        first = await started_service(tmp_path, encoder)
        ids = sorted(first._corpus.store.all_ids())
        assert len(ids) > 2
        first._corpus.store.delete(ids[:1])
        first._corpus.store.flush()
        await first.shutdown()
        second = await started_service(tmp_path, encoder)
        assert second.status_snapshot()['available'], second.status_snapshot()
        assert second.status_snapshot()['status'] == 'ready', second.status_snapshot()
        assert second._corpus.store.all_ids() == set(ids)
        await second.shutdown()
    asyncio.run(scenario())


def test_markdown_heading_breadcrumb_ignores_fenced_code():
    from backend.retrieval.config import ChunkingConfig
    text = '# Real\n```python\n# fake heading\n' + ('x = 1\n' * 35) + '```\n\n## Child\nparagraph\n'
    chunks = split_markdown(text, source_path='x.md', tokenizer=CharacterTokenizer(),
                            config=ChunkingConfig(max_chars=80, overlap_chars=10, max_tokens=100))
    assert all('fake heading' not in c.heading for c in chunks)
    assert any(c.heading == 'Real' for c in chunks)


def test_budget_callback_runs_off_eventloop_and_long_query_is_raw(tmp_path):
    import threading
    async def scenario():
        source = tmp_path / 'memory/agent-local'
        source.mkdir(parents=True)
        (source / 'a.md').write_text('memory')
        service = await started_service(tmp_path, OfflineEncoder())
        request_thread = threading.get_ident()
        observed = []
        def validator(text):
            observed.append(threading.get_ident())
            return True
        turn = await service.prepare_user_turn('memory?', enabled=True, context_validator=validator)
        assert turn.status == 'matched' and observed and all(x != request_thread for x in observed)
        turn = await service.prepare_user_turn('q' * 600, enabled=True, context_validator=validator)
        assert turn.status == 'query_too_long' and turn.model_user_content == 'q' * 600
        await service.shutdown()
    asyncio.run(scenario())


def test_retrieval_threshold_does_not_force_low_relevance_chunks(tmp_path):
    async def scenario():
        source = tmp_path / 'memory/agent-local'
        source.mkdir(parents=True)
        (source / 'a.md').write_text('memory')
        encoder = OfflineEncoder()
        encoder.encode_query = lambda _: [0.] + [1.] + [0.] * 382
        service = await started_service(tmp_path, encoder)
        turn = await service.prepare_user_turn('unrelated', enabled=True, context_validator=lambda _: True)
        assert turn.model_user_content == 'unrelated' and not turn.sources
        await service.shutdown()
    asyncio.run(scenario())


def test_regressed_mtime_is_reindexed_and_unmodified_file_is_not(tmp_path):
    import os
    async def scenario():
        source = tmp_path / 'memory/agent-local'
        source.mkdir(parents=True)
        file = source / 'a.md'
        file.write_text('first version')
        encoder = OfflineEncoder()
        first = await started_service(tmp_path, encoder)
        await first.shutdown()
        calls = encoder.calls
        old = file.stat()
        file.write_text('other version')
        os.utime(file, ns=(old.st_atime_ns, old.st_mtime_ns - 100))
        second = await started_service(tmp_path, encoder)
        assert encoder.calls == calls + 1
        turn = await second.prepare_user_turn('memory?', enabled=True, context_validator=lambda _: True)
        assert 'other version' in turn.model_user_content
        await second.shutdown()
    asyncio.run(scenario())


def test_source_and_generated_parent_symlinks_are_rejected(tmp_path):
    from backend.retrieval.safe_fs import read_regular, atomic_json
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'a.md').write_text('secret')
    (tmp_path / 'link').symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        read_regular(tmp_path / 'link/a.md', 100)
    with pytest.raises(OSError):
        atomic_json(tmp_path / 'link/state.json', {'x': 1})
    assert not (outside / 'state.json').exists()


def test_first_generation_crash_recovers_from_durable_empty_baseline(tmp_path, monkeypatch):
    from backend.retrieval.corpus import MemoryCorpus
    async def scenario():
        source = tmp_path / 'memory/agent-local'
        source.mkdir(parents=True)
        (source / 'a.md').write_text('First-build memory that must survive an interrupted index.')
        encoder = OfflineEncoder()
        def interrupted(_corpus):
            raise RuntimeError('injected first generation publication interruption')
        with monkeypatch.context() as patch:
            patch.setattr(MemoryCorpus, 'commit_generation', interrupted)
            first = await started_service(tmp_path, encoder)
            assert first.status_snapshot()['status'] == 'degraded'
            await first.shutdown()
        recovered = await started_service(tmp_path, encoder)
        assert recovered.status_snapshot()['status'] == 'ready', recovered.status_snapshot()
        turn = await recovered.prepare_user_turn('memory?', enabled=True, context_validator=lambda _: True)
        assert turn.status == 'matched'
        assert 'First-build memory' in turn.model_user_content
        manifest = json.loads((tmp_path / 'data/agent-local-memory/manifest.json').read_text())
        assert set(recovered._corpus.store.all_ids()) == {chunk for record in manifest['files'].values() for chunk in record['chunk_ids']}
        await recovered.shutdown()
    asyncio.run(scenario())


def test_missing_committed_manifest_still_degrades_without_deleting_source(tmp_path):
    async def scenario():
        source = tmp_path / 'memory/agent-local'
        source.mkdir(parents=True)
        file = source / 'a.md'
        file.write_text('User-maintained memory remains untouched.')
        first = await started_service(tmp_path, OfflineEncoder())
        await first.shutdown()
        (tmp_path / 'data/agent-local-memory/manifest.json').unlink()
        second = await started_service(tmp_path, OfflineEncoder())
        assert second.status_snapshot()['status'] == 'degraded'
        assert second.status_snapshot()['available'] is False
        assert file.read_text() == 'User-maintained memory remains untouched.'
        assert not (tmp_path / 'data/agent-local-memory/manifest.json').exists()
        await second.shutdown()
    asyncio.run(scenario())


def test_malicious_manifest_cannot_write_or_unlink_outside_state(tmp_path):
    from backend.retrieval.corpus import MemoryCorpus
    source = tmp_path / 'memory/agent-local'
    source.mkdir(parents=True)
    outside = tmp_path / 'outside-state.json'
    outside.write_text('{"unrelated":true}')
    state = tmp_path / 'data/agent-local-memory'
    state.mkdir(parents=True)
    malicious = {'schema_version': 1, 'fingerprint': None, 'files': {
        str(outside.with_suffix('')): {'source_path': 'deleted.md', 'chunk_ids': []}
    }}
    (state / 'manifest.json').write_text(json.dumps(malicious))
    corpus = MemoryCorpus(LocalMemoryConfig(), tmp_path)
    with pytest.raises(ValueError):
        corpus.recover()
        corpus.delete_missing({}, True)
    assert outside.read_text() == '{"unrelated":true}'
    assert json.loads((state / 'manifest.json').read_text()) == malicious


@pytest.mark.parametrize('file_id', ['absolute', '../outside', 'not-a-hash', 'a' * 63, 'A' * 64])
def test_journal_helpers_require_safe_hash_leaf_before_mutation(tmp_path, file_id):
    from backend.retrieval.corpus import MemoryCorpus
    corpus = MemoryCorpus(LocalMemoryConfig(), tmp_path)
    if file_id == 'absolute':
        file_id = str(tmp_path / 'outside-journal')
    with pytest.raises(ValueError):
        corpus._journal(file_id, {'kind': 'delete', 'old_ids': [], 'new_ids': []})
    with pytest.raises(ValueError):
        corpus._finish_journal(file_id)
    assert not (tmp_path / 'data').exists()


@pytest.mark.parametrize('mutation', ['absolute_path', 'parent_path', 'file_id_mismatch', 'revision_id', 'chunk_id', 'negative_size', 'bool_mtime', 'indexed_at', 'status', 'unknown_field'])
def test_manifest_schema_is_validated_before_native_open(tmp_path, mutation):
    from backend.retrieval.corpus import MemoryCorpus, digest
    corpus = MemoryCorpus(LocalMemoryConfig(), tmp_path)
    fingerprint = 'f' * 64
    source_path = 'notes.md'
    file_id = digest(source_path)
    content_hash = 'b' * 64
    item = {'source_path': source_path, 'sha256': content_hash, 'mtime_ns': 100,
            'size': 1, 'revision_id': digest(file_id + content_hash + fingerprint),
            'chunk_ids': ['c' * 64], 'indexed_at': '2026-10-15T01:00:00+00:00', 'status': 'ready'}
    manifest = {'schema_version': 1, 'fingerprint': fingerprint, 'files': {file_id: item}}
    if mutation == 'absolute_path': item['source_path'] = '/tmp/outside.md'
    elif mutation == 'parent_path': item['source_path'] = '../outside.md'
    elif mutation == 'file_id_mismatch': manifest['files'] = {'a' * 64: item}
    elif mutation == 'revision_id': item['revision_id'] = 'd' * 64
    elif mutation == 'chunk_id': item['chunk_ids'] = ['../outside']
    elif mutation == 'negative_size': item['size'] = -1
    elif mutation == 'bool_mtime': item['mtime_ns'] = True
    elif mutation == 'indexed_at': item['indexed_at'] = 'not-a-time'
    elif mutation == 'status': item['status'] = 'partial'
    elif mutation == 'unknown_field': item['new_field'] = True
    state = tmp_path / 'data/agent-local-memory'
    state.mkdir(parents=True)
    (state / 'manifest.json').write_text(json.dumps(manifest))
    opened = []
    corpus._open_store = lambda fp: opened.append(fp)
    with pytest.raises(ValueError): corpus.recover()
    assert opened == []
    assert json.loads((state / 'manifest.json').read_text()) == manifest


@pytest.mark.parametrize('filename,payload', [
    ('invalid.json', {'kind': 'delete', 'old_ids': [], 'new_ids': []}),
    ('a' * 64 + '.json', {'kind': 'delete', 'old_ids': ['/tmp/victim'], 'new_ids': []}),
    ('a' * 64 + '.json', {'kind': 'delete', 'old_ids': [], 'new_ids': ['b' * 64]}),
    ('a' * 64 + '.json', {'kind': 'unknown', 'old_ids': [], 'new_ids': []}),
    ('a' * 64 + '.json', {'kind': 'delete', 'old_ids': [], 'new_ids': [], 'path': '/tmp/victim'}),
])
def test_invalid_journal_is_rejected_before_open_or_cleanup(tmp_path, filename, payload):
    from backend.retrieval.corpus import MemoryCorpus
    state = tmp_path / 'data/agent-local-memory'
    (state / 'journal').mkdir(parents=True)
    manifest = {'schema_version': 1, 'fingerprint': None, 'files': {}}
    (state / 'manifest.json').write_text(json.dumps(manifest))
    journal = state / 'journal' / filename
    journal.write_text(json.dumps(payload))
    corpus = MemoryCorpus(LocalMemoryConfig(), tmp_path)
    opened = []
    corpus._open_store = lambda fp: opened.append(fp)
    with pytest.raises(ValueError): corpus.recover()
    assert opened == [] and json.loads(journal.read_text()) == payload


def test_persisted_source_symlink_escape_rejected_before_native_open(tmp_path):
    from backend.retrieval.corpus import MemoryCorpus, digest
    source = tmp_path / 'memory/agent-local'
    source.mkdir(parents=True)
    outside = tmp_path / 'outside.md'
    outside.write_text('unrelated source')
    (source / 'notes.md').symlink_to(outside)
    fingerprint = 'f' * 64
    file_id, checksum = digest('notes.md'), digest('unrelated source')
    manifest = {'schema_version': 1, 'fingerprint': fingerprint, 'files': {file_id: {
        'source_path': 'notes.md', 'sha256': checksum, 'mtime_ns': 100, 'size': 16,
        'revision_id': digest(file_id + checksum + fingerprint), 'chunk_ids': [],
        'indexed_at': '2026-10-15T01:00:00+00:00', 'status': 'ready'}}}
    state = tmp_path / 'data/agent-local-memory'
    state.mkdir(parents=True)
    (state / 'manifest.json').write_text(json.dumps(manifest))
    corpus = MemoryCorpus(LocalMemoryConfig(), tmp_path)
    opened = []
    corpus._open_store = lambda fp: opened.append(fp)
    with pytest.raises(ValueError): corpus.recover()
    assert not opened and outside.read_text() == 'unrelated source'


@pytest.mark.parametrize('filename', ['bad\\name.md', 'bad\nname.md'])
def test_unsupported_source_filename_cannot_corrupt_valid_library(tmp_path, filename):
    async def scenario():
        source = tmp_path / 'memory/agent-local'
        source.mkdir(parents=True)
        (source / 'valid.md').write_text('Valid indexed user preference.')
        encoder = OfflineEncoder()
        first = await started_service(tmp_path, encoder)
        await first.shutdown()
        (source / filename).write_text('Unsupported name must not enter persisted state.')
        second = await started_service(tmp_path, encoder)
        assert second.status_snapshot()['available'], second.status_snapshot()
        turn = await second.prepare_user_turn('preference?', enabled=True, context_validator=lambda _: True)
        assert 'Valid indexed user preference.' in turn.model_user_content
        assert all(item.source == 'valid.md' for item in turn.sources)
        await second.shutdown()
        third = await started_service(tmp_path, encoder)
        assert third.status_snapshot()['available'], third.status_snapshot()
        await third.shutdown()
    asyncio.run(scenario())


def test_invalid_snapshot_metadata_rejected_before_encoder_or_native_mutation(tmp_path):
    from backend.retrieval.corpus import MemoryCorpus
    async def scenario():
        source = tmp_path / 'memory/agent-local'
        source.mkdir(parents=True)
        (source / 'valid.md').write_text('Valid indexed user preference.')
        encoder = OfflineEncoder()
        first = await started_service(tmp_path, encoder)
        await first.shutdown()
        corpus = MemoryCorpus(LocalMemoryConfig(), tmp_path, encoder)
        corpus.recover()
        corpus.set_encoder(encoder)
        (source / 'bad.md').write_text('A file with unsupported timestamp.')
        snapshots, _ = corpus.scan()
        invalid = dict(snapshots['bad.md'], mtime_ns=-10)
        before_calls, before_ids = encoder.calls, corpus.store.all_ids()
        with pytest.raises(ValueError): corpus.index_file('bad.md', invalid)
        assert encoder.calls == before_calls
        assert corpus.store.all_ids() == before_ids
        assert not list((tmp_path / 'data/agent-local-memory/journal').glob('*.json'))
        corpus.close()
        second = await started_service(tmp_path, encoder)
        assert second.status_snapshot()['available'], second.status_snapshot()
        await second.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('raw,memory_text', [
    ('请按偏好整理内容，用户输入 /tmp/request.txt 保持原样。', '回答保持简洁。用户记录中的 /opt/private-note.md 需要保留。'),
    ('Apply my preference; keep /tmp/request.txt unchanged.', 'Keep answers concise. Preserve the user-written path /opt/private-note.md.'),
])
def test_new_tail_text_only_and_frozen_memory_snapshot(tmp_path, raw, memory_text):
    async def scenario():
        source = tmp_path / 'memory/agent-local/confidential-directory'
        source.mkdir(parents=True)
        file = source / 'provenance-filename-must-remain-private.md'
        file.write_text(memory_text)
        service = await started_service(tmp_path, OfflineEncoder())
        prepared = await service.prepare_user_turn(raw, enabled=True, context_validator=lambda _: True)
        assert prepared.status == 'matched'
        tail = prepared.model_user_content[len(raw):]
        assert 'confidential-directory' not in tail
        assert 'provenance-filename-must-remain-private.md' not in tail
        assert '"source"' not in tail and '"lines"' not in tail
        payloads = [json.loads(line) for line in tail.splitlines() if line.startswith('{')]
        assert payloads == [{'text': memory_text}]
        assert prepared.memory_texts == (memory_text,)
        assert prepared.sources[0].source == 'confidential-directory/provenance-filename-must-remain-private.md'
        assert prepared.model_user_content.startswith(raw)
        assert '/opt/private-note.md' in prepared.model_user_content
        # The accepted display/model snapshot must survive later source edits.
        frozen_content, frozen_texts = prepared.model_user_content, prepared.memory_texts
        file.write_text('Modified later; cannot alter an accepted turn.')
        next_turn = await service.prepare_user_turn(raw, enabled=True, context_validator=lambda _: True)
        assert next_turn.memory_texts == ()
        assert prepared.model_user_content == frozen_content and prepared.memory_texts == frozen_texts
        await service.shutdown()
    asyncio.run(scenario())


def test_memory_snapshot_contains_only_chunks_accepted_by_context_budget(tmp_path):
    async def scenario():
        source = tmp_path / 'memory/agent-local'
        source.mkdir(parents=True)
        (source / 'a.md').write_text('first selected chunk text')
        (source / 'b.md').write_text('second selected chunk text')
        service = await started_service(tmp_path, OfflineEncoder())
        prepared = await service.prepare_user_turn('question', enabled=True,
                            context_validator=lambda content: 'second selected chunk text' not in content)
        assert prepared.status == 'matched'
        assert prepared.memory_texts == ('first selected chunk text',)
        assert 'second selected chunk text' not in prepared.model_user_content
        assert len(prepared.sources) == 1
        await service.shutdown()
    asyncio.run(scenario())


def test_all_skipped_or_failed_preparations_have_no_display_memory_texts(tmp_path):
    async def scenario():
        source = tmp_path / 'memory/agent-local'
        source.mkdir(parents=True)
        (source / 'a.md').write_text('memory')
        encoder = OfflineEncoder()
        service = await started_service(tmp_path, encoder)
        for settings in ({'enabled': False}, {'enabled': True, 'is_aborted': lambda: True},
                         {'enabled': True, 'context_validator': lambda _: False}, {'enabled': True}):
            prepared = await service.prepare_user_turn('question', **settings)
            assert prepared.model_user_content == 'question' and prepared.memory_texts == ()
        encoder.encode_query = lambda _: [0., 1.] + [0.] * 382
        no_match = await service.prepare_user_turn('question', enabled=True, context_validator=lambda _: True)
        assert no_match.memory_texts == () and no_match.model_user_content == 'question'
        def failure(_):
            raise RuntimeError('injected encoder failure')
        encoder.encode_query = failure
        failed = await service.prepare_user_turn('question', enabled=True, context_validator=lambda _: True)
        assert failed.status == 'query_failed' and failed.memory_texts == ()
        await service.shutdown()
    asyncio.run(scenario())
