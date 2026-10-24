"""
系统命令执行工具 (System Operations)

提供受结构化 CommandPolicy 保护的命令执行功能。
普通命令使用受限 shell 兼容路径；浏览器命令使用独立 argv 参数边界。
"""

import math
import re
import shlex
import subprocess
from pathlib import Path

from backend.security.command_policy import CommandDecision, CommandPolicy
from backend.security.capabilities import (
    CapabilityError,
    OperationCapability,
    require_capability,
)
from backend.security.environment_builder import (
    CommandRuntimeConfig,
    EnvironmentBuildError,
    EnvironmentBuilder,
)
from backend.security.path_policy import PathPolicy, PathPolicyError
from backend.security.process_supervisor import ProcessResult, ProcessSupervisor
from backend.computer_use.executable_protocol import (
    VisionDesktopCommandResult,
    VisionDesktopProtocolError,
    execute_vision_desktop_command,
    log_vision_desktop_rejection,
    public_protocol_error_detail,
)


_SHELL_BOUNDARY_TOKENS = frozenset({"&&", "||", ";", "|", "&"})


def _get_command_runtime(
    runtime_config: CommandRuntimeConfig | None = None,
    capability: OperationCapability | None = None,
) -> CommandRuntimeConfig:
    """Return only the session capability's immutable runtime snapshot."""
    bound = require_capability(capability, "command")
    return bound.command_runtime_config()


def _build_command_policy(runtime: CommandRuntimeConfig) -> CommandPolicy:
    """Build policy from the same immutable snapshot used by the executor."""
    python_executable = str(EnvironmentBuilder(runtime).resolve_python_executable())
    return CommandPolicy(
        command_mode=runtime.command_mode,
        # Trust only the configured venv launcher, not arbitrary Python paths
        # or the global interpreter behind a virtual-environment symlink.
        allowed_executables=(*runtime.allowed_executables, python_executable),
        command_cwd_allowlist=runtime.command_cwd_allowlist,
        custom_blacklist=runtime.command_blacklist,
        max_exec_timeout_seconds=runtime.max_exec_timeout_seconds,
        max_output_bytes=runtime.max_output_bytes,
    )


def _normalize_browser_command(command: str) -> str:
    """将 browser_command 的简写统一展开为 playwright-cli 命令。"""
    stripped = command.strip()
    if not stripped:
        return "playwright-cli snapshot"

    if "playwright-cli" in stripped:
        return stripped

    if stripped.startswith("npx "):
        return stripped

    return f"playwright-cli {stripped}"


def _get_current_agent_session_id(session_id: str | None = None) -> str | None:
    """获取当前 agent 会话 ID，用于绑定外部工具会话。"""
    if session_id is not None:
        return session_id

    try:
        from backend.agent.agent_runner import current_session_context

        return current_session_context.get() or None
    except Exception:
        return None


