"""Validate persisted memory state before any replay or native collection access."""
from datetime import datetime
import hashlib
from pathlib import PurePosixPath
import re

_HASH = re.compile(r'[0-9a-f]{64}\Z')
_MAX_IDS = 100000
_MAX_INTEGER = (1 << 63) - 1


def require_hash(value):
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError('invalid local-memory hash identifier')
    return value


def has_memory_source_suffix(value):
    return value.lower().endswith(('.md', '.txt'))


def require_source_path(value):
    if (not isinstance(value, str) or not value or len(value) > 4096 or '\\' in value
            or any(ord(char) < 32 for char in value)):
        raise ValueError('invalid memory source path')
    parts = value.split('/')
    if (any(part in ('', '.', '..') for part in parts) or PurePosixPath(value).is_absolute()
            or not has_memory_source_suffix(value)):
        raise ValueError('memory source path must be canonical and relative')
    return value


def require_ids(value):
    if not isinstance(value, list) or len(value) > _MAX_IDS:
        raise ValueError('invalid local-memory chunk identifiers')
    for chunk_id in value:
        require_hash(chunk_id)
    if len(set(value)) != len(value):
        raise ValueError('duplicate local-memory chunk identifier')
    return value


def require_file_metadata(value):
    for name in ('mtime_ns', 'size'):
        if name not in value or type(value[name]) is not int or not 0 <= value[name] <= _MAX_INTEGER:
            raise ValueError('invalid local-memory file metadata')


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('data'), bytes):
        raise ValueError('invalid local-memory source snapshot')
    require_file_metadata(snapshot)
    require_hash(snapshot.get('sha256'))
    if len(snapshot['data']) != snapshot['size'] or hashlib.sha256(snapshot['data']).hexdigest() != snapshot['sha256']:
        raise ValueError('inconsistent local-memory source snapshot')


def validate_manifest(manifest, *, source_root):
    if (not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'fingerprint', 'files'}
            or type(manifest['schema_version']) is not int or manifest['schema_version'] != 1
            or not isinstance(manifest['files'], dict) or len(manifest['files']) > _MAX_IDS):
        raise ValueError('invalid local-memory manifest')
    fingerprint = manifest['fingerprint']
    if fingerprint is None:
        if manifest['files']:
            raise ValueError('uninitialized memory manifest cannot contain files')
        return manifest
    require_hash(fingerprint)
    seen_ids = set()
    required = {'source_path', 'sha256', 'mtime_ns', 'size', 'revision_id', 'chunk_ids', 'indexed_at', 'status'}
    for file_id, item in manifest['files'].items():
        require_hash(file_id)
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError('invalid local-memory file record')
        relative = require_source_path(item['source_path'])
        if hashlib.sha256(relative.encode('utf-8')).hexdigest() != file_id:
            raise ValueError('file identifier does not match source path')
        # Deleted sources remain valid state, but persisted paths may not resolve
        # through existing symlinks outside the fixed configured read scope.
        expected_root = source_root.absolute()
        if not (expected_root / relative).resolve().is_relative_to(expected_root.resolve()):
            raise ValueError('memory source path escapes configured scope')
        content_hash = require_hash(item['sha256'])
        revision = require_hash(item['revision_id'])
        if hashlib.sha256((file_id + content_hash + fingerprint).encode()).hexdigest() != revision:
            raise ValueError('revision identifier does not match file fingerprint')
        require_file_metadata(item)
        chunks = require_ids(item['chunk_ids'])
        if seen_ids.intersection(chunks):
            raise ValueError('chunk identifiers shared across file records')
        seen_ids.update(chunks)
        if item['status'] != 'ready' or not isinstance(item['indexed_at'], str):
            raise ValueError('invalid local-memory file status')
        try:
            indexed_at = datetime.fromisoformat(item['indexed_at'])
        except ValueError as exc:
            raise ValueError('invalid local-memory index timestamp') from exc
        if indexed_at.tzinfo is None:
            raise ValueError('local-memory index timestamp needs timezone')
    return manifest


def validate_journal(operation):
    if not isinstance(operation, dict) or set(operation) != {'kind', 'old_ids', 'new_ids'}:
        raise ValueError('invalid local-memory journal')
    if operation['kind'] not in ('delete', 'replace'):
        raise ValueError('invalid local-memory journal operation')
    require_ids(operation['old_ids'])
    require_ids(operation['new_ids'])
    if operation['kind'] == 'delete' and operation['new_ids']:
        raise ValueError('deletion journal cannot publish new chunks')
    return operation
