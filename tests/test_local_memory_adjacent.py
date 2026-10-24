"""Adjacent memory context contracts, using synthetic notes and native zvec only."""
import asyncio
from dataclasses import replace
import json
import math

import pytest

from backend.retrieval.config import LocalMemoryConfig, load_local_memory_config
from backend.retrieval.local_memory import LocalMemoryService


@pytest.fixture(autouse=True)
def isolated_startup_environment(monkeypatch):
    monkeypatch.delenv('LOCAL_MEMORY_STARTUP_GATE', raising=False)
    monkeypatch.delenv('LOCAL_MEMORY_STARTUP_NONCE', raising=False)
    monkeypatch.setenv('WEB_CONCURRENCY', '1')
    monkeypatch.setenv('UVICORN_WORKERS', '1')


@pytest.mark.parametrize('distance', [0, 1, 2, 10**30])
def test_adjacent_distance_loads_strict_nonnegative_integer(tmp_path, distance):
    path = tmp_path / 'config.yaml'
    path.write_text(f'local_memory:\n  retrieval:\n    adjacent_distance: {distance}\n')
    config = load_local_memory_config(path, tmp_path)
    assert getattr(config.retrieval, 'adjacent_distance', None) == distance


@pytest.mark.parametrize('value', ['-1', 'true', 'false', '1.5', '"2"'])
def test_adjacent_distance_rejects_invalid_values(tmp_path, value):
    path = tmp_path / 'config.yaml'
    path.write_text(f'local_memory:\n  retrieval:\n    adjacent_distance: {value}\n')
    with pytest.raises(ValueError):
        load_local_memory_config(path, tmp_path)


class CharacterTokenizer:
    def encode(self, text, add_special_tokens=True):
        return list(range(len(text)))


class ScoredEncoder:
    """Normalized deterministic vectors; only marked seeds match the query."""
    tokenizer = CharacterTokenizer()
    fingerprint = 'adjacent-tests-offline-v1'

    def __init__(self, scores):
        self.scores = scores

    def encode_query(self, text):
        return [1.0] + [0.0] * 383

    def encode_documents(self, texts):
        vectors = []
        for text in texts:
            score = self.scores.get(text[:3], 0.0)
            vectors.append([score, math.sqrt(1 - score * score)] + [0.0] * 382)
        return vectors


def chunk(label):
    return label + '|' + 'x' * 12


async def memory_service(tmp_path, scores, *, distance=None, top_k=6, max_chars=6000, notes=None):
    root = tmp_path / 'notes'
    root.mkdir()
    for name, labels in (notes or {'private.md': [f'A{x:02}' for x in range(36)]}).items():
        (root / name).write_text(''.join(chunk(label) for label in labels))
    config = LocalMemoryConfig(source_dir='notes', state_dir='index')
    config = replace(config, chunking=replace(config.chunking, max_chars=16, overlap_chars=0, max_tokens=64),
        retrieval=replace(config.retrieval, **({'adjacent_distance': distance} if distance is not None else {}), top_k=top_k,
            candidate_k=64, max_injected_chars=max_chars, timeout_ms=5000))
    service = LocalMemoryService(config, project_root=tmp_path, encoder=ScoredEncoder(scores))
    await service.start()
    await service._startup_task
    assert service.status_snapshot()['available'], service.status_snapshot()
    return service


def labels(result):
    return [text[:3] for text in result.memory_texts]


def test_radius_two_unions_adjacent_seeds_in_source_order_without_low_score_filter(tmp_path):
    async def scenario():
        service = await memory_service(tmp_path, {'A01': .99, 'A08': .98, 'A09': .97,
            'A18': .96, 'A19': .95, 'A33': .94})
        try:
            result = await service.prepare_user_turn('question', enabled=True, context_validator=lambda _: True)
            assert labels(result) == ['A00','A01','A02','A03','A06','A07','A08','A09','A10','A11',
                'A16','A17','A18','A19','A20','A21','A31','A32','A33','A34','A35']
            assert result.model_user_content.count(chunk('A08')) == 1
            assert 'private.md' not in result.model_user_content
            payloads = [json.loads(line) for line in result.model_user_content.splitlines() if line.startswith('{')]
            assert all(set(payload) == {'text'} for payload in payloads)
            assert tuple(item['text'] for item in payloads) == result.memory_texts
        finally:
            await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize(('distance', 'want'), [
    (0, ['A02']), (1, ['A01','A02','A03']), (10**30, ['A00','A01','A02','A03','A04'])])
def test_radius_zero_one_and_huge_distance_clip_file_ends(tmp_path, distance, want):
    async def scenario():
        service = await memory_service(tmp_path, {'A02': .99}, distance=distance,
            notes={'a.md': ['A00','A01','A02','A03','A04']})
        try:
            result = await service.prepare_user_turn('question', enabled=True, context_validator=lambda _: True)
            assert labels(result) == want
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_neighbors_do_not_cross_files_and_groups_follow_seed_rank(tmp_path):
    async def scenario():
        service = await memory_service(tmp_path, {'B00': .99, 'A02': .95}, distance=1,
            notes={'a.md': ['A00','A01','A02'], 'b.md': ['B00','B01','B02']})
        try:
            result = await service.prepare_user_turn('question', enabled=True, context_validator=lambda _: True)
            assert labels(result) == ['B00','B01','A01','A02']
        finally:
            await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize(('allowed', 'want', 'attempt_counts'), [
    (3, ['A01','A02','A03'], [5, 3]), (1, ['A02'], [5, 3, 1])])
