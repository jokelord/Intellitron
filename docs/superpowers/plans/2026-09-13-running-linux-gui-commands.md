# Running Linux GUI Commands Compatibility Implementation Plan

> For agentic workers: execute this plan in the current repository; keep changes limited to the GUI launcher skill, its command-policy/config integration, security documentation, and focused tests/prompts.

**Goal:** Make `running-linux-gui-commands` safe and usable under this repository's Agent command policy on Ubuntu Wayland + XWayland while remaining compatible with local X11-only hosts.

**Architecture:** Keep session discovery and endpoint resolution in the Bash launcher. Register only its exact entrypoint paths, add a narrow launcher-local deny layer for the otherwise opaque inner argv, recursively validate `env` wrappers in the shared CommandPolicy, constrain custom logs, and document asynchronous GUI lifetime as a trusted-launch exception.

**Tech Stack:** Bash, `loginctl`/`systemctl --user`, Wayland/X11 Unix sockets, Python/pytest integration checks, YAML prompt configuration.

## Global Constraints

- Preserve argv exactly after `--`; never turn command arguments into shell source.
- Allow Wayland, X11, and Wayland + XWayland; reject remote/unverifiable X11 endpoints.
- Keep non-root callers confined to their own UID and make root cross-user launches drop privileges.
- Do not use git commands or change dependency source.
- Verify shell syntax, focused launcher tests, skill discovery, and prompt integration before reporting completion.

## Task 1: Add regression coverage first

- Extend the launcher fixture tests so they run as the current non-root user as well as root, without hard-coded `nobody` ownership.
- Add failing cases for remote `DISPLAY`, standard `unix:N` X11 syntax, inferred Wayland/XWayland mode, explicit ineligible sessions, seat ambiguity, and excessive probe delays.
- Add a focused Python contract test for absolute `{{SKILL_DIR}}` expansion, removal of the legacy display guessing prompt, and root custom-log rejection.

## Task 2: Fix launcher resolution and safety

- Validate explicit sessions as active, local graphical sessions and prevent cross-session fallback to caller environment or user-global manager values.
- Verify X11 sockets are local Unix sockets owned by the target user or root; support `:N`, `unix/:N`, and `unix:N` forms.
- Remove seat-based preference that can silently resolve an ambiguous desktop; infer session type before computing `X11_MODE`.
- Bound `--probe-seconds` and reject custom log paths for root callers to avoid privileged truncation/chown hazards.

## Task 3: Adapt the skill and repository Agent prompts

- Make skill examples resolve the launcher from `{{SKILL_DIR}}` so the skill works when loaded by this repository’s skill loader.
- Replace the old hard-coded `DISPLAY`/`XAUTHORITY` wrapper guidance with instructions to read and use this skill.
- Keep manager-agent tool policy consistent with the existing child-agent shell execution boundary.

## Task 4: Verify

- Run `bash -n` on both launcher scripts.
- Run the focused shell and Python tests plus the repository skill-session test.
- Run a real local `--diagnose` on the current Wayland/XWayland host and confirm the reported backend/mode.

## Task 5: Command-policy compatibility and trusted-launch boundary

- Register both skill entrypoints by exact absolute path without weakening generic executable-path checks.
- Recursively apply semantic and allowlist checks to commands wrapped by `env`.
- Reject dangerous inner commands and shell wrappers before GUI session discovery; keep the launcher deny set explicitly narrower than an OS sandbox.
- Restrict non-root custom logs to the real invocation cwd or `/tmp`, reject symlink components, and keep root custom logs disabled.
- Ensure `agent-gui-diagnose` cannot turn a caller-supplied `--` into a launch path.
- Record that `--wait` remains supervised while the default asynchronous GUI lifetime is the explicit trusted-launch exception.
