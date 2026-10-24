from __future__ import annotations

import asyncio
import pytest
from dataclasses import replace

from backend.computer_use.audit import audit_attempt
from backend.computer_use.config import ComputerUseConfig
from backend.computer_use.contracts import (
    CanonicalFrame,
    DetailCrop,
    DispatchReceipt,
    GroundingRequest,
    InputBinding,
    ProtocolDescriptor,
    InteractDetail,
)
from backend.computer_use.dispatch import Dispatcher
from backend.computer_use.stale_check import check_freshness


def _fixture():
    sid = "11111111-1111-4111-8111-111111111111"
    binding = InputBinding("b1", sid, 1, "x11", (0, 0), (8, 6), 1, "d", "r", {}, True)
    frame = CanonicalFrame("f1", "o1", binding, 8, 6, bytes([1, 2, 3]) * 48,
                           1, 1, 100.0, 100.01, True, {}, {})
    request = GroundingRequest("r1", sid, "b1", "target", "left_click",
                              ProtocolDescriptor("custom_isotropic_1000"), "fp")
    crop = DetailCrop("c1", "r1", sid, "o1", "f1", "b1", 1, 8, 6, 0, 0, 8, 6,
                      1024, 1024, 1000, 1000, 1024, 1024, "custom_isotropic_1000",
                      "relative_extent", "proto", "fp", 500, 500, 2, True, 16,
                      100.02, "r1")
    # Explicit strict policy fixture for legacy freshness fault injection.
    config = ComputerUseConfig(enabled=True, calibration_fingerprint="fp", pre_action_validation="frame_and_binding",
                               max_observation_age_seconds=10,
                               max_sample_age_seconds=1,
                               max_validation_to_dispatch_seconds=.1,
                               global_diff_threshold=.01,
                               target_diff_threshold=.01,
                               double_click_interval_seconds=.01)
    return binding, frame, request, crop, config


def test_identical_pixels_without_liveness_are_indeterminate():
    binding, frame, _request, _crop, config = _fixture()
    stalled = CanonicalFrame("f2", "o2", binding, 8, 6, frame.rgb, 1, 2,
                             100.1, 100.11, False, {}, {})
    result = check_freshness(stalled, stalled, binding, 100.2, config)
    assert result.state == "INDETERMINATE"


def test_same_request_new_crop_never_replays():
    binding, frame, request, crop, config = _fixture()
    from backend.computer_use.frame_store import FrameStore

    class Native:
        def __init__(self): self.emit_calls = []
        async def send_event(self, *args): self.emit_calls.append(args)

    async def run():
        native = Native()
        dispatcher = Dispatcher(native, config=config, binding=binding, clock=lambda: 100.2)
        first = await dispatcher.dispatch(request, crop)
        count = len(native.emit_calls)
        second_crop = DetailCrop(**{**crop.__dict__, "crop_id": "c2"})
        second = await dispatcher.dispatch(request, second_crop)
        assert second == first
        assert len(native.emit_calls) == count

    asyncio.run(run())


def test_unknown_delivery_is_not_false_success():
    _binding, _frame, request, crop, _config = _fixture()
    receipt = DispatchReceipt("r1", "c1", "ACTION_DELIVERY_UNKNOWN", 2, "release_uncertain")
    record = audit_attempt(request, (), crop, receipt)
    assert record["action_sent"] is None
    assert record["last_possible_event"] == 2
    assert record["state"] != "ACTION_SUCCEEDED"


def test_wayland_endpoint_stays_inside_input_region_and_unknown_backend_fails_closed():
    binding, frame, request, crop, config = _fixture()
    from backend.computer_use.frame_store import FrameStore

    class Native:
        def __init__(self):
            self.events = []

        def resolve_binding(self): return replace(binding, backend="wayland")
        def freeze(self, binding_id): return replace(frame, binding=self.resolve_binding(), frame_id="fresh", sequence=2)
        async def send_event(self, *args):
            self.events.append(args)

    async def run():
        native = Native()
        wayland = replace(binding, backend="wayland")
        store = FrameStore()
        store.put(replace(frame, binding=wayland))
        dispatcher = Dispatcher(native, config=config, binding=wayland, frame_store=store, clock=lambda: 100.2)
        detail = InteractDetail(crop.crop_id, crop.local_width, crop.local_height, request.action)
        receipt = await dispatcher.dispatch(request, crop, decision=detail)
        assert receipt.state == "ACTION_SENT"
        point = native.events[0][2]
        assert 0 <= point["x"] <= wayland.size[0] - 1
        assert 0 <= point["y"] <= wayland.size[1] - 1

        unknown = replace(binding, backend="mystery")
        denied = Dispatcher(native, config=config, binding=unknown, frame_store=store, clock=lambda: 100.2)
        result = await denied.dispatch(request, crop, decision=detail)
        assert result.state == "CANCELLED"
        assert result.cleanup_status == "unsupported_backend"

    asyncio.run(run())