def test_context_budget_retries_complete_smaller_radii_before_dropping_seeds(tmp_path, allowed, want, attempt_counts):
    async def scenario():
        service = await memory_service(tmp_path, {'A02': .99}, notes={'a.md': ['A00','A01','A02','A03','A04']})
        attempts = []
        def validator(text):
            items = [json.loads(line)['text'][:3] for line in text.splitlines() if line.startswith('{')]
            attempts.append(items)
            return len(items) <= allowed
        try:
            result = await service.prepare_user_turn('question', enabled=True, context_validator=validator)
            assert labels(result) == want
            assert [len(items) for items in attempts] == attempt_counts
        finally:
            await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('budget', ['tail', 'candidate'])
def test_character_budget_retries_radius_one_without_cutting_arbitrary_neighbors(tmp_path, budget):
    async def scenario():
        service = await memory_service(tmp_path, {'A02': .99}, max_chars=180 if budget == 'tail' else 6000,
            notes={'a.md': ['A00','A01','A02','A03','A04']})
        try:
            result = await service.prepare_user_turn('question', enabled=True, context_validator=lambda _: True,
                context_budget_chars=180 if budget == 'candidate' else None)
            assert labels(result) == ['A01','A02','A03']
            assert len(result.model_user_content) - (len('question') if budget == 'tail' else 0) <= 180
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_drop_weakest_seed_only_after_zero_radius_and_do_not_reexpand(tmp_path):
    async def scenario():
        service = await memory_service(tmp_path, {'A05': .99, 'A01': .90}, top_k=2,
            notes={'a.md': ['A00','A01','A02','A03','A04','A05','A06','A07']})
        attempts = []
        def validator(text):
            items = [json.loads(line)['text'][:3] for line in text.splitlines() if line.startswith('{')]
            attempts.append(items)
            return len(items) <= 1
        try:
            result = await service.prepare_user_turn('question', enabled=True, context_validator=validator)
            assert labels(result) == ['A05']
            assert attempts == [
                ['A00','A01','A02','A03','A04','A05','A06','A07'],
                ['A00','A01','A02','A04','A05','A06'], ['A01','A05'], ['A05']]
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_stop_during_neighbor_fetch_skips_context_validation(tmp_path):
    async def scenario():
        service = await memory_service(tmp_path, {'A02': .99},
            notes={'a.md': ['A00','A01','A02','A03','A04']})
        stopped = False
        validations = []
        fetch = service._corpus.store.fetch_rows
        def stop_after_native_fetch(ids):
            nonlocal stopped
            rows = fetch(ids)
            stopped = True
            return rows
        service._corpus.store.fetch_rows = stop_after_native_fetch
        try:
            result = await service.prepare_user_turn('question', enabled=True, is_aborted=lambda: stopped,
                context_validator=lambda text: validations.append(text) or True)
            assert result.model_user_content == 'question'
            assert result.status == 'aborted'
            assert validations == []
        finally:
            await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ['missing', 'revision_id', 'ordinal', 'source_path', 'file_id', 'text'])
def test_corrupt_native_neighbor_cannot_enter_injected_union(tmp_path, damage):
    async def scenario():
        service = await memory_service(tmp_path, {'A02': .99},
            notes={'a.md': ['A00','A01','A02','A03','A04']})
        try:
            item = next(iter(service._corpus.manifest['files'].values()))
            key = item['chunk_ids'][1]
            row = service._corpus.store.fetch_rows([key])[0]
            service._corpus.store.delete([key])
            if damage != 'missing':
                row[damage] = {'revision_id': 'f' * 64, 'ordinal': 4, 'source_path': 'another.md',
                    'file_id': 'e' * 64, 'text': 'unindexed substituted private text'}[damage]
                row['embedding'] = [0.0, 1.0] + [0.0] * 382
                service._corpus.store.insert([row])
            service._corpus.store.flush()
            result = await service.prepare_user_turn('question', enabled=True, context_validator=lambda _: True)
            assert result.model_user_content == 'question'
            assert result.memory_texts == ()
            assert result.status == 'query_failed'
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_source_changed_during_expanded_context_validation_is_not_injected(tmp_path):
    async def scenario():
        service = await memory_service(tmp_path, {'A02': .99},
            notes={'a.md': ['A00','A01','A02','A03','A04']})
        def validator(text):
            (tmp_path / 'notes/a.md').write_text('new source not yet indexed')
            return True
        try:
            result = await service.prepare_user_turn('question', enabled=True, context_validator=validator)
            assert result.model_user_content == 'question'
            assert result.memory_texts == ()
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_top_k_limits_seeds_while_neighbors_can_exceed_it(tmp_path):
    async def scenario():
        service = await memory_service(tmp_path, {'A02': .99, 'A07': .9}, distance=1, top_k=1,
            notes={'a.md': ['A00','A01','A02','A03','A04','A05','A06','A07','A08']})
        try:
            result = await service.prepare_user_turn('question', enabled=True, context_validator=lambda _: True)
            assert labels(result) == ['A01','A02','A03']
        finally:
            await service.shutdown()
    asyncio.run(scenario())


def test_equal_score_seeds_use_ordinal_order_even_on_same_line(tmp_path):
    async def scenario():
        service = await memory_service(tmp_path, {'A00': .99, 'A01': .99, 'A02': .99, 'A03': .99},
            distance=0, top_k=2, notes={'a.md': ['A00','A01','A02','A03']})
        try:
            result = await service.prepare_user_turn('question', enabled=True, context_validator=lambda _: True)
            assert labels(result) == ['A00','A01']
        finally:
            await service.shutdown()
    asyncio.run(scenario())
