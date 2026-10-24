from pathlib import Path
import importlib.util
import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from backend.config import get_config
from backend.security.capabilities import build_session_capability
from backend.tools.sys_ops import exec_command


REPO_ROOT = Path(__file__).parents[1]
LAUNCHER = REPO_ROOT / "backend" / "skills" / "playwright-browser" / "scripts" / "playwright-browser"


def _load_browser_skill_module():
    path = LAUNCHER.parents[1] / "playwright_browser.py"
    spec = importlib.util.spec_from_file_location("playwright_browser_skill_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _playwright_node_executable() -> str:
    from playwright._impl._driver import compute_driver_executable

    node_path, _ = compute_driver_executable()
    node = Path(node_path)
    if not node.is_file():
        pytest.skip(f"Playwright bundled Node runtime is unavailable: {node}")
    return str(node)


def _run_snapshot_script(skill, scenario: str):
    harness = r"""
const collect = eval(`(${process.argv[1]})`);
class Element {
    constructor(name, options = {}) {
        this.name = name;
        this.tagName = (options.tag || 'button').toUpperCase();
        this.nodeType = 1;
        this.parentElement = options.parent || null;
        this.rootHost = options.rootHost || null;
        this.attrs = {...(options.attrs || {})};
        if (options.typeAttribute !== undefined) this.attrs.type = options.typeAttribute;
        this.innerText = options.text || '';
        this.type = options.effectiveType || this.attrs.type || (this.tagName === 'INPUT' ? 'text' : '');
        this.width = options.width === undefined ? 100 : options.width;
        this.height = options.height === undefined ? 20 : options.height;
        this.display = options.display || 'block';
        this.visibility = options.visibility || 'visible';
        this.disabled = Boolean(options.disabled);
        this.candidate = options.candidate !== false;
        this.childNodes = options.children || [];
    }
    getAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
    }
    hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
    matches(selector) { return selector === ':disabled' ? this.disabled : this.candidate; }
    getBoundingClientRect() { return {width: this.width, height: this.height}; }
    getClientRects() { return this.width > 0 && this.height > 0 ? [{}] : []; }
    getRootNode() { return this.rootHost ? {host: this.rootHost} : {host: null}; }
    setAttribute(name, value) { this.attrs[name] = value; }
    removeAttribute(name) { delete this.attrs[name]; }
}
global.Node = {ELEMENT_NODE: 1, TEXT_NODE: 3};
global.documentOldRefs = [];
global.document = {
    querySelectorAll: () => global.documentOldRefs,
    createRange: () => ({
        selectNode() {},
        getBoundingClientRect() { return {width: 10, height: 10}; },
    }),
};
global.window = {
    getComputedStyle: element => ({display: element.display, visibility: element.visibility}),
};
const snapshotOptions = {
    candidateSelector: 'candidate-selector',
    fillableTypes: ['text', 'search', 'email'],
    buttonTypes: ['submit', 'button'],
    hiddenTypes: ['hidden'],
    maxSnapshotElements: 80,
    maxScanElements: 500,
};
""" + scenario
    result = subprocess.run(
        [_playwright_node_executable(), "-e", harness, skill._SNAPSHOT_EVALUATE_SCRIPT],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


class _FakeElement:
    def __init__(
        self,
        *,
        tag: str,
        visible: bool = True,
        enabled: bool = True,
        editable: bool = False,
        element_type: str | None = None,
        role: str | None = None,
        label: str = "",
        placeholder: str | None = None,
        href: str | None = None,
    ):
        self.tag = tag
        self.visible = visible
        self.enabled = enabled
        self.editable = editable
        self.element_type = element_type
        self.role = role
        self.label = label
        self.placeholder = placeholder
        self.href = href
        self.ref = None
        self.fill_calls = []

    def evaluate(self, expression, *args):
        if "setAttribute" in expression:
            self.ref = args[0]
            return None
        if "tagName" in expression:
            return self.tag
        raise AssertionError(f"unexpected evaluate expression: {expression}")

    def get_attribute(self, name):
        return {
            "role": self.role,
            "type": self.element_type,
            "aria-label": self.label or None,
            "placeholder": self.placeholder,
            "value": self.label if self.tag == "input" else None,
            "href": self.href,
            "readonly": None,
        }.get(name)

    def inner_text(self, timeout=None):
        return self.label

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def is_editable(self):
        return self.editable

    def fill(self, value, **kwargs):
        self.fill_calls.append((value, kwargs))


class _FailingFillElement(_FakeElement):
    def fill(self, value, **kwargs):
        raise TimeoutError("locator.fill timed out while the page changed")


class _FakeCollection:
    def __init__(self, elements):
        self.elements = elements

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]


class _FakePage:
    def __init__(self, elements):
        self.url = "https://example.test"
        self.elements = elements

    def title(self):
        return "Example"

    def locator(self, selector):
        if selector == "[data-agent-ref]":
            return _FakeCollection(self.elements)
        if selector.startswith('[data-agent-ref="'):
            ref = selector.split('"', 2)[1]
            return _FakeSingleLocator(next((item for item in self.elements if item.ref == ref), None))
        return _FakeCollection(self.elements)


class _FakeKeyboard:
    def __init__(self, events):
        self.events = events

    def press(self, key):
        self.events.append(("press", key))


class _NavigatingFakePage(_FakePage):
    def __init__(self, elements, events):
        super().__init__(elements)
        self.events = events
        self.keyboard = _FakeKeyboard(events)

    def wait_for_load_state(self, state, *, timeout):
        self.events.append(("wait_for_load_state", state, timeout))


class _FakeSingleLocator:
    def __init__(self, element):
        self.element = element

    @property
    def first(self):
        return self

    def count(self):
        return 0 if self.element is None else 1

    def is_visible(self):
        return self.element.is_visible()

    def is_enabled(self):
        return self.element.is_enabled()

    def is_editable(self):
        return self.element.is_editable()

    def get_attribute(self, name):
        return self.element.get_attribute(name)

    def evaluate(self, expression, *args):
        return self.element.evaluate(expression, *args)

    def fill(self, value, **kwargs):
        return self.element.fill(value, **kwargs)


class _BatchOnlyCollection:
    def __init__(self):
        self.evaluate_all_calls = 0

    def evaluate_all(self, expression, options=None):
        self.evaluate_all_calls += 1
        return {
            "elements": [
                {
                    "ref": "e1",
                    "tag": "input",
                    "type": "search",
                    "role": "textbox",
                    "visible": True,
                    "enabled": True,
                    "editable": True,
                    "readonly": False,
                    "label": "Search",
                    "href": None,
                    "actions": "fill,type",
                },
                {
                    "ref": "e2",
                    "tag": "a",
                    "type": None,
                    "role": "link",
                    "visible": True,
                    "enabled": True,
                    "editable": False,
                    "readonly": False,
                    "label": "Docs",
                    "href": "/docs",
                    "actions": "click",
                },
            ],
            "limited": False,
        }

    def count(self):
        raise AssertionError("snapshot must not make per-element Playwright round trips")

    def nth(self, index):
        raise AssertionError("snapshot must not make per-element Playwright round trips")


class _BatchOnlyPage:
    url = "https://example.test/batch"

    def __init__(self):
        self.collection = _BatchOnlyCollection()
        self.selectors = []

    def title(self):
        return "Batch snapshot"

    def locator(self, selector):
        self.selectors.append(selector)
        return self.collection


def _runtime_with_fake_page(skill, elements):
    runtime = skill.BrowserRuntime.__new__(skill.BrowserRuntime)
    runtime.session_id = "test-session"
    runtime.headless = True
    runtime._playwright = None
    runtime._context = SimpleNamespace(pages=[_FakePage(elements)])
    runtime._current_page_index = 0
    return runtime


def test_snapshot_collects_element_metadata_in_one_batch_without_per_element_ipc():
    skill = _load_browser_skill_module()
    runtime = skill.BrowserRuntime.__new__(skill.BrowserRuntime)
    runtime.session_id = "batch-snapshot-session"
    runtime.headless = True
    runtime._playwright = None
    page = _BatchOnlyPage()
    runtime._context = SimpleNamespace(pages=[page])
    runtime._current_page_index = 0

    snapshot = runtime.snapshot()

    assert "Page URL: https://example.test/batch" in snapshot
    assert "- e1: textbox type=search visible=true enabled=true editable=true actions=fill,type label='Search'" in snapshot
    assert "- e2: link visible=true enabled=true editable=false actions=click label='Docs' -> /docs" in snapshot
    assert page.collection.evaluate_all_calls == 1
    assert len(page.selectors) == 1
    assert "[data-agent-ref]" in page.selectors[0]


def test_batch_snapshot_preserves_default_text_input_editability():
    skill = _load_browser_skill_module()
    result = _run_snapshot_script(
        skill,
        r"""
const result = collect([
    new Element('default-input', {tag: 'input', attrs: {'aria-label': 'Default text'}}),
], snapshotOptions);
process.stdout.write(JSON.stringify(result));
""",
    )
    metadata = result["elements"][0]

    assert metadata == {
        "ref": "e1",
        "tag": "input",
        "type": None,
        "role": "input",
        "visible": True,
        "enabled": True,
        "editable": True,
        "readonly": False,
        "label": "Default text",
        "href": None,
        "actions": "fill,type",
    }


def test_snapshot_script_clears_shadow_old_refs_before_reassigning_unique_refs():
    skill = _load_browser_skill_module()

    result = _run_snapshot_script(
        skill,
        r"""
const oldShadowTarget = new Element('old', {attrs: {'aria-label': 'Old'}});
collect([oldShadowTarget], snapshotOptions);
oldShadowTarget.candidate = false;
const newTarget = new Element('new', {attrs: {'aria-label': 'New'}});
const second = collect([oldShadowTarget, newTarget], snapshotOptions);
const resolved = [oldShadowTarget, newTarget].find(
    element => element.getAttribute('data-agent-ref') === 'e1'
);
process.stdout.write(JSON.stringify({
    second,
    oldRef: oldShadowTarget.getAttribute('data-agent-ref'),
    newRef: newTarget.getAttribute('data-agent-ref'),
    resolvedName: resolved && resolved.name,
}));
""",
    )

    assert result == {
        "second": {
            "elements": [
                {
                    "ref": "e1",
                    "tag": "button",
                    "type": None,
                    "role": "button",
                    "visible": True,
                    "enabled": True,
                    "editable": False,
                    "readonly": False,
                    "label": "New",
                    "href": None,
                    "actions": "click",
                }
            ],
            "limited": False,
        },
        "oldRef": None,
        "newRef": "e1",
        "resolvedName": "new",
    }


def test_snapshot_script_matches_playwright_visibility_and_keeps_hidden_file_input():
    skill = _load_browser_skill_module()

    result = _run_snapshot_script(
        skill,
        r"""
const visibleChild = new Element('child', {tag: 'span', candidate: false, width: 12, height: 8});
const elements = [
    new Element('zero-zero', {attrs: {'aria-label': 'zero-zero'}, width: 0, height: 0}),
    new Element('zero-wide', {attrs: {'aria-label': 'zero-wide'}, width: 0, height: 20}),
    new Element('zero-high', {attrs: {'aria-label': 'zero-high'}, width: 20, height: 0}),
    new Element('hidden-submit', {
        tag: 'input', typeAttribute: 'submit', attrs: {'aria-label': 'hidden-submit'}, display: 'none',
    }),
    new Element('contents', {
        attrs: {'aria-label': 'contents'}, width: 0, height: 0, display: 'contents', children: [visibleChild],
    }),
    new Element('file', {
        tag: 'input', typeAttribute: 'file', attrs: {'aria-label': 'file'}, display: 'none', width: 0, height: 0,
    }),
];
const snapshot = collect(elements, snapshotOptions);
process.stdout.write(JSON.stringify(snapshot));
""",
    )

    assert [item["label"] for item in result["elements"]] == ["contents", "file"]
    assert result["elements"][0]["visible"] is True
    assert result["elements"][1]["visible"] is False
    assert result["elements"][1]["actions"] == "upload"


def test_snapshot_script_preserves_exact_scan_and_exposed_limits():
    skill = _load_browser_skill_module()

    result = _run_snapshot_script(
        skill,
        r"""
const staleNonCandidates = Array.from({length: 25}, (_, index) => new Element(`stale-${index}`, {
    candidate: false, attrs: {'data-agent-ref': `e${index + 1}`, 'aria-label': `stale-${index}`},
}));
const scanCandidates = Array.from({length: 501}, (_, index) => new Element(`scan-${index + 1}`, {
    attrs: {'aria-label': `scan-${index + 1}`}, display: index < 499 ? 'none' : 'block',
}));
const scanLimited = collect([...staleNonCandidates, ...scanCandidates], snapshotOptions);
const exposedLimited = collect(
    Array.from({length: 81}, (_, index) => new Element(`visible-${index + 1}`, {
        attrs: {'aria-label': `visible-${index + 1}`},
    })),
    snapshotOptions,
);
process.stdout.write(JSON.stringify({scanLimited, exposedLimited}));
""",
    )

    assert [item["label"] for item in result["scanLimited"]["elements"]] == ["scan-500"]
    assert result["scanLimited"]["limited"] is True
    assert len(result["exposedLimited"]["elements"]) == 80
    assert result["exposedLimited"]["elements"][-1]["ref"] == "e80"
    assert result["exposedLimited"]["limited"] is True


def test_snapshot_script_uses_nearest_aria_disabled_and_crosses_shadow_host():
    skill = _load_browser_skill_module()

    result = _run_snapshot_script(
        skill,
        r"""
const disabledAncestor = new Element('disabled-ancestor', {
    candidate: false, attrs: {'aria-disabled': 'true'},
});
const explicitFalse = new Element('explicit-false', {
    parent: disabledAncestor, attrs: {'aria-label': 'explicit-false', 'aria-disabled': 'false'},
});
const inheritedTrue = new Element('inherited-true', {
    parent: disabledAncestor, attrs: {'aria-label': 'inherited-true'},
});
const shadowHost = new Element('shadow-host', {
    candidate: false, attrs: {'aria-disabled': 'true'},
});
const shadowChild = new Element('shadow-child', {
    rootHost: shadowHost, attrs: {'aria-label': 'shadow-child'},
});
const snapshot = collect([explicitFalse, inheritedTrue, shadowChild], snapshotOptions);
process.stdout.write(JSON.stringify(snapshot));
""",
    )

    assert {item["label"]: item["enabled"] for item in result["elements"]} == {
        "explicit-false": True,
        "inherited-true": False,
        "shadow-child": False,
    }


@pytest.mark.parametrize("disconnect_error", [BrokenPipeError, ConnectionResetError])
@pytest.mark.parametrize("stop_command", ["close-all", "__shutdown__"])
def test_daemon_survives_disconnected_response_and_honors_later_stop(
    monkeypatch, tmp_path, disconnect_error, stop_command
):
    skill = _load_browser_skill_module()
    socket_path = tmp_path / "browser.sock"

    class _Connection:
        def __init__(self, command, *, fail_send=False):
            self.incoming = [(json.dumps({"command": command}) + "\n").encode("utf-8")]
            self.fail_send = fail_send
            self.outgoing = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def settimeout(self, timeout):
            self.timeout = timeout

        def recv(self, size):
            return self.incoming.pop(0) if self.incoming else b""

        def sendall(self, payload):
            if self.fail_send:
                raise disconnect_error("client disconnected")
            self.outgoing.append(payload)

    disconnected = _Connection("slow-command", fail_send=True)
    stopper = _Connection(stop_command)

    class _Server:
        def __init__(self):
            self.connections = [disconnected, stopper]
            self.closed = False

        def bind(self, path):
            Path(path).touch()

        def listen(self, backlog):
            self.backlog = backlog

        def accept(self):
            return self.connections.pop(0), None

        def close(self):
            self.closed = True

    server = _Server()

    class _Runtime:
        instances = []

        def __init__(self, session_id, *, headless):
            self.commands = []
            self.closed = False
            self.__class__.instances.append(self)

        def execute(self, command):
            self.commands.append(command)
            return f"completed:{command}"

        def close(self):
            self.closed = True

    monkeypatch.setattr(skill, "_socket_path", lambda session_id: socket_path)
    monkeypatch.setattr(skill, "_ensure_state_dirs", lambda: None)
    monkeypatch.setattr(skill, "_reject_symlink_components", lambda path: None)
    monkeypatch.setattr(skill.socket, "socket", lambda *args, **kwargs: server)
    monkeypatch.setattr(skill, "BrowserRuntime", _Runtime)

    assert skill._run_daemon("disconnect-session", headless=True) == 0

    runtime = _Runtime.instances[0]
    expected_commands = ["slow-command"]
    if stop_command == "close-all":
        expected_commands.append("close-all")
    assert runtime.commands == expected_commands
    assert runtime.closed is True
    assert server.closed is True
    payload = json.loads(stopper.outgoing[0].decode("utf-8"))
    assert payload["ok"] is True


def test_fill_rejects_visible_submit_as_non_editable_without_calling_playwright_fill():
    skill = _load_browser_skill_module()
    submit = _FakeElement(
        tag="input",
        element_type="submit",
        label="Search",
        visible=True,
        editable=False,
    )
    submit.ref = "e1"
    runtime = _runtime_with_fake_page(skill, [submit])

    with pytest.raises(skill.BrowserSkillError) as exc_info:
        runtime.execute('fill e1 "广州番禺9月14恶意伤人事件"')

    assert exc_info.value.code == "element_not_editable"
    assert "input[type=submit]" in str(exc_info.value)
    assert submit.fill_calls == []


def test_press_enter_retries_snapshot_after_navigation_context_is_destroyed():
    skill = _load_browser_skill_module()
    events = []
    page = _NavigatingFakePage([], events)
    runtime = skill.BrowserRuntime.__new__(skill.BrowserRuntime)
    runtime.session_id = "press-navigation-session"
    runtime.headless = True
    runtime._playwright = None
    runtime._context = SimpleNamespace(pages=[page])
    runtime._current_page_index = 0

    snapshots = iter(
        [
            skill.BrowserSkillError(
                "snapshot failed: Error: Locator.evaluate_all: Execution context was destroyed, "
                "most likely because of a navigation"
            ),
            "Page URL: https://example.test/search\nPage title: Search",
        ]
    )

    def snapshot():
        result = next(snapshots)
        if isinstance(result, Exception):
            raise result
        return result

    runtime.snapshot = snapshot

    assert runtime.execute("press Enter") == "Page URL: https://example.test/search\nPage title: Search"
    assert events == [
        ("press", "Enter"),
        ("wait_for_load_state", "domcontentloaded", skill._NAVIGATION_WAIT_TIMEOUT_MS),
    ]


def test_fill_uses_bounded_timeout_for_an_editable_control():
    skill = _load_browser_skill_module()
    text_input = _FakeElement(
        tag="input",
        element_type="search",
        editable=True,
        placeholder="Search",
    )
    text_input.ref = "e1"
    runtime = _runtime_with_fake_page(skill, [text_input])
    runtime.snapshot = lambda: "snapshot"

    runtime.execute('fill e1 "query"')

    assert text_input.fill_calls == [("query", {"timeout": skill._ACTION_TIMEOUT_MS})]


def test_fill_runtime_failure_is_structured_and_includes_recovery_hint():
    skill = _load_browser_skill_module()
    text_input = _FailingFillElement(
        tag="input",
        element_type="search",
        editable=True,
        placeholder="Search",
    )
    text_input.ref = "e1"
    runtime = _runtime_with_fake_page(skill, [text_input])

    with pytest.raises(skill.BrowserSkillError) as exc_info:
        runtime.execute('fill e1 "query"')

    assert exc_info.value.code == "browser_action_failed"
    assert exc_info.value.details["ref"] == "e1"
    assert "snapshot" in exc_info.value.details["hint"]


def test_browser_error_response_contains_stable_code_and_details():
    skill = _load_browser_skill_module()

    class _FailingRuntime:
        def execute(self, command):
            raise skill.BrowserSkillError(
                "element e1 is not editable",
                code="element_not_editable",
                details={"ref": "e1", "hint": "choose a textbox"},
            )

    class _Connection:
        def __init__(self):
            self.incoming = [b'{"command":"fill e1 text"}\n']
            self.outgoing = []

        def settimeout(self, timeout):
            self.timeout = timeout

        def recv(self, size):
            return self.incoming.pop(0) if self.incoming else b""

        def sendall(self, payload):
            self.outgoing.append(payload)

    connection = _Connection()
    skill._handle_connection(connection, _FailingRuntime())
    payload = json.loads(connection.outgoing[0].decode("utf-8"))

    assert payload == {
        "ok": False,
        "code": "element_not_editable",
        "error": "element e1 is not editable",
        "details": {"ref": "e1", "hint": "choose a textbox"},
    }


def test_browser_skill_launcher_uses_sglang_playwright_despite_runtime_overrides(
    tmp_path,
):
    invalid_node = tmp_path / "not-an-executable"
    result = subprocess.run(
        [str(LAUNCHER), "--probe-runtime"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "VIRTUAL_ENV": str(tmp_path / "agent-environment"),
            "PYTHONPATH": str(tmp_path / "agent-pythonpath"),
            "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "browser-cache"),
            "PLAYWRIGHT_NODEJS_PATH": str(invalid_node),
        },
    )

    assert result.returncode == 0, result.stderr
    assert f"python={REPO_ROOT / '.sglang' / 'bin' / 'python'}" in result.stdout
    assert f"prefix={REPO_ROOT / '.sglang'}" in result.stdout
    assert "virtual_env=<unset>" in result.stdout
    assert "playwright=" in result.stdout
    assert "site-packages/playwright" in result.stdout
    assert "chromium=" in result.stdout


def test_browser_skill_launcher_preserves_agent_path_and_gui_environment(
    tmp_path,
):
    repository = tmp_path / "repository"
    launcher = (
        repository
        / "backend"
        / "skills"
        / "playwright-browser"
        / "scripts"
        / "playwright-browser"
    )
    launcher.parent.mkdir(parents=True)
    shutil.copy2(LAUNCHER, launcher)

    python = repository / ".sglang" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexec env\n", encoding="utf-8")
    python.chmod(0o755)

    agent_bin = tmp_path / "agent-bin"
    agent_bin.mkdir()
    agent_path = os.pathsep.join((str(agent_bin), "/usr/bin", "/bin"))
    home = str(tmp_path / "home")
    gui_environment = {
        "DISPLAY": ":77",
        "WAYLAND_DISPLAY": "wayland-test",
        "XAUTHORITY": str(tmp_path / "xauthority"),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
    }
    runtime_overrides = {
        name: str(tmp_path / f"override-{name.lower()}")
        for name in (
            "VIRTUAL_ENV",
            "PYTHONHOME",
            "PYTHONPATH",
            "PYTHONSTARTUP",
            "PYTHONINSPECT",
            "PYTHONDEBUG",
            "PYTHONWARNINGS",
            "UV_PROJECT_ENVIRONMENT",
            "PLAYWRIGHT_BROWSERS_PATH",
            "PLAYWRIGHT_NODEJS_PATH",
            "NODE_OPTIONS",
            "BASH_ENV",
            "ENV",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "LD_AUDIT",
            "DYLD_INSERT_LIBRARIES",
            "DYLD_LIBRARY_PATH",
        )
    }
    result = subprocess.run(
        [str(launcher), "--probe-runtime"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        env={
            "HOME": home,
            "PATH": agent_path,
            **gui_environment,
            **runtime_overrides,
        },
    )

    assert result.returncode == 0, result.stderr
    child_environment = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    assert child_environment["PATH"] == agent_path
    assert child_environment["HOME"] == home
    for name, value in gui_environment.items():
        assert child_environment[name] == value
    for name in runtime_overrides:
        assert name not in child_environment


def test_browser_skill_launcher_is_executable_and_not_the_legacy_cli():
    assert LAUNCHER.is_file()
    assert LAUNCHER.stat().st_mode & 0o111
    content = LAUNCHER.read_text(encoding="utf-8")
    assert "playwright-cli" not in content
    assert ".sglang/bin/python" in content
    assert "PLAYWRIGHT_BROWSERS_PATH" in content


def test_browser_skill_help_does_not_require_a_browser_process():
    result = subprocess.run(
        [str(LAUNCHER), "--session-id", "playwright-skill-help", "--headless", "help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Supported browser skill commands:" in result.stdout


def test_browser_skill_state_is_derived_independently_per_session():
    skill = _load_browser_skill_module()

    assert skill._socket_path("session-a") != skill._socket_path("session-b")
    assert skill._profile_path("session-a") != skill._profile_path("session-b")
    assert str(skill._socket_path("session-a")).startswith("/tmp/agent-browser/")
    assert str(skill._profile_path("session-a")).startswith("/tmp/agent-browser/")


def test_agent_exec_policy_allows_only_the_registered_skill_launcher():
    config = get_config()
    capability = build_session_capability(
        config, session_id="playwright-skill-policy-check", mode="tool_calling"
    )
    launcher = LAUNCHER.resolve()

    result = exec_command(
        f"{launcher} --probe-runtime",
        session_id=capability.session_id,
        capability=capability,
    )

    assert f"python={REPO_ROOT / '.sglang' / 'bin' / 'python'}" in result
    assert "site-packages/playwright" in result


def test_agent_exec_policy_rejects_a_cross_session_browser_skill_id():
    config = get_config()
    capability = build_session_capability(
        config, session_id="playwright-skill-session-a", mode="tool_calling"
    )
    result = exec_command(
        f"{LAUNCHER} --session-id playwright-skill-session-b --headless snapshot",
        session_id=capability.session_id,
        capability=capability,
    )

    assert "capability_session_mismatch" in result


def test_agent_exec_policy_rejects_a_browser_skill_without_a_session_id():
    capability = build_session_capability(
        get_config(), session_id="playwright-skill-session-a", mode="tool_calling"
    )

    result = exec_command(
        f"{LAUNCHER} --headless help",
        session_id=capability.session_id,
        capability=capability,
    )

    assert "invalid_browser_skill_session" in result


def test_agent_exec_policy_allows_a_browser_skill_bound_to_its_session():
    capability = build_session_capability(
        get_config(), session_id="playwright-skill-session-a", mode="tool_calling"
    )

    result = exec_command(
        f"{LAUNCHER} --session-id {capability.session_id} --headless help",
        session_id=capability.session_id,
        capability=capability,
    )

    assert "Supported browser skill commands:" in result


def test_unregistered_browser_launcher_path_is_denied():
    capability = build_session_capability(
        get_config(), session_id="playwright-skill-session-a", mode="tool_calling"
    )

    result = exec_command(
        "/tmp/playwright-browser --probe-runtime",
        session_id=capability.session_id,
        capability=capability,
    )

    assert "executable_path_not_allowed" in result
