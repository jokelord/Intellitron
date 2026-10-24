from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from io import BytesIO

from PIL import Image

from backend.security.capabilities import build_session_capability
from backend.config import AgentConfig, AppConfig, ReadFileConfig


def _capability(tmp_path: Path):
    root = tmp_path / "allowed"
    root.mkdir()
    return build_session_capability(
        AppConfig(
            read_file=ReadFileConfig(allowed_roots=[str(root)]),
            agent=AgentConfig(
                search_whitelist_paths=[str(root)],
                work_dir=str(tmp_path),
                python_executable=str(Path(__file__).parents[2] / ".sglang/bin/python"),
                nvm_dir=str(tmp_path / "nvm"),
                allow_desktop_capture=True,
                allow_desktop_input=True,
            ),
        ),
        session_id="11111111-1111-4111-8111-111111111111",
        mode="tool_calling",
    )


def test_backend_adapter_preserves_one_request_across_two_stages(tmp_path: Path):
    from backend.computer_use.executable_backend import VisionDesktopGroundingBackend
    from backend.computer_use.executable_protocol import (
        VisionDesktopExecutableBridge,
        parse_vision_desktop_command,
    )
    from backend.computer_use.contracts import (
        DetailCrop,
        InspectRegion,
        InteractDetail,
    )

    def png(color: str, size: tuple[int, int]) -> bytes:
        output = BytesIO()
        Image.new("RGB", size, color=color).save(output, format="PNG")
        return output.getvalue()

    class Engine:
        config = SimpleNamespace(
            protocol="custom_isotropic_1000",
            pixel_semantics=None,
            calibration_fingerprint="calibration",
        )
        binding = SimpleNamespace(binding_id="binding-1", session_id="11111111-1111-4111-8111-111111111111")

        def __init__(self):
            self.calls = []
            self.crop = SimpleNamespace(crop_id="crop-1", geometry_version=4)

        async def observe(self, request, capability, check_abort):
            self.calls.append(("observe", request))
            return SimpleNamespace(
                frame=SimpleNamespace(
                    observation_id="observation-1",
                    frame_id="frame-1",
                    geometry_version=4,
                    binding=SimpleNamespace(binding_id="binding-1"),
                    width=100,
                    height=100,
                ),
                overview=SimpleNamespace(
                        png=png("red", (100, 100)),
                        width=100,
                        height=100,
                    renderer_fingerprint="overview",
                ),
            )

        async def inspect_region(self, request, observation, decision, check_abort):
            assert observation.frame.frame_id == "frame-1"
            assert isinstance(decision, InspectRegion)
            self.calls.append(("inspect_region", request))
            crop = DetailCrop(
                crop_id="crop-1",
                request_id=request.request_id,
                session_id=request.session_id,
                observation_id=observation.frame.observation_id,
                frame_id=observation.frame.frame_id,
                binding_id=request.binding_id,
                geometry_version=4,
                source_width=100,
                source_height=100,
                x0=0,
                y0=0,
                x1=100,
                y1=100,
                detail_width=1024,
                detail_height=1024,
                local_width=1000,
                local_height=1000,
                model_width=1024,
                model_height=1024,
                wire_protocol=request.protocol.name,
                wire_semantics="relative_extent",
                protocol_fingerprint="fp",
                calibration_fingerprint=request.calibration_fingerprint,
                rough_x=decision.x,
                rough_y=decision.y,
                zoom=decision.zoom,
                grid_enabled=True,
                grid_divisions=16,
                created_monotonic=1.0,
                dispatch_identity=request.request_id,
            )
            return SimpleNamespace(
                frame=observation.frame,
                crop=crop,
                detail=SimpleNamespace(
                        png=png("blue", (1024, 1024)),
                        width=1024,
                        height=1024,
                    renderer_fingerprint="detail",
                ),
            )

        async def interact_detail(self, request, detail, decision, capability, check_abort):
            assert isinstance(decision, InteractDetail)
            assert decision.crop_id == "crop-1"
            assert (decision.x, decision.y) == (615, 430)
            self.calls.append(("interact_detail", request))
            return SimpleNamespace(state="ACTION_SENT")

    engine = Engine()
    backend = VisionDesktopGroundingBackend(lambda session_id, capability: engine)
    bridge = VisionDesktopExecutableBridge(backend)
    capability = _capability(tmp_path)
    session_id = capability.session_id

    observe = bridge.execute(
        parse_vision_desktop_command(
            "vision_desktop observe --target-description 'Save button' --action left_click"
        ),
        session_id=session_id,
        capability=capability,
    )
    inspect = bridge.execute(
        parse_vision_desktop_command(
            "vision_desktop inspect_region --x 400 --y 500 --zoom 4"
        ),
        session_id=session_id,
        capability=capability,
    )
    # A missed 4x crop can be replaced directly from the same overview.
    retry_from_same_overview = bridge.execute(
        parse_vision_desktop_command("vision_desktop inspect_region --x 650 --y 400 --zoom 4"),
        session_id=session_id, capability=capability,
    )
    assert retry_from_same_overview.attachment is not None
    # Or the Agent may request a fresh overview before trying another point.
    retry_overview = bridge.execute(
        parse_vision_desktop_command("vision_desktop reobserve --reason target_not_visible"),
        session_id=session_id, capability=capability,
    )
    retry_detail = bridge.execute(
        parse_vision_desktop_command("vision_desktop inspect_region --x 600 --y 350 --zoom 4"),
        session_id=session_id, capability=capability,
    )
    interact = bridge.execute(
        parse_vision_desktop_command(
            "vision_desktop interact_detail --x 615 --y 430 --action left_click"
        ),
        session_id=session_id,
        capability=capability,
    )

    assert observe.attachment is not None
    assert inspect.attachment is not None
    assert retry_overview.attachment is not None
    assert retry_detail.attachment is not None
    assert isinstance(observe.attachment.image, Image.Image)
    assert isinstance(inspect.attachment.image, Image.Image)
    assert interact.state == "ACTION_SENT"
    assert [name for name, _ in engine.calls] == [
        "observe",
        "inspect_region",
        "inspect_region",
        "observe",
        "inspect_region",
        "interact_detail",
    ]
    assert len({request.request_id for _, request in engine.calls}) == 1

    recovered = bridge.execute(parse_vision_desktop_command("vision_desktop reobserve"), session_id=session_id, capability=capability)
    assert recovered.attachment is not None
    recovered_again = bridge.execute(parse_vision_desktop_command("vision_desktop reobserve"), session_id=session_id, capability=capability)
    assert recovered_again.state == "REOBSERVE_LIMIT_REACHED"
    assert recovered_again.attachment is None
    import pytest
    from backend.computer_use.executable_protocol import VisionDesktopProtocolError
    with pytest.raises(VisionDesktopProtocolError, match="OBSERVE_REQUIRED"):
        bridge.execute(parse_vision_desktop_command("vision_desktop inspect_region --x 400 --y 500 --zoom 4"), session_id=session_id, capability=capability)
    new_observe = bridge.execute(parse_vision_desktop_command("vision_desktop observe --target-description 'Next button' --action left_click"), session_id=session_id, capability=capability)
    assert new_observe.attachment is not None
    assert engine.calls[-1][1].request_id != engine.calls[0][1].request_id
