"""Structured policy decisions for argv and restricted shell commands.

This module is an application-level deny boundary, not a shell sandbox.  The
restricted-shell path deliberately accepts only a small, inspectable syntax
surface.  OS process isolation remains the responsibility of the later
ProcessSupervisor integration.
"""

from __future__ import annotations

import math
import os
import posixpath
import re
import shlex
from pathlib import Path
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class CommandDecision:
    """A sanitized command-policy result safe to return to a caller."""

    allowed: bool
    mode: str
    reason_code: str | None = None
    display_reason: str | None = None


@dataclass(frozen=True)
class _Rule:
    pattern_text: str
    pattern: re.Pattern[str]
    reason_code: str
    display_reason: str
    mode: str
    builtin: bool = False


DEFAULT_ALLOWED_EXECUTABLES = (
    "awk",
    "bun",
    "cat",
    "cp",
    "curl",
    "cut",
    "date",
    "diff",
    "du",
    "echo",
    "env",
    "find",
    "git",
    "grep",
    "head",
    "ls",
    "mkdir",
    "mv",
    "node",
    "npm",
    "npx",
    "pnpm",
    "printf",
    "pwd",
    "pytest",
    "python",
    "python3",
    "rg",
    "sed",
    "sleep",
    "sort",
    "tail",
    "tar",
    "test",
    "touch",
    "tr",
    "true",
    "uniq",
    "uv",
    "wc",
    "wget",
    "which",
    "yarn",
)


