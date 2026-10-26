"""Offline E5 encoder using the existing .sglang transformers and torch."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from .config import EmbeddingConfig, resolve_local_memory_path
from .safe_fs import atomic_json, read_json, read_regular

ARTIFACT_MANIFEST = 'artifact-manifest.json'


def _artifact_files(path: Path):
    files = {}
    for file in sorted(path.rglob('*')):
        relative = file.relative_to(path)
        if '.cache' in relative.parts or file.name == ARTIFACT_MANIFEST:
            continue
        if file.is_symlink():
            raise ValueError('artifact symlinks are not allowed; prepare regular local files')
        if file.is_file():
            data, _ = read_regular(file, 2 * 1024 ** 3)
            files[relative.as_posix()] = hashlib.sha256(data).hexdigest()
    if 'config.json' not in files or not any('tokenizer' in name for name in files) or not any(name.endswith(('.safetensors', '.bin')) for name in files):
        raise ValueError('incomplete encoder artifact')
    return files


def write_artifact_manifest(path: Path, *, model_id: str, revision: str):
    manifest = {'model_id': model_id, 'revision': revision, 'files': _artifact_files(path)}
    atomic_json(path / ARTIFACT_MANIFEST, manifest)
    return manifest


def artifact_digest(path: Path, *, model_id: str, revision: str) -> str:
    manifest = read_json(path / ARTIFACT_MANIFEST)
    if manifest.get('model_id') != model_id or manifest.get('revision') != revision:
        raise ValueError('encoder artifact revision mismatch')
    if manifest.get('files') != _artifact_files(path):
        raise ValueError('encoder artifact checksum mismatch')
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


class LocalE5Encoder:
    def __init__(self, config: EmbeddingConfig, project_root: Path):
        self.config = config
        path = resolve_local_memory_path(project_root, config.local_model_path)
        digest = artifact_digest(path, model_id=config.model_id, revision=config.revision)
        # Import only after validating prepared files; no runtime network fallback.
        import torch
        from transformers import AutoModel, AutoTokenizer
        self._torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True, trust_remote_code=False)
        self.model = AutoModel.from_pretrained(str(path), local_files_only=True, trust_remote_code=False).to('cpu').float().eval()
        identity = {'config': asdict(config), 'artifact_digest': digest, 'pooling': 'masked_mean', 'normalization': 'l2', 'query_prefix': 'query: ', 'passage_prefix': 'passage: '}
        self.fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def _encode(self, texts, prefix):
        torch = self._torch
        result = []
        for offset in range(0, len(texts), self.config.batch_size):
            batch = [prefix + text for text in texts[offset:offset + self.config.batch_size]]
            if any(len(self.tokenizer.encode(text, add_special_tokens=True)) > 512 for text in batch):
                raise ValueError('embedding token limit exceeded')
            inputs = self.tokenizer(batch, padding=True, truncation=False, return_tensors='pt')
            with torch.inference_mode():
                output = self.model(**inputs).last_hidden_state
                mask = inputs['attention_mask'].unsqueeze(-1).to(output.dtype)
                pooled = (output * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1)
                vectors = torch.nn.functional.normalize(pooled, p=2, dim=1)
            if vectors.shape[1] != self.config.dimension or not torch.isfinite(vectors).all():
                raise ValueError('invalid encoder vector')
            result.extend(vectors.float().tolist())
        return result

    def encode_documents(self, texts):
        return self._encode(list(texts), 'passage: ')

    def encode_query(self, text):
        return self._encode([text], 'query: ')[0]