def _validate_playwright_skill_session(
    command: str,
    session_id: str | None,
    allowed_executables: tuple[str, ...],
) -> str | None:
    """Bind the pure browser-skill launcher to the current Agent session.

    The launcher is a trusted ``exec_command`` executable, so its explicit
    session argument must not become a model-controlled escape hatch into a
    sibling session's browser profile.  The read-only ``--probe-runtime``
    diagnostic is the sole no-session exception. Legacy ``playwright-cli``
    commands do not match this path and are rejected by the public exec route.
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    # These are exact paths already granted by the immutable capability. The
    # basename selects additional binding checks; it never grants execution.
    launchers = {
        executable for executable in allowed_executables
        if Path(executable).is_absolute() and Path(executable).name == "playwright-browser"
    }
    launcher_indexes = [index for index, token in enumerate(tokens) if token in launchers]
    if not launcher_indexes:
        return None

    if len(launcher_indexes) != 1:
        return "命令被安全策略阻断 [invalid_browser_skill_invocation]: launcher must appear exactly once"
    launcher_index = launcher_indexes[0]
    segment_end = launcher_index + 1
    while segment_end < len(tokens) and tokens[segment_end] not in _SHELL_BOUNDARY_TOKENS:
        segment_end += 1
    launcher_args = tokens[launcher_index + 1 : segment_end]
    # Runtime probing only imports the pinned Python package and checks the
    # Chromium executable; it creates no browser context or session state.
    if "--probe-runtime" in launcher_args:
        return None
    session_values: list[str] = []
    index = 0
    while index < len(launcher_args):
        token = launcher_args[index]
        if token == "--session-id":
            if index + 1 >= len(launcher_args):
                return "命令被安全策略阻断 [invalid_browser_skill_session]: --session-id requires a value"
            session_values.append(launcher_args[index + 1])
            index += 2
            continue
        if token.startswith("--session-id="):
            session_values.append(token.split("=", 1)[1])
        index += 1

    if len(session_values) != 1 or session_id is None:
        return "命令被安全策略阻断 [invalid_browser_skill_session]: exactly one bound --session-id is required"
    if session_values[0] != session_id:
        return "命令被安全策略阻断 [capability_session_mismatch]: browser skill session does not match the bound Agent session"
    return None


def _unwrap_playwright_bash_wrapper(command: str) -> str:
    """将错误套用到 playwright-cli 上的 GUI bash 模板还原为原始命令。

    某些提示词会要求 GUI 程序走后台包装模板，但这会让 playwright-cli
    丢失快照/refs 输出，只返回通用的 SUCCESS 文本，导致后续轮次无法继续
    基于 eXX 引用操作页面。检测到已知模板时，提取其中真正的
    playwright-cli / npx playwright-cli 命令并直接执行。
    """
    stripped = command.lstrip()
    if not stripped.startswith("bash -c ") or "playwright-cli" not in stripped:
        return command

    marker = " > /tmp/agent_cmd.log 2>&1 & PID=$!"
    marker_idx = command.find(marker)
    if marker_idx == -1:
        return command

    prefix = command[:marker_idx]
    match = re.search(
        r"((?:npx(?:\s+--no-install)?\s+)?playwright-cli\b.*)$",
        prefix,
    )
    if not match:
        return command

    return match.group(1).strip()


def _browser_shell_syntax_reason(command: str) -> str | None:
    """Reject shell-only syntax before parsing a browser command into argv."""
    if any(marker in command for marker in ("$(", "`", "\n", "\r")):
        return "unsupported_command_substitution"
    if ";" in command or "|" in command or re.search(r"(?:^|\s)[<>]|(?:^|\s)&(?:\s|$)", command):
        return "unsupported_browser_shell_syntax"
    return None


def _build_browser_argv(
    command: str,
    session_id: str | None = None,
    *,
    profile_dir: str | None = None,
) -> tuple[str, ...]:
    """Return the final user-dependent Playwright argv.

    The session token is validated with the same contract used by session and
    filesystem code, then inserted as its own argv element.  User-supplied
    session switches are rejected so a tool call cannot escape its bound
    browser profile/session.
    """
    normalized = _normalize_browser_command(_unwrap_playwright_bash_wrapper(command))
    syntax_reason = _browser_shell_syntax_reason(normalized)
    if syntax_reason:
        raise ValueError(syntax_reason)
    try:
        argv = shlex.split(normalized, posix=True)
    except ValueError as exc:
        raise ValueError("invalid_browser_command") from exc

    if not argv:
        argv = ["playwright-cli", "snapshot"]
    if argv[0] == "playwright-cli":
        cli_index = 0
    elif argv[0] == "npx" and len(argv) >= 2:
        cli_index = 2 if argv[1] == "--no-install" else 1
        if cli_index >= len(argv) or argv[cli_index] != "playwright-cli":
            raise ValueError("invalid_browser_command")
    else:
        raise ValueError("invalid_browser_command")

    browser_args = argv[cli_index + 1 :]
    if any(
        arg in {"-s", "--session"}
        or arg.startswith("-s=")
        or arg.startswith("--session=")
        for arg in browser_args
    ):
        raise ValueError("browser_session_override")

    resolved_session_id = _get_current_agent_session_id(session_id)
    if resolved_session_id is not None:
        PathPolicy.validate_session_id(resolved_session_id)
        argv[cli_index + 1 : cli_index + 1] = ["-s", resolved_session_id]

    action_index = cli_index + 1 + (2 if resolved_session_id is not None else 0)
    action = argv[action_index] if action_index < len(argv) else ""
    if action == "open":
        if not any(
            arg == "--browser" or arg.startswith("--browser=") for arg in argv
        ):
            argv.append("--browser=chromium")
        if "--headed" not in argv and "--headless" not in argv:
            argv.append("--headed")
        if not any(
            arg == "--profile" or arg.startswith("--profile=") for arg in argv
        ) and "--persistent" not in argv:
            if profile_dir:
                argv.append(f"--profile={profile_dir}")
            else:
                argv.append("--persistent")
    return tuple(argv)


def _annotate_playwright_output(command: str, output: str) -> str:
    """为 Playwright 结果补充对小模型更直接的操作提示。"""
    if not output or "playwright-cli" not in command:
        return output

    click_like = any(
        token in command
        for token in ("playwright-cli click", "playwright-cli dblclick")
    )
    if not click_like:
        return output

    tabs_idx = output.find("### Open tabs")
    page_idx = output.find("### Page")
    if tabs_idx == -1 or page_idx == -1 or page_idx <= tabs_idx:
        return output

    tabs_block = output[tabs_idx:page_idx]
    current_line = None
    other_tab_lines = []
    for raw_line in tabs_block.splitlines():
        line = raw_line.strip()
        if line.startswith("- ") and "(current)" in line:
            current_line = line
        elif line.startswith("- "):
            other_tab_lines.append(line)

    if current_line is None or not other_tab_lines:
        return output

    page_url_match = re.search(r"^- Page URL:\s*(.+)$", output, re.MULTILINE)
    page_url = page_url_match.group(1).strip() if page_url_match else ""
    current_url_match = re.search(r"\((https?://[^)]+)\)", current_line)
    current_tab_url = current_url_match.group(1).strip() if current_url_match else ""

    if page_url and current_tab_url and page_url != current_tab_url:
        return output

    new_tab_indices = []
    seen_urls = set()
    for line in other_tab_lines:
        index_match = re.match(r"-\s+(\d+):", line)
        url_match = re.search(r"\((https?://[^)]+)\)", line)
        if not index_match or not url_match:
            continue
        idx = index_match.group(1)
        url = url_match.group(1).strip()
        if current_tab_url and url == current_tab_url:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        new_tab_indices.append(idx)

    if not new_tab_indices:
        return output

    hint = (
        "\n\n[提示]\n"
        "检测到点击后可能在新标签页打开了页面，但当前标签页仍停留在原门户页。"
        f" 请优先尝试 `playwright-cli tab-select {new_tab_indices[0]}` 切换到新标签页，"
        "必要时先执行 `playwright-cli tab-list` 确认标签索引；不要重复点击同一链接。"
    )
    return output + hint


def _format_policy_denial(decision: CommandDecision) -> str:
    reason_code = decision.reason_code or "policy_denied"
    display_reason = decision.display_reason or "command denied"
    return f"命令被安全策略阻断 [{reason_code}]: {display_reason}"


def _run_process(
    argv: tuple[str, ...] | list[str],
    *,
    timeout: float,
    check_abort: callable,
    env: dict[str, str],
    cwd: str,
    command_label: str,
    max_output_bytes: int,
    termination_grace_seconds: float,
) -> str:
    """Run an argv command through the shared process supervisor."""
    supervisor = ProcessSupervisor(
        max_output_bytes=max_output_bytes,
        termination_grace_seconds=termination_grace_seconds,
        popen_factory=subprocess.Popen,
    )
    result = supervisor.run_argv(
        argv,
        cwd=cwd,
        env=env,
        timeout=timeout,
        check_abort=check_abort,
    )
    return _format_process_result(result, timeout=timeout, command_label=command_label)


def _run_shell_compat_process(
    command: str,
    *,
    timeout: float,
    check_abort: callable,
    env: dict[str, str],
    cwd: str,
    command_label: str,
    max_output_bytes: int,
    termination_grace_seconds: float,
) -> str:
    """Run explicitly policy-approved compatibility text under supervision."""
    supervisor = ProcessSupervisor(
        max_output_bytes=max_output_bytes,
        termination_grace_seconds=termination_grace_seconds,
        popen_factory=subprocess.Popen,
    )
    result = supervisor.run_shell_compat(
        command,
        cwd=cwd,
        env=env,
        timeout=timeout,
        check_abort=check_abort,
    )
    return _format_process_result(result, timeout=timeout, command_label=command_label)


def _format_process_result(
    result: ProcessResult,
    *,
    timeout: float,
    command_label: str,
) -> str:
    """Translate structured lifecycle metadata to the existing tool response."""
    if result.aborted:
        return "命令已被用户中止"
    if result.timed_out:
        return f"命令执行超时 ({timeout:g} 秒)"

    output_parts = []
    if result.stdout:
        output_parts.append(result.stdout)
    if result.stderr:
        output_parts.append(f"[stderr]\n{result.stderr}")
    if result.truncated:
        output_parts.append("[输出已截断]")
    if result.returncode != 0:
        output_parts.append(f"[退出码: {result.returncode}]")
    return "\n".join(output_parts) if output_parts else "(无输出)"


def _looks_like_browser_command(command: str) -> bool:
    candidate = _unwrap_playwright_bash_wrapper(command).lstrip()
    return candidate.startswith(
        ("playwright-cli", "npx playwright-cli", "npx --no-install playwright-cli")
    )


def _looks_like_vision_desktop_command(command: object) -> bool:
    """Recognize only the reserved executable token before any shell path."""
    if not isinstance(command, str):
        return False
    candidate = command.lstrip()
    if not candidate.startswith("vision_desktop"):
        return False
    suffix = candidate[len("vision_desktop") : len("vision_desktop") + 1]
    return not suffix or suffix.isspace() or suffix in ";|&<>"


def _parse_argv_command(command: str) -> tuple[str, ...]:
    """Parse the public string adapter into exactly one argv command."""
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise ValueError("invalid_argv")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        argv = tuple(lexer)
    except ValueError as exc:
        raise ValueError("invalid_argv") from exc
    if not argv:
        raise ValueError("invalid_argv")
    if any(value in {"|", "||", "&", "&&", ";", "<", "<<", ">", ">>"} for value in argv):
        raise ValueError("unsupported_shell_syntax")
    return argv


def _execute_browser_command(
    command: str,
    *,
    timeout: float | None,
    check_abort: callable,
    session_id: str | None,
    runtime_config: CommandRuntimeConfig | None,
    capability: OperationCapability | None,
) -> str:
    try:
        if session_id is not None:
            PathPolicy.validate_session_id(session_id)
        bound = require_capability(capability, "browser")
        require_capability(bound, "session", session_id)
    except CapabilityError as exc:
        return f"命令被安全策略阻断 [{exc.code}]: {exc.message}"
    except PathPolicyError as exc:
        return f"命令被安全策略阻断 [{exc.code}]: {exc.message}"
    if check_abort and check_abort():
        return "命令已被用户中止"

    try:
        runtime = _get_command_runtime(runtime_config, bound)
        resolved_timeout = runtime.exec_timeout if timeout is None else timeout
        policy = _build_command_policy(runtime)
        if (
            isinstance(resolved_timeout, bool)
            or not isinstance(resolved_timeout, (int, float))
            or not math.isfinite(float(resolved_timeout))
            or resolved_timeout <= 0
            or resolved_timeout > policy.max_exec_timeout_seconds
        ):
            return "命令被安全策略阻断 [invalid_timeout]: timeout is outside the configured limit"
        browser_argv = _build_browser_argv(
            command,
            session_id=session_id,
            profile_dir=runtime.playwright_profile_dir,
        )
        decision = policy.evaluate_argv(browser_argv)
        if not decision.allowed:
            return _format_policy_denial(decision)

        launch_plan = EnvironmentBuilder(runtime).build_argv(browser_argv)
        final_decision = policy.evaluate_argv(launch_plan.argv)
        if not final_decision.allowed:
            return _format_policy_denial(final_decision)
        output = _run_process(
            launch_plan.argv,
            timeout=float(resolved_timeout),
            check_abort=check_abort,
            env=launch_plan.env,
            cwd=launch_plan.cwd,
            command_label="playwright-cli",
            max_output_bytes=policy.max_output_bytes,
            termination_grace_seconds=runtime.termination_grace_seconds,
        )
        return _annotate_playwright_output(" ".join(browser_argv), output)
    except PathPolicyError as exc:
        return f"命令被安全策略阻断 [{exc.code}]: {exc.message}"
    except EnvironmentBuildError as exc:
        return f"命令被安全策略阻断 [{exc.code}]: {exc.message}"
    except ValueError as exc:
        reason_code = str(exc) if str(exc) else "invalid_browser_command"
        return f"命令被安全策略阻断 [{reason_code}]: browser command is invalid"
    except Exception as exc:
        return f"命令执行异常: {type(exc).__name__}: {exc}"


def exec_command(
    command: str,
    timeout: float | None = None,
    check_abort: callable = None,
    session_id: str | None = None,
    runtime_config: CommandRuntimeConfig | None = None,
    capability: OperationCapability | None = None,
) -> str | VisionDesktopCommandResult:
    """
    执行 shell 命令，受黑名单安全保护。

    Args:
        command: 要执行的 shell 命令
        timeout: 超时时间（秒），防止死循环卡死
        check_abort: 可选的中止检查回调，返回 True 表示已被中止
        session_id: 可选的 agent 会话 ID，用于在线程切换后仍绑定同一个浏览器 skill session
        runtime_config: 后端注入的已解析命令运行时配置，不向模型暴露
        capability: 后端注入的不可变 session capability，不向模型暴露

    Returns:
        命令输出结果（stdout + stderr），或安全阻断/错误信息
    """
    try:
        if session_id is not None:
            PathPolicy.validate_session_id(session_id)
        bound = require_capability(capability, "command")
        require_capability(bound, "session", session_id)
    except CapabilityError as exc:
        if _looks_like_vision_desktop_command(command):
            log_vision_desktop_rejection(session_id=session_id, code=exc.code)
            return VisionDesktopCommandResult("REJECTED", f"vision_desktop rejected [{exc.code}]: required capability or session binding is missing")
        return f"命令被安全策略阻断 [{exc.code}]: {exc.message}"
    except PathPolicyError as exc:
        if _looks_like_vision_desktop_command(command):
            log_vision_desktop_rejection(session_id=session_id, code=exc.code)
            return VisionDesktopCommandResult("REJECTED", f"vision_desktop rejected [{exc.code}]: invalid session binding")
        return f"命令被安全策略阻断 [{exc.code}]: {exc.message}"

    skill_session_error = _validate_playwright_skill_session(
        command, bound.session_id, bound.allowed_executables
    )
    if skill_session_error:
        return skill_session_error

    if check_abort and check_abort():
        return "命令已被用户中止"

    if _looks_like_vision_desktop_command(command):
        try:
            return execute_vision_desktop_command(
                command,
                session_id=bound.session_id,
                capability=bound,
                check_abort=check_abort,
            )
        except VisionDesktopProtocolError as exc:
            return VisionDesktopCommandResult(
                state="REJECTED",
                text=f"vision_desktop rejected [{exc.code}]: {public_protocol_error_detail(exc)}",
            )

    if isinstance(command, str) and _looks_like_browser_command(command):
        # The Agent-facing path is now the pure Python skill launcher.  Keep
        # the old direct adapter below for legacy internal callers, but never
        # let a model command revive the npm CLI even if a custom allowlist
        # still contains its name.
        return (
            "命令被安全策略阻断 [legacy_browser_cli_disabled]: "
            "legacy playwright-cli is disabled; read the "
            "playwright-browser-skill and call its exact launcher with the "
            "bound --session-id"
        )

    try:
        runtime = _get_command_runtime(runtime_config, bound)
        resolved_timeout = runtime.exec_timeout if timeout is None else timeout
        policy = _build_command_policy(runtime)
        if (
            isinstance(resolved_timeout, bool)
            or not isinstance(resolved_timeout, (int, float))
            or not math.isfinite(float(resolved_timeout))
            or resolved_timeout <= 0
            or resolved_timeout > policy.max_exec_timeout_seconds
        ):
            return "命令被安全策略阻断 [invalid_timeout]: timeout is outside the configured limit"
        environment_builder = EnvironmentBuilder(runtime)
        if runtime.command_mode == "argv":
            argv = _parse_argv_command(command)
            decision = policy.evaluate_argv(argv)
            if not decision.allowed:
                return _format_policy_denial(decision)
            launch_plan = environment_builder.build_argv(argv)
            final_decision = policy.evaluate_argv(launch_plan.argv)
            if not final_decision.allowed:
                return _format_policy_denial(final_decision)
            return _run_process(
                launch_plan.argv,
                timeout=float(resolved_timeout),
                check_abort=check_abort,
                env=launch_plan.env,
                cwd=launch_plan.cwd,
                command_label=command,
                max_output_bytes=policy.max_output_bytes,
                termination_grace_seconds=runtime.termination_grace_seconds,
            )

        decision = policy.evaluate_shell(command)
        if not decision.allowed:
            return _format_policy_denial(decision)
        execution_environment = environment_builder.build_shell_compat()
        return _run_shell_compat_process(
            command,
            timeout=float(resolved_timeout),
            check_abort=check_abort,
            env=execution_environment.env,
            cwd=execution_environment.cwd,
            command_label=command,
            max_output_bytes=policy.max_output_bytes,
            termination_grace_seconds=runtime.termination_grace_seconds,
        )
    except EnvironmentBuildError as exc:
        return f"命令被安全策略阻断 [{exc.code}]: {exc.message}"
    except ValueError as exc:
        reason_code = str(exc) if str(exc) else "invalid_command"
        return f"命令被安全策略阻断 [{reason_code}]: command is invalid"
    except Exception as exc:
        return f"命令执行异常: {type(exc).__name__}: {exc}"


def browser_command(
    command: str,
    timeout: float | None = None,
    check_abort: callable = None,
    session_id: str | None = None,
    runtime_config: CommandRuntimeConfig | None = None,
    capability: OperationCapability | None = None,
) -> str:
    """Legacy direct browser-command adapter.

    This function remains for compatibility with older internal callers and
    tests, but it is deliberately not registered in the Agent tool catalog.
    New Agent browser automation must use the pure Playwright skill launcher.

    Args:
        command: 浏览器命令简写，如 `snapshot`、`click e58`、`open https://example.com`
        timeout: 超时时间（秒）
        check_abort: 可选的中止检查回调
        session_id: 可选的 agent 会话 ID
        runtime_config: 后端注入的已解析命令运行时配置，不向模型暴露
        capability: 后端注入的不可变 session capability，不向模型暴露

    Returns:
        Playwright CLI 输出结果
    """
    return _execute_browser_command(
        command,
        timeout=timeout,
        check_abort=check_abort,
        session_id=session_id,
        runtime_config=runtime_config,
        capability=capability,
    )


def check_process(
    name: str,
    capability: OperationCapability | None = None,
) -> str:
    """
    查询正在运行的进程（只读，不修改任何进程）。

    Args:
        name: 进程名称或关键词，用于 pgrep -af 匹配
        capability: 后端注入的会话 capability，不向模型暴露

    Returns:
        匹配到的进程列表，或"未找到"提示
    """
    try:
        require_capability(capability, "check_process")
        result = subprocess.run(
            ["pgrep", "-af", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = [
            line
            for line in result.stdout.strip().splitlines()
            if "pgrep" not in line  # 排除 pgrep 自身
        ]
        if lines:
            return f"找到 {len(lines)} 个匹配进程:\n" + "\n".join(lines)
        else:
            return f"未找到匹配 '{name}' 的运行中进程"
    except Exception as e:
        return f"进程查询异常: {type(e).__name__}: {e}"
