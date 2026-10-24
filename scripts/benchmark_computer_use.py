"""Deterministic acceptance manifest for the pure-vision computer-use stack."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIRED_GATES = (
    "geometry", "protocol", "native_ipc", "live_wayland", "live_x11",
    "live_calibration", "dispatch_faults",
)


def acceptance_ready(results: dict[str, str], backend: str | None = None) -> bool:
    gates = REQUIRED_GATES if backend is None else tuple(name for name in REQUIRED_GATES
        if name not in {"live_wayland", "live_x11"} or name == "live_" + backend)
    return backend in {None, "wayland", "x11"} and all(results.get(name) == "pass" for name in gates)


def build_manifest(*, backend: str, fixture_only: bool) -> dict[str, object]:
    results = {
        "geometry": "not_run",
        "protocol": "not_run",
        "native_ipc": "not_run",
        "live_wayland": "not_run",
        "live_x11": "not_run",
        "live_calibration": "not_run",
        "dispatch_faults": "not_run",
    }
    results["live_x11" if backend == "wayland" else "live_wayland"] = "not_applicable"
    return {
        "schema_version": 1,
        "backend": backend,
        "fixture_only": fixture_only,
        "results": results,
        "ready": acceptance_ready(results, backend),
        "incomplete_reasons": [name for name, state in results.items() if state not in {"pass", "not_applicable"}],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("wayland", "x11"), required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--fixture-only", action="store_true")
    args = parser.parse_args()
    manifest = build_manifest(backend=args.backend, fixture_only=args.fixture_only)
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    output = args.artifact_dir / "computer-use-acceptance.json"
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, sort_keys=True))
    return 0 if manifest["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
