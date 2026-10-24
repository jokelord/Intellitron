"""Filesystem containment and no-follow checks shared by backend tools.

The policy deliberately consumes already-resolved runtime settings.  It never
loads YAML or derives authority from a prompt, task, or UI value.
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


class PathPolicyError(ValueError):
    """A stable, safe-to-display rejection from a filesystem policy."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


@dataclass(frozen=True)
class ResolvedPath:
    requested: str
    resolved: str
    root: str
    operation: str
    relative_parts: tuple[str, ...] = field(default_factory=tuple, repr=False)

    @property
    def name(self) -> str:
        if not self.relative_parts:
            raise PathPolicyError("invalid_path", "a root directory is not a file target")
        return self.relative_parts[-1]


class PathPolicy:
    """Filesystem authority for one already-resolved operation policy."""

    _SESSION_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")

    @classmethod
    def validate_session_id(cls, session_id: str) -> str:
        """Validate the opaque token before it can select any session resource."""
        if (
            not isinstance(session_id, str)
            or session_id in {".", ".."}
            or not cls._SESSION_ID.fullmatch(session_id)
        ):
            raise PathPolicyError("invalid_session_id", "session identifier is invalid")
        return session_id

    @classmethod
    def is_canonical_session_id(cls, session_id: str) -> bool:
        """Return whether an already-safe token is the generated UUID form."""
        cls.validate_session_id(session_id)
        try:
            return str(uuid.UUID(session_id)) == session_id
        except (ValueError, AttributeError):
            return False

    def __init__(
        self,
        *,
        file_operation_roots: Iterable[str | os.PathLike[str]],
        image_input_roots: Iterable[str | os.PathLike[str]] | None = None,
        session_base_dir: str | os.PathLike[str] = "/tmp/agent_images",
        max_file_bytes: int = 536_870_912,
        allow_overwrite_existing: bool = True,
    ) -> None:
        self.file_operation_roots = self._resolve_roots(file_operation_roots)
        self.image_input_roots = self._resolve_roots(
            image_input_roots if image_input_roots is not None else ()
        )
        if not isinstance(max_file_bytes, int) or isinstance(max_file_bytes, bool) or max_file_bytes < 0:
            raise ValueError("max_file_bytes must be a non-negative integer")
        self.max_file_bytes = max_file_bytes
        self.allow_overwrite_existing = bool(allow_overwrite_existing)
        try:
            self.session_base_dir = Path(session_base_dir).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, TypeError) as exc:
            raise ValueError("session_base_dir is invalid") from exc

    @staticmethod
    def _resolve_roots(roots: Iterable[str | os.PathLike[str]]) -> tuple[Path, ...]:
        resolved: list[Path] = []
        for raw_root in roots:
            try:
                root = Path(raw_root).expanduser().resolve(strict=True)
            except (OSError, RuntimeError, TypeError) as exc:
                raise ValueError("configured filesystem root is invalid") from exc
            if not root.is_dir():
                raise ValueError("configured filesystem root must be a directory")
            if root not in resolved:
                resolved.append(root)
        return tuple(resolved)

    @staticmethod
    def _coerce_absolute(path: str | os.PathLike[str]) -> Path:
        try:
            raw = os.fspath(path)
        except TypeError as exc:
            raise PathPolicyError("invalid_path", "path must be a string") from exc
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise PathPolicyError("invalid_path", "path is malformed")
        try:
            return Path(os.path.abspath(os.path.expanduser(raw)))
        except (OSError, RuntimeError) as exc:
            raise PathPolicyError("invalid_path", "path is malformed") from exc

    @staticmethod
    def _contains(root: Path, candidate: Path) -> bool:
        return candidate == root or root in candidate.parents

    @staticmethod
    def _raise_for_oserror(exc: OSError) -> None:
        if exc.errno in {errno.ELOOP}:
            raise PathPolicyError("symlink_not_allowed", "symbolic links are not allowed") from exc
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            raise PathPolicyError("path_race", "path changed during validation") from exc
        raise PathPolicyError("invalid_path", "path could not be safely inspected") from exc

    def _select_root(self, candidate: Path, roots: tuple[Path, ...]) -> tuple[Path, tuple[str, ...]]:
        matches = [root for root in roots if self._contains(root, candidate)]
        if not matches:
            raise PathPolicyError("path_not_allowed", "path is outside configured roots")
        root = max(matches, key=lambda value: len(value.parts))
        return root, candidate.relative_to(root).parts

    def _check_canonical_containment(self, candidate: Path, root: Path) -> None:
        try:
            canonical = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise PathPolicyError("invalid_path", "path could not be resolved") from exc
        if not self._contains(root, canonical):
            raise PathPolicyError("path_not_allowed", "resolved path is outside configured roots")

    @staticmethod
    def _lstat(path: Path) -> os.stat_result:
        try:
            return os.lstat(path)
        except FileNotFoundError as exc:
            raise PathPolicyError("path_not_found", "path does not exist") from exc
        except OSError as exc:
            PathPolicy._raise_for_oserror(exc)
            raise AssertionError("unreachable")

    def _assert_no_symlink_components(self, root: Path, relative_parts: tuple[str, ...]) -> None:
        current = root
        for part in relative_parts:
            current = current / part
            try:
                item = os.lstat(current)
            except FileNotFoundError:
                return
            except OSError as exc:
                self._raise_for_oserror(exc)
            if stat.S_ISLNK(item.st_mode):
                raise PathPolicyError("symlink_not_allowed", "symbolic links are not allowed")

    def _validate_no_follow_target(
        self,
        path: str | os.PathLike[str],
        operation: str,
        *,
        allow_missing: bool,
        roots: tuple[Path, ...],
    ) -> ResolvedPath:
        candidate = self._coerce_absolute(path)
        root, relative_parts = self._select_root(candidate, roots)
        if not relative_parts:
            raise PathPolicyError("invalid_file_type", "a directory is not a file target")

        # A final symlink gets its own stable denial even when it resolves out
        # of root; parent escapes are reported by canonical containment below.
        try:
            final_stat = os.lstat(candidate)
        except FileNotFoundError:
            final_stat = None
        except OSError as exc:
            self._raise_for_oserror(exc)
            raise AssertionError("unreachable")
        if final_stat is not None and stat.S_ISLNK(final_stat.st_mode):
            raise PathPolicyError("symlink_not_allowed", "symbolic links are not allowed")

        self._check_canonical_containment(candidate, root)
        self._assert_no_symlink_components(root, relative_parts)
        if final_stat is None:
            if not allow_missing:
                raise PathPolicyError("path_not_found", "path does not exist")
        elif not stat.S_ISREG(final_stat.st_mode):
            raise PathPolicyError("invalid_file_type", "target must be a regular file")
        elif final_stat.st_size > self.max_file_bytes:
            raise PathPolicyError("file_too_large", "file exceeds configured byte limit")

        return ResolvedPath(
            requested=os.fspath(path),
            resolved=str(candidate),
            root=str(root),
            operation=operation,
            relative_parts=relative_parts,
        )

    def resolve_existing(self, path: str | os.PathLike[str], operation: str) -> ResolvedPath:
        """Resolve a read-only existing file; in-root symlinks remain readable."""
        candidate = self._coerce_absolute(path)
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PathPolicyError("path_not_found", "path does not exist") from exc
        root, _ = self._select_root(resolved, self.file_operation_roots)
        try:
            metadata = os.stat(resolved)
        except OSError as exc:
            self._raise_for_oserror(exc)
            raise AssertionError("unreachable")
        if not stat.S_ISREG(metadata.st_mode):
            raise PathPolicyError("invalid_file_type", "target must be a regular file")
        if metadata.st_size > self.max_file_bytes:
            raise PathPolicyError("file_too_large", "file exceeds configured byte limit")
        return ResolvedPath(str(path), str(resolved), str(root), operation)

    def validate_modify(self, path: str | os.PathLike[str], operation: str) -> ResolvedPath:
        return self._validate_no_follow_target(
            path, operation, allow_missing=False, roots=self.file_operation_roots
        )

    def validate_create(self, path: str | os.PathLike[str], operation: str) -> ResolvedPath:
        return self._validate_no_follow_target(
            path, operation, allow_missing=True, roots=self.file_operation_roots
        )

    def validate_image_input(self, path: str | os.PathLike[str]) -> ResolvedPath:
        return self._validate_no_follow_target(
            path, "image", allow_missing=False, roots=self.image_input_roots
        )

    def validate_content(self, content: str) -> bytes:
        if not isinstance(content, str):
            raise PathPolicyError("invalid_content", "content must be text")
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            raise PathPolicyError("file_too_large", "content exceeds configured byte limit")
        return encoded

    def validate_session_dir(self, session_id: str) -> str:
        self.validate_session_id(session_id)
        directory = self.session_base_dir / session_id
        if directory.parent != self.session_base_dir:
            raise PathPolicyError("invalid_session_id", "session identifier is invalid")
        return str(directory)

    def allocate_session_dir(self, session_id: str) -> str:
        directory = Path(self.validate_session_dir(session_id))
        self.session_base_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            try:
                metadata = os.lstat(directory)
            except OSError as exc:
                self._raise_for_oserror(exc)
                raise AssertionError("unreachable")
            if stat.S_ISLNK(metadata.st_mode):
                raise PathPolicyError("symlink_not_allowed", "symbolic links are not allowed")
            if not stat.S_ISDIR(metadata.st_mode):
                raise PathPolicyError("invalid_file_type", "session target must be a directory")
        except OSError as exc:
            self._raise_for_oserror(exc)
        return str(directory)

    def cleanup_session_dir(self, session_id: str) -> bool:
        """Remove one validated session directory beneath the fixed base.

        This is independent of file capabilities and is used only for
        best-effort cleanup after a session object has already gone away.
        """
        directory = Path(self.validate_session_dir(session_id))
        try:
            metadata = os.lstat(directory)
        except FileNotFoundError:
            return False
        except OSError as exc:
            self._raise_for_oserror(exc)
            raise AssertionError("unreachable")
        if stat.S_ISLNK(metadata.st_mode):
            raise PathPolicyError("symlink_not_allowed", "symbolic links are not allowed")
        if not stat.S_ISDIR(metadata.st_mode):
            raise PathPolicyError("invalid_file_type", "session target must be a directory")
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            return False
        return True

    def open_parent_dir(self, target: ResolvedPath, *, create: bool) -> int:
        """Return a no-follow descriptor for a target's containing directory."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(target.root, flags | nofollow)
        except OSError as exc:
            self._raise_for_oserror(exc)
            raise AssertionError("unreachable")
        try:
            for part in target.relative_parts[:-1]:
                try:
                    next_fd = os.open(part, flags | nofollow, dir_fd=directory_fd)
                except FileNotFoundError:
                    if not create:
                        raise PathPolicyError("path_not_found", "parent directory does not exist") from None
                    try:
                        os.mkdir(part, 0o700, dir_fd=directory_fd)
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        self._raise_for_oserror(exc)
                    try:
                        next_fd = os.open(part, flags | nofollow, dir_fd=directory_fd)
                    except OSError as exc:
                        self._raise_for_oserror(exc)
                except OSError as exc:
                    self._raise_for_oserror(exc)
                try:
                    if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                        raise PathPolicyError("invalid_file_type", "parent must be a directory")
                except Exception:
                    os.close(next_fd)
                    raise
                os.close(directory_fd)
                directory_fd = next_fd
            return directory_fd
        except Exception:
            os.close(directory_fd)
            raise

    def check_atomic_target(
        self,
        parent_fd: int,
        name: str,
        *,
        require_existing: bool,
        allow_overwrite: bool,
    ) -> os.stat_result | None:
        """Validate and return metadata for the final component.

        The returned metadata was obtained with ``follow_symlinks=False`` from
        the already-safe parent descriptor, so callers can preserve attributes
        without resolving a replacement path.
        """
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if require_existing:
                raise PathPolicyError("path_not_found", "path does not exist") from None
            return None
        except OSError as exc:
            self._raise_for_oserror(exc)
            raise AssertionError("unreachable")
        if stat.S_ISLNK(metadata.st_mode):
            raise PathPolicyError("symlink_not_allowed", "symbolic links are not allowed")
        if not stat.S_ISREG(metadata.st_mode):
            raise PathPolicyError("invalid_file_type", "target must be a regular file")
        if metadata.st_size > self.max_file_bytes:
            raise PathPolicyError("file_too_large", "file exceeds configured byte limit")
        if not allow_overwrite:
            raise PathPolicyError("overwrite_not_allowed", "overwriting an existing file is disabled")
        return metadata

    def open_existing_file(self, target: ResolvedPath) -> int:
        parent_fd = self.open_parent_dir(target, create=False)
        try:
            self.check_atomic_target(
                parent_fd, target.name, require_existing=True, allow_overwrite=True
            )
            try:
                descriptor = os.open(
                    target.name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                self._raise_for_oserror(exc)
                raise AssertionError("unreachable")
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise PathPolicyError("invalid_file_type", "target must be a regular file")
                if metadata.st_size > self.max_file_bytes:
                    raise PathPolicyError("file_too_large", "file exceeds configured byte limit")
                return descriptor
            except Exception:
                os.close(descriptor)
                raise
        finally:
            os.close(parent_fd)

    @staticmethod
    def temporary_name() -> str:
        return f".agent-write-{secrets.token_hex(16)}"
