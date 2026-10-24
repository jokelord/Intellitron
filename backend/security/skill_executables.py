"""Resolve explicitly configured skill executables at configuration load time."""

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import re

from backend.tools.skill_ops import _iter_skill_records


def resolve_skill_executables(entries: Sequence[object], skills_dir: Path) -> list[str]:
    """Expand stable skill identities without granting other discovered scripts.

    Ordinary executable strings retain their existing CommandPolicy semantics.
    Discovery happens only for structured references and never at tool execution.
    """
    references = [entry for entry in entries if not isinstance(entry, str)]
    if not references:
        return list(entries)
    for reference in references:
        if not isinstance(reference, Mapping) or set(reference) != {"skill", "path"}:
            raise ValueError("skill executable reference must contain only skill and path")
        name, path = reference["skill"], reference["path"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", name):
            raise ValueError("skill executable reference requires an exact skill name")
        if (
            not isinstance(path, str)
            or not path
            or "\x00" in path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
        ):
            raise ValueError("skill executable path must be a relative path without traversal")

    try:
        root = skills_dir.resolve(strict=True)
        records = _iter_skill_records(root)
    except (OSError, ValueError) as exc:
        raise ValueError("skill executable discovery requires a readable skills directory") from exc
    resolved_entries: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            resolved_entries.append(entry)
            continue
        matches = [
            record for record in records
            if record["name"] == entry["skill"] and not record.get("error")
        ]
        if len(matches) != 1:
            raise ValueError(
                f"skill executable reference {entry['skill']!r} requires one unique valid skill"
            )
        directory = root / matches[0]["directory"]
        executable = directory / entry["path"]
        try:
            target_directory = directory.resolve(strict=True)
            metadata = (directory / "SKILL.md").resolve(strict=True)
            target = executable.resolve(strict=True)
            if (
                not target_directory.is_relative_to(root)
                or not metadata.is_relative_to(target_directory)
                or not target.is_relative_to(target_directory)
                or not target.is_file()
                or not os.access(target, os.X_OK)
            ):
                raise ValueError("target must be an executable file contained in its skill")
        except (OSError, ValueError) as exc:
            raise ValueError(f"invalid skill executable reference {entry['skill']!r}: {exc}") from exc
        # Keep the explicitly granted invocation path (including its basename),
        # while checking resolved containment above to reject symlink escapes.
        resolved_entries.append(str(executable))
    return resolved_entries
