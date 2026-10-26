"""Small path helpers shared by launch-time configuration readers."""

from pathlib import Path


def resolve_path(
    value: str | Path,
    base_dir: str | Path,
) -> Path:
    """Expand a leading home marker, then resolve relative paths against ``base_dir``."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return Path(base_dir).expanduser() / path
