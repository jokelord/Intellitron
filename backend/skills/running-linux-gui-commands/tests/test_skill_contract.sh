#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SKILL="$ROOT/SKILL.md"
[[ -f "$SKILL" ]] || { echo "FAIL: missing SKILL.md"; exit 1; }
head -n1 "$SKILL" | grep -qx -- '---'
grep -q '^name: running-linux-gui-commands$' "$SKILL"
grep -q '^description: Use when ' "$SKILL"
grep -q 'scripts/agent-gui-run' "$SKILL"
grep -qi 'Wayland' "$SKILL"
grep -qi 'XWayland' "$SKILL"
grep -qi 'X11' "$SKILL"
grep -qi 'root' "$SKILL"
grep -q 'xhost +' "$SKILL"
grep -qi 'ambigu' "$SKILL"
grep -q 'current working directory or `/tmp`' "$SKILL"
if grep -q 'For intentional shell syntax' "$SKILL"; then
  echo 'FAIL: skill still recommends an arbitrary shell wrapper' >&2
  exit 1
fi
echo "PASS: skill contract"
