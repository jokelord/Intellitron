from __future__ import annotations

import runpy
import sys
from pathlib import Path


def test_collect_texts_cli_accepts_home_spelled_directory(tmp_path, monkeypatch):
    home = tmp_path / "home"
    source = home / "notes"
    source.mkdir(parents=True)
    (source / "note.txt").write_text("home path content", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    script = Path(__file__).resolve().parents[1] / "backend/skills/collect_texts_to_csv/collect_texts_to_csv.py"
    monkeypatch.setattr(sys, "argv", [str(script), "~/notes"])

    runpy.run_path(str(script), run_name="__main__")

    outputs = list(source.glob("collected_texts_*.csv"))
    assert len(outputs) == 1
    assert "home path content" in outputs[0].read_text(encoding="utf-8-sig")
