#!/usr/bin/env bash
set -euo pipefail

# Read-only readiness probe.  It deliberately does not request desktop
# permissions or launch a helper.
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec "${PYTHON:-python3}" -m backend.computer_use.doctor "$@"