def test_dispatch_samples_real_current_frame_before_any_input():
    from backend.computer_use.frame_store import FrameStore
    binding, frame, request, crop, config = _fixture()
    class Native:
        calls = 0
        events = []
        def resolve_binding(self): return binding
        def freeze(self, binding_id):
            self.calls += 1
            return replace(frame, frame_id='fresh', sequence=2, rgb=b'\xff' * len(frame.rgb))
        def send_event(self, *args): self.events.append(args)
    native = Native()
    store = FrameStore()
    store.put(frame)
    result = asyncio.run(Dispatcher(native, config=config, binding=binding, frame_store=store,
                                    clock=lambda: 100.2).dispatch(request, crop))
    assert native.calls == 1
    assert result.state == 'CANCELLED'
    assert result.cleanup_status == 'FRAME_CHANGED'
    assert native.events == []


def test_target_change_rejected_even_below_global_threshold():
    binding, frame, _, crop, config = _fixture()
    changed = bytearray(frame.rgb)
    changed[:3] = b'\xff' * 3
    current = replace(frame, frame_id='new', sequence=2, rgb=bytes(changed))
    target = replace(crop, x1=4, y1=3)
    result = check_freshness(frame, current, binding, 100.2,
                            replace(config, global_diff_threshold=.1), crop=target)
    assert result.code == 'TARGET_CHANGED'


def test_every_native_event_has_bounded_deadline():
    from backend.computer_use.frame_store import FrameStore
    binding, frame, request, crop, config = _fixture()
    class Native:
        deadlines = []
        def resolve_binding(self): return binding
        def freeze(self, binding_id): return replace(frame, frame_id='fresh', sequence=2)
        def send_event(self, *args, deadline_monotonic=None):
            assert deadline_monotonic is not None and deadline_monotonic > 100.2
            self.deadlines.append(deadline_monotonic)
    native = Native()
    store = FrameStore()
    store.put(frame)
    result = asyncio.run(Dispatcher(native, config=config, binding=binding, frame_store=store,
                                    clock=lambda: 100.2).dispatch(request, crop))
    assert result.state == 'ACTION_SENT'
    assert len(native.deadlines) == 4
    assert native.deadlines[0] < native.deadlines[-1]


@pytest.mark.parametrize('comparison_seconds', [.2, 2.0])
def test_dispatch_deadline_starts_after_validation_and_sample_is_rechecked(monkeypatch, comparison_seconds):
    from backend.computer_use import stale_check
    from backend.computer_use.frame_store import FrameStore
    binding, frame, request, crop, config = _fixture()
    clock = [100.2]
    original_score = stale_check._pixel_score
    def delayed_score(a, b):
        score = original_score(a, b)
        clock[0] += comparison_seconds / 2
        return score
    monkeypatch.setattr(stale_check, '_pixel_score', delayed_score)
    class Native:
        events = []
        def resolve_binding(self): return binding
        def freeze(self, _binding_id): return replace(frame, frame_id='fresh', sequence=2)
        def send_event(self, *args, deadline_monotonic=None):
            assert deadline_monotonic > clock[0]
            self.events.append(args)
    native = Native()
    store = FrameStore()
    store.put(frame)
    receipt = asyncio.run(Dispatcher(native, config=config, binding=binding,
        frame_store=store, clock=lambda: clock[0]).dispatch(request, crop))
    if comparison_seconds < config.max_sample_age_seconds:
        assert receipt.state == 'ACTION_SENT' and len(native.events) == 4
        assert receipt.freshness['age_seconds'] == pytest.approx(.4)
    else:
        assert receipt.state == 'CANCELLED' and receipt.cleanup_status == 'CAPTURE_STALLED'
        assert not native.events and receipt.last_possible_event is None
        assert receipt.freshness['age_seconds'] == pytest.approx(2.2)


@pytest.mark.parametrize('code', ['PIPEWIRE_NO_PROCESS_CALLBACK', 'PIPEWIRE_STREAM_PAUSED', 'PRIVATE_TARGET_PROSE'])
def test_native_freeze_reason_is_allowlisted_and_no_input_is_replayed(code):
    from backend.computer_use.native_client import NativeUnavailable
    from backend.computer_use.frame_store import FrameStore
    binding, frame, request, crop, config = _fixture()
    class Native:
        calls = 0
        def resolve_binding(self): return binding
        def freeze(self, _binding_id):
            self.calls += 1
            raise NativeUnavailable(code)
        def send_event(self, *_args): raise AssertionError('unexpected input')
    native = Native()
    store = FrameStore()
    store.put(frame)
    dispatcher = Dispatcher(native, config=config, binding=binding, frame_store=store, clock=lambda: 100.2)
    first = asyncio.run(dispatcher.dispatch(request, crop))
    second = asyncio.run(dispatcher.dispatch(request, crop))
    assert second is first and native.calls == 1
    assert first.state == 'INDETERMINATE' and first.last_possible_event is None
    assert first.cleanup_status == ('freshness_unavailable' if code == 'PRIVATE_TARGET_PROSE' else code)
