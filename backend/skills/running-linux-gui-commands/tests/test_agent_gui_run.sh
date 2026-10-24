#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUNNER="$ROOT/scripts/agent-gui-run"
DIAG="$ROOT/scripts/agent-gui-diagnose"

if (( EUID == 0 )); then
  TEST_UID=65534
  TEST_USER=nobody
  OTHER_UID=1
  OTHER_USER=daemon
else
  TEST_UID=$(id -u)
  TEST_USER=$(id -un)
  OTHER_UID=$TEST_UID
  OTHER_USER=$TEST_USER
fi

REAL_WAYLAND_SOCKET=''
REAL_BUS_SOCKET=''
REAL_X11_SOCKET=''
for candidate in "/run/user/$TEST_UID"/wayland-*; do
  if [[ -S "$candidate" ]]; then REAL_WAYLAND_SOCKET=$candidate; break; fi
done
if [[ -S "/run/user/$TEST_UID/bus" ]]; then REAL_BUS_SOCKET="/run/user/$TEST_UID/bus"; fi
for candidate in /tmp/.X11-unix/X*; do
  if [[ -S "$candidate" ]]; then REAL_X11_SOCKET=$candidate; break; fi
done

pass=0
fail=0
TMPROOT=$(mktemp -d /tmp/agent-gui-tests.XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT
chmod 755 "$TMPROOT"

ok() { printf 'ok - %s\n' "$1"; pass=$((pass+1)); }
not_ok() { printf 'not ok - %s\n' "$1" >&2; fail=$((fail+1)); }
assert_contains() { local hay=$1 needle=$2; [[ "$hay" == *"$needle"* ]]; }

make_socket() {
  local path=$1 owner=${2:-}
  if python3 - "$path" <<'PY' 2>/dev/null
import os, socket, sys
p=sys.argv[1]
os.makedirs(os.path.dirname(p), exist_ok=True)
try: os.unlink(p)
except FileNotFoundError: pass
s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(p)
s.close()
PY
  then
    if [[ -n "$owner" && $EUID -eq 0 ]]; then chown "$owner" "$path"; fi
    return 0
  fi

  local fallback=''
  case "$(basename "$path")" in
    wayland-*) fallback=$REAL_WAYLAND_SOCKET ;;
    bus) fallback=$REAL_BUS_SOCKET ;;
    X*) fallback=$REAL_X11_SOCKET ;;
  esac
  [[ -n "$fallback" ]] || { printf 'no usable socket fixture for %s\n' "$path" >&2; return 1; }
  ln -s "$fallback" "$path"
}

fixture() {
  local name=$1
  FX="$TMPROOT/$name"
  mkdir -p "$FX/bin" "$FX/loginctl" "$FX/runtime" "$FX/x11"
  chmod 755 "$FX" "$FX/bin" "$FX/loginctl" "$FX/x11"
  chmod 700 "$FX/runtime"
  : > "$FX/manager.env"
  cat > "$FX/bin/loginctl" <<SH
#!/usr/bin/env bash
set -e
D='$FX/loginctl'
case "\${1:-}" in
  list-sessions)
    cat "\$D/list" 2>/dev/null || true
    ;;
  show-session)
    cat "\$D/session-\$2"
    ;;
  show-user)
    # The runner asks only for RuntimePath in --value form.
    cat "\$D/runtime-\$2" 2>/dev/null || exit 1
    ;;
  *) exit 1 ;;
esac
SH
  cat > "$FX/bin/systemctl" <<SH
#!/usr/bin/env bash
if [[ "\$*" == *"--user show-environment"* ]]; then
  cat '$FX/manager.env'
  exit 0
fi
exit 1
SH
  chmod +x "$FX/bin/loginctl" "$FX/bin/systemctl"
  export AGENT_GUI_LOGINCTL="$FX/bin/loginctl"
  export AGENT_GUI_SYSTEMCTL="$FX/bin/systemctl"
  export AGENT_GUI_RUNTIME_ROOT="$FX/run-user"
  export AGENT_GUI_X11_SOCKET_DIR="$FX/x11"
  mkdir -p "$FX/run-user"
  chmod 755 "$FX/run-user"
}

