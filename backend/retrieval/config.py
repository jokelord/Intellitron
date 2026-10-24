"""Validated, immutable runtime configuration for local Agent memory."""
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import get_type_hints
import math
import yaml
from .decoding import normalize_encoding
from .state import require_source_path


@dataclass(frozen=True)
class EmbeddingConfig:
    model_id: str = 'intfloat/multilingual-e5-small'
    revision: str = '614241f622f53c4eeff9890bdc4f31cfecc418b3'
    local_model_path: str = 'models/embeddings/multilingual-e5-small'
    local_files_only: bool = True
    device: str = 'cpu'
    dimension: int = 384
    batch_size: int = 8


@dataclass(frozen=True)
class ChunkingConfig:
    max_chars: int = 1000
    overlap_chars: int = 120
    max_tokens: int = 320


@dataclass(frozen=True)
class DecodingConfig:
    encoding: str = 'auto'
    overrides: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalConfig:
    min_similarity: float = .80
    candidate_k: int = 16
    top_k: int = 4
    adjacent_distance: int = 2
    max_injected_chars: int = 6000
    timeout_ms: int = 1500
    max_pending_queries: int = 8


@dataclass(frozen=True)
class IndexingConfig:
    max_file_bytes: int = 2097152
    max_files: int = 256


@dataclass(frozen=True)
class StartupConfig:
    gate_timeout_seconds: int = 30
    searxng_attempt_timeout_seconds: int = 120


@dataclass(frozen=True)
class LocalMemoryConfig:
    enabled: bool = True
    sync_on_startup: bool = True
    source_dir: str = 'memory/agent-local'
    state_dir: str = 'data/agent-local-memory'
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    indexing: IndexingConfig = field(default_factory=IndexingConfig)
    startup: StartupConfig = field(default_factory=StartupConfig)
    decoding: DecodingConfig = field(default_factory=DecodingConfig)

    def validate(self, project_root: Path):
        for obj in (self, self.embedding, self.chunking, self.retrieval, self.indexing, self.startup, self.decoding):
            for item in fields(obj):
                value = getattr(obj, item.name)
                expected = get_type_hints(type(obj))[item.name]
                if expected is bool and type(value) is not bool:
                    raise ValueError(f'{item.name} must be a boolean')
                if expected is int and (type(value) is not int or value <= 0):
                    # Distances may be zero; resource limits must remain positive.
                    if item.name not in ('overlap_chars', 'adjacent_distance') or type(value) is not int or value < 0:
                        raise ValueError(f'{item.name} must be a positive integer')
                if expected is str and (not isinstance(value, str) or not value.strip()):
                    raise ValueError(f'{item.name} must be a nonempty string')
        if not isinstance(self.decoding.overrides, dict):
            raise ValueError('decoding overrides must be a mapping')
        overrides = {}
        for relative, encoding in self.decoding.overrides.items():
            require_source_path(relative)
            if any(marker in relative for marker in ('*', '?', '[')):
                raise ValueError('decoding overrides require exact paths without glob patterns')
            overrides[relative] = normalize_encoding(encoding, allow_auto=False)
        # Store a validated copy of the input mapping without preventing the
        # project's AppConfig deepcopy. Alias-only edits retain policy identity.
        object.__setattr__(self.decoding, 'encoding', normalize_encoding(self.decoding.encoding))
        object.__setattr__(self.decoding, 'overrides', overrides)
        score = self.retrieval.min_similarity
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not -1 <= score <= 1:
            raise ValueError('min_similarity must be finite and between -1 and 1')
        if self.embedding.device != 'cpu' or self.embedding.dimension != 384 or not self.embedding.local_files_only:
            raise ValueError('local memory requires CPU, 384 dimensions and local_files_only=true')
        if self.retrieval.candidate_k < self.retrieval.top_k:
            raise ValueError('candidate_k must be >= top_k')
        if self.chunking.overlap_chars >= self.chunking.max_chars or not 16 <= self.chunking.max_tokens <= 512:
            raise ValueError('invalid chunking overlap or token limit')
        root = Path(project_root).absolute()
        source = (root / self.source_dir).resolve()
        state = (root / self.state_dir).resolve()
        if state == source or state.is_relative_to(source) or source.is_relative_to(state):
            raise ValueError('source_dir and state_dir must not overlap')
        return self


def _construct(cls, values):
    if not isinstance(values, dict):
        raise ValueError(f'{cls.__name__} must be a mapping')
    definitions = {f.name: f for f in fields(cls)}
    if set(values) - definitions.keys():
        raise ValueError(f'unknown local-memory settings: {sorted(set(values) - definitions.keys())}')
    hints = get_type_hints(cls)
    return cls(**{key: _construct(hints[key], value) if is_dataclass(hints[key]) else value for key, value in values.items()})


def load_local_memory_config(path: str | Path, project_root: str | Path) -> LocalMemoryConfig:
    with Path(path).open(encoding='utf-8') as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict) or set(data) - {'local_memory'}:
        raise ValueError('expected a local_memory YAML section')
    return _construct(LocalMemoryConfig, data.get('local_memory', {})).validate(Path(project_root))
