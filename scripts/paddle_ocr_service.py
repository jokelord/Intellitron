#!/usr/bin/env python3
"""Apply the CUDA ceiling and expand local pipeline paths before PaddleX."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator


GIB = 1024**3


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(payload, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def apply_cuda_memory_limit(
    limit_gb: int,
    report_path: Path,
    *,
    torch_module: Any | None = None,
) -> dict[str, object]:
    if isinstance(limit_gb, bool) or not isinstance(limit_gb, int) or limit_gb <= 0:
        raise ValueError("CUDA memory limit must be a positive integer GiB value")
    if torch_module is None:
        import torch as torch_module
    if not torch_module.cuda.is_available():
        raise RuntimeError("CUDA is unavailable; refusing to start PaddleOCR")

    total_bytes = int(torch_module.cuda.get_device_properties(0).total_memory)
    limit_bytes = limit_gb * GIB
    if total_bytes <= 0 or limit_bytes > total_bytes:
        raise ValueError("CUDA memory limit exceeds device-visible memory")
    fraction = limit_bytes / total_bytes
    torch_module.cuda.set_per_process_memory_fraction(fraction, device=0)

    report: dict[str, object] = {
        "schemaVersion": 1,
        "pid": os.getpid(),
        "cudaMemoryLimitGiB": limit_gb,
        "cudaMemoryLimitBytes": limit_bytes,
        "deviceTotalMemoryBytes": total_bytes,
        "allocatorFraction": fraction,
    }
    _atomic_write_json(report_path, report)
    return report


def _expand_pipeline_paths(value: Any) -> bool:
    """Expand only local path fields; keep protocol strings and labels verbatim."""
    changed = False
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"model_dir", "chat_template"} and isinstance(child, str):
                expanded = os.path.expanduser(child)
                if expanded != child:
                    value[key] = expanded
                    changed = True
            else:
                changed = _expand_pipeline_paths(child) or changed
    elif isinstance(value, list):
        for child in value:
            changed = _expand_pipeline_paths(child) or changed
    return changed


@contextmanager
def expanded_pipeline_arguments(arguments: list[str]) -> Iterator[list[str]]:
    """Give PaddleX an expanded runtime copy while preserving attested YAML bytes."""
    expanded_arguments = list(arguments)
    with tempfile.TemporaryDirectory(prefix="paddleocr-pipeline-") as temporary:
        for index, argument in enumerate(arguments):
            if argument == "--pipeline" and index + 1 < len(arguments):
                value_index, prefix = index + 1, ""
                pipeline = arguments[value_index]
            elif argument.startswith("--pipeline="):
                value_index, prefix = index, "--pipeline="
                pipeline = argument.split("=", 1)[1]
            else:
                continue
            if not pipeline.endswith((".yaml", ".yml")):
                continue
            pipeline_path = Path(pipeline).expanduser()
            import yaml

            config = yaml.safe_load(pipeline_path.read_text(encoding="utf-8"))
            if _expand_pipeline_paths(config):
                runtime_path = Path(temporary) / f"pipeline-{index}.yaml"
                runtime_path.write_text(
                    yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
                )
                pipeline_path = runtime_path
            expanded_arguments[value_index] = prefix + str(pipeline_path)
        yield expanded_arguments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--cuda-memory-limit-gb", required=True, type=int)
    parser.add_argument("--memory-report", required=True, type=Path)
    args, paddlex_args = parser.parse_known_args(argv)
    args.memory_report = args.memory_report.expanduser()
    if not paddlex_args:
        parser.error("PaddleX arguments are required")

    apply_cuda_memory_limit(args.cuda_memory_limit_gb, args.memory_report)
    from paddlex.__main__ import console_entry

    with expanded_pipeline_arguments(paddlex_args) as runtime_args:
        sys.argv = ["paddlex", *runtime_args]
        return int(console_entry() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
