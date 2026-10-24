---
name: running-linux-gui-commands
description: Use when launching a Linux desktop GUI program or diagnosing its graphical session from SSH, a service shell, or a root shell.
---

# Running Linux GUI Commands

## Overview

Call the supplied launcher through `exec_command`. It finds a real local graphical login for Wayland, X11, or XWayland and, when called as root, runs the GUI as that desktop user. Use it only for the requested GUI program.

## Required Pattern

```bash
# Normal agent launch: detached, one-second liveness probe, output captured.
{{SKILL_DIR}}/scripts/agent-gui-run -- program arg1 arg2

# Need the real exit code.
{{SKILL_DIR}}/scripts/agent-gui-run --wait -- program arg1 arg2

# Diagnose without launching anything.
{{SKILL_DIR}}/scripts/agent-gui-diagnose
```

`agent-gui-diagnose` accepts `--user`, `--session`, and `--debug`; it rejects `--` and does not launch a program.

If root sees an ambiguity, do not guess. Retry with the intended desktop identity:

```bash
{{SKILL_DIR}}/scripts/agent-gui-run --user alice -- program arg
{{SKILL_DIR}}/scripts/agent-gui-run --session 3 -- program arg
```

Pass one GUI executable and its arguments directly after `--`. Do not wrap it with `bash -lc`, `sh -c`, `source`, `eval`, pipelines, redirection, or background shell syntax.

`--session` must identify an active, local graphical logind session. If a selected session is no longer eligible, the launcher fails instead of borrowing environment values from another session.

## Decision Rules

| Situation | Action |
|---|---|
| Same desktop user | Run normally; caller environment is preserved except session-critical variables are resolved. |
| root/service shell | Let the launcher find one active local graphical user; it drops privileges before the GUI command. |
| Multiple active desktops | Use `--user` or `--session`; ambiguity is a hard failure. |
| GUI launch failed | Run `{{SKILL_DIR}}/scripts/agent-gui-diagnose` and inspect `*_SOURCE`, `BACKEND`, and session fields. |
| No usable endpoint | Treat it as no graphical login/session; do not fabricate `/run/user/...`, `:0`, or `wayland-0`. |

On X11, only local `:N`, `unix/:N`, or `unix:N` displays backed by a target-user/root-owned `/tmp/.X11-unix/XN` socket are accepted. Hostname displays such as `server:0`, `localhost:10`, and SSH-forwarded displays are rejected by design.

## Launch constraints

- Never use `xhost +`, loosen Wayland socket permissions, or copy another user's display credentials to root.
- Never run a root GUI process against another user's `XDG_RUNTIME_DIR`; run the GUI as that user.
- Never guess UID 1000, username, `DISPLAY=:0`, `WAYLAND_DISPLAY=wayland-0`, or a fixed Xauthority path.
- Never force `GDK_BACKEND` or `QT_QPA_PLATFORM` globally just to make a launch work.
- Root callers must omit `--log`; the launcher creates a private temporary log and reports its path. A non-root custom log must be an absolute regular-file path below the real current working directory or `/tmp`, with no symlink path component.
- Default asynchronous mode starts a detached program that may survive command timeout or abort. Use `--wait` when the command must remain attached until it exits and return its real status.

## Result Contract

`AGENT_GUI_STATUS=running` means only that the detached command survived the probe, not that the requested GUI task finished. `finished EXIT_CODE=0` means the program exited successfully. `failed` returns its nonzero status and prints the log tail. The status line includes `USER`, `UID`, `SESSION`, `BACKEND`, and `LOG`; read `LOG` as a shell-escaped path. `--probe-seconds N` changes the asynchronous probe delay (default 1 second, maximum 60).

Resolver failures use: `64` bad arguments, `69` no graphical endpoint/session, `77` insufficient privilege or built-in command safety denial, `78` ambiguous/inconsistent session configuration.

