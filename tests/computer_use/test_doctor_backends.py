
import pytest
from backend.computer_use import doctor

@pytest.mark.parametrize("session,wayland,display,expected", [
    ("x11", "wayland-0", ":1", "x11"),
    ("wayland", "wayland-0", ":1", "wayland"),
    ("", "wayland-0", ":1", "wayland"),
    ("", "", ":1", "x11"),
    ("", "", "", "unavailable"),
    ("tty", "", ":1", "unavailable"),
])
def test_doctor_selects_session_backend_without_claiming_readiness(monkeypatch, session, wayland, display, expected):
    monkeypatch.setenv("XDG_SESSION_TYPE", session)
    monkeypatch.setenv("WAYLAND_DISPLAY", wayland)
    monkeypatch.setenv("DISPLAY", display)
    monkeypatch.setattr(doctor, "_pkg_config_probe", lambda: {
        "x11": "1", "xext": "1", "xtst": "1", "xrandr": "1",
        "gio-unix-2.0": None, "json-glib-1.0": "1",
        "libpipewire-0.3": None, "libspa-0.2": None, "libei-1.0": None})
    result = doctor.diagnose()
    assert result["backend"] == expected
    assert result["ready"] is False
    assert result["capture_ready"] is False
    assert result["input_ready"] is False
    if expected == "x11":
        assert result["missing_development_packages"] == []
        assert result["evidence"]["portal_interfaces"] == "not_applicable"
        assert result["evidence"]["x11_connection"] == "not_run"
    if expected == "wayland":
        assert "libei-1.0" in result["missing_development_packages"]
