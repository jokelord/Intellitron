import pytest


def test_frame_copy_survives_source_reuse():
    from backend.computer_use.frame_store import owned_rgb

    source = bytearray([255, 0, 0] * 4)
    snapshot = owned_rgb(source, width=2, height=2)
    source[:] = bytes(len(source))
    assert snapshot[:3] == bytes([255, 0, 0])


def test_store_rejects_stale_binding_and_geometry(frame):
    from backend.computer_use.errors import ComputerUseError
    from backend.computer_use.frame_store import FrameStore

    item = frame()
    store = FrameStore(max_frames=2)
    store.put(item)
    assert store.get(item.frame_id, item.binding.binding_id, item.binding.geometry_version) is item
    with pytest.raises(ComputerUseError, match="GEOMETRY_CHANGED"):
        store.get(item.frame_id, item.binding.binding_id, item.binding.geometry_version + 1)
    store.invalidate(item.binding.binding_id)
    with pytest.raises(ComputerUseError, match="INVALID_CONTEXT_BINDING"):
        store.get(item.frame_id, item.binding.binding_id, item.binding.geometry_version)


def test_pinned_frame_is_not_evicted(frame):
    from backend.computer_use.frame_store import FrameStore

    store = FrameStore(max_frames=1)
    first = frame(sequence=1)
    second = frame(sequence=2)
    store.put(first)
    store.pin(first.frame_id)
    store.put(second)
    assert store.get(first.frame_id, first.binding.binding_id, first.binding.geometry_version) is first
    store.release(first.frame_id)