set_session() {
  local sid=$1 uid=$2 user=$3 type=$4 active=${5:-yes} display=${6:-} class=${7:-user} remote=${8:-no} seat=${9:-seat0}
  printf '%s %s %s %s tty2 active no -\n' "$sid" "$uid" "$user" "$seat" >> "$FX/loginctl/list"
  cat > "$FX/loginctl/session-$sid" <<EOF2
User=$uid
Name=$user
Type=$type
Class=$class
Active=$active
Remote=$remote
State=active
Seat=$seat
Display=$display
Leader=12345
EOF2
}

set_runtime() {
  local uid=$1 path=$2 owner=$3
  mkdir -p "$path"
  if (( EUID == 0 )); then chown "$owner" "$path"; fi
  chmod 700 "$path"
  printf '%s\n' "$path" > "$FX/loginctl/runtime-$uid"
}

run_capture() {
  local __outvar=$1 __rcvar=$2; shift 2
  local captured_out captured_rc
  set +e
  captured_out=$(env \
    -u XDG_RUNTIME_DIR -u DBUS_SESSION_BUS_ADDRESS -u WAYLAND_DISPLAY -u DISPLAY \
    -u XAUTHORITY -u XDG_SESSION_TYPE -u XDG_SESSION_ID -u XDG_SESSION_CLASS \
    -u XDG_SEAT "$RUNNER" "$@" 2>&1)
  captured_rc=$?
  set -e
  printf -v "$__outvar" '%s' "$captured_out"
  printf -v "$__rcvar" '%s' "$captured_rc"
}

run_capture_in_dir() {
  local __outvar=$1 __rcvar=$2 directory=$3; shift 3
  local captured_out captured_rc
  set +e
  captured_out=$(cd "$directory" && env \
    -u XDG_RUNTIME_DIR -u DBUS_SESSION_BUS_ADDRESS -u WAYLAND_DISPLAY -u DISPLAY \
    -u XAUTHORITY -u XDG_SESSION_TYPE -u XDG_SESSION_ID -u XDG_SESSION_CLASS \
    -u XDG_SEAT "$RUNNER" "$@" 2>&1)
  captured_rc=$?
  set -e
  printf -v "$__outvar" '%s' "$captured_out"
  printf -v "$__rcvar" '%s' "$captured_rc"
}

log_from_status() {
  sed -n 's/.* LOG=\([^ ]*\).*/\1/p' <<<"$1" | tail -n1
}

# 1. Root -> graphical user, Wayland + XWayland, privilege drop and session env.
test_wayland_cross_user() {
  fixture wayland-cross-user
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime" xa="$FX/runtime/xauth"
  set_session s1 "$uid" "$user" wayland yes ''
  set_runtime "$uid" "$rt" "$user"
  make_socket "$rt/wayland-9" "$user"
  make_socket "$rt/bus" "$user"
  make_socket "$FX/x11/X77" "$user"
  printf 'cookie\n' > "$xa"; chown "$user" "$xa"; chmod 600 "$xa"
  cat > "$FX/manager.env" <<EOF2
PATH=/usr/bin:/bin
WAYLAND_DISPLAY=wayland-9
DISPLAY=:77
XAUTHORITY=$xa
XDG_SESSION_TYPE=wayland
XDG_CURRENT_DESKTOP=GNOME
EOF2
  cat > "$FX/bin/capture-session-env" <<'SH'
#!/bin/bash
printf 'uid=%s\nuser=%s\nwayland=%s\ndisplay=%s\nruntime=%s\ndbus=%s\nhome=%s\n' \
  "$(id -u)" "$USER" "$WAYLAND_DISPLAY" "$DISPLAY" "$XDG_RUNTIME_DIR" \
  "$DBUS_SESSION_BUS_ADDRESS" "$HOME"
SH
  chmod +x "$FX/bin/capture-session-env"
  local out rc log
  run_capture out rc --wait -- "$FX/bin/capture-session-env"
  [[ $rc -eq 0 ]] || return 1
  assert_contains "$out" 'AGENT_GUI_STATUS=finished' || return 1
  assert_contains "$out" 'BACKEND=wayland+x11' || return 1
  log=$(log_from_status "$out"); [[ -r "$log" ]] || return 1
  grep -qx "uid=$TEST_UID" "$log" || return 1
  grep -qx "user=$TEST_USER" "$log" || return 1
  grep -qx 'wayland=wayland-9' "$log" || return 1
  grep -qx 'display=:77' "$log" || return 1
  grep -qx "runtime=$rt" "$log" || return 1
  grep -qx "dbus=unix:path=$rt/bus" "$log" || return 1
  grep -qx "home=$(getent passwd "$TEST_USER" | cut -d: -f6)" "$log" || return 1
}

