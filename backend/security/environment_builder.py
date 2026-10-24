"""Resolved command runtime and minimal child-process environment.

The builder consumes an already parsed runtime configuration.  It does not
read YAML, inherit the parent environment wholesale, or execute shell startup
files to discover toolchains.
"""

from __future__ import annotations

import math
import os
import pwd
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from backend.security.command_policy import CommandPolicy


DEFAULT_ENVIRONMENT_ALLOWLIST = (
    "COLORTERM",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
)

_PROTECTED_ENVIRONMENT_NAMES = frozenset(
    {
        "HOME",
        "NVM_DIR",
        "PATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "UV_PROJECT_ENVIRONMENT",
        "VIRTUAL_ENV",
        # Loader, interpreter, shell-startup, and Git hook/configuration
        # variables can turn an otherwise allowed executable into arbitrary
        # code execution or redirect it to attacker-controlled state.
        "BASH_ENV",
        "ENV",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "NODE_OPTIONS",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PERL5OPT",
        "RUBYOPT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_SSH_COMMAND",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "CDPATH",
        "IFS",
        "SHELLOPTS",
        "BASHOPTS",
        "PROMPT_COMMAND",
    }
)
_SECRET_ENVIRONMENT_NAME = re.compile(
    r"(?:^|_)(?:API_?KEY|CREDENTIALS?|PASSWORD|PASSWD|PRIVATE_?KEY|SECRET|TOKEN)(?:_|$)",
    re.IGNORECASE,
)
_SENSITIVE_ENVIRONMENT_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "GCP_",
    "GITHUB_",
    "GITLAB_",
    "GOOGLE_",
    "HF_",
    "HUGGINGFACE_",
    "OPENAI_",
    "SSH_",
)
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class EnvironmentBuildError(ValueError):
    """Fail-closed runtime-environment rejection safe for tool output."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


@dataclass(frozen=True)
class CommandRuntimeConfig:
    """One immutable, validated command/process configuration snapshot."""

    command_mode: str
    allowed_executables: tuple[str, ...]
    command_blacklist: Sequence[Mapping[str, object]] | None
    exec_timeout: float
    max_exec_timeout_seconds: float
    max_output_bytes: int
    termination_grace_seconds: float
    allow_browser: bool
    work_dir: str
    python_executable: str
    nvm_dir: str
    playwright_profile_dir: str | None = None
    environment_allowlist: tuple[str, ...] = DEFAULT_ENVIRONMENT_ALLOWLIST
    command_path_entries: tuple[str, ...] = ()
    command_cwd_allowlist: tuple[str, ...] = ()
    allow_desktop_capture: bool = False
    allow_desktop_input: bool = False

    def __post_init__(self) -> None:
        policy = CommandPolicy(
            command_mode=self.command_mode,
            allowed_executables=self.allowed_executables,
            command_cwd_allowlist=self.command_cwd_allowlist,
            custom_blacklist=self.command_blacklist,
            max_exec_timeout_seconds=self.max_exec_timeout_seconds,
            max_output_bytes=self.max_output_bytes,
        )
        timeout = self._finite_positive(self.exec_timeout, "exec_timeout")
        if timeout > policy.max_exec_timeout_seconds:
            raise ValueError(
                "exec_timeout must be no greater than max_exec_timeout_seconds"
            )
        grace = self._finite_nonnegative(
            self.termination_grace_seconds, "termination_grace_seconds"
        )
        if grace > 60.0:
            raise ValueError("termination_grace_seconds must not exceed 60 seconds")
        if not isinstance(self.allow_browser, bool):
            raise ValueError("allow_browser must be a boolean")
        if not isinstance(self.allow_desktop_capture, bool):
            raise ValueError("allow_desktop_capture must be a boolean")
        if not isinstance(self.allow_desktop_input, bool):
            raise ValueError("allow_desktop_input must be a boolean")
        if self.allow_desktop_input and not self.allow_desktop_capture:
            raise ValueError("allow_desktop_input requires allow_desktop_capture")
        for name, value in (
            ("work_dir", self.work_dir),
            ("python_executable", self.python_executable),
            ("nvm_dir", self.nvm_dir),
        ):
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"{name} must be non-empty text")
            self._validate_configured_path(value, name)
        if self.playwright_profile_dir is not None:
            if (
                not isinstance(self.playwright_profile_dir, str)
                or not self.playwright_profile_dir.strip()
                or "\x00" in self.playwright_profile_dir
            ):
                raise ValueError("playwright_profile_dir must be non-empty text")
            self._validate_configured_path(
                self.playwright_profile_dir, "playwright_profile_dir"
            )
        self._validate_environment_allowlist(self.environment_allowlist)
        self._validate_path_entries(self.command_path_entries)
        object.__setattr__(self, "command_cwd_allowlist", policy.command_cwd_allowlist)
        if self.command_blacklist is not None:
            object.__setattr__(
                self,
                "command_blacklist",
                tuple(dict(rule) for rule in self.command_blacklist),
            )

    @classmethod
    def from_agent_config(cls, agent: object) -> "CommandRuntimeConfig":
        return cls(
            command_mode=getattr(agent, "command_mode"),
            allowed_executables=tuple(getattr(agent, "allowed_executables")),
            command_blacklist=getattr(agent, "command_blacklist", None),
            exec_timeout=float(getattr(agent, "exec_timeout")),
            max_exec_timeout_seconds=float(
                getattr(agent, "max_exec_timeout_seconds")
            ),
            max_output_bytes=getattr(agent, "max_output_bytes"),
            termination_grace_seconds=float(
                getattr(agent, "termination_grace_seconds")
            ),
            allow_browser=getattr(agent, "allow_browser"),
            allow_desktop_capture=getattr(agent, "allow_desktop_capture", False),
            allow_desktop_input=getattr(agent, "allow_desktop_input", False),
            work_dir=getattr(agent, "work_dir"),
            python_executable=getattr(agent, "python_executable"),
            nvm_dir=getattr(agent, "nvm_dir"),
            playwright_profile_dir=getattr(agent, "playwright_profile_dir", None),
            environment_allowlist=tuple(
                getattr(agent, "environment_allowlist", DEFAULT_ENVIRONMENT_ALLOWLIST)
            ),
            command_path_entries=tuple(getattr(agent, "command_path_entries", ())),
            command_cwd_allowlist=getattr(agent, "command_cwd_allowlist", ()),
        )

    @staticmethod
    def _finite_positive(value: object, name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be finite and positive")
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{name} must be finite and positive") from None
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError(f"{name} must be finite and positive")
        return parsed

    @staticmethod
    def _finite_nonnegative(value: object, name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be finite and non-negative")
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{name} must be finite and non-negative") from None
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError(f"{name} must be finite and non-negative")
        return parsed

    @staticmethod
    def _validate_environment_allowlist(values: object) -> None:
        if isinstance(values, (str, bytes)) or not isinstance(values, tuple):
            raise ValueError("environment_allowlist must be a list of safe names")
        for value in values:
            if (
                not isinstance(value, str)
                or not _ENVIRONMENT_NAME.fullmatch(value)
                or value in _PROTECTED_ENVIRONMENT_NAMES
                or _SECRET_ENVIRONMENT_NAME.search(value)
                or value.upper().startswith(_SENSITIVE_ENVIRONMENT_PREFIXES)
            ):
                raise ValueError("environment_allowlist contains an unsafe name")

    @staticmethod
    def _validate_configured_path(value: str, name: str) -> None:
        """Reject ambiguous relative paths before they become authority."""
        try:
            if value == "~" or value.startswith("~/"):
                expanded = os.path.expanduser(value)
            elif value.startswith("~"):
                raise ValueError
            else:
                expanded = value
            if not os.path.isabs(expanded):
                raise ValueError
            Path(expanded)
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ValueError(f"{name} must be an absolute path") from None

    @staticmethod
    def _validate_path_entries(values: object) -> None:
        if isinstance(values, (str, bytes)) or not isinstance(values, tuple):
            raise ValueError("command_path_entries must be a list of paths")
        for value in values:
            if (
                not isinstance(value, str)
                or not value.strip()
                or "\x00" in value
            ):
                raise ValueError("command_path_entries must contain non-empty paths")
            CommandRuntimeConfig._validate_configured_path(value, "command_path_entries")


@dataclass(frozen=True)
class ExecutionEnvironment:
    env: dict[str, str]
    cwd: str
    path_entries: tuple[str, ...]


@dataclass(frozen=True)
class ArgvExecutionPlan:
    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: str


class EnvironmentBuilder:
    """Build a minimal environment and resolve argv executables directly."""

    def __init__(
        self,
        runtime: CommandRuntimeConfig,
        *,
        parent_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.runtime = runtime
        self._parent_environment = (
            os.environ if parent_environment is None else parent_environment
        )

    def build_environment(self) -> ExecutionEnvironment:
        work_dir = self._resolve_work_dir(self.runtime.work_dir)
        python_path = self.resolve_python_executable()
        agent_bin = python_path.parent
        agent_venv = agent_bin.parent
        nvm_dir = self._expand_absolute(self.runtime.nvm_dir, "nvm_dir")

        path_entries = self._build_path_entries(agent_bin, nvm_dir)
        if self.runtime.playwright_profile_dir is not None:
            self._validate_optional_directory(self.runtime.playwright_profile_dir)
        environment: dict[str, str] = {}
        for name in self.runtime.environment_allowlist:
            value = self._parent_environment.get(name)
            if isinstance(value, str) and "\x00" not in value:
                environment[name] = value

        home_path = self._trusted_home()
        home = str(home_path)
        environment.update(
            {
                "HOME": home,
                "NVM_DIR": str(nvm_dir),
                "PATH": os.pathsep.join(path_entries),
                "VIRTUAL_ENV": str(agent_venv),
            }
        )
        return ExecutionEnvironment(
            env=environment,
            cwd=str(work_dir),
            path_entries=path_entries,
        )

    def build_argv(self, argv: Sequence[str]) -> ArgvExecutionPlan:
        if isinstance(argv, (str, bytes)) or not argv:
            raise EnvironmentBuildError("invalid_argv", "argv is invalid")
        normalized = tuple(argv)
        if any(not isinstance(value, str) or "\x00" in value for value in normalized):
            raise EnvironmentBuildError("invalid_argv", "argv is invalid")
        environment = self.build_environment()
        python_executable = str(self.resolve_python_executable())
        # The configured interpreter is also advertised by its absolute path.
        # Preserve that venv launcher (often a symlink), not its global target.
        executable = (
            python_executable
            if normalized[0] == python_executable
            else self._resolve_executable(normalized[0], environment.env["PATH"])
        )
        return ArgvExecutionPlan(
            argv=(executable, *normalized[1:]),
            env=environment.env,
            cwd=environment.cwd,
        )

    def build_shell_compat(self) -> ExecutionEnvironment:
        """Return the fixed environment for explicitly approved shell text.

        Shell parsing remains compatibility semantics.  The command itself is
        passed separately to ProcessSupervisor's constant positional boundary;
        it is never interpolated into an environment preamble.
        """
        return self.build_environment()

    @staticmethod
    def _trusted_home() -> Path:
        try:
            return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
        except (KeyError, OSError, RuntimeError) as exc:
            raise EnvironmentBuildError(
                "invalid_runtime_path", "OS account home directory is unavailable"
            ) from exc

    @classmethod
    def _expand_absolute(cls, value: str, name: str) -> Path:
        try:
            if value == "~":
                path = cls._trusted_home()
            elif value.startswith("~/"):
                path = cls._trusted_home() / value[2:]
            elif value.startswith("~"):
                raise ValueError("named-user expansion is not supported")
            else:
                path = Path(value)
            if not path.is_absolute():
                path = Path.cwd() / path
            return Path(os.path.abspath(path))
        except (OSError, RuntimeError, TypeError) as exc:
            raise EnvironmentBuildError(
                "invalid_runtime_path", f"configured {name} is invalid"
            ) from exc

    @classmethod
    def _resolve_work_dir(cls, value: str) -> Path:
        candidate = cls._expand_absolute(value, "work_dir")
        cls._reject_symlink_components(candidate, "invalid_work_dir")
        try:
            metadata = os.lstat(candidate)
        except FileNotFoundError:
            try:
                parent = candidate.parent.resolve(strict=True)
                if not parent.is_dir():
                    raise EnvironmentBuildError(
                        "invalid_work_dir", "configured work directory is unusable"
                    )
                candidate.mkdir(mode=0o700)
                metadata = os.lstat(candidate)
            except EnvironmentBuildError:
                raise
            except (OSError, RuntimeError) as exc:
                raise EnvironmentBuildError(
                    "invalid_work_dir", "configured work directory is unusable"
                ) from exc
        except OSError as exc:
            raise EnvironmentBuildError(
                "invalid_work_dir", "configured work directory is unusable"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise EnvironmentBuildError(
                "invalid_work_dir", "configured work directory is unusable"
            )
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise EnvironmentBuildError(
                "invalid_work_dir", "configured work directory is unusable"
            ) from exc
        if not os.access(resolved, os.R_OK | os.W_OK | os.X_OK):
            raise EnvironmentBuildError(
                "invalid_work_dir", "configured work directory is unusable"
            )
        return resolved

    @classmethod
    def _validate_optional_directory(cls, value: str) -> Path:
        candidate = cls._expand_absolute(value, "playwright_profile_dir")
        cls._reject_symlink_components(candidate, "invalid_profile_dir")
        try:
            metadata = os.lstat(candidate)
        except FileNotFoundError:
            try:
                parent = candidate.parent.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise EnvironmentBuildError(
                    "invalid_profile_dir", "configured browser profile directory is unusable"
                ) from exc
            if not parent.is_dir():
                raise EnvironmentBuildError(
                    "invalid_profile_dir", "configured browser profile directory is unusable"
                )
            return candidate
        except OSError as exc:
            raise EnvironmentBuildError(
                "invalid_profile_dir", "configured browser profile directory is unusable"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise EnvironmentBuildError(
                "invalid_profile_dir", "configured browser profile directory is unusable"
            )
        return candidate.resolve(strict=True)

    @staticmethod
    def _reject_symlink_components(candidate: Path, error_code: str) -> None:
        current = Path(candidate.anchor)
        for part in candidate.parts[1:]:
            current /= part
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise EnvironmentBuildError(
                    error_code, "configured runtime directory is unusable"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise EnvironmentBuildError(
                    error_code, "configured runtime directory is unusable"
                )

    @classmethod
    def _resolve_python(cls, value: str) -> Path:
        candidate = cls._expand_absolute(value, "python_executable")
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise EnvironmentBuildError(
                "invalid_python_executable",
                "configured agent Python executable is unusable",
            )
        return candidate

    def resolve_python_executable(self) -> Path:
        """Validate the snapshot's interpreter and expand its trusted-home path."""
        return self._resolve_python(self.runtime.python_executable)

    def _build_path_entries(self, agent_bin: Path, nvm_dir: Path) -> tuple[str, ...]:
        candidates: list[Path] = [agent_bin]
        candidates.extend(
            self._expand_absolute(value, "command_path_entries")
            for value in self.runtime.command_path_entries
        )
        node_bin = self._latest_nvm_node_bin(nvm_dir)
        if node_bin is not None:
            candidates.append(node_bin)
        home = self._trusted_home()
        candidates.extend(
            (
                home / ".local" / "bin",
                home / ".bun" / "bin",
                home / ".local" / "share" / "pnpm",
                Path("/usr/local/bin"),
                Path("/usr/bin"),
                Path("/bin"),
            )
        )

        resolved: list[str] = []
        for candidate in candidates:
            try:
                canonical = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not canonical.is_dir():
                continue
            rendered = str(canonical)
            if rendered not in resolved:
                resolved.append(rendered)
        if not resolved:
            raise EnvironmentBuildError(
                "invalid_runtime_path", "no trusted executable directories are available"
            )
        return tuple(resolved)

    @staticmethod
    def _latest_nvm_node_bin(nvm_dir: Path) -> Path | None:
        versions_dir = nvm_dir / "versions" / "node"
        try:
            candidates = [
                item / "bin"
                for item in versions_dir.iterdir()
                if item.is_dir() and (item / "bin").is_dir()
            ]
        except OSError:
            return None

        def version_key(path: Path) -> tuple[int, ...]:
            numbers = re.findall(r"\d+", path.parent.name)
            return tuple(int(value) for value in numbers)

        return max(candidates, key=version_key) if candidates else None

    @staticmethod
    def _resolve_executable(executable: str, path_value: str) -> str:
        if not executable or os.path.basename(executable) != executable:
            raise EnvironmentBuildError(
                "executable_not_resolved",
                "argv executable must resolve from the trusted command path",
            )
        resolved = shutil.which(executable, path=path_value)
        if resolved is None:
            raise EnvironmentBuildError(
                "executable_not_found",
                "allowed executable is unavailable in the trusted command path",
            )
        # Preserve the trusted PATH entry's executable name for the final
        # allowlist check.  Virtual-environment launchers are commonly symlinks
        # whose targets have versioned basenames (for example python3.12).
        return os.path.abspath(resolved)


__all__ = [
    "ArgvExecutionPlan",
    "CommandRuntimeConfig",
    "DEFAULT_ENVIRONMENT_ALLOWLIST",
    "EnvironmentBuildError",
    "EnvironmentBuilder",
    "ExecutionEnvironment",
]
