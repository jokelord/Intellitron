#!/usr/bin/env python3
"""Small session-scoped Playwright browser skill.

The Agent calls the sibling ``scripts/playwright-browser`` launcher through
``exec_command``.  This process uses the repository's ``.sglang`` Python
interpreter and keeps one headed persistent Playwright context per Agent
session in a local daemon.  No npm package or global playwright-cli is used.

The daemon protocol is private to this skill: one JSON command per Unix-domain
socket connection.  Socket and profile paths are derived from a validated
session-id hash, so two Agent sessions cannot share browser state by accident.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse


_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REF_RE = re.compile(r"^e[0-9]{1,4}$")
# Keep state under one fixed, private temporary root.  The Agent command
# policy intentionally cannot redirect this path through an environment
# assignment or an extra launcher argument.
_STATE_ROOT = Path("/tmp/agent-browser")
_SOCKET_ROOT = _STATE_ROOT / "sockets"
_PROFILE_ROOT = _STATE_ROOT / "profiles"
_LOG_ROOT = _STATE_ROOT / "logs"
_MAX_REQUEST_BYTES = 256 * 1024
_MAX_SNAPSHOT_ELEMENTS = 80
_MAX_SNAPSHOT_SCAN_ELEMENTS = 500
_ACTION_TIMEOUT_MS = 5_000
_NAVIGATION_WAIT_TIMEOUT_MS = 30_000

_SNAPSHOT_EVALUATE_SCRIPT = r"""
(elements, options) => {
    elements.forEach(element => element.removeAttribute('data-agent-ref'));

    const fillableTypes = new Set(options.fillableTypes);
    const buttonTypes = new Set(options.buttonTypes);
    const hiddenTypes = new Set(options.hiddenTypes);
    const candidates = elements.filter(element => {
        try {
            return element.matches(options.candidateSelector);
        } catch (_error) {
            return false;
        }
    });
    const scanCount = Math.min(candidates.length, options.maxScanElements);
    const collected = [];
    let limited = candidates.length > scanCount;

    const isTextNodeVisible = node => {
        if (!node.nodeValue || !node.nodeValue.trim()) {
            return false;
        }
        const range = document.createRange();
        range.selectNode(node);
        const rect = range.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };

    const isVisible = element => {
        const style = window.getComputedStyle(element);
        if (style.visibility === 'hidden' || style.visibility === 'collapse') {
            return false;
        }
        if (style.display === 'contents') {
            return Array.from(element.childNodes).some(node => {
                if (node.nodeType === Node.ELEMENT_NODE) {
                    return isVisible(node);
                }
                return node.nodeType === Node.TEXT_NODE && isTextNodeVisible(node);
            });
        }
        if (style.display === 'none') {
            return false;
        }
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };

    const isEnabled = element => {
        if (element.matches(':disabled')) {
            return false;
        }
        for (let current = element; current;) {
            const ariaDisabled = (current.getAttribute('aria-disabled') || '').toLowerCase();
            if (ariaDisabled === 'true') {
                return false;
            }
            if (ariaDisabled === 'false') {
                return true;
            }
            const root = current.getRootNode();
            current = current.parentElement || (root && root.host) || null;
        }
        return true;
    };

    for (let index = 0; index < scanCount; index += 1) {
        try {
            const element = candidates[index];
            const tag = element.tagName.toLowerCase();
            const typeValue = (element.getAttribute('type') || '').toLowerCase();
            const elementType = typeValue || null;
            const effectiveInputType = tag === 'input'
                ? String(element.type || typeValue).toLowerCase()
                : typeValue;
            const visible = isVisible(element);
            const enabled = isEnabled(element);
            const readonly = element.hasAttribute('readonly');
            const contenteditable = (element.getAttribute('contenteditable') || '').toLowerCase() === 'true';
            const editable = enabled && !readonly && (
                tag === 'textarea' || contenteditable
                || (tag === 'input' && fillableTypes.has(effectiveInputType))
            );

            if (!visible && !(tag === 'input' && elementType === 'file')) {
                continue;
            }
            if (tag === 'input' && hiddenTypes.has(typeValue)) {
                continue;
            }
            if (collected.length >= options.maxSnapshotElements) {
                limited = true;
                break;
            }

            const roleAttribute = element.getAttribute('role');
            let role;
            if (roleAttribute) {
                role = roleAttribute;
            } else if (
                tag === 'textarea' || contenteditable || (tag === 'input' && fillableTypes.has(typeValue))
            ) {
                role = 'textbox';
            } else if (tag === 'select') {
                role = 'combobox';
            } else if (tag === 'input' && buttonTypes.has(typeValue)) {
                role = 'button';
            } else {
                role = tag;
            }

            let label = element.getAttribute('aria-label')
                || element.getAttribute('placeholder')
                || element.getAttribute('value')
                || element.getAttribute('name');
            if (!label) {
                try {
                    label = element.innerText;
                } catch (_error) {
                    label = '';
                }
            }
            label = label || role;

            let actions;
            if (tag === 'input' && elementType === 'file') {
                actions = 'upload';
            } else if (editable && !readonly) {
                actions = 'fill,type';
            } else if (tag === 'select') {
                actions = 'select';
            } else if (
                ['button', 'link', 'checkbox', 'tab'].includes(role) || ['a', 'button'].includes(tag)
            ) {
                actions = 'click';
            } else {
                actions = 'inspect';
            }

            const ref = `e${collected.length + 1}`;
            element.setAttribute('data-agent-ref', ref);
            collected.push({
                ref,
                tag,
                type: elementType,
                role,
                visible,
                enabled,
                editable,
                readonly,
                label,
                href: element.getAttribute('href'),
                actions,
            });
        } catch (_error) {
            // Dynamic pages may detach or mutate individual elements while the
            // batch is evaluated. Omit only that element.
        }
    }
    return {elements: collected, limited};
}
"""

_FILLABLE_INPUT_TYPES = frozenset(
    {
        "text",
        "search",
        "email",
        "url",
        "tel",
        "password",
        "number",
        "date",
        "datetime-local",
        "month",
        "time",
        "week",
    }
)
_BUTTON_INPUT_TYPES = frozenset({"submit", "button", "reset", "image"})
_HIDDEN_INPUT_TYPES = frozenset({"hidden"})

_SNAPSHOT_CANDIDATE_SELECTOR = (
    "a,button,input:not([type='hidden']),textarea,select,[contenteditable='true'],"
    "[role=button],[role=link],[role=checkbox],[role=tab]"
)


class BrowserSkillError(RuntimeError):
    """An actionable, user-safe browser skill error."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "browser_skill_error",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_RE.fullmatch(session_id):
        raise BrowserSkillError(
            "session-id must be a non-empty identifier containing only letters, numbers, '.', '_' ':' or '-'."
        )
    return session_id


