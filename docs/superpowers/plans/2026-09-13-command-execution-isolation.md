# Command Execution Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Replace the current shell-string-plus-blacklist boundary with explicit command policy, safe browser argument handling, bounded process groups, and a minimal execution environment.

**Architecture:** Introduce CommandPolicy and ProcessSupervisor behind the existing tool adapter. New internal calls use argv; the legacy command string is an explicitly governed compatibility path. Every transformed command is evaluated before process creation, and all child processes share a cleanup boundary.

**Tech Stack:** Python 3.12, subprocess, os, signal, resource where supported, shlex only for compatibility parsing, pytest, existing uv-managed .sglang environment.

**Spec:** docs/superpowers/specs/2026-09-13-agent-operation-security-hardening-design.md

## Global Constraints

- Do not modify API authentication, listener binding, CORS, or the remote safety topic.
- Do not treat regular-expression matching as complete shell security.
- Do not use eval for browser or agent command arguments.
- Use the project environment command ./.sglang/bin/python -m pytest for tests.
- Preserve ordinary successful tool output and existing SSE event shape where possible.
- Do not use git commands without separate user permission.

## File Map

- Create: backend/security/command_policy.py — structured command decisions and shell compatibility rules.
- Create: backend/security/process_supervisor.py — process groups, deadlines, output limits, abort, cleanup.
- Modify: backend/tools/sys_ops.py — delegate safety and execution to the new components.
- Modify: backend/agent/agent_runner.py — pass resolved runtime policy and stop using shell-text browser rewrites.
- Modify: backend/config.py — parse and validate command/process policy settings.
- Modify: config/config-agent-operation.yaml — add explicit security settings only after config defaults and tests exist.
- Test: tests/test_command_policy.py
- Test: tests/test_process_supervisor.py
- Test: tests/test_sys_ops_security.py
- Test: tests/test_command_config.py

## Interfaces

The plan produces these interfaces:

    @dataclass(frozen=True)
    class CommandDecision:
        allowed: bool
        mode: str
        reason_code: str | None
        display_reason: str | None

    class CommandPolicy:
        def evaluate_argv(self, argv: tuple[str, ...]) -> CommandDecision: ...
        def evaluate_shell(self, command: str) -> CommandDecision: ...
        def merge_blacklist(self, configured): ...

    class ProcessResult:
        stdout: str
        stderr: str
        returncode: int | None
        timed_out: bool
        aborted: bool
        truncated: bool

    class ProcessSupervisor:
        def run_argv(self, argv, *, cwd, env, timeout, check_abort) -> ProcessResult: ...
        def run_shell_compat(self, command, *, cwd, env, timeout, check_abort) -> ProcessResult: ...
        def shutdown(self) -> None: ...

    class EnvironmentBuilder:
        def build_argv(self, executable, args, *, work_dir) -> tuple: ...
        def build_shell_compat(self, command, *, work_dir) -> tuple: ...

## Task 1: Command policy and blacklist merge

**Files:**
- Create: backend/security/command_policy.py
- Modify: backend/config.py
- Test: tests/test_command_policy.py
- Test: tests/test_command_config.py

**Interfaces:**
- Consumes: resolved command configuration and built-in protection rules.
- Produces: CommandDecision for argv and compatibility shell paths, with built-in rules always retained.

- [ ] Write failing tests for exact built-in dangerous commands and harmless commands, asserting stable reason codes rather than only localized strings.
- [ ] Write failing tests for known bypass forms: reordered flags, wildcard/root variants, command substitution, eval, shell nesting, dangerous redirection, device paths, service/process control, and pipelines that invoke interpreters.
- [ ] Write a failing test proving a custom blacklist adds rules without removing every built-in rule.
- [ ] Write failing tests for malformed regex, missing reason, empty pattern, invalid mode, empty executable allowlist, and out-of-range timeout/output values.
- [ ] Implement built-in rule registration plus validated custom-rule merge; custom rules may add or refine a reason but may not remove built-ins.
- [ ] Implement argv evaluation using executable and argument policy, and a restricted shell evaluator that rejects syntax the policy cannot safely constrain.
- [ ] Return sanitized decisions without echoing the full command or environment.
- [ ] Run ./.sglang/bin/python -m pytest tests/test_command_policy.py tests/test_command_config.py -q.

## Task 2: Browser command argument boundary

**Files:**
- Modify: backend/tools/sys_ops.py
- Modify: backend/agent/agent_runner.py
- Test: tests/test_sys_ops_security.py

**Interfaces:**
- Consumes: CommandPolicy from Task 1 and the existing session-bound browser behavior.
- Produces: browser command construction using validated argv or a dedicated adapter without eval.

- [ ] Write failing tests for safe session IDs, invalid session IDs, whitespace, shell metacharacters, Unicode separators, and excessively long values.
- [ ] Write a failing test that inspects the final process argv and proves the session ID is one argument, not shell syntax.
- [ ] Remove the raw string injection path for -s= and replace it with a structured argument list or a quoting routine whose output is never passed to eval.
- [ ] Ensure browser commands are evaluated after session binding and before process creation.
- [ ] Preserve default session behavior when no session ID is supplied.
- [ ] Run ./.sglang/bin/python -m pytest tests/test_sys_ops_security.py -q.

## Task 3: Process supervisor lifecycle

**Files:**
- Create: backend/security/process_supervisor.py
- Modify: backend/tools/sys_ops.py
- Test: tests/test_process_supervisor.py

