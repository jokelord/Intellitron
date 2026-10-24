"""Opt-in exact accepted PNG artifacts; private, bounded and best effort."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
import threading
import uuid

_VISION_ROOT = Path(__file__).resolve().parents[2] / 'logs' / 'computer-use' / 'vision'
_LOCK = threading.Lock()
_OWNED_NAME = re.compile(r'[0-9a-f]{24}-[0-9]{8,}-stage[12]-[0-9a-f]{32}\.png')


def save_debug_image(config, envelope, stage, artifact):
    if not getattr(config, 'debug_save_images', False):
        return None
    try:
        raw = artifact.png
        if len(raw) > config.debug_image_max_bytes:
            return None
        context = hashlib.sha256((envelope.session_id + ':' + envelope.context_nonce).encode()).hexdigest()[:24]
        name = f'{context}-{envelope.sequence:08d}-{stage}-{uuid.uuid4().hex}.png'
        with _LOCK:
            if _VISION_ROOT.is_symlink():
                return None
            _VISION_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
            _VISION_ROOT.chmod(0o700)
            import fcntl
            lock_fd = os.open(_VISION_ROOT / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                path = _VISION_ROOT / name
                files = sorted((p for p in _VISION_ROOT.glob('*.png') if _OWNED_NAME.fullmatch(p.name)
                                and p.is_file() and not p.is_symlink()),
                               key=lambda p: p.stat().st_mtime_ns)
                total = sum(p.stat().st_size for p in files)
                while files and (len(files) >= config.debug_image_max_files or total + len(raw) > config.debug_image_max_bytes):
                    oldest = files.pop(0)
                    total -= oldest.stat().st_size
                    oldest.unlink()
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(raw)
                except BaseException:
                    path.unlink(missing_ok=True)
                    raise
                return str(path)
            finally:
                os.close(lock_fd)
    except Exception:
        # Disk failures never reject observations or alter input delivery.
        return None