# 2. Pure X11 session uses login/user-manager display and Xauthority.
test_x11() {
  fixture x11
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime" xa="$FX/runtime/Xauthority"
  set_session x1 "$uid" "$user" x11 yes 'unix:42'
  set_runtime "$uid" "$rt" "$user"
  make_socket "$FX/x11/X42" "$user"
  make_socket "$rt/bus" "$user"
  printf cookie > "$xa"; chown "$user" "$xa"; chmod 600 "$xa"
  cat > "$FX/manager.env" <<EOF2
PATH=/usr/bin:/bin
DISPLAY=unix:42
XAUTHORITY=$xa
XDG_SESSION_TYPE=x11
EOF2
  cat > "$FX/bin/capture-x11-env" <<'SH'
#!/bin/bash
printf 'display=%s\nxauth=%s\nwayland=%s\n' "$DISPLAY" "$XAUTHORITY" "${WAYLAND_DISPLAY-}"
SH
  chmod +x "$FX/bin/capture-x11-env"
  local out rc log
  run_capture out rc --wait -- "$FX/bin/capture-x11-env"
  [[ $rc -eq 0 ]] || return 1
  assert_contains "$out" 'BACKEND=x11' || return 1
  log=$(log_from_status "$out")
  grep -qx 'display=unix:42' "$log" || return 1
  grep -qx "xauth=$xa" "$log" || return 1
  grep -qx 'wayland=' "$log" || return 1
}

# 3. Stale manager WAYLAND_DISPLAY must not win over a unique valid socket.
test_stale_wayland_fallback() {
  fixture stale-wayland
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime"
  set_session s2 "$uid" "$user" wayland yes ''
  set_runtime "$uid" "$rt" "$user"
  make_socket "$rt/wayland-2" "$user"
  cat > "$FX/manager.env" <<EOF2
PATH=/usr/bin:/bin
WAYLAND_DISPLAY=wayland-stale
XDG_SESSION_TYPE=wayland
EOF2
  local out rc
  run_capture out rc --diagnose
  [[ $rc -eq 0 ]] || return 1
  assert_contains "$out" 'WAYLAND_DISPLAY=wayland-2' || return 1
  assert_contains "$out" 'WAYLAND_SOURCE=socket-scan' || return 1
}

# 4. Ambiguous active graphical users must fail closed.
test_ambiguous_users() {
  fixture ambiguous
  set_session a1 "$TEST_UID" "$TEST_USER" wayland yes ''
  if (( EUID == 0 )); then
    set_session a2 "$OTHER_UID" "$OTHER_USER" wayland yes ''
  else
    set_session a2 "$TEST_UID" "$TEST_USER" wayland yes ''
  fi
  local out rc
  run_capture out rc --diagnose
  [[ $rc -eq 78 ]] || return 1
  assert_contains "$out" 'ambiguous' || return 1
  if (( EUID == 0 )); then
    assert_contains "$out" '--user' || return 1
  else
    assert_contains "$out" '--session' || return 1
  fi
}

# 5. No display endpoint is a no-graphical-session failure.
test_no_endpoint() {
  fixture no-endpoint
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime"
  set_session n1 "$uid" "$user" wayland yes ''
  set_runtime "$uid" "$rt" "$user"
  printf 'PATH=/usr/bin:/bin\nXDG_SESSION_TYPE=wayland\n' > "$FX/manager.env"
  local out rc
  run_capture out rc --diagnose
  [[ $rc -eq 69 ]] || return 1
  assert_contains "$out" 'no usable Wayland or X11 endpoint' || return 1
}

