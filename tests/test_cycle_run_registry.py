import asyncio
import importlib
import json

import pytest


def registry_module():
    assert importlib.util.find_spec('backend.cycle.run_registry') is not None
    return importlib.import_module('backend.cycle.run_registry')


@pytest.mark.anyio
async def test_registry_detach_replay_fingerprint_and_terminal_retention():
    module = registry_module()
    clock = [0.0]
    registry = module.CycleRunRegistry(retention_seconds=10, max_records=3, clock=lambda: clock[0])
    record, created = registry.admit('run', 'fingerprint')
    assert created
    assert registry.admit('run', 'fingerprint') == (record, False)
    with pytest.raises(module.CycleRunConflict):
        registry.admit('run', 'different')
    stream = record.events(0, heartbeat_seconds=0.01)
    record.publish({'type': 'cycle_started'})
    first = json.loads((await anext(stream)).strip()[6:])
    assert first['event_sequence'] == 1
    await stream.aclose()
    record.publish({'type': 'llm_done', 'content': 'after disconnect'})
    record.finish('completed', {'type': 'cycle_completed'})
    replay = [json.loads(frame.strip()[6:]) async for frame in record.events(1) if '[DONE]' not in frame]
    assert [item['type'] for item in replay] == ['llm_done', 'cycle_completed']
    assert registry.resolve('run') is record
    clock[0] = 11
    with pytest.raises(module.CycleRunNotFound):
        registry.resolve('run')


@pytest.mark.anyio
async def test_bounded_journal_explains_truncation_and_does_not_replay_live_payloads():
    module = registry_module()
    registry = module.CycleRunRegistry(retention_seconds=10, max_records=3)
    record, _ = registry.admit('run', 'fp')
    for i in range(5):
        record.publish({'type': 'step_end', 'path': [i]})
    record.publish({'type': 'llm_chunk', 'content': 'live only'})
    record.publish({'type': 'photo_captured', 'image_base64': 'private-large-image', 'camera_index': 0})
    record.finish('completed', {'type': 'cycle_completed'})
    frames = [value async for value in record.events(0)]
    payloads = [json.loads(value.strip()[6:]) for value in frames if '[DONE]' not in value]
    assert payloads[0]['type'] == 'journal_truncated'
    assert payloads[-1]['type'] == 'cycle_completed'
    assert 'private-large-image' not in ''.join(frames)
    assert 'live only' not in ''.join(frames)
    assert len(record.hub.records) <= 3
    assert [p['event_sequence'] for p in payloads] == sorted(set(p['event_sequence'] for p in payloads))


@pytest.mark.anyio
async def test_heartbeat_is_transport_only_and_active_record_does_not_expire():
    module = registry_module()
    clock = [0.0]
    registry = module.CycleRunRegistry(retention_seconds=1, max_records=3, clock=lambda: clock[0])
    record, _ = registry.admit('run', 'fp')
    stream = record.events(0, heartbeat_seconds=0.001)
    assert await anext(stream) == ': ping\n\n'
    assert record.hub.last_sequence == 0
    clock[0] = 100
    assert registry.resolve('run') is record
    await stream.aclose()


@pytest.mark.anyio
async def test_terminal_record_is_evicted_without_another_api_request():
    registry = registry_module().CycleRunRegistry(retention_seconds=0.01, max_records=3)
    record, _ = registry.admit('finished', 'fp')
    record.finish('completed', {'type': 'cycle_completed'})
    await asyncio.sleep(0.03)
    assert 'finished' not in registry._records
    assert len(record.hub.records) == 0