**Interfaces:**
- Consumes: CommandDecision, resolved timeout, output, and environment policy.
- Produces: ProcessResult with explicit timeout, abort, cleanup, and truncation states for both argv and explicitly governed shell compatibility calls.

- [ ] Write failing real-process tests that start a child and grandchild, trigger timeout, and assert both are reaped.
- [ ] Write failing tests for targeted abort, normal exit, nonzero exit, and supervisor shutdown while a process is running.
- [ ] Write a failing test that emits more than the configured stdout and stderr limits and asserts bounded memory plus truncated=True.
- [ ] Write a failing test proving timeout uses the configured deadline and cleanup grace, not a hard-coded value.
- [ ] Implement fresh process-group/session creation, nonblocking output collection, byte accounting, deadline-aware abort checks, group termination, grace wait, and forced group kill for both run_argv and run_shell_compat.
- [ ] Ensure pipes are closed in every return path and stale children cannot write indefinitely after the caller receives a result.
- [ ] Keep the public sys_ops return string compatible by translating ProcessResult at the adapter boundary.
- [ ] Run ./.sglang/bin/python -m pytest tests/test_process_supervisor.py -q and inspect process cleanup explicitly.

## Task 4: Environment preamble and runtime configuration

**Files:**
- Modify: backend/tools/sys_ops.py
- Modify: backend/config.py
- Modify: config/config-agent-operation.yaml
- Test: tests/test_command_config.py
- Test: tests/test_sys_ops_security.py

**Interfaces:**
- Consumes: ProcessSupervisor and the resolved command/process policy fields; AgentSession capability binding is supplied by the capability integration plan.
- Produces: a single resolved command runtime policy and an EnvironmentBuilder result used for every exec and browser operation.

- [ ] Write failing tests proving agent.exec_timeout changes the supervisor deadline.
- [ ] Write failing tests proving sensitive environment variables are absent and only an explicit safe environment allowlist is inherited.
- [ ] Write failing tests for configured output limits, process grace, shell mode, executable allowlist, and browser enablement.
- [ ] Write failing tests proving argv execution retains the required agent bin, VIRTUAL_ENV, PATH, and Node/nvm tool resolution currently supplied by the shell preamble.
- [ ] Write failing tests proving browser argv execution works without login-shell initialization or eval.
- [ ] Implement config parsing with explicit defaults and validation; reject invalid values instead of silently reverting to unlimited or unrestricted behavior.
- [ ] Implement EnvironmentBuilder that resolves the configured agent Python/bin, VIRTUAL_ENV, PATH, work_dir, and required Node/nvm executable locations into an explicit environment and argv plan.
- [ ] For argv mode, resolve required executables directly and set environment variables explicitly; do not depend on an interactive or login shell to discover nvm/venv state.
- [ ] For shell compatibility mode, use a fixed preamble with no user-data interpolation in the preamble itself; pass the already policy-evaluated command through the designated compatibility boundary and never through eval.
- [ ] Remove env=os.environ.copy() as the default authority and construct a minimal environment, preserving only toolchain variables that tests prove necessary.
- [ ] Remove cwd=None as an implicit policy decision; use the resolved configured work directory after validating it through the filesystem policy.
- [ ] Wire the resolved config through the agent runtime without rereading YAML inside individual tool calls.
- [ ] Run ./.sglang/bin/python -m pytest tests/test_command_config.py tests/test_sys_ops_security.py -q.

## Task 5: Integrated command regression

**Files:**
- Modify: backend/tools/sys_ops.py only where adapters remain.
- Test: tests/test_sys_ops.py
- Test: tests/test_agent_operation_config.py
- Test: tests/test_command_execution_integration.py

**Interfaces:**
- Consumes: policy, browser adapter, supervisor, and config from Tasks 1–4.
- Produces: end-to-end coverage from tool call through final process cleanup.

- [ ] Write failing integration tests for allowed argv execution, denied shell syntax, browser session binding, timeout, abort, output truncation, and nonzero exit.
- [ ] Add regression tests for existing normal commands required by the project; document any command that must be migrated to argv or an explicit shell capability.
- [ ] Add regression tests for the existing agent bin, VIRTUAL_ENV, PATH, Node/nvm, and playwright-cli launch behavior through the final adapter.
- [ ] Exercise the actual exec_command wrapper rather than only private policy functions.
- [ ] Run focused command tests and compare process counts before and after timeout/abort.
- [ ] Fix adapter-only regressions without weakening the policy; if compatibility requires a new explicit capability, add it to config and tests.
- [ ] Run ./.sglang/bin/python -m pytest tests/test_sys_ops.py tests/test_agent_operation_config.py tests/test_command_execution_integration.py -q.

## Completion Gate

- [ ] Search for all subprocess.Popen, /bin/bash, eval, playwright-cli, and _check_command_safety call sites with rg.
- [ ] Confirm every process launch is supervised or explicitly documented as an internal trusted launch.
- [x] Record `running-linux-gui-commands` as the internal trusted-launch exception: exact launcher paths are policy-gated, `--wait` remains supervised, and only default detached GUI lifetime is exempt from outer process-group cleanup.
- [ ] Confirm no timeout or abort path only terminates the shell parent.
- [ ] Confirm custom configuration cannot disable built-in dangerous-command protection.
- [ ] Confirm the final command representation, not only the original user string, is policy checked.
