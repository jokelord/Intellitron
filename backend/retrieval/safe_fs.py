"""Descriptor-relative, no-follow access to configured local-memory files."""
import json
import os
import stat
import uuid
from pathlib import Path


def open_directory(path: Path, *, create=False):
    path = Path(path).absolute()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if part in ('', '.', '..'):
                raise ValueError('unsafe path component')
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_regular(path: Path, limit: int) -> tuple[bytes, os.stat_result]:
    parent = open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('source is not a bounded regular file')
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_mtime_ns, s.st_ctime_ns, s.st_size)
        if len(data) > limit or identity(before) != identity(after):
            raise ValueError('source changed while reading')
        return bytes(data), after
    finally:
        os.close(fd)


def atomic_json(path: Path, value):
    parent = open_directory(path.parent, create=True)
    temporary = '.tmp-' + uuid.uuid4().hex
    try:
        try:
            existing = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode):
                raise ValueError('generated target is not a regular file')
        except FileNotFoundError:
            pass
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
    finally:
        os.close(parent)


def read_json(path: Path):
    data, _ = read_regular(path, 16 * 1024 * 1024)
    return json.loads(data)
