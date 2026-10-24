"""Never uses the login bus; distinguish sandbox socket denial from test failure."""
import subprocess
import sys
result = subprocess.run(["dbus-run-session", "--", sys.argv[1], "private-bus"],
                        capture_output=True, text=True, timeout=10)
print(result.stdout, end="")
print(result.stderr, end="", file=sys.stderr)
if result.returncode and ("Operation not permitted" in result.stderr or "Permission denied" in result.stderr):
    print("SKIP: sandbox cannot create private D-Bus; rerun fixture gate on host")
    sys.exit(77)
sys.exit(result.returncode)
