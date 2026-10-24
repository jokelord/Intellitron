"""Small data contracts shared by parsing, indexing and request preparation."""
from dataclasses import dataclass
from typing import Protocol, Sequence, Any


@dataclass(frozen=True)
class MemoryChunk:
    text: str
    source_path: str
    start_line: int
    end_line: int
    ordinal: int
    heading: str = ''


@dataclass(frozen=True)
class MemorySource:
    source: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class PreparedUserTurn:
    raw_user_text: str
    model_user_content: str
    status: str
    sources: tuple[MemorySource, ...] = ()
    memory_texts: tuple[str, ...] = ()


class Encoder(Protocol):
    tokenizer: Any
    fingerprint: str
    def encode_query(self, text: str) -> list[float]: ...
    def encode_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


class VectorStore(Protocol):
    def insert(self, rows: list[dict]) -> None: ...
    def delete(self, ids: list[str]) -> None: ...
    def search(self, vector: list[float], revisions: set[str], topk: int) -> list[dict]: ...
    def fetch_ids(self, ids: list[str]) -> set[str]: ...
    def fetch_rows(self, ids: list[str]) -> list[dict]: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...