# 6. argv must be preserved exactly; no shell re-evaluation.
test_argv_preserved() {
  fixture argv
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime" marker="$FX/SHOULD_NOT_EXIST"
  set_session s3 "$uid" "$user" wayland yes ''
  set_runtime "$uid" "$rt" "$user"
  make_socket "$rt/wayland-0" "$user"
  printf 'PATH=/usr/bin:/bin\nWAYLAND_DISPLAY=wayland-0\nXDG_SESSION_TYPE=wayland\n' > "$FX/manager.env"
  local out rc log payload='$(touch SHOULD_NOT_EXIST); a b * ?'
  run_capture out rc --wait -- /usr/bin/printf '%s\n' "$payload"
  [[ $rc -eq 0 ]] || return 1
  log=$(log_from_status "$out")
  grep -Fqx "$payload" "$log" || return 1
  [[ ! -e "$marker" ]] || return 1
}

# 7. Nonzero command exit propagates in --wait mode and is reported.
test_wait_failure_status() {
  fixture wait-failure
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime"
  set_session s4 "$uid" "$user" wayland yes ''
  set_runtime "$uid" "$rt" "$user"
  make_socket "$rt/wayland-0" "$user"
  printf 'PATH=/usr/bin:/bin\nWAYLAND_DISPLAY=wayland-0\n' > "$FX/manager.env"
  cat > "$FX/bin/fail-with-output" <<'SH'
#!/bin/bash
echo boom
exit 23
SH
  chmod +x "$FX/bin/fail-with-output"
  local out rc
  run_capture out rc --wait -- "$FX/bin/fail-with-output"
  [[ $rc -eq 23 ]] || return 1
  assert_contains "$out" 'AGENT_GUI_STATUS=failed' || return 1
  assert_contains "$out" 'EXIT_CODE=23' || return 1
  assert_contains "$out" 'boom' || return 1
}

# 8. Default detached mode reports a still-running command as success.
test_async_running_status() {
  fixture async-running
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime"
  set_session s5 "$uid" "$user" wayland yes ''
  set_runtime "$uid" "$rt" "$user"
  make_socket "$rt/wayland-0" "$user"
  printf 'PATH=/usr/bin:/bin\nWAYLAND_DISPLAY=wayland-0\n' > "$FX/manager.env"
  local out rc
  run_capture out rc --probe-seconds 0.1 -- /bin/sleep 1
  [[ $rc -eq 0 ]] || return 1
  assert_contains "$out" 'AGENT_GUI_STATUS=running' || return 1
}

# 9. Explicit --session and conflicting --user must be rejected.
test_target_conflict() {
  fixture target-conflict
  set_session s6 "$TEST_UID" "$TEST_USER" wayland yes ''
  local out rc
  run_capture out rc --session s6 --user root --diagnose
  [[ $rc -eq 64 ]] || return 1
  assert_contains "$out" 'conflicts' || return 1
}

# 10. Root fallback without logind may use one unique runtime Wayland owner.
test_runtime_root_fallback() {
  (( EUID == 0 )) || return 0
  fixture runtime-fallback
  # Make loginctl unusable.
  cat > "$FX/bin/loginctl" <<'SH'
#!/usr/bin/env bash
exit 1
SH
  chmod +x "$FX/bin/loginctl"
  local uid=65534 user=nobody rt
  rt="$FX/run-user/$uid"
  mkdir -p "$rt"; chown "$user" "$rt"; chmod 700 "$rt"
  make_socket "$rt/wayland-4" "$user"
  # systemctl environment is optional in this path.
  printf 'PATH=/usr/bin:/bin\n' > "$FX/manager.env"
  local out rc
  run_capture out rc --diagnose
  [[ $rc -eq 0 ]] || return 1
  assert_contains "$out" 'TARGET_USER=nobody' || return 1
  assert_contains "$out" 'WAYLAND_DISPLAY=wayland-4' || return 1
}

# 11. Diagnostic wrapper delegates correctly.
test_diag_wrapper() {
  fixture diag-wrapper
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime"
  set_session d1 "$uid" "$user" wayland yes ''
  set_runtime "$uid" "$rt" "$user"
  make_socket "$rt/wayland-8" "$user"
  printf 'PATH=/usr/bin:/bin\nWAYLAND_DISPLAY=wayland-8\n' > "$FX/manager.env"
  [[ -x "$DIAG" ]] || return 1
  local out rc
  set +e; out=$("$DIAG" 2>&1); rc=$?; set -e
  [[ $rc -eq 0 ]] || return 1
  assert_contains "$out" 'MODE=diagnose' || return 1
}

