from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_SCRIPT = PROJECT_ROOT / "scripts" / "manage_sglang_compile_cache.sh"
LAUNCH_SCRIPT = PROJECT_ROOT / "scripts" / "1a_start_sglang.sh"


def _run_cache_script(home: Path, snapshot_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "SGLANG_COMPILE_CACHE_BASE": str(home),
            "SGLANG_CACHE_SNAPSHOT_DIR": str(snapshot_root),
            "FLASHINFER_VERSION": "9.9.0",
        }
    )
    return subprocess.run(
        [str(CACHE_SCRIPT), *args],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_cache_snapshot_clear_and_restore_round_trip(tmp_path: Path) -> None:
    home = tmp_path / "home"
    snapshot_root = tmp_path / "snapshots"
    jit_file = home / ".cache/flashinfer/9.9.0/110a/cached_ops/fp4/kernel.o"
    autotune_file = (
        home
        / ".cache/sglang/flashinfer/autotune/9.9.0/sm110/hash/rank_tp0_pp0_dp0.json"
    )
    jit_file.parent.mkdir(parents=True)
    autotune_file.parent.mkdir(parents=True)
    jit_file.write_bytes(b"compiled-object")
    autotune_file.write_text('{"tactic": 7}\n', encoding="utf-8")

    snapshot = _run_cache_script(home, snapshot_root, "snapshot", "known-good")
    assert "快照完成" in snapshot.stdout
    assert (snapshot_root / "known-good/COMPLETE").is_file()
    assert (snapshot_root / "known-good/SHA256SUMS").is_file()

    cleared = _run_cache_script(
        home,
        snapshot_root,
        "clear",
        "--scope",
        "flashinfer-current",
        "--yes",
    )
    assert "缓存已移入隔离区" in cleared.stdout
    assert not jit_file.exists()
    assert not autotune_file.exists()

    restored = _run_cache_script(home, snapshot_root, "restore", "known-good", "--yes")
    assert "快照已恢复" in restored.stdout
    assert jit_file.read_bytes() == b"compiled-object"
    assert autotune_file.read_text(encoding="utf-8") == '{"tactic": 7}\n'


def test_nvfp4_launcher_pins_cache_paths_and_serializes_compilation() -> None:
    overall = yaml.safe_load(
        (PROJECT_ROOT / "config/config-overall.yaml").read_text(encoding="utf-8")
    )
    assert "NVFP4" in Path(overall["infer"]).name
    config_path = PROJECT_ROOT / overall["infer"]
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["sglang"]["compile_max_jobs"] == 1
    assert config["sglang"]["triton_num_compile_threads"] == 1

    launch_text = LAUNCH_SCRIPT.read_text(encoding="utf-8")
    assert 'export FLASHINFER_WORKSPACE_BASE=' in launch_text
    assert 'export SGLANG_CACHE_DIR=' in launch_text
    assert 'export MAX_JOBS="$COMPILE_MAX_JOBS"' in launch_text
    assert 'export TRITON_NUM_COMPILE_THREADS="$TRITON_COMPILE_THREADS"' in launch_text
