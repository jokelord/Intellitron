#!/usr/bin/env python3
"""Repair the pre-2026-06-18 Qwen3.6 vision tensor key layout.

The affected checkpoint stores its vision tower below
``model.language_model.visual.``. vLLM's multimodal Qwen3.5 loader expects
the sibling prefix ``model.visual.``. This utility rewrites only the
safetensors header and copies the tensor-data region byte-for-byte.
"""

import argparse
import json
import os
import shutil
import struct
import tempfile
from pathlib import Path
from typing import Any


SOURCE_PREFIX = "model.language_model.visual."
TARGET_PREFIX = "model.visual."


def _read_header(source: Path) -> tuple[dict[str, Any], int]:
    source = Path(source).expanduser()
    with source.open("rb") as handle:
        raw_length = handle.read(8)
        if len(raw_length) != 8:
            raise ValueError("not a safetensors file: missing header length")
        header_length = struct.unpack("<Q", raw_length)[0]
        raw_header = handle.read(header_length)
        if len(raw_header) != header_length:
            raise ValueError("not a safetensors file: truncated header")

    try:
        header = json.loads(raw_header)
    except json.JSONDecodeError as error:
        raise ValueError("not a safetensors file: invalid JSON header") from error
    if not isinstance(header, dict):
        raise ValueError("not a safetensors file: header is not an object")
    return header, 8 + header_length


def rewrite_vision_keys(source: Path, target: Path) -> int:
    """Write a repaired safetensors file and return the number of renamed keys."""
    source = Path(source).expanduser().resolve()
    target = Path(target).expanduser().resolve()
    if source == target:
        raise ValueError("source and target must be different files")
    if not source.is_file():
        raise FileNotFoundError(source)
    if target.exists():
        raise FileExistsError(f"target already exists: {target}")

    header, data_offset = _read_header(source)
    repaired_header: dict[str, Any] = {}
    renamed = 0
    for key, value in header.items():
        repaired_key = key
        if key.startswith(SOURCE_PREFIX):
            repaired_key = TARGET_PREFIX + key.removeprefix(SOURCE_PREFIX)
            renamed += 1
        if repaired_key in repaired_header:
            raise ValueError(f"target tensor key already exists: {repaired_key}")
        repaired_header[repaired_key] = value

    if not renamed:
        raise ValueError(f"no tensor keys begin with {SOURCE_PREFIX!r}")

    target.parent.mkdir(parents=True, exist_ok=True)
    header_bytes = json.dumps(repaired_header, separators=(",", ":")).encode("utf-8")
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=target.parent, prefix=f".{target.name}.", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        temporary.write(struct.pack("<Q", len(header_bytes)))
        temporary.write(header_bytes)
        with source.open("rb") as input_file:
            input_file.seek(data_offset)
            shutil.copyfileobj(input_file, temporary, length=4 * 1024 * 1024)
        temporary.flush()
        os.fsync(temporary.fileno())

    try:
        os.replace(temporary_path, target)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return renamed


def build_repaired_model_directory(source: Path, output_dir: Path) -> int:
    """Create a model directory with linked metadata and repaired weight file."""
    source = Path(source).expanduser().absolute()
    output_dir = Path(output_dir).expanduser().absolute()
    if output_dir.exists():
        raise FileExistsError(f"output model directory already exists: {output_dir}")

    output_dir.mkdir(parents=True)
    try:
        for entry in source.parent.iterdir():
            if entry.name == source.name:
                continue
            destination = output_dir / entry.name
            if entry.is_dir():
                shutil.copytree(entry, destination, symlinks=False)
            else:
                shutil.copy2(entry, destination, follow_symlinks=True)
        return rewrite_vision_keys(source, output_dir / source.name)
    except Exception:
        shutil.rmtree(output_dir)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="old model.safetensors file")
    parser.add_argument(
        "target", nargs="?", type=Path, help="new repaired safetensors file"
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        help="create a complete repaired model directory with linked companion files",
    )
    args = parser.parse_args()

    if bool(args.target) == bool(args.model_dir):
        parser.error("provide exactly one of target or --model-dir")
    if args.model_dir:
        renamed = build_repaired_model_directory(args.source, args.model_dir)
        print(f"Repaired {renamed} vision tensor keys: {args.model_dir}")
    else:
        renamed = rewrite_vision_keys(args.source, args.target)
        print(f"Repaired {renamed} vision tensor keys: {args.target}")


if __name__ == "__main__":
    main()
