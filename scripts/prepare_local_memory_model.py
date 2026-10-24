#!/usr/bin/env python3
"""Explicitly provision and checksum the pinned local Agent-memory encoder."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.retrieval.config import load_local_memory_config
from backend.retrieval.embedding import write_artifact_manifest, artifact_digest


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=root / 'config/config-local-memory.yaml')
    args = parser.parse_args()
    config = load_local_memory_config(args.config, root).embedding
    path = root / config.local_model_path
    if (path / 'artifact-manifest.json').exists():
        artifact_digest(path, model_id=config.model_id, revision=config.revision)
        print('Local encoder artifact already validated.')
        return
    from huggingface_hub import snapshot_download
    snapshot_download(repo_id=config.model_id, revision=config.revision, local_dir=path,
                      allow_patterns=['*.json', '*.safetensors', '*.txt', '*.model', '*.bin'],
                      ignore_patterns=['onnx/*', 'openvino/*', 'pytorch_model.bin'])
    write_artifact_manifest(path, model_id=config.model_id, revision=config.revision)
    print('Prepared pinned local encoder and artifact checksums.')


if __name__ == '__main__':
    main()
