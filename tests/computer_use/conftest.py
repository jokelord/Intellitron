from __future__ import annotations

from fractions import Fraction

import pytest


SESSION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def settings():
    from backend.computer_use.config import ComputerUseConfig

    def build(**overrides):
        values = {"enabled": False}
        values.update(overrides)
        return ComputerUseConfig.from_mapping(values)

    return build


@pytest.fixture
def protocol():
    from backend.computer_use.contracts import ProtocolDescriptor

    return ProtocolDescriptor(name="custom_isotropic_1000")


@pytest.fixture
def grounding_request(protocol):
    from backend.computer_use.contracts import GroundingRequest

    def build(**overrides):
        values = {
            "request_id": "r1",
            "session_id": SESSION_ID,
            "binding_id": "b1",
            "target_description": "Settings gear",
            "action": "left_click",
            "protocol": protocol,
            "calibration_fingerprint": "test-calibration",
        }
        values.update(overrides)
        return GroundingRequest(**values)

    return build


@pytest.fixture
def binding():
    from backend.computer_use.contracts import InputBinding

    return InputBinding(
        binding_id="b1",
        session_id=SESSION_ID,
        geometry_version=7,
        backend="x11",
        origin=(-1920, 0),
        size=(1920, 1080),
        stream_generation=3,
        device_id="device-1",
        region_id="region-1",
        mapping_evidence={"coverage": {"kind": "full"}},
        ready=True,
    )


@pytest.fixture
def frame(binding):
    from backend.computer_use.contracts import CanonicalFrame

    def build(width=8, height=6, sequence=1, live=True, coverage="full", **overrides):
        values = {
            "frame_id": f"f{sequence}",
            "observation_id": f"o{sequence}",
            "binding": binding,
            "width": width,
            "height": height,
            "rgb": bytes([50, 100, 150]) * width * height,
            "stream_generation": binding.stream_generation,
            "sequence": sequence,
            "captured_monotonic": 100.0,
            "arrived_monotonic": 100.01,
            "liveness_proven": live,
            "coverage_evidence": {"coverage": coverage},
            "transform_evidence": {"orientation": "displayed"},
        }
        values.update(overrides)
        return CanonicalFrame(**values)

    return build


@pytest.fixture
def crop(frame, grounding_request):
    from backend.computer_use.contracts import DetailCrop

    request_builder = grounding_request

    def build(source_frame=None, grounding_request=None, crop_id="c1", **overrides):
        source_frame = source_frame or frame(width=800, height=900)
        grounding_request = grounding_request or request_builder()
        values = {
            "crop_id": crop_id,
            "request_id": grounding_request.request_id,
            "session_id": grounding_request.session_id,
            "observation_id": source_frame.observation_id,
            "frame_id": source_frame.frame_id,
            "binding_id": source_frame.binding.binding_id,
            "geometry_version": source_frame.binding.geometry_version,
            "source_width": source_frame.width,
            "source_height": source_frame.height,
            "x0": 0,
            "y0": 0,
            "x1": source_frame.width,
            "y1": source_frame.height,
            "detail_width": 1024,
            "detail_height": 1024,
            "local_width": Fraction(1000),
            "local_height": Fraction(1000),
            "model_width": 1024,
            "model_height": 1024,
            "wire_protocol": grounding_request.protocol.name,
            "wire_semantics": "relative_extent",
            "protocol_fingerprint": "proto-fp",
            "calibration_fingerprint": grounding_request.calibration_fingerprint,
            "rough_x": Fraction(500),
            "rough_y": Fraction(500),
            "zoom": 2,
            "grid_enabled": True,
            "grid_divisions": 16,
            "created_monotonic": 100.02,
            "dispatch_identity": grounding_request.request_id,
        }
        values.update(overrides)
        return DetailCrop(**values)

    return build
