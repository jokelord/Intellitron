from __future__ import annotations

import inspect


def test_grounding_engine_does_not_open_a_private_llm_context():
    from backend.computer_use.grounding import GroundingEngine

    assert not hasattr(GroundingEngine, "_infer")
    assert "chat_stream_async" not in inspect.getsource(GroundingEngine)


def test_executable_sequence_is_the_only_stage_boundary():
    from backend.computer_use.executable_protocol import (
        BackendExecutableResult,
        VisionDesktopExecutableBridge,
        parse_vision_desktop_command,
    )
    from tests.computer_use.test_executable_observation_protocol import _capability

    class Backend:
        def __init__(self):
            self.operations = []

        def execute(self, envelope, capability, check_abort):
            self.operations.append((envelope.sequence, envelope.operation.value))
            return BackendExecutableResult(
                state="OBSERVATION_READY",
                text="ok",
                verified=True,
                image=object(),
                observation_id=f"obs-{envelope.sequence}",
                frame_id=f"frame-{envelope.sequence}",
                geometry_version=1,
            )

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        capability = _capability(Path(directory))
        backend = Backend()
        bridge = VisionDesktopExecutableBridge(backend)
        bridge.execute(
            parse_vision_desktop_command(
                "vision_desktop observe --target-description gear --action left_click"
            ),
            session_id=capability.session_id,
            capability=capability,
        )
        bridge.execute(
            parse_vision_desktop_command(
                "vision_desktop inspect_region --x 200 --y 700 --zoom 4"
            ),
            session_id=capability.session_id,
            capability=capability,
        )
        assert backend.operations == [(1, "observe"), (2, "inspect_region")]
