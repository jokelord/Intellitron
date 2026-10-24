"""Immutable operation authority resolved once at Agent session admission.

This module consumes already-parsed application configuration only.  It never
loads YAML and never derives authority from prompts, tasks, timeline events, or
tool arguments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from backend.security.environment_builder import (
    DEFAULT_ENVIRONMENT_ALLOWLIST,
    CommandRuntimeConfig,
)
from backend.security.path_policy import PathPolicy


class CapabilityError(ValueError):
    """Stable, safe-to-display capability construction or binding failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


_CHILD_ID = re.compile(r"[A-Za-z0-9_.:-]{3,128}\Z")


def validate_child_id(child_id: str) -> str:
    """Validate the canonical manager dispatch identity contract."""
    if not isinstance(child_id, str) or child_id != child_id.strip():
        raise ValueError("child_id must be a trimmed string")
    if not 3 <= len(child_id) <= 128:
        raise ValueError("child_id must be 3..128 characters")
    if not _CHILD_ID.fullmatch(child_id):
        raise ValueError(
            "child_id may only contain letters, digits, underscore, dot, colon, and hyphen"
        )
    return child_id


@dataclass(frozen=True)
class CommandBlacklistRule:
    """Deeply immutable representation of one configured command rule."""

    pattern: str
    reason: str
    mode: str = "both"

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "CommandBlacklistRule":
        return cls(
            pattern=str(value.get("pattern", "")),
            reason=str(value.get("reason", "")),
            mode=str(value.get("mode", "both")),
        )

    def as_mapping(self) -> dict[str, str]:
        return {
            "pattern": self.pattern,
            "reason": self.reason,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class OperationCapability:
    """One session-pinned, immutable snapshot of all operation authority."""

    session_id: str
    read_roots: tuple[str, ...]
    search_roots: tuple[str, ...]
    file_operation_roots: tuple[str, ...]
    image_input_roots: tuple[str, ...]
    session_temp_base_dir: str
    command_mode: str
    allowed_executables: tuple[str, ...]
    max_exec_timeout_seconds: float
    max_output_bytes: int
    max_file_bytes: int
    allow_overwrite_existing: bool
    allow_browser: bool
    exec_timeout: float
    termination_grace_seconds: float
    work_dir: str
    python_executable: str
    nvm_dir: str
    playwright_profile_dir: str | None = None
    environment_allowlist: tuple[str, ...] = DEFAULT_ENVIRONMENT_ALLOWLIST
    command_path_entries: tuple[str, ...] = ()
    command_cwd_allowlist: tuple[str, ...] = ()
    command_blacklist: tuple[CommandBlacklistRule, ...] = ()
    allow_desktop_capture: bool = False
    allow_desktop_input: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "read_roots",
            "search_roots",
            "file_operation_roots",
            "image_input_roots",
            "allowed_executables",
            "environment_allowlist",
            "command_path_entries",
            "command_cwd_allowlist",
            "command_blacklist",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, tuple):
                raise CapabilityError(
                    "invalid_capability_config",
                    f"{field_name} must be immutable tuple data",
                )
        if any(
            not isinstance(rule, CommandBlacklistRule)
            for rule in self.command_blacklist
        ):
            raise CapabilityError(
                "invalid_capability_config",
                "command_blacklist must contain immutable rule objects",
            )
        PathPolicy.validate_session_id(self.session_id)
        if not isinstance(self.allow_overwrite_existing, bool):
            raise CapabilityError(
                "invalid_capability_config",
                "allow_overwrite_existing must be a boolean",
            )
        for field_name in ("allow_desktop_capture", "allow_desktop_input"):
            if not isinstance(getattr(self, field_name), bool):
                raise CapabilityError(
                    "invalid_capability_config",
                    f"{field_name} must be a boolean",
                )
        if self.allow_desktop_input and not self.allow_desktop_capture:
            raise CapabilityError(
                "invalid_capability_config",
                "allow_desktop_input requires allow_desktop_capture",
            )
        try:
            self.command_runtime_config()
            self.path_policy()
        except CapabilityError:
            raise
        except (TypeError, ValueError) as exc:
            raise CapabilityError(
                "invalid_capability_config",
                "operation capability configuration is invalid",
            ) from exc

    def path_policy(self) -> PathPolicy:
        """Build a policy adapter without combining operation-specific roots."""
        return PathPolicy(
            file_operation_roots=self.file_operation_roots,
            image_input_roots=self.image_input_roots,
            session_base_dir=self.session_temp_base_dir,
            max_file_bytes=self.max_file_bytes,
            allow_overwrite_existing=self.allow_overwrite_existing,
        )

    def command_runtime_config(self) -> CommandRuntimeConfig:
        """Return the validated command snapshot consumed by ``sys_ops``."""
        rules: Sequence[Mapping[str, object]] | None = (
            tuple(rule.as_mapping() for rule in self.command_blacklist)
            if self.command_blacklist
            else None
        )
        return CommandRuntimeConfig(
            command_mode=self.command_mode,
            allowed_executables=self.allowed_executables,
            command_blacklist=rules,
            exec_timeout=self.exec_timeout,
            max_exec_timeout_seconds=self.max_exec_timeout_seconds,
            max_output_bytes=self.max_output_bytes,
            termination_grace_seconds=self.termination_grace_seconds,
            allow_browser=self.allow_browser,
            work_dir=self.work_dir,
            python_executable=self.python_executable,
            nvm_dir=self.nvm_dir,
            playwright_profile_dir=self.playwright_profile_dir,
            environment_allowlist=self.environment_allowlist,
            command_path_entries=self.command_path_entries,
            command_cwd_allowlist=self.command_cwd_allowlist,
            allow_desktop_capture=self.allow_desktop_capture,
            allow_desktop_input=self.allow_desktop_input,
        )

    def for_session(self, session_id: str) -> "OperationCapability":
        """Bind identical authority to another validated internal identity."""
        PathPolicy.validate_session_id(session_id)
        return replace(self, session_id=session_id)