# 12. The diagnose wrapper must stay read-only even if a caller supplies `--`.
test_diag_wrapper_cannot_launch_command() {
  fixture diag-wrapper-command
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime" marker="$FX/executed"
  set_session d2 "$uid" "$user" wayland yes ''
  set_runtime "$uid" "$rt" "$user"
  make_socket "$rt/wayland-8" "$user"
  printf 'PATH=/usr/bin:/bin\nWAYLAND_DISPLAY=wayland-8\n' > "$FX/manager.env"
  cat > "$FX/bin/mark-executed" <<SH
#!/bin/bash
: > '$marker'
SH
  chmod +x "$FX/bin/mark-executed"

  local out rc
  set +e; out=$("$DIAG" -- "$FX/bin/mark-executed" 2>&1); rc=$?; set -e

  [[ $rc -eq 64 ]] || return 1
  assert_contains "$out" 'does not accept --' || return 1
  [[ ! -e "$marker" ]] || return 1
}

# 13. Shell-escaped systemd environment lines must never be reinterpreted or passed literally.
test_escaped_manager_env_skipped() {
  fixture escaped-manager-env
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime"
  set_session e1 "$uid" "$user" wayland yes ''
  set_runtime "$uid" "$rt" "$user"
  make_socket "$rt/wayland-6" "$user"
  cat > "$FX/manager.env" <<'EOF2'
PATH=/usr/bin:/bin
WAYLAND_DISPLAY=wayland-6
WEIRD=$'a b'
EOF2
  cat > "$FX/bin/capture-weird-env" <<'SH'
#!/bin/bash
printf 'weird=%s\n' "${WEIRD-unset}"
SH
  chmod +x "$FX/bin/capture-weird-env"
  local out rc log
  run_capture out rc --wait -- "$FX/bin/capture-weird-env"
  [[ $rc -eq 0 ]] || return 1
  log=$(log_from_status "$out")
  grep -qx 'weird=unset' "$log" || return 1
}

# 13. A remote DISPLAY must not be treated as a local X11 endpoint.
test_remote_display_rejected() {
  fixture remote-display
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime"
  set_session r1 "$uid" "$user" wayland yes ''
  set_runtime "$uid" "$rt" "$user"
  make_socket "$rt/wayland-1" "$user"
  cat > "$FX/manager.env" <<EOF2
WAYLAND_DISPLAY=wayland-1
DISPLAY=attacker.example:99
XDG_SESSION_TYPE=wayland
EOF2
  local out rc
  run_capture out rc --diagnose
  [[ $rc -eq 0 ]] || return 1
  assert_contains "$out" 'BACKEND=wayland' || return 1
  assert_contains "$out" 'DISPLAY=' || return 1
  [[ "$out" != *'DISPLAY=attacker.example:99'* ]] || return 1
}

# 14. Wayland + XWayland mode is inferred from the resolved manager environment.
test_inferred_wayland_xwayland_mode() {
  fixture inferred-mode
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime"
  set_runtime "$uid" "$rt" "$user"
  make_socket "$rt/wayland-5" "$user"
  make_socket "$FX/x11/X42" "$user"
  cat > "$FX/manager.env" <<EOF2
WAYLAND_DISPLAY=wayland-5
DISPLAY=unix:42
XDG_SESSION_TYPE=wayland
EOF2
  local out rc
  run_capture out rc --user "$user" --diagnose
  [[ $rc -eq 0 ]] || return 1
  assert_contains "$out" 'SESSION_TYPE=wayland' || return 1
  assert_contains "$out" 'BACKEND=wayland+x11' || return 1
  assert_contains "$out" 'X11_MODE=xwayland' || return 1
}

# 15. Explicitly selected non-graphical sessions must fail before endpoint lookup.
test_explicit_ineligible_session() {
  fixture ineligible-session
  set_session bad1 "$TEST_UID" "$TEST_USER" tty yes ''
  local out rc
  run_capture out rc --session bad1 --diagnose
  [[ $rc -eq 78 ]] || return 1
  assert_contains "$out" 'not an eligible local graphical session' || return 1
}

