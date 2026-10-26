"""Incremental text snapshots with durable per-file revision publication."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
import stat
from pathlib import Path
import threading
from .config import resolve_local_memory_path
from .decoding import decode_source, decoding_fingerprint
from .markdown import split_markdown
from .safe_fs import atomic_json, open_directory, read_json, read_regular
from .zvec_store import ZvecStore
from .state import has_memory_source_suffix, require_hash, require_source_path, validate_manifest, validate_journal, validate_snapshot


def digest(value):
    if isinstance(value, str):
        value = value.encode('utf-8')
    return hashlib.sha256(value).hexdigest()


class MemoryCorpus:
    def __init__(self, config, project_root, encoder=None):
        self.config = config
        self.root = Path(project_root).expanduser().absolute()
        self.source = resolve_local_memory_path(self.root, config.source_dir)
        self.state = resolve_local_memory_path(self.root, config.state_dir)
        self.encoder = encoder
        self.store = None
        self.lock = threading.RLock()
        self.manifest = {'schema_version': 1, 'fingerprint': None, 'files': {}}
        self.quarantined = set()
        self.scan_error = None
        self._building_generation = False

    def _publish(self, manifest):
        validate_manifest(manifest, source_root=self.source)
        if not self._building_generation:
            atomic_json(self.state / 'manifest.json', manifest)
        self.manifest = manifest

    def _journal(self, file_id, operation):
        require_hash(file_id)
        validate_journal(operation)
        atomic_json(self.state / 'journal' / (file_id + '.json'), operation)

    def _finish_journal(self, file_id):
        require_hash(file_id)
        from .safe_fs import open_directory
        fd = open_directory(self.state / 'journal', create=True)
        try:
            try:
                metadata = os.stat(file_id + '.json', dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError('journal target must be a regular file')
                os.unlink(file_id + '.json', dir_fd=fd)
                os.fsync(fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(fd)

    def _open_store(self, fingerprint):
        self.store = ZvecStore(self.state / 'collections' / fingerprint, self.config.embedding.dimension)

    def _load_journals(self):
        directory = self.state / 'journal'
        if not directory.exists() and not directory.is_symlink():
            return []
        fd = open_directory(directory)
        try:
            names = sorted(os.listdir(fd))
        finally:
            os.close(fd)
        operations = []
        for name in names:
            path = directory / name
            if name.startswith('.tmp-'):
                # Atomic-write remnants are never replayed or unlinked by name.
                metadata = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError('invalid temporary journal entry')
                continue
            if not name.endswith('.json'):
                raise ValueError('invalid journal filename')
            file_id = require_hash(name[:-5])
            operation = validate_journal(read_json(path))
            operations.append((file_id, operation))
        return operations

    def recover(self):
        fd = open_directory(self.state, create=True)
        os.close(fd)
        path = self.state / 'manifest.json'
        manifest = read_json(path) if path.exists() or path.is_symlink() else None
        if manifest is not None:
            validate_manifest(manifest, source_root=self.source)
        # Validate every journal before native open, deletion, publication or replay.
        journals = self._load_journals()
        if manifest is None:
            if (self.state / 'collections').exists() or journals:
                raise ValueError('local-memory manifest missing beside existing state')
            # First construction has durable authority before native files exist.
            self._publish(self.manifest)
        else:
            self.manifest = manifest
            if manifest['fingerprint']:
                self._open_store(manifest['fingerprint'])
        if self.store:
            active = {chunk_id for item in self.manifest['files'].values() for chunk_id in item['chunk_ids']}
            for file_id, operation in journals:
                involved = set(operation['old_ids']) | set(operation['new_ids'])
                self.store.delete(sorted(involved - active))
                self.store.flush()
                self._finish_journal(file_id)
            existing = self.store.all_ids()
            self.store.delete(sorted(existing - active))
            self.store.flush()
            for item in self.manifest['files'].values():
                if not set(item['chunk_ids']) <= existing:
                    self.quarantined.add(item['revision_id'])

    def scan(self):
        files = {}
        complete = True
        try:
            fd = open_directory(self.source)
            os.close(fd)
        except (OSError, ValueError):
            return {}, False
        def failed(_error):
            nonlocal complete
            complete = False
        for directory, dirs, names in os.walk(self.source, followlinks=False, onerror=failed):
            safe_dirs = []
            for name in dirs:
                if (Path(directory) / name).is_symlink():
                    complete = False
                else:
                    safe_dirs.append(name)
            dirs[:] = sorted(safe_dirs)
            for name in sorted(names):
                if not has_memory_source_suffix(name):
                    continue
                relative = (Path(directory) / name).relative_to(self.source).as_posix()
                try:
                    require_source_path(relative)
                except ValueError:
                    complete = False
                    continue
                if len(files) >= self.config.indexing.max_files:
                    complete = False
                    continue
                files[relative] = None
                try:
                    data, metadata = read_regular(self.source / relative, self.config.indexing.max_file_bytes)
                    files[relative] = {'data': data, 'sha256': digest(data), 'mtime_ns': metadata.st_mtime_ns,
                                      'size': metadata.st_size, 'inode': metadata.st_ino, 'ctime_ns': metadata.st_ctime_ns}
                except (OSError, ValueError):
                    complete = False
        return files, complete

    def _remove(self, file_id):
        previous = self.manifest['files'][file_id]
        self._journal(file_id, {'old_ids': previous['chunk_ids'], 'new_ids': [], 'kind': 'delete'})
        updated = dict(self.manifest, files=dict(self.manifest['files']))
        del updated['files'][file_id]
        with self.lock:
            self._publish(updated)
        if self.store:
            self.store.delete(previous['chunk_ids'])
            self.store.flush()
        self._finish_journal(file_id)

    def delete_missing(self, snapshots, complete):
        if complete:
            for file_id, previous in list(self.manifest['files'].items()):
                if previous['source_path'] not in snapshots:
                    self._remove(file_id)
        self.scan_error = None if complete else 'scan_incomplete'

    def set_encoder(self, encoder):
        self.encoder = encoder
        fingerprint = digest(json.dumps({'encoder': encoder.fingerprint, 'chunking': asdict(self.config.chunking),
                                         'decoding': decoding_fingerprint(self.config.decoding),
                                         'schema': 1, 'metric': 'IP', 'dimension': self.config.embedding.dimension}, sort_keys=True))
        if fingerprint != self.manifest['fingerprint']:
            if self.store:
                self.store.close()
            # Build a separate generation. The old durable manifest remains the
            # authority until the first successful file (or empty corpus) publish.
            self._open_store(fingerprint)
            self._building_generation = True
            self.manifest = {'schema_version': 1, 'fingerprint': fingerprint, 'files': {}}
            self.quarantined.clear()
            # A prior failed construction may have uncommitted rows.
            self.store.delete(sorted(self.store.all_ids()))
            self.store.flush()
        elif self.store is None:
            self._open_store(fingerprint)

    def commit_generation(self):
        validate_manifest(self.manifest, source_root=self.source)
        atomic_json(self.state / 'manifest.json', self.manifest)
        self._building_generation = False

    def index_file(self, relative, snapshot):
        require_source_path(relative)
        validate_snapshot(snapshot)
        file_id = digest(relative)
        old = self.manifest['files'].get(file_id)
        if old and old['sha256'] == snapshot['sha256'] and old['revision_id'] not in self.quarantined:
            if old['mtime_ns'] != snapshot['mtime_ns'] or old['size'] != snapshot['size']:
                updated_item = dict(old, mtime_ns=snapshot['mtime_ns'], size=snapshot['size'])
                with self.lock:
                    self._publish(dict(self.manifest, files=dict(self.manifest['files'], **{file_id: updated_item})))
            return
        text = decode_source(snapshot['data'], relative, self.config.decoding)
        chunks = split_markdown(text, source_path=relative, tokenizer=self.encoder.tokenizer, config=self.config.chunking)
        vectors = self.encoder.encode_documents([chunk.text for chunk in chunks])
        if len(vectors) != len(chunks):
            raise ValueError('encoder returned an incomplete file')
        revision = digest(file_id + snapshot['sha256'] + self.manifest['fingerprint'])
        rows = []
        for chunk, vector in zip(chunks, vectors):
            fields = asdict(chunk)
            chunk_id = digest(file_id + revision + str(chunk.ordinal) + digest(chunk.text))
            rows.append(dict(fields, id=chunk_id, file_id=file_id, revision_id=revision, embedding=vector))
        # Revalidate immediately before committing a possibly long encoding batch.
        current, metadata = read_regular(self.source / relative, self.config.indexing.max_file_bytes)
        if digest(current) != snapshot['sha256'] or metadata.st_mtime_ns != snapshot['mtime_ns']:
            raise ValueError('source changed before index publication')
        old_ids = old['chunk_ids'] if old else []
        new_ids = [row['id'] for row in rows]
        item = {'source_path': relative, 'sha256': snapshot['sha256'], 'mtime_ns': snapshot['mtime_ns'],
                'size': snapshot['size'], 'revision_id': revision, 'chunk_ids': new_ids,
                'indexed_at': datetime.now(timezone.utc).isoformat(), 'status': 'ready'}
        updated = dict(self.manifest, files=dict(self.manifest['files'], **{file_id: item}))
        validate_manifest(updated, source_root=self.source)
        self._journal(file_id, {'old_ids': old_ids, 'new_ids': new_ids, 'kind': 'replace'})
        # Recovery may retry the same deterministic revision after a partial insert.
        self.store.delete(new_ids if revision in self.quarantined else sorted(set(new_ids) - set(old_ids)))
        self.store.insert(rows)
        self.store.flush()
        with self.lock:
            self._publish(updated)
        self.store.delete(sorted(set(old_ids) - set(new_ids)))
        self.store.flush()
        self.quarantined.discard(revision)
        self._finish_journal(file_id)

    def search(self, text, deadline, aborted):
        import time
        if time.monotonic() >= deadline or aborted():
            return [], 'aborted' if aborted() else 'timeout'
        if len(self.encoder.tokenizer.encode('query: ' + text, add_special_tokens=True)) > 512:
            return [], 'query_too_long'
        if time.monotonic() >= deadline or aborted():
            return [], 'aborted' if aborted() else 'timeout'
        vector = self.encoder.encode_query(text)
        valid = {item['revision_id']: item for item in self.manifest['files'].values()
                 if item['revision_id'] not in self.quarantined and item['chunk_ids']}
        checked = {}
        results = []
        for _ in range(self.config.indexing.max_files + 1):
            if time.monotonic() >= deadline or aborted():
                return [], 'aborted' if aborted() else 'timeout'
            with self.lock:
                candidates = self.store.search(vector, set(valid), self.config.retrieval.candidate_k)
            invalid = set()
            for row in candidates:
                revision = row['revision_id']
                if revision not in checked:
                    try:
                        expected = valid[revision]
                        data, metadata = read_regular(self.source / expected['source_path'], self.config.indexing.max_file_bytes)
                        checked[revision] = (digest(data) == expected['sha256'], metadata)
                    except (OSError, ValueError, KeyError):
                        checked[revision] = (False, None)
                if not checked[revision][0]:
                    invalid.add(revision)
            if invalid:
                for revision in invalid:
                    valid.pop(revision, None)
                self.quarantined.update(invalid)
                continue
            # Freeze source identity with the result, then perform a final stat
            # on the request thread before constructing the accepted user tail.
            results = [dict(row, _source_stat=checked[row['revision_id']][1]) for row in candidates]
            break
        return results, 'matched' if results else 'no_match'

    def expand_neighbors(self, seeds, distance, deadline, aborted):
        """Fetch the complete per-file union from the active manifest revision."""
        import time
        groups = {}
        limit = 0
        for seed in seeds:
            item = self.manifest['files'].get(seed['file_id'])
            ordinal = seed['ordinal']
            if (item is None or item['revision_id'] != seed['revision_id']
                    or item['revision_id'] in self.quarantined
                    or item['source_path'] != seed['source_path']
                    or type(ordinal) is not int or not 0 <= ordinal < len(item['chunk_ids'])
                    or item['chunk_ids'][ordinal] != seed['id']):
                raise ValueError('seed does not match active memory revision')
            group = groups.setdefault(seed['file_id'], {'item': item, 'stat': seed['_source_stat'], 'ordinals': set()})
            group['ordinals'].add(ordinal)
            limit = max(limit, ordinal, len(item['chunk_ids']) - 1 - ordinal)
        # Distances beyond every endpoint produce the same union. Never loop in
        # proportion to an arbitrarily large user-supplied integer.
        radius = min(distance, limit)
        expected = {}
        for file_id, group in groups.items():
            item = group['item']
            selected = set()
            for ordinal in group['ordinals']:
                selected.update(range(max(0, ordinal - radius), min(len(item['chunk_ids']), ordinal + radius + 1)))
            for ordinal in sorted(selected):
                expected[item['chunk_ids'][ordinal]] = (file_id, item, ordinal, group['stat'])
        rows = {}
        ids = list(expected)
        for start in range(0, len(ids), 512):
            if time.monotonic() >= deadline or aborted():
                return [], radius
            with self.lock:
                fetched = self.store.fetch_rows(ids[start:start + 512])
            for row in fetched:
                identity = expected.get(row.get('id'))
                if identity is None or row['id'] in rows:
                    raise ValueError('unexpected memory neighbor identity')
                file_id, item, ordinal, metadata = identity
                if (row.get('file_id') != file_id or row.get('revision_id') != item['revision_id']
                        or row.get('source_path') != item['source_path']
                        or type(row.get('ordinal')) is not int or row['ordinal'] != ordinal
                        or not isinstance(row.get('text'), str)
                        or digest(file_id + item['revision_id'] + str(ordinal) + digest(row['text'])) != row['id']):
                    raise ValueError('neighbor does not match active memory revision')
                rows[row['id']] = dict(row, _source_stat=metadata)
        if set(rows) != set(expected):
            raise ValueError('incomplete memory neighbor union')
        return [rows[chunk_id] for chunk_id in ids], radius

    def close(self):
        if self.store:
            self.store.flush()
            self.store.close()
            self.store = None
