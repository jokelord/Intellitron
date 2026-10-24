"""The sole application vector-store adapter; one native owner per collection."""
import math
from pathlib import Path
from .safe_fs import open_directory
import os

# Native zvec insert accepts at most 1024 documents per call. Bound all bulk
# operations at the adapter; a source file remains one corpus transaction.
_NATIVE_BATCH_SIZE = 1024


class ZvecStore:
    def __init__(self, path: Path, dimension: int = 384):
        import zvec
        self.zvec = zvec
        # Validate directory components before native code opens collection files.
        fd = open_directory(path.parent, create=True)
        os.close(fd)
        if path.exists() or path.is_symlink():
            fd = open_directory(path)
            os.close(fd)
            self.collection = zvec.open(str(path))
        else:
            schema = zvec.CollectionSchema(name='agent_local_memory', fields=[
                zvec.FieldSchema('file_id', zvec.DataType.STRING, index_param=zvec.InvertIndexParam()),
                zvec.FieldSchema('revision_id', zvec.DataType.STRING, index_param=zvec.InvertIndexParam()),
                *[zvec.FieldSchema(name, zvec.DataType.STRING) for name in ('source_path', 'text', 'heading')],
                *[zvec.FieldSchema(name, zvec.DataType.INT64) for name in ('start_line', 'end_line', 'ordinal')],
            ], vectors=zvec.VectorSchema('embedding', zvec.DataType.VECTOR_FP32, dimension,
                        index_param=zvec.FlatIndexParam(metric_type=zvec.MetricType.IP)))
            self.collection = zvec.create_and_open(str(path), schema)
        self.dimension = dimension

    @staticmethod
    def _check(statuses):
        if not isinstance(statuses, list):
            statuses = [statuses]
        for status in statuses:
            if not status.ok():
                raise RuntimeError('zvec document operation failed')

    def insert(self, rows):
        if not rows:
            return
        docs = []
        for row in rows:
            vector = row['embedding']
            if len(vector) != self.dimension or any(not math.isfinite(x) for x in vector) or not .999 < sum(x*x for x in vector) < 1.001:
                raise ValueError('invalid normalized vector')
            docs.append(self.zvec.Doc(id=row['id'], fields={k: v for k, v in row.items() if k not in ('id', 'embedding')}, vectors={'embedding': vector}))
        for start in range(0, len(docs), _NATIVE_BATCH_SIZE):
            self._check(self.collection.insert(docs[start:start + _NATIVE_BATCH_SIZE]))

    def delete(self, ids):
        if not ids:
            return
        # Idempotent recovery: do not treat an already removed ID as an error.
        for start in range(0, len(ids), _NATIVE_BATCH_SIZE):
            existing = self.fetch_ids(ids[start:start + _NATIVE_BATCH_SIZE])
            if existing:
                self._check(self.collection.delete(sorted(existing)))

    def fetch_ids(self, ids):
        if not ids:
            return set()
        existing = set()
        for start in range(0, len(ids), _NATIVE_BATCH_SIZE):
            existing.update(self.collection.fetch(ids[start:start + _NATIVE_BATCH_SIZE], include_vector=False))
        return existing

    def fetch_rows(self, ids):
        if not ids:
            return []
        rows = []
        for start in range(0, len(ids), _NATIVE_BATCH_SIZE):
            documents = self.collection.fetch(ids[start:start + _NATIVE_BATCH_SIZE], include_vector=False)
            rows.extend(dict(doc.fields, id=doc.id) for doc in documents.values())
        return rows

    def search(self, vector, revisions, topk):
        if not revisions:
            return []
        if any(len(r) != 64 or any(c not in '0123456789abcdef' for c in r) for r in revisions):
            raise ValueError('unsafe revision ID')
        expression = 'revision_id in (' + ','.join("'" + r + "'" for r in sorted(revisions)) + ')'
        results = self.collection.query(self.zvec.Query('embedding', vector=vector), filter=expression, topk=topk)
        return [dict(doc.fields, id=doc.id, score=float(doc.score)) for doc in results]

    def all_ids(self):
        with self.collection.iter_docs() as iterator:
            return {doc.id for doc in iterator}

    def flush(self):
        self.collection.flush()

    def close(self):
        self.collection.close()
