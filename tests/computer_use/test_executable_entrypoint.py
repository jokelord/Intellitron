from __future__ import annotations

import os
from pathlib import Path


def test_vision_desktop_entrypoint_is_present_and_executable():
    path = (
        Path(__file__).parents[2]
        / "backend"
        / "skills"
        / "computer-use-vision"
        / "bin"
        / "vision_desktop"
    )
    assert path.is_file()
    assert os.access(path, os.X_OK)
