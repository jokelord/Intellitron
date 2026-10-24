from backend.security.environment_builder import DEFAULT_ENVIRONMENT_ALLOWLIST, EnvironmentBuilder
from test_command_execution_integration import _runtime


def test_session_type_is_preserved_without_desktop_environment_overrides(tmp_path):
    parent = {'XDG_SESSION_TYPE': 'x11', 'DISPLAY': ':1', 'WAYLAND_DISPLAY': 'stale-wayland',
              'XAUTHORITY': '/trusted/authority', 'OPENAI_API_KEY': 'secret'}
    runtime = _runtime(tmp_path, environment_allowlist=DEFAULT_ENVIRONMENT_ALLOWLIST)
    environment = EnvironmentBuilder(runtime, parent_environment=parent).build_environment().env
    assert environment['XDG_SESSION_TYPE'] == 'x11'
    assert environment['DISPLAY'] == ':1'
    assert environment['WAYLAND_DISPLAY'] == 'stale-wayland'
    assert environment['XAUTHORITY'] == '/trusted/authority'
    assert 'OPENAI_API_KEY' not in environment
