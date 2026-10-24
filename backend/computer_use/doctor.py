"""Readiness is evidence, never inferred from a display environment variable."""
import argparse
import json
import os
import shutil
import subprocess


_PKG_CONFIG_NAMES = (
    "gio-unix-2.0", "json-glib-1.0", "libpipewire-0.3", "libspa-0.2",
    "libei-1.0", "x11", "xext", "xtst", "xrandr",
)


def _pkg_config_probe() -> dict[str, str | None]:
    pkg_config = shutil.which("pkg-config")
    if pkg_config is None:
        return {name: None for name in _PKG_CONFIG_NAMES}
    result: dict[str, str | None] = {}
    for name in _PKG_CONFIG_NAMES:
        try:
            completed = subprocess.run(
                [pkg_config, "--modversion", name],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            result[name] = None
        else:
            result[name] = completed.stdout.strip() if completed.returncode == 0 else None
    return result


def _session_backend():
    session_type = os.environ.get("XDG_SESSION_TYPE", "")
    if session_type in {"x11", "wayland"}:
        return session_type
    if session_type:
        return "unavailable"
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    return "x11" if os.environ.get("DISPLAY") else "unavailable"


def diagnose(helper_binary=None):
    executable = bool(helper_binary and os.path.isabs(helper_binary)
                      and os.path.isfile(helper_binary) and os.access(helper_binary, os.X_OK))
    backend = _session_backend()
    required = (("json-glib-1.0", "x11", "xext", "xtst", "xrandr")
                if backend == "x11" else
                ("gio-unix-2.0", "json-glib-1.0", "libpipewire-0.3", "libspa-0.2", "libei-1.0")
                if backend == "wayland" else _PKG_CONFIG_NAMES)
    packages = _pkg_config_probe()
    missing = [name for name in required if packages.get(name) is None]
    reasons = (['HELPER_NOT_BUILT'] if not executable else [])
    if backend == "unavailable":
        reasons.append("DESKTOP_BACKEND_UNAVAILABLE")
    if missing:
        reasons.append('MISSING_DEVELOPMENT_PACKAGES')
    reasons += ['LIVE_CAPABILITIES_UNVERIFIED', 'CALIBRATION_UNVERIFIED']
    return {'backend': backend, 'ready': False, 'helper_executable': executable,
        'capture_ready': False, 'input_ready': False,
        'reasons': reasons,
        'evidence': {'packages': 'pass' if not missing else 'incomplete',
                     'portal_interfaces': 'not_applicable' if backend == 'x11' else 'not_run',
                     'x11_connection': 'not_run' if backend == 'x11' else 'not_applicable',
                     'xrandr_mapping': 'not_run' if backend == 'x11' else 'not_applicable',
                     'xtest_input': 'not_run' if backend == 'x11' else 'not_applicable',
                     'live_frame': 'not_run',
                     'input_outcome': 'not_run', 'calibration': 'not_run'},
        'development_packages': packages,
        'missing_development_packages': missing,
        'required_development_packages': list(required)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--helper-binary')
    args = parser.parse_args()
    result = diagnose(args.helper_binary)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