def derive_child_capability(
    parent: OperationCapability,
    child_id: str,
) -> OperationCapability:
    """Bind a child to an equal immutable snapshot without consulting task text."""
    if not isinstance(parent, OperationCapability):
        raise CapabilityError(
            "missing_capability", "parent operation capability is required"
        )
    validate_child_id(child_id)
    return replace(
        parent,
        session_id=child_id,
        allow_desktop_capture=False,
        allow_desktop_input=False,
    )


def _resolved_roots(values: object, *, field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or values is None:
        raise CapabilityError(
            "invalid_capability_config", f"{field_name} must be a list of paths"
        )
    try:
        policy = PathPolicy(file_operation_roots=tuple(values))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CapabilityError(
            "invalid_capability_config", f"{field_name} is invalid"
        ) from exc
    return tuple(str(root) for root in policy.file_operation_roots)


def _configured_roots(owner: object, field_name: str, fallback: object) -> object:
    if hasattr(owner, field_name):
        configured = getattr(owner, field_name)
        if configured is not None:
            return configured
    return fallback


def build_session_capability(
    config: object,
    *,
    session_id: str,
    mode: str,
) -> OperationCapability:
    """Resolve one immutable capability before any session state is allocated."""
    PathPolicy.validate_session_id(session_id)
    if mode != "tool_calling":
        raise CapabilityError("invalid_agent_mode", "agent mode is not supported")

    agent = getattr(config, "agent", None)
    read_file = getattr(config, "read_file", None)
    if agent is None or read_file is None:
        raise CapabilityError(
            "capability_config_missing",
            "parsed application security configuration is required",
        )

    try:
        read_roots = _resolved_roots(
            getattr(read_file, "allowed_roots"), field_name="read_file.allowed_roots"
        )
        search_roots = _resolved_roots(
            getattr(agent, "search_whitelist_paths"),
            field_name="agent.search_whitelist_paths",
        )
        file_operation_roots = _resolved_roots(
            _configured_roots(agent, "file_operation_roots", read_roots),
            field_name="agent.file_operation_roots",
        )
        image_input_roots = _resolved_roots(
            _configured_roots(agent, "image_input_roots", read_roots),
            field_name="agent.image_input_roots",
        )
        runtime = CommandRuntimeConfig.from_agent_config(agent)
        raw_blacklist = runtime.command_blacklist or ()
        immutable_blacklist = tuple(
            CommandBlacklistRule.from_mapping(rule) for rule in raw_blacklist
        )
        configured_max_file_bytes = getattr(agent, "max_file_bytes", None)
        max_file_bytes = (
            getattr(read_file, "max_source_bytes")
            if configured_max_file_bytes is None
            else configured_max_file_bytes
        )
        if (
            isinstance(max_file_bytes, bool)
            or not isinstance(max_file_bytes, int)
            or max_file_bytes < 0
        ):
            raise CapabilityError(
                "invalid_capability_config",
                "agent.max_file_bytes must be a non-negative integer or null",
            )
        session_temp_base_dir = getattr(
            agent, "session_temp_base_dir", "/tmp/agent_images"
        )
        allow_overwrite_existing = getattr(
            agent, "allow_overwrite_existing", True
        )
        if not isinstance(allow_overwrite_existing, bool):
            raise CapabilityError(
                "invalid_capability_config",
                "agent.allow_overwrite_existing must be a boolean",
            )
        return OperationCapability(
            session_id=session_id,
            read_roots=read_roots,
            search_roots=search_roots,
            file_operation_roots=file_operation_roots,
            image_input_roots=image_input_roots,
            session_temp_base_dir=session_temp_base_dir,
            command_mode=runtime.command_mode,
            allowed_executables=runtime.allowed_executables,
            max_exec_timeout_seconds=runtime.max_exec_timeout_seconds,
            max_output_bytes=runtime.max_output_bytes,
            max_file_bytes=max_file_bytes,
            allow_overwrite_existing=allow_overwrite_existing,
            allow_browser=runtime.allow_browser,
            exec_timeout=runtime.exec_timeout,
            termination_grace_seconds=runtime.termination_grace_seconds,
            work_dir=runtime.work_dir,
            python_executable=runtime.python_executable,
            nvm_dir=runtime.nvm_dir,
            playwright_profile_dir=runtime.playwright_profile_dir,
            environment_allowlist=runtime.environment_allowlist,
            command_path_entries=runtime.command_path_entries,
            command_cwd_allowlist=runtime.command_cwd_allowlist,
            command_blacklist=immutable_blacklist,
            allow_desktop_capture=runtime.allow_desktop_capture,
            allow_desktop_input=runtime.allow_desktop_input,
        )
    except CapabilityError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapabilityError(
            "invalid_capability_config",
            f"operation capability configuration is invalid: {exc}",
        ) from exc


def require_capability(
    capability: OperationCapability | None,
    operation: str,
    target: object = None,
) -> OperationCapability:
    """Fail closed when a side-effect boundary lacks session authority."""
    if not isinstance(capability, OperationCapability):
        raise CapabilityError(
            "missing_capability", "operation capability is required"
        )
    if operation == "session" and target != capability.session_id:
        raise CapabilityError(
            "capability_session_mismatch",
            "operation capability does not match the bound session",
        )
    if operation == "browser" and not capability.allow_browser:
        raise CapabilityError("browser_disabled", "browser execution is disabled")
    desktop_flags = {
        "desktop_capture": capability.allow_desktop_capture,
        "desktop_input": capability.allow_desktop_input,
    }
    if operation in desktop_flags and not desktop_flags[operation]:
        raise CapabilityError("desktop_disabled", f"{operation} is disabled")
    return capability


__all__ = [
    "CapabilityError",
    "CommandBlacklistRule",
    "OperationCapability",
    "build_session_capability",
    "derive_child_capability",
    "require_capability",
    "validate_child_id",
]