_BUILTIN_RULE_DEFINITIONS = (
    (r"\bsudo(?:\s|$)", "privilege_escalation", "privilege escalation is not allowed", "both"),
    (r"\b(?:doas|pkexec)(?:\s|$)", "privilege_escalation", "privilege escalation is not allowed", "both"),
    (r"\bsu(?:\s|$)", "privilege_escalation", "privilege escalation is not allowed", "both"),
    (r"\bmkfs(?:\.[A-Za-z0-9_-]+)?(?:\s|$)", "filesystem_format", "filesystem formatting is not allowed", "both"),
    (r"\b(?:shutdown|reboot|poweroff|halt)(?:\s|$)", "system_power_control", "system power control is not allowed", "both"),
    (r"\b(?:systemctl|service|init|telinit)(?:\s|$)", "system_process_control", "system service control is not allowed", "both"),
    (r"\b(?:kill|pkill|killall)(?:\s|$)", "system_process_control", "system process control is not allowed", "both"),
    (r"(?:^|\s)(?:of=)?/dev/(?:sd|nvme|mmcblk|vd|xvd|loop)[^\s]*", "device_write", "direct device access is not allowed", "both"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork_bomb", "fork bombs are not allowed", "shell"),
)


_SHELL_INTERPRETERS = frozenset({"bash", "dash", "fish", "ksh", "sh", "zsh"})
_PIPE_INTERPRETERS = _SHELL_INTERPRETERS | frozenset(
    {"lua", "node", "perl", "php", "python", "python2", "python3", "ruby"}
)
_SHELL_OPERATORS = frozenset({"|", "||", "&&", ";"})
_REDIRECTION_OPERATORS = frozenset({"<", "<<", "<<<", ">", ">>", "<>"})
_PROTECTED_ENVIRONMENT_OVERRIDES = frozenset(
    {
        "HOME",
        "NVM_DIR",
        "PATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "UV_PROJECT_ENVIRONMENT",
        "VIRTUAL_ENV",
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


class CommandPolicy:
    """Evaluate final command representations without executing them.

    Regex rules are one defense layer only.  Shell compatibility is separately
    constrained through tokenization and explicit syntax rejection; callers
    must not interpret an allowed decision as proof that arbitrary shell text
    is safe.
    """

    def evaluate_desktop_helper(self, argv, capability, *, configured_binary, owner_session_id):
        """Authorize only the deployment-configured private helper FD entrypoint."""
        from backend.security.capabilities import OperationCapability
        valid = (
            isinstance(capability, OperationCapability)
            and isinstance(configured_binary, str) and os.path.isabs(configured_binary)
            and '\x00' not in configured_binary
            and isinstance(argv, (tuple, list)) and len(argv) == 3
            and argv[0] == configured_binary and argv[1] == '--control-fd'
            and isinstance(argv[2], str) and len(argv[2]) <= 10
            and argv[2].isascii() and argv[2].isdigit()
            and 3 <= int(argv[2]) <= 2147483647
            and getattr(capability, 'session_id', None) == owner_session_id
            and isinstance(owner_session_id, str) and bool(owner_session_id)
            and getattr(capability, 'allow_desktop_capture', False) is True
        )
        return CommandDecision(bool(valid), 'desktop_helper',
            None if valid else 'desktop_helper_denied',
            None if valid else 'desktop helper authority or fixed arguments invalid')

    def __init__(
        self,
        *,
        command_mode: str = "restricted_shell",
        allowed_executables: Iterable[str] | None = None,
        command_cwd_allowlist: Sequence[str] = (),
        custom_blacklist: Sequence[Mapping[str, object]] | None = None,
        max_exec_timeout_seconds: float = 3600.0,
        max_output_bytes: int = 4_194_304,
    ) -> None:
        if not isinstance(command_mode, str) or command_mode not in {
            "argv",
            "restricted_shell",
        }:
            raise ValueError("command_mode must be 'argv' or 'restricted_shell'")
        self.command_mode = command_mode
        self.max_exec_timeout_seconds = self._positive_finite_number(
            max_exec_timeout_seconds, "max_exec_timeout_seconds", upper=86_400.0
        )
        if (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or max_output_bytes <= 0
            or max_output_bytes > 1_073_741_824
        ):
            raise ValueError("max_output_bytes must be a positive integer")
        self.max_output_bytes = max_output_bytes

        configured_executables = (
            DEFAULT_ALLOWED_EXECUTABLES
            if allowed_executables is None
            else tuple(allowed_executables)
        )
        if isinstance(allowed_executables, (str, bytes)):
            raise ValueError("allowed_executables must be a non-empty list")
        if not configured_executables:
            raise ValueError("allowed_executables must not be empty")
        normalized_executables: list[str] = []
        for executable in configured_executables:
            if (
                not isinstance(executable, str)
                or not executable.strip()
                or "\x00" in executable
                or executable.strip() == "*"
            ):
                raise ValueError("allowed_executables must contain explicit non-empty names")
            normalized_executables.append(executable.strip())
        self.allowed_executables = frozenset(normalized_executables)
        if isinstance(command_cwd_allowlist, (str, bytes)) or not isinstance(
            command_cwd_allowlist, Sequence
        ):
            raise ValueError("command_cwd_allowlist must be a list of absolute directories")
        roots = []
        for root in command_cwd_allowlist:
            if (
                not isinstance(root, str)
                or not self._literal_absolute_directory(root)
                or os.path.realpath(root) == "/"
            ):
                raise ValueError("command_cwd_allowlist requires explicit absolute non-root directories")
            roots.append(os.path.normpath(root))
        self.command_cwd_allowlist = tuple(roots)

        self._builtin_rules = tuple(
            _Rule(
                pattern_text=pattern,
                pattern=re.compile(pattern, re.IGNORECASE),
                reason_code=reason_code,
                display_reason=display_reason,
                mode=mode,
                builtin=True,
            )
            for pattern, reason_code, display_reason, mode in _BUILTIN_RULE_DEFINITIONS
        )
        self._custom_rules, self._builtin_reason_overrides = self.merge_blacklist(
            custom_blacklist
        )

    @staticmethod
    def _positive_finite_number(
        value: object, name: str, *, upper: float | None = None
    ) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be finite and positive")
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{name} must be finite and positive") from None
        if not math.isfinite(parsed) or parsed <= 0 or (upper is not None and parsed > upper):
            raise ValueError(f"{name} must be finite and positive")
        return parsed

    def merge_blacklist(
        self, configured: Sequence[Mapping[str, object]] | None
    ) -> tuple[tuple[_Rule, ...], dict[str, str]]:
        """Validate additive rules and optional built-in reason refinements."""
        if configured is None:
            return (), {}
        if isinstance(configured, (str, bytes)) or not isinstance(configured, Sequence):
            raise ValueError("command_blacklist must be a list of rule mappings")

        builtin_by_pattern = {rule.pattern_text: rule for rule in self._builtin_rules}
        custom_rules: list[_Rule] = []
        overrides: dict[str, str] = {}
        allowed_keys = {"pattern", "reason", "mode"}
        for index, item in enumerate(configured):
            if not isinstance(item, Mapping):
                raise ValueError(f"command_blacklist[{index}] must be a rule mapping")
            unknown_keys = set(item) - allowed_keys
            if unknown_keys:
                raise ValueError(f"command_blacklist[{index}] contains unsupported keys")
            pattern_text = item.get("pattern")
            reason = item.get("reason")
            mode = item.get("mode", "both")
            if not isinstance(pattern_text, str) or not pattern_text.strip():
                raise ValueError(f"command_blacklist[{index}].pattern must not be empty")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"command_blacklist[{index}].reason must not be empty")
            if mode not in {"argv", "shell", "both"}:
                raise ValueError(
                    f"command_blacklist[{index}].mode must be argv, shell, or both"
                )
            try:
                compiled = re.compile(pattern_text, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(
                    f"command_blacklist[{index}].pattern is not a valid regular expression"
                ) from exc

            builtin = builtin_by_pattern.get(pattern_text)
            if builtin is not None and mode in {builtin.mode, "both"}:
                overrides[builtin.reason_code] = reason.strip()
                continue
            custom_rules.append(
                _Rule(
                    pattern_text=pattern_text,
                    pattern=compiled,
                    reason_code="custom_blacklist",
                    display_reason=reason.strip(),
                    mode=str(mode),
                )
            )
        return tuple(custom_rules), overrides

    @staticmethod
    def _allow(mode: str) -> CommandDecision:
        return CommandDecision(True, mode)

    def _deny(self, mode: str, reason_code: str, display_reason: str) -> CommandDecision:
        return CommandDecision(
            False,
            mode,
            reason_code,
            self._builtin_reason_overrides.get(reason_code, display_reason),
        )

    @staticmethod
    def _mode_matches(rule_mode: str, mode: str) -> bool:
        requested = "shell" if mode == "restricted_shell" else "argv"
        return rule_mode in {requested, "both"}

    def _match_regex_rules(self, rendered: str, mode: str) -> CommandDecision | None:
        for rule in self._builtin_rules:
            if self._mode_matches(rule.mode, mode) and rule.pattern.search(rendered):
                return self._deny(mode, rule.reason_code, rule.display_reason)
        for rule in self._custom_rules:
            if self._mode_matches(rule.mode, mode) and rule.pattern.search(rendered):
                return self._deny(mode, rule.reason_code, rule.display_reason)
        return None

    @staticmethod
    def _validate_argv(argv: Sequence[str]) -> tuple[str, ...] | None:
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv:
            return None
        normalized = tuple(argv)
        if any(
            not isinstance(arg, str) or "\x00" in arg or (index == 0 and not arg)
            for index, arg in enumerate(normalized)
        ):
            return None
        return normalized

    @staticmethod
    def _root_target(value: str) -> bool:
        normalized = re.sub(r"/+", "/", value.strip())
        try:
            normalized = posixpath.normpath(normalized)
        except (TypeError, ValueError):
            return False
        return normalized in {"/", "/*"} or normalized.startswith("/{")

    def _semantic_argv_check(
        self, argv: tuple[str, ...], mode: str
    ) -> CommandDecision | None:
        executable = os.path.basename(argv[0]).lower()
        args = argv[1:]

        if executable == "env":
            command_index: int | None = None
            index = 0
            while index < len(args):
                arg = args[index]
                if arg == "--":
                    command_index = index + 1
                    break
                if arg in {"-S", "--split-string"} or arg.startswith(
                    "--split-string="
                ):
                    return self._deny(
                        mode,
                        "env_command_string",
                        "env split-string execution is not supported",
                    )
                if arg in {"-C", "--chdir"} or arg.startswith("--chdir="):
                    return self._deny(
                        mode,
                        "cwd_override",
                        "command working directory cannot be overridden",
                    )
                if arg in {"-P", "--path"} or arg.startswith("--path="):
                    return self._deny(
                        mode,
                        "path_override",
                        "command executable path cannot be overridden",
                    )
                protected_name = None
                if arg in {"-u", "--unset"}:
                    if index + 1 >= len(args):
                        break
                    protected_name = args[index + 1]
                    index += 2
                elif arg.startswith("--unset="):
                    protected_name = arg.split("=", 1)[1]
                    index += 1
                else:
                    assignment = re.fullmatch(
                        r"([A-Za-z_][A-Za-z0-9_]*)=.*", arg, re.DOTALL
                    )
                    if assignment is not None:
                        protected_name = assignment.group(1)
                        index += 1
                    elif arg in {"-", "-i", "--ignore-environment", "-0", "--null"}:
                        index += 1
                    else:
                        command_index = index
                if protected_name in _PROTECTED_ENVIRONMENT_OVERRIDES:
                    return self._deny(
                        mode,
                        "protected_environment_override",
                        "runtime environment authority cannot be overridden",
                    )
                if command_index is not None:
                    break

            if command_index is not None and command_index < len(args):
                nested = tuple(args[command_index:])
                nested_executable = nested[0]
                nested_name = os.path.basename(nested_executable).lower()
                if (
                    "/" in nested_executable
                    and nested_executable not in self.allowed_executables
                ):
                    return self._deny(
                        mode,
                        "executable_path_not_allowed",
                        "env executable paths must be explicitly allowlisted",
                    )
                if mode == "restricted_shell" and nested_name in _SHELL_INTERPRETERS:
                    return self._deny(
                        mode,
                        "unsupported_shell_nesting",
                        "nested shell execution is not supported",
                    )
                nested_semantic = self._semantic_argv_check(nested, mode)
                if nested_semantic is not None:
                    return nested_semantic
                nested_regex = self._match_regex_rules(shlex.join(nested), mode)
                if nested_regex is not None:
                    return nested_regex
                if not self._executable_allowed(nested_executable):
                    return self._deny(
                        mode,
                        "executable_not_allowed",
                        "the env-wrapped executable is not in the configured allowlist",
                    )

        if executable == "rm":
            recursive = any(
                arg == "--recursive"
                or (arg.startswith("-") and not arg.startswith("--") and "r" in arg[1:].lower())
                for arg in args
            )
            targets = [arg for arg in args if not arg.startswith("-")]
            if recursive and any(self._root_target(target) for target in targets):
                return self._deny(
                    mode, "recursive_root_delete", "recursive deletion of root is not allowed"
                )

        if executable == "chmod" and args:
            targets = [arg for arg in args[1:] if not arg.startswith("-")]
            mode_value = next((arg for arg in args if re.fullmatch(r"[0-7]{3,4}", arg)), None)
            if (
                mode_value
                and mode_value[-3] == "7"
                and any(self._root_target(target) for target in targets)
            ):
                return self._deny(
                    mode, "root_permission_change", "root permission expansion is not allowed"
                )
        return None

    def _executable_allowed(self, executable: str) -> bool:
        if "/" in executable:
            return executable in self.allowed_executables or os.path.basename(
                executable
            ) in self.allowed_executables
        return executable in self.allowed_executables

    @staticmethod
    def _literal_absolute_directory(value: str) -> bool:
        return value.startswith("/") and not any(
            char in value for char in "\x00\n\r$`*?[]{}~\\"
        )

    def _evaluate_cd(self, argv: tuple[str, ...]) -> CommandDecision:
        """Allow only explicit directory changes; never infer cwd from shell state.

        This is admission checking, not filesystem isolation: a shell process
        still owns its cwd after validation, like other allowed executables.
        """
        mode = "restricted_shell"
        args = argv[1:]
        if args and args[0] == "--":
            args = args[1:]
        if len(args) != 1 or not self._literal_absolute_directory(args[0]):
            return self._deny(mode, "unsupported_cd", "cd requires one literal absolute directory")
        target = Path(args[0])
        try:
            # Reject links at every component, including '..' traversal before
            # normalization, so shell logical cwd cannot differ from validation.
            if any(part.is_symlink() for part in (target, *target.parents)):
                raise ValueError("symlink directory")
            resolved = target.resolve(strict=True)
            allowed = resolved.is_dir() and any(
                resolved.is_relative_to(Path(root))
                for root in self.command_cwd_allowlist
            )
        except (OSError, RuntimeError, ValueError):
            allowed = False
        if not allowed:
            return self._deny(mode, "cwd_not_allowed", "cd target is outside command_cwd_allowlist or is not an accessible directory")
        return self._allow(mode)

    def evaluate_argv(self, argv: Sequence[str]) -> CommandDecision:
        mode = "argv"
        normalized = self._validate_argv(argv)
        if normalized is None:
            return self._deny(mode, "invalid_argv", "argv must be a non-empty string sequence")

        semantic = self._semantic_argv_check(normalized, mode)
        if semantic is not None:
            return semantic
        rendered = shlex.join(normalized)
        matched = self._match_regex_rules(rendered, mode)
        if matched is not None:
            return matched
        if not self._executable_allowed(normalized[0]):
            return self._deny(
                mode,
                "executable_not_allowed",
                "the requested executable is not in the configured allowlist",
            )
        return self._allow(mode)

    @staticmethod
    def _shell_tokens(command: str) -> list[str]:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)

    @staticmethod
    def _split_shell_segments(tokens: Sequence[str]) -> tuple[list[list[str]], bool]:
        segments: list[list[str]] = [[]]
        has_pipeline = False
        for token in tokens:
            if token in _SHELL_OPERATORS:
                if token == "|":
                    has_pipeline = True
                if not segments[-1]:
                    return [], has_pipeline
                segments.append([])
            else:
                segments[-1].append(token)
        if not segments[-1]:
            return [], has_pipeline
        return segments, has_pipeline

    @staticmethod
    def _segment_command(segment: Sequence[str]) -> tuple[str, tuple[str, ...]] | None:
        offset = 0
        while offset < len(segment) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[offset], re.DOTALL
        ):
            offset += 1
        if offset >= len(segment):
            return None
        return segment[offset], tuple(segment[offset:])

    def evaluate_shell(self, command: str) -> CommandDecision:
        mode = "restricted_shell"
        if self.command_mode != "restricted_shell":
            return self._deny(
                mode, "shell_mode_disabled", "shell compatibility mode is disabled"
            )
        if (
            not isinstance(command, str)
            or not command.strip()
            or "\x00" in command
            or "\n" in command
            or "\r" in command
        ):
            return self._deny(mode, "invalid_shell", "shell command must be non-empty text")

        if "$(" in command or "`" in command or "<(" in command or ">(" in command:
            return self._deny(
                mode,
                "unsupported_command_substitution",
                "command and process substitution are not supported",
            )
        if re.search(r"(?:^|[;&|]\s*)eval(?:\s|$)", command, re.IGNORECASE):
            return self._deny(mode, "unsupported_eval", "eval is not supported")
        if re.search(r"(?:^|[;&|]\s*)(?:source|\.)(?:\s|$)", command, re.IGNORECASE):
            return self._deny(mode, "unsupported_source", "source is not supported")

        matched = self._match_regex_rules(command, mode)
        if matched is not None:
            return matched
        try:
            tokens = self._shell_tokens(command)
        except ValueError:
            return self._deny(mode, "invalid_shell", "shell quoting is malformed")
        if not tokens:
            return self._deny(mode, "invalid_shell", "shell command must be non-empty text")

        for token in tokens:
            if token in _REDIRECTION_OPERATORS or token.startswith(">&"):
                return self._deny(
                    mode,
                    "dangerous_redirection",
                    "redirection is not supported in restricted shell mode",
                )
            if token == "&":
                return self._deny(
                    mode,
                    "unsupported_background",
                    "background execution is not supported",
                )

        segments, has_pipeline = self._split_shell_segments(tokens)
        if not segments:
            return self._deny(mode, "invalid_shell", "shell operator placement is invalid")

        normalized_segments: list[tuple[str, ...]] = []
        commands: list[str] = []
        for segment in segments:
            for token in segment:
                assignment = re.fullmatch(
                    r"([A-Za-z_][A-Za-z0-9_]*)=.*", token, re.DOTALL
                )
                if assignment is None:
                    break
                if assignment.group(1) in _PROTECTED_ENVIRONMENT_OVERRIDES:
                    return self._deny(
                        mode,
                        "protected_environment_override",
                        "runtime environment authority cannot be overridden",
                    )
            parsed = self._segment_command(segment)
            if parsed is None:
                return self._deny(mode, "invalid_shell", "shell command is missing an executable")
            executable, normalized = parsed
            executable_name = os.path.basename(executable).lower()
            if executable == "cd":
                decision = self._evaluate_cd(normalized)
                if not decision.allowed:
                    return decision
                commands.append(executable_name)
                normalized_segments.append(normalized)
                continue
            if "/" in executable and executable not in self.allowed_executables:
                return self._deny(
                    mode,
                    "executable_path_not_allowed",
                    "shell executable paths must be explicitly allowlisted",
                )
            if executable_name in _SHELL_INTERPRETERS:
                return self._deny(
                    mode,
                    "unsupported_shell_nesting",
                    "nested shell execution is not supported",
                )
            semantic = self._semantic_argv_check(normalized, mode)
            if semantic is not None:
                return semantic
            if not self._executable_allowed(executable):
                return self._deny(
                    mode,
                    "executable_not_allowed",
                    "the requested executable is not in the configured allowlist",
                )
            commands.append(executable_name)
            normalized_segments.append(normalized)

        if has_pipeline and any(command_name in _PIPE_INTERPRETERS for command_name in commands):
            return self._deny(
                mode,
                "interpreter_pipeline",
                "pipelines into interpreters are not supported",
            )

        for segment in normalized_segments:
            rendered = shlex.join(segment)
            matched = self._match_regex_rules(rendered, mode)
            if matched is not None:
                return matched
        return self._allow(mode)


__all__ = ["CommandDecision", "CommandPolicy", "DEFAULT_ALLOWED_EXECUTABLES"]
