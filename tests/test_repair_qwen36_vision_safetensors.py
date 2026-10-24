import importlib.util
import json
import struct
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "repair_qwen36_vision_safetensors.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("repair_qwen36_vision_safetensors", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_safetensors(path: Path, header: dict, body: bytes) -> None:
    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    path.write_bytes(struct.pack("<Q", len(header_bytes)) + header_bytes + body)


def _read_safetensors(path: Path) -> tuple[dict, bytes]:
    raw = path.read_bytes()
    header_length = struct.unpack("<Q", raw[:8])[0]
    header_end = 8 + header_length
    return json.loads(raw[8:header_end]), raw[header_end:]


def test_rewrites_only_misnested_vision_keys_and_preserves_data(tmp_path: Path):
    source = tmp_path / "source.safetensors"
    target = tmp_path / "fixed.safetensors"
    body = b"vision-bytes-text-bytes"
    header = {
        "model.language_model.visual.blocks.0.attn.qkv.weight": {
            "dtype": "BF16",
            "shape": [1],
            "data_offsets": [0, 12],
        },
        "model.language_model.layers.0.weight": {
            "dtype": "BF16",
            "shape": [1],
            "data_offsets": [12, len(body)],
        },
    }
    _write_safetensors(source, header, body)

    module = _load_module()
    renamed = module.rewrite_vision_keys(source, target)

    repaired_header, repaired_body = _read_safetensors(target)
    source_header, source_body = _read_safetensors(source)
    assert renamed == 1
    assert "model.visual.blocks.0.attn.qkv.weight" in repaired_header
    assert "model.language_model.visual.blocks.0.attn.qkv.weight" not in repaired_header
    assert "model.language_model.layers.0.weight" in repaired_header
    assert repaired_body == body
    assert source_header == header
    assert source_body == body


def test_rejects_target_key_collision(tmp_path: Path):
    source = tmp_path / "source.safetensors"
    target = tmp_path / "fixed.safetensors"
    header = {
        "model.language_model.visual.blocks.0.weight": {
            "dtype": "BF16",
            "shape": [1],
            "data_offsets": [0, 1],
        },
        "model.visual.blocks.0.weight": {
            "dtype": "BF16",
            "shape": [1],
            "data_offsets": [1, 2],
        },
    }
    _write_safetensors(source, header, b"ab")

    module = _load_module()

    with pytest.raises(ValueError, match="already exists"):
        module.rewrite_vision_keys(source, target)


def test_builds_repaired_model_directory_with_copied_companion_files(tmp_path: Path):
    source_dir = tmp_path / "source-model"
    source_dir.mkdir()
    source = source_dir / "model.safetensors"
    _write_safetensors(
        source,
        {
            "model.language_model.visual.patch_embed.weight": {
                "dtype": "BF16",
                "shape": [1],
                "data_offsets": [0, 1],
            }
        },
        b"x",
    )
    (source_dir / "config.json").write_text('{"model_type":"qwen3_5_moe"}')
    output_dir = tmp_path / "fixed-model"

    module = _load_module()
    renamed = module.build_repaired_model_directory(source, output_dir)

    assert renamed == 1
    assert not (output_dir / "config.json").is_symlink()
    assert (output_dir / "config.json").read_text() == '{"model_type":"qwen3_5_moe"}'
    repaired_header, repaired_body = _read_safetensors(output_dir / "model.safetensors")
    assert "model.visual.patch_embed.weight" in repaired_header
    assert repaired_body == b"x"