# 16. An explicit session must not borrow a valid but session-agnostic manager endpoint.
test_explicit_session_is_session_bound() {
  fixture explicit-session-bound
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime"
  set_session es1 "$uid" "$user" wayland yes ''
  set_runtime "$uid" "$rt" "$user"
  make_socket "$rt/wayland-0" "$user"
  make_socket "$rt/wayland-1" "$user"
  printf 'WAYLAND_DISPLAY=wayland-1\nXDG_SESSION_TYPE=wayland\n' > "$FX/manager.env"
  local out rc
  run_capture out rc --session es1 --diagnose
  [[ $rc -eq 69 ]] || return 1
  assert_contains "$out" 'no usable Wayland or X11 endpoint' || return 1
}

# 17. A seat preference must not hide two otherwise-equivalent desktops.
test_ambiguous_same_user_seats() {
  fixture ambiguous-same-user
  set_session z1 "$TEST_UID" "$TEST_USER" wayland yes '' user no seat0
  set_session z2 "$TEST_UID" "$TEST_USER" wayland yes '' user no seat1
  local rt="$FX/runtime"
  set_runtime "$TEST_UID" "$rt" "$TEST_USER"
  make_socket "$rt/wayland-0" "$TEST_USER"
  printf 'WAYLAND_DISPLAY=wayland-0\nXDG_SESSION_TYPE=wayland\n' > "$FX/manager.env"
  local out rc
  run_capture out rc --user "$TEST_USER" --diagnose
  [[ $rc -eq 78 ]] || return 1
  assert_contains "$out" 'ambiguous graphical sessions' || return 1
}

# 18. Probe delays are bounded so an agent cannot accidentally wait forever.
test_probe_limit() {
  fixture probe-limit
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime"
  set_session p1 "$uid" "$user" wayland yes ''
  set_runtime "$uid" "$rt" "$user"
  make_socket "$rt/wayland-0" "$user"
  printf 'WAYLAND_DISPLAY=wayland-0\nXDG_SESSION_TYPE=wayland\n' > "$FX/manager.env"
  cat > "$FX/bin/sleep" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  chmod +x "$FX/bin/sleep"
  local out rc old_path=$PATH
  PATH="$FX/bin:$PATH"
  run_capture out rc --probe-seconds 60.1 -- /bin/true
  PATH=$old_path
  [[ $rc -eq 64 ]] || return 1
  assert_contains "$out" 'maximum probe duration' || return 1
}

test_log_path_is_shell_escaped() {
  (( EUID == 0 )) && return 0
  fixture log-path
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime" log_path="$FX/log with space.log"
  set_session l1 "$uid" "$user" wayland yes ''
  set_runtime "$uid" "$rt" "$user"
  make_socket "$rt/wayland-0" "$user"
  printf 'WAYLAND_DISPLAY=wayland-0\nXDG_SESSION_TYPE=wayland\n' > "$FX/manager.env"
  local out rc escaped
  run_capture out rc --wait --log "$log_path" -- /bin/true
  [[ $rc -eq 0 ]] || return 1
  escaped=$(printf '%q' "$log_path")
  assert_contains "$out" "LOG=$escaped" || return 1
  [[ -f "$log_path" ]] || return 1
}

test_x11_socket_owner() {
  (( EUID == 0 )) || return 0
  fixture x11-socket-owner
  local uid=65534 user=nobody rt="$FX/runtime"
  set_session xo1 "$uid" "$user" x11 yes ':42'
  set_runtime "$uid" "$rt" "$user"
  make_socket "$FX/x11/X42" daemon
  printf 'DISPLAY=:42\nXDG_SESSION_TYPE=x11\n' > "$FX/manager.env"
  local out rc
  run_capture out rc --diagnose
  [[ $rc -eq 69 ]] || return 1
  assert_contains "$out" 'no usable Wayland or X11 endpoint' || return 1
}