def _session_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _socket_path(session_id: str) -> Path:
    return _SOCKET_ROOT / f"{_session_key(session_id)}.sock"


def _profile_path(session_id: str) -> Path:
    return _PROFILE_ROOT / _session_key(session_id)


def _reject_symlink_components(path: Path) -> None:
    """Prevent a writable /tmp state directory from joining another profile."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                raise BrowserSkillError(f"browser state path contains a symlink: {current}")
        except OSError as exc:
            raise BrowserSkillError(f"cannot inspect browser state path: {current}") from exc


def _ensure_private_dir(path: Path) -> None:
    """Create a skill-owned directory with user-only permissions."""
    _reject_symlink_components(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise BrowserSkillError(f"cannot secure browser state directory: {path}") from exc


def _ensure_state_dirs() -> None:
    _reject_symlink_components(_STATE_ROOT)
    _ensure_private_dir(_STATE_ROOT)
    for directory in (_SOCKET_ROOT, _PROFILE_ROOT, _LOG_ROOT):
        _ensure_private_dir(directory)
    _reject_symlink_components(_STATE_ROOT)


def _safe_output_path(raw_path: str, *, default_name: str) -> Path:
    value = raw_path or default_name
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve()
    allowed_roots = (Path.cwd().resolve(), Path("/tmp").resolve())
    if not any(candidate == root or root in candidate.parents for root in allowed_roots):
        raise BrowserSkillError("output path must be below the Agent work directory or /tmp")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def _safe_upload_path(raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve(strict=True)
    allowed_roots = (Path.cwd().resolve(), Path("/tmp").resolve())
    if not any(candidate == root or root in candidate.parents for root in allowed_roots):
        raise BrowserSkillError("upload path must be below the Agent work directory or /tmp")
    if not candidate.is_file():
        raise BrowserSkillError(f"upload path is not a regular file: {candidate}")
    return candidate


def _validate_url(value: str) -> str:
    parsed = urlparse(value)
    if value == "about:blank":
        return value
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BrowserSkillError("open/goto accepts only http(s) URLs or about:blank")
    return value


def _text(value: str | None, limit: int = 180) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


class BrowserRuntime:
    def __init__(self, session_id: str, *, headless: bool = False) -> None:
        self.session_id = _validate_session_id(session_id)
        self.headless = bool(headless)
        self._playwright = None
        self._context = None
        self._current_page_index = 0

    def _ensure_context(self):
        if self._context is not None:
            return self._context
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - depends on interpreter packaging
            raise BrowserSkillError(
                f".sglang Playwright import failed: {type(exc).__name__}: {exc}"
            ) from exc

        profile = _profile_path(self.session_id)
        _ensure_state_dirs()
        _reject_symlink_components(profile)
        _ensure_private_dir(profile)
        _reject_symlink_components(profile)
        try:
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=self.headless,
                viewport={"width": 1440, "height": 1000},
            )
        except Exception as exc:
            self.close()
            raise BrowserSkillError(
                f"无法启动 Playwright Chromium（检查图形会话或浏览器缓存）：{type(exc).__name__}: {exc}"
            ) from exc
        if not self._context.pages:
            self._context.new_page()
        self._current_page_index = min(self._current_page_index, len(self._context.pages) - 1)
        return self._context

    @property
    def pages(self):
        return list(self._ensure_context().pages)

    @property
    def page(self):
        pages = self.pages
        if not pages:
            self._ensure_context().new_page()
            pages = self.pages
        self._current_page_index = min(self._current_page_index, len(pages) - 1)
        return pages[self._current_page_index]

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            self._context = None
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                finally:
                    self._playwright = None

    def _tabs(self) -> str:
        pages = self.pages
        lines = ["Open tabs:"]
        for index, page in enumerate(pages):
            try:
                title = _text(page.title(), 100)
            except Exception:
                title = ""
            marker = " (current)" if index == self._current_page_index else ""
            lines.append(f"- {index}: {page.url or 'about:blank'}{marker} {title}")
        return "\n".join(lines)

    def snapshot(self) -> str:
        page = self.page
        try:
            title = page.title()
        except Exception:
            title = ""
        lines = [f"Page URL: {page.url}", f"Page title: {_text(title, 160)}", self._tabs(), "Elements:"]
        try:
            snapshot_data = page.locator(
                f"{_SNAPSHOT_CANDIDATE_SELECTOR},[data-agent-ref]"
            ).evaluate_all(
                _SNAPSHOT_EVALUATE_SCRIPT,
                {
                    "candidateSelector": _SNAPSHOT_CANDIDATE_SELECTOR,
                    "fillableTypes": sorted(_FILLABLE_INPUT_TYPES),
                    "buttonTypes": sorted(_BUTTON_INPUT_TYPES),
                    "hiddenTypes": sorted(_HIDDEN_INPUT_TYPES),
                    "maxSnapshotElements": _MAX_SNAPSHOT_ELEMENTS,
                    "maxScanElements": _MAX_SNAPSHOT_SCAN_ELEMENTS,
                },
            )
        except Exception as exc:
            raise BrowserSkillError(f"snapshot failed: {type(exc).__name__}: {exc}") from exc
        elements = snapshot_data.get("elements", []) if isinstance(snapshot_data, dict) else []
        for metadata in elements:
            if not isinstance(metadata, dict) or not isinstance(metadata.get("ref"), str):
                continue
            try:
                metadata["label"] = _text(str(metadata.get("label") or ""), 140)
                lines.append(self._format_snapshot_element(str(metadata["ref"]), metadata))
            except Exception:
                # Ignore one malformed result if a dynamic page changed an
                # element during serialization.
                continue
        if isinstance(snapshot_data, dict) and snapshot_data.get("limited"):
            lines.append(f"- … snapshot limited to {_MAX_SNAPSHOT_ELEMENTS} elements")
        return "\n".join(lines)

    @staticmethod
    def _element_metadata(locator) -> dict[str, object]:
        tag = str(locator.evaluate("el => el.tagName.toLowerCase()"))
        element_type = (locator.get_attribute("type") or "").lower() or None
        role_attr = locator.get_attribute("role")
        visible = bool(locator.is_visible())
        enabled = bool(locator.is_enabled())
        try:
            editable = bool(locator.is_editable())
        except Exception:
            editable = False
        contenteditable = (locator.get_attribute("contenteditable") or "").lower() == "true"
        if role_attr:
            role = role_attr
        elif tag == "textarea" or contenteditable or (
            tag == "input" and element_type in _FILLABLE_INPUT_TYPES
        ):
            role = "textbox"
        elif tag == "select":
            role = "combobox"
        elif tag == "input" and element_type in _BUTTON_INPUT_TYPES:
            role = "button"
        else:
            role = tag
        label = (
            locator.get_attribute("aria-label")
            or locator.get_attribute("placeholder")
            or locator.get_attribute("value")
            or locator.get_attribute("name")
        )
        if not label:
            try:
                label = _text(locator.inner_text(timeout=500), 140)
            except Exception:
                label = ""
        label = label or role
        href = locator.get_attribute("href")
        readonly = locator.get_attribute("readonly") is not None
        if tag == "input" and element_type == "file":
            actions = "upload"
        elif editable and not readonly:
            actions = "fill,type"
        elif tag == "select":
            actions = "select"
        elif role in {"button", "link", "checkbox", "tab"} or tag in {"a", "button"}:
            actions = "click"
        else:
            actions = "inspect"
        return {
            "tag": tag,
            "type": element_type,
            "role": role,
            "visible": visible,
            "enabled": enabled,
            "editable": editable,
            "readonly": readonly,
            "label": _text(str(label), 140),
            "href": href,
            "actions": actions,
        }

    @staticmethod
    def _include_in_snapshot(metadata: dict[str, object]) -> bool:
        tag = str(metadata["tag"])
        element_type = metadata.get("type")
        visible = bool(metadata["visible"])
        # Hidden file inputs are intentionally retained because upload controls
        # commonly hide the input and expose only a styled button.
        if not visible and not (tag == "input" and element_type == "file"):
            return False
        if tag == "input" and element_type in _HIDDEN_INPUT_TYPES:
            return False
        return True

    @staticmethod
    def _format_snapshot_element(ref: str, metadata: dict[str, object]) -> str:
        parts = [
            f"- {ref}: {metadata['role']}",
            f"visible={str(bool(metadata['visible'])).lower()}",
            f"enabled={str(bool(metadata['enabled'])).lower()}",
            f"editable={str(bool(metadata['editable'])).lower()}",
        ]
        if metadata.get("type"):
            parts.insert(1, f"type={metadata['type']}")
        if metadata.get("readonly"):
            parts.append("readonly=true")
        parts.append(f"actions={metadata['actions']}")
        parts.append(f"label={metadata['label']!r}")
        if metadata.get("href"):
            parts.append(f"-> {metadata['href']}")
        return " ".join(parts)

    @staticmethod
    def _locator_details(locator) -> dict[str, object]:
        try:
            metadata = BrowserRuntime._element_metadata(locator)
        except Exception:
            return {"tag": "unknown", "type": None}
        return {
            "tag": metadata["tag"],
            "type": metadata.get("type"),
            "role": metadata["role"],
            "visible": metadata["visible"],
            "enabled": metadata["enabled"],
            "editable": metadata["editable"],
            "readonly": metadata["readonly"],
            "label": metadata["label"],
        }

    def _require_actionable(self, locator, ref: str, action: str, *, file_upload: bool = False) -> None:
        details = self._locator_details(locator)
        details.update({"ref": ref, "action": action})
        if file_upload:
            if details.get("type") != "file":
                details["hint"] = "upload 只能用于 input[type=file]"
                raise BrowserSkillError(
                    f"element {ref} is not a file input; use snapshot to choose input[type=file]",
                    code="element_not_file_input",
                    details=details,
                )
        elif not details.get("visible"):
            details["hint"] = "重新 snapshot 并选择 visible=true 的元素"
            raise BrowserSkillError(
                f"element {ref} is not visible; run snapshot and choose a visible element",
                code="element_not_visible",
                details=details,
            )
        if not details.get("enabled"):
            details["hint"] = "重新 snapshot 并选择 enabled=true 的元素"
            raise BrowserSkillError(
                f"element {ref} is disabled; run snapshot and choose an enabled element",
                code="element_disabled",
                details=details,
            )
        if action in {"fill", "type"} and (
            not details.get("editable") or details.get("readonly")
        ):
            element_type = details.get("type") or details.get("tag")
            details["hint"] = "请使用 click 操作按钮，或重新 snapshot 选择 textbox"
            raise BrowserSkillError(
                f"element {ref} is not editable ({details.get('tag')}[type={element_type}]); "
                "use click for buttons or choose a textbox",
                code="element_not_editable",
                details=details,
            )

    def _run_action(self, locator, ref: str, action: str, callback):
        try:
            return callback()
        except BrowserSkillError:
            raise
        except Exception as exc:
            details = self._locator_details(locator)
            details.update(
                {
                    "ref": ref,
                    "action": action,
                    "hint": "页面可能已变化；重新 snapshot 后使用新的引用重试",
                }
            )
            message = _text(f"{type(exc).__name__}: {exc}", 800)
            raise BrowserSkillError(
                f"{action} failed for element {ref}: {message}",
                code="browser_action_failed",
                details=details,
            ) from exc

    def _snapshot_after_action(self) -> str:
        """Return a post-action snapshot, recovering from navigation races."""
        try:
            return self.snapshot()
        except BrowserSkillError as exc:
            if "execution context was destroyed" not in str(exc).lower():
                raise

            page = self.page
            try:
                page.wait_for_load_state(
                    "domcontentloaded", timeout=_NAVIGATION_WAIT_TIMEOUT_MS
                )
            except Exception as wait_exc:
                raise BrowserSkillError(
                    "browser navigation did not settle before the post-action snapshot",
                    code="browser_navigation_failed",
                    details={
                        "hint": "等待页面导航完成后重新 snapshot；不要重复提交原操作",
                    },
                ) from wait_exc
            return self.snapshot()

    def _ref(self, value: str):
        if not _REF_RE.fullmatch(value):
            raise BrowserSkillError(
                f"invalid element reference: {value}", code="invalid_element_reference"
            )
        locator = self.page.locator(f'[data-agent-ref="{value}"]').first
        try:
            if locator.count() == 0:
                raise BrowserSkillError(
                    f"element {value} is missing or stale; run snapshot first",
                    code="element_missing_or_stale",
                    details={"ref": value, "hint": "run snapshot before retrying"},
                )
        except BrowserSkillError:
            raise
        except Exception as exc:
            raise BrowserSkillError(
                f"cannot resolve element {value}: {type(exc).__name__}: {exc}",
                code="element_resolution_failed",
            ) from exc
        return locator

    def execute(self, command: str) -> str:
        if not isinstance(command, str) or not command.strip():
            raise BrowserSkillError("a browser subcommand is required")
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise BrowserSkillError(f"invalid browser command quoting: {exc}") from exc
        action = argv[0]
        args = argv[1:]

        if action in {"open", "goto"}:
            if len(args) != 1:
                raise BrowserSkillError(f"{action} requires exactly one URL")
            self.page.goto(_validate_url(args[0]), wait_until="domcontentloaded", timeout=30_000)
            return self.snapshot()
        if action == "snapshot":
            if args:
                raise BrowserSkillError("snapshot takes no arguments")
            return self.snapshot()
        if action in {"click", "dblclick", "hover", "check", "uncheck"}:
            if len(args) != 1:
                raise BrowserSkillError(f"{action} requires one element reference")
            locator = self._ref(args[0])
            self._require_actionable(locator, args[0], action)
            self._run_action(
                locator,
                args[0],
                action,
                lambda: getattr(locator, action)(timeout=_ACTION_TIMEOUT_MS),
            )
            if action in {"click", "dblclick"}:
                time.sleep(0.2)
            return self.snapshot()
        if action in {"fill", "type"}:
            if len(args) < 2:
                raise BrowserSkillError(f"{action} requires a reference and text")
            locator = self._ref(args[0])
            value = " ".join(args[1:])
            self._require_actionable(locator, args[0], action)
            if action == "fill":
                self._run_action(
                    locator,
                    args[0],
                    action,
                    lambda: locator.fill(value, timeout=_ACTION_TIMEOUT_MS),
                )
            else:
                self._run_action(
                    locator,
                    args[0],
                    action,
                    lambda: locator.press_sequentially(value, timeout=_ACTION_TIMEOUT_MS),
                )
            return self.snapshot()
        if action == "select":
            if len(args) != 2:
                raise BrowserSkillError("select requires a reference and option value")
            locator = self._ref(args[0])
            self._require_actionable(locator, args[0], action)
            self._run_action(
                locator,
                args[0],
                action,
                lambda: locator.select_option(args[1], timeout=_ACTION_TIMEOUT_MS),
            )
            return self.snapshot()
        if action == "upload":
            if len(args) != 2:
                raise BrowserSkillError("upload requires a reference and a local file")
            locator = self._ref(args[0])
            self._require_actionable(locator, args[0], action, file_upload=True)
            path = _safe_upload_path(args[1])
            self._run_action(
                locator,
                args[0],
                action,
                lambda: locator.set_input_files(str(path)),
            )
            return self.snapshot()
        if action == "press":
            if len(args) == 1:
                self.page.keyboard.press(args[0])
            elif len(args) == 2:
                self._ref(args[0]).press(args[1])
            else:
                raise BrowserSkillError("press takes KEY or REF KEY")
            return self._snapshot_after_action()
        if action in {"keydown", "keyup"}:
            if len(args) != 1:
                raise BrowserSkillError(f"{action} requires one key")
            getattr(self.page.keyboard, action)(args[0])
            return self.snapshot()
        if action == "reload":
            self.page.reload(wait_until="domcontentloaded", timeout=30_000)
            return self.snapshot()
        if action in {"go-back", "go-forward"}:
            result = getattr(self.page, action.replace("-", "_"))(
                wait_until="domcontentloaded", timeout=30_000
            )
            return self.snapshot() if result is not None else self.snapshot()
        if action == "tab-list":
            return self._tabs()
        if action == "tab-select":
            if len(args) != 1 or not args[0].isdigit():
                raise BrowserSkillError("tab-select requires a numeric tab index")
            index = int(args[0])
            if index < 0 or index >= len(self.pages):
                raise BrowserSkillError(f"tab index out of range: {index}")
            self._current_page_index = index
            return self.snapshot()
        if action == "tab-new":
            page = self._ensure_context().new_page()
            self._current_page_index = len(self.pages) - 1
            if args:
                if len(args) != 1:
                    raise BrowserSkillError("tab-new accepts at most one URL")
                page.goto(_validate_url(args[0]), wait_until="domcontentloaded", timeout=30_000)
            return self.snapshot()
        if action == "tab-close":
            pages = self.pages
            if len(pages) <= 1:
                raise BrowserSkillError("cannot close the last browser tab")
            self.page.close()
            self._current_page_index = min(self._current_page_index, len(self.pages) - 1)
            return self.snapshot()
        if action == "close":
            self.page.close()
            return "Browser tab closed.\n" + (self._tabs() if self._context and self.pages else "No open tabs.")
        if action == "close-all":
            self.close()
            return "Browser context closed for this Agent session."
        if action == "screenshot":
            filename = ""
            for value in args:
                if value.startswith("--filename="):
                    filename = value.split("=", 1)[1]
                else:
                    raise BrowserSkillError("screenshot only accepts --filename=PATH")
            path = _safe_output_path(filename, default_name=f"/tmp/agent-browser/{_session_key(self.session_id)}.png")
            self.page.screenshot(path=str(path), full_page=True)
            return f"Screenshot saved: {path}"
        if action == "pdf":
            filename = ""
            for value in args:
                if value.startswith("--filename="):
                    filename = value.split("=", 1)[1]
                else:
                    raise BrowserSkillError("pdf only accepts --filename=PATH")
            path = _safe_output_path(filename, default_name=f"/tmp/agent-browser/{_session_key(self.session_id)}.pdf")
            self.page.pdf(path=str(path))
            return f"PDF saved: {path}"
        if action == "eval":
            if not args:
                raise BrowserSkillError("eval requires a JavaScript expression")
            expression = " ".join(args)
            value = self.page.evaluate(expression)
            return json.dumps(value, ensure_ascii=False, indent=2, default=str)
        if action == "state-save":
            if len(args) != 1:
                raise BrowserSkillError("state-save requires a filename")
            path = _safe_output_path(args[0], default_name="state.json")
            self._ensure_context().storage_state(path=str(path))
            return f"Browser storage state saved: {path}"
        if action == "cookie-list":
            return json.dumps(self._ensure_context().cookies(), ensure_ascii=False, indent=2)
        if action == "cookie-get":
            if len(args) != 1:
                raise BrowserSkillError("cookie-get requires a cookie name")
            cookies = [item for item in self._ensure_context().cookies() if item.get("name") == args[0]]
            return json.dumps(cookies, ensure_ascii=False, indent=2)
        if action == "help":
            return HELP_TEXT
        raise BrowserSkillError(f"unsupported browser subcommand: {action}")


HELP_TEXT = """Supported browser skill commands:
  open URL | goto URL | snapshot | click REF | dblclick REF | fill REF TEXT
  type REF TEXT | hover REF | select REF VALUE | check REF | uncheck REF
  upload REF FILE | press KEY or press REF KEY | keydown KEY | keyup KEY
  reload | go-back | go-forward | tab-list | tab-new [URL] | tab-select INDEX
  tab-close | screenshot [--filename=PATH] | pdf [--filename=PATH]
  eval JAVASCRIPT | state-save PATH | cookie-list | cookie-get NAME
  close | close-all