# 21. Dangerous commands are rejected before graphical-session resolution,
# including commands hidden behind env's argv-preserving wrapper.
test_dangerous_inner_commands_rejected() {
  fixture dangerous-inner-command
  local out rc

  run_capture out rc --wait -- /usr/bin/sudo id
  [[ $rc -eq 77 ]] || return 1
  assert_contains "$out" 'command rejected by built-in safety policy' || return 1

  run_capture out rc --wait -- /sbin/mkfs.ext4 /dev/sda1
  [[ $rc -eq 77 ]] || return 1

  run_capture out rc --wait -- /bin/chmod 777 /tmp/../
  [[ $rc -eq 77 ]] || return 1

  run_capture out rc --wait -- /bin/bash -lc 'printf unsafe-shell-wrapper'
  [[ $rc -eq 77 ]] || return 1

  run_capture out rc --wait -- /usr/bin/env env rm --recursive /tmp/../
  [[ $rc -eq 77 ]] || return 1

  run_capture out rc --wait -- /usr/bin/env X=1 rm --recursive /tmp/../
  [[ $rc -eq 77 ]] || return 1

  run_capture out rc --wait -- /bin/dd if=/tmp/image of=/dev/nvme0n1
  [[ $rc -eq 77 ]] || return 1
}

# 22. A custom log cannot escape the real invocation cwd or /tmp.
test_log_path_outside_allowed_roots_rejected() {
  (( EUID == 0 )) && return 0
  fixture log-outside-roots
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime" launch_dir="$FX/launch"
  local log_path="$ROOT/tests/.agent-gui-forbidden-$$.log"
  set_session lo1 "$uid" "$user" wayland yes ''
  set_runtime "$uid" "$rt" "$user"
  make_socket "$rt/wayland-0" "$user"
  printf 'WAYLAND_DISPLAY=wayland-0\nXDG_SESSION_TYPE=wayland\n' > "$FX/manager.env"
  mkdir -p "$launch_dir"
  printf 'sentinel\n' > "$log_path"

  local out rc content
  run_capture_in_dir out rc "$launch_dir" --wait --log "$log_path" -- /bin/true
  content=$(<"$log_path")
  rm -f -- "$log_path"

  [[ $rc -eq 64 ]] || return 1
  assert_contains "$out" 'outside allowed log roots' || return 1
  [[ "$content" == sentinel ]] || return 1
}

# 23. Even an otherwise allowed /tmp path cannot traverse a symlink parent.
test_log_path_symlink_parent_rejected() {
  (( EUID == 0 )) && return 0
  fixture log-symlink-parent
  local uid=$TEST_UID user=$TEST_USER rt="$FX/runtime"
  local target_dir="$FX/log-target" link_dir="$FX/log-link" log_path
  set_session ls1 "$uid" "$user" wayland yes ''
  set_runtime "$uid" "$rt" "$user"
  make_socket "$rt/wayland-0" "$user"
  printf 'WAYLAND_DISPLAY=wayland-0\nXDG_SESSION_TYPE=wayland\n' > "$FX/manager.env"
  mkdir -p "$target_dir"
  ln -s "$target_dir" "$link_dir"
  log_path="$link_dir/output.log"

  local out rc
  run_capture out rc --wait --log "$log_path" -- /bin/true

  [[ $rc -eq 64 ]] || return 1
  assert_contains "$out" 'symlink log path component' || return 1
  [[ ! -e "$target_dir/output.log" ]] || return 1
}

for t in \
  test_wayland_cross_user \
  test_x11 \
  test_stale_wayland_fallback \
  test_ambiguous_users \
  test_no_endpoint \
  test_argv_preserved \
  test_wait_failure_status \
  test_async_running_status \
  test_target_conflict \
  test_runtime_root_fallback \
  test_diag_wrapper \
  test_diag_wrapper_cannot_launch_command \
  test_escaped_manager_env_skipped \
  test_remote_display_rejected \
  test_inferred_wayland_xwayland_mode \
  test_explicit_ineligible_session \
  test_explicit_session_is_session_bound \
  test_ambiguous_same_user_seats \
  test_probe_limit \
  test_log_path_is_shell_escaped \
  test_x11_socket_owner \
  test_dangerous_inner_commands_rejected \
  test_log_path_outside_allowed_roots_rejected \
  test_log_path_symlink_parent_rejected
 do
  if "$t"; then ok "$t"; else not_ok "$t"; fi
 done

printf '# pass=%d fail=%d\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