"""


def _handle_connection(conn: socket.socket, runtime: BrowserRuntime) -> bool:
    conn.settimeout(35.0)
    chunks: list[bytes] = []
    total = 0
    while b"\n" not in b"".join(chunks):
        data = conn.recv(8192)
        if not data:
            break
        chunks.append(data)
        total += len(data)
        if total > _MAX_REQUEST_BYTES:
            raise BrowserSkillError("browser command is too large")
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        return False
    request = json.loads(raw.decode("utf-8"))
    command = request.get("command")
    if command == "__shutdown__":
        result = "Browser skill daemon stopped."
        should_stop = True
    else:
        try:
            result = runtime.execute(command)
            should_stop = command == "close-all"
            payload = {"ok": True, "result": result}
        except BrowserSkillError as exc:
            should_stop = False
            payload = {"ok": False, "code": exc.code, "error": str(exc)}
            if exc.details:
                payload["details"] = exc.details
        except Exception as exc:
            should_stop = False
            payload = {
                "ok": False,
                "code": "browser_command_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        _send_response(conn, payload)
        return should_stop
    _send_response(conn, {"ok": True, "result": result})
    return should_stop


def _send_response(conn: socket.socket, payload: dict[str, object]) -> bool:
    try:
        conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    except (BrokenPipeError, ConnectionResetError):
        return False
    return True


def _run_daemon(session_id: str, *, headless: bool) -> int:
    path = _socket_path(session_id)
    _ensure_state_dirs()
    _reject_symlink_components(path)
    try:
        if path.exists():
            path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(8)
    except Exception as exc:
        print(f"playwright-browser daemon failed to bind: {exc}", file=sys.stderr)
        return 78

    runtime = BrowserRuntime(session_id, headless=headless)
    try:
        while True:
            conn, _ = server.accept()
            with conn:
                try:
                    stop = _handle_connection(conn, runtime)
                except Exception as exc:
                    payload = {
                        "ok": False,
                        "code": getattr(exc, "code", "browser_command_failed"),
                        "error": str(exc),
                    }
                    details = getattr(exc, "details", None)
                    if details:
                        payload["details"] = details
                    _send_response(conn, payload)
                    stop = False
            if stop:
                break
    finally:
        runtime.close()
        server.close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return 0


def _connect(path: Path, timeout: float = 0.4) -> socket.socket | None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except OSError:
        sock.close()
        return None
    return sock


def _start_daemon(session_id: str, *, headless: bool) -> None:
    _ensure_state_dirs()
    path = _socket_path(session_id)
    lock_path = _SOCKET_ROOT / f"{_session_key(session_id)}.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(lock_fd, 0o600)
    with os.fdopen(lock_fd, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        existing = _connect(path)
        if existing is not None:
            existing.close()
            return
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        log_path = _LOG_ROOT / f"{_session_key(session_id)}.log"
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(log_fd, 0o600)
        log = os.fdopen(log_fd, "ab", buffering=0)
        try:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--daemon", "--session-id", session_id]
                + (["--headless"] if headless else []),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log.close()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            sock = _connect(path)
            if sock is not None:
                sock.close()
                return
            time.sleep(0.05)
        raise BrowserSkillError(f"browser skill daemon did not start; inspect {log_path}")


def _send_command(session_id: str, command: str, *, headless: bool) -> str:
    path = _socket_path(session_id)
    _start_daemon(session_id, headless=headless)
    sock = _connect(path, timeout=35.0)
    if sock is None:
        raise BrowserSkillError("browser skill daemon is unavailable")
    with sock:
        sock.sendall((json.dumps({"command": command}, ensure_ascii=False) + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        while b"\n" not in b"".join(chunks):
            data = sock.recv(8192)
            if not data:
                break
            chunks.append(data)
        raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise BrowserSkillError("browser skill daemon returned no response")
    payload = json.loads(raw.decode("utf-8"))
    if not payload.get("ok"):
        code = str(payload.get("code") or "browser_command_failed")
        raise BrowserSkillError(
            str(payload.get("error") or "browser command failed"),
            code=code,
            details=payload.get("details") if isinstance(payload.get("details"), dict) else None,
        )
    return str(payload.get("result", ""))


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if "--probe-runtime" in raw_argv:
        try:
            from importlib.metadata import version as package_version
            import playwright
            from playwright.sync_api import sync_playwright

            version = package_version("playwright")
            print(f"python={sys.executable}")
            print(f"prefix={sys.prefix}")
            print(f"virtual_env={os.environ.get('VIRTUAL_ENV', '<unset>')}")
            print(f"playwright={version} path={playwright.__file__}")
            with sync_playwright() as playwright_runtime:
                chromium = Path(playwright_runtime.chromium.executable_path)
                print(f"chromium={chromium} exists={chromium.is_file()}")
                if not chromium.is_file():
                    return 78
            return 0
        except Exception as exc:
            print(f"playwright-browser: .sglang Playwright import failed: {exc}", file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(description="session-scoped Playwright browser skill")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(raw_argv)
    try:
        session_id = _validate_session_id(args.session_id)
        command = shlex.join(args.command).strip()
        if args.daemon:
            return _run_daemon(session_id, headless=args.headless)
        if command == "help":
            print(HELP_TEXT)
            return 0
        print(_send_command(session_id, command, headless=args.headless))
        return 0
    except Exception as exc:
        if isinstance(exc, BrowserSkillError):
            print(f"playwright-browser: [{exc.code}] {exc}", file=sys.stderr)
            if exc.details:
                print(
                    "details: "
                    + json.dumps(exc.details, ensure_ascii=False, separators=(",", ":")),
                    file=sys.stderr,
                )
            if exc.details.get("hint"):
                print(f"hint: {exc.details['hint']}", file=sys.stderr)
        else:
            print(f"playwright-browser: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
