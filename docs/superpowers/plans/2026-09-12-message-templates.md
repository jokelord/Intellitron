# Message Templates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a shared message-template feature to the text and vision Web UI pages, with YAML initialization, browser-cache precedence, editable/deletable/addable items, a safe default fill mode, and an opt-in immediate-send mode.

**Architecture:** The backend parses `webUI.components.message_templates.items` and exposes the YAML defaults through the existing `/config` response, but never reads or writes browser state. The frontend uses the existing `llm-message-templates` list key plus an independent `llm-message-template-immediate-send` boolean key, resolves the complete effective list with explicit absent/valid/invalid states, and renders one shared controlled `MessageTemplateMenu` component in both pages; selected templates enter the existing page send callbacks without changing LLM wire formats or prompt history.

**Tech Stack:** Python 3, FastAPI configuration endpoint, dataclasses, pytest, Next.js 14, React 18, TypeScript, Node test runner, Tailwind CSS, lucide-react.

**Spec:** `docs/superpowers/specs/2026-09-12-message-templates-design.md`

## Global Constraints

- Do not run any git command without explicit user permission; this plan intentionally omits commit steps.
- Do not modify third-party dependency source under `.sglang` or `frontend/node_modules`.
- Do not add a backend template-save API and do not make backend code read browser localStorage.
- The YAML path is `webUI.components.message_templates.items`; the browser cache key is `llm-message-templates`.
- A missing cache key uses YAML defaults; a valid cached array, including `[]`, replaces the entire YAML list.
- A malformed cache is left untouched and falls back to YAML; a mixed-validity array is malformed rather than partially accepted.
- Cache writes happen only after browser-side add, edit, or delete; the initial YAML list is not automatically written to cache.
- “恢复默认” removes the browser cache and restores the currently loaded YAML list; it does not write YAML values back into cache.
- The default template selection mode is insert; immediate send is controlled by one `ToggleSwitch`, persisted under its own key, and safely defaults off for missing, invalid, or unreadable cache values.
- Template rendering may truncate only the visual button label; the full `aria-label`, textarea, input, and sent text retain the full non-empty string. Template items have no custom or native full-text tooltip.
- Text and vision pages must use the same storage helpers, component semantics, labels, validation, and lifecycle guards.
- Template selection must not add timeline/context-pack fields or alter system prompts, tool traces, compact metadata, or Prefix-Cache request shape.
- In vision mode, selecting a template keeps current pending images; immediate send delegates to the existing vision send pipeline.
- Use `./.sglang/bin/python -m pytest` for backend tests and run frontend commands from `frontend/`.
- Use `npm run build:verify`, not `npm run build`, for frontend production-build verification.
- Use test-first steps: write a focused failing test, run it, implement the smallest change, then run the focused test again.

## File Map

| File | Responsibility in this change |
|---|---|
| `config/config-webUI.yaml` | Initial YAML template examples under `webUI.components.message_templates.items` |
| `backend/config.py` | Parse and normalize the YAML template list into `WebUIConfig.message_templates` |
| `backend/main.py` | Serialize the backend-validated list in `/config.webUI.components` |
| `tests/test_web_ui_config.py` | YAML parsing and malformed-item coverage |
| `tests/test_runtime_config_limits.py` | `/config` response contract coverage |
| `frontend/src/lib/api.ts` | TypeScript `/config.webUI.components.message_templates` contract |
| `frontend/src/lib/web-ui-config.ts` | Shared config type, storage key, cache parser, precedence and persistence helpers |
| `frontend/src/lib/web-ui-config.test.mts` | Pure helper and storage precedence tests |
| `frontend/src/components/MessageTemplateMenu.tsx` | Shared grid-cell trigger, full-row panel, immediate-send switch, item controls, editor and default restore UI |
| `frontend/src/components/MessageTemplateMenu.test.mjs` | Rendered component interaction tests using the existing custom renderer |
| `frontend/src/app/page.tsx` | Text-page config loading and message-template send/fill wiring |
| `frontend/src/app/vision/page.tsx` | Vision-page config loading and message-template send/fill wiring |
| `frontend/src/components/ImageUploader.tsx` | Vision five-column action grid that directly renders the shared message-template component |
| `frontend/src/lib/message-template-pages.test.mjs` | Static synchronization checks for both page integrations |

## Task 1: Add the backend YAML and `/config` contract

**Files:**

- Modify: `tests/test_web_ui_config.py`
- Modify: `tests/test_runtime_config_limits.py`
- Modify: `backend/config.py:275-341,1545-1699`
- Modify: `backend/main.py:756-765,903-916`
- Modify: `config/config-webUI.yaml:88-107`

**Interfaces:**

- Consumes: existing `ConfigLoader._data["webUI"]["components"]` and `WebUIConfig` construction.
- Produces: `WebUIConfig.message_templates: list[str]`.
- Produces: `/config` JSON at `webUI.components.message_templates.items`.
- Produces: YAML defaults that frontend tasks can read through `ConfigResponse.webUI.components.message_templates.items`.

- [ ] **Step 1: Write failing parser tests for valid, empty, and invalid YAML template values**

Append tests to `tests/test_web_ui_config.py` using the existing `ConfigLoader.__new__` fixture style:

```python
def test_parse_web_ui_message_templates_preserves_order_and_text():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {
        "webUI": {
            "components": {
                "message_templates": {
                    "items": [
                        " Hello, what is your name? ",
                        "Tell me the weather forecast for Changsha.\nUse Celsius.",
                    ]
                }
            }
        }
    }

    assert loader._parse_web_ui_config().message_templates == [
        " Hello, what is your name? ",
        "Tell me the weather forecast for Changsha.\nUse Celsius.",
    ]


def test_parse_web_ui_message_templates_filters_blank_and_non_string_items():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {
        "webUI": {
            "components": {
                "message_templates": {
                    "items": ["first", "  ", None, 42, "second"]
                }
            }
        }
    }

    assert loader._parse_web_ui_config().message_templates == ["first", "second"]


def test_parse_web_ui_message_templates_defaults_to_empty_list_for_invalid_shape():
    loader = ConfigLoader.__new__(ConfigLoader)

    for value in (None, [], "not-an-object", {"items": "not-an-array"}):
        loader._data = {
            "webUI": {"components": {"message_templates": value}}
        }
        assert loader._parse_web_ui_config().message_templates == []


def test_web_ui_config_constructor_defaults_message_templates_to_empty_list():
    assert WebUIConfig().message_templates == []
```

- [ ] **Step 2: Run the parser tests and confirm failure**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_web_ui_config.py -k message_templates -q
```

Expected: FAIL because `WebUIConfig.message_templates` does not exist yet.

- [ ] **Step 3: Add the backend field and parser**

In `backend/config.py`, add the field next to the other flattened component fields:

```python
message_templates: list[str] = field(default_factory=list)
```

In `_parse_web_ui_config()`, read the nested object without calling `.get()` on malformed values, then retain non-blank strings exactly as supplied:

```python
message_templates_config = components.get("message_templates")
message_templates_raw = (
    message_templates_config.get("items")
    if isinstance(message_templates_config, dict)
    else None
)
parsed_message_templates = (
    [item for item in message_templates_raw if isinstance(item, str) and item.strip()]
    if isinstance(message_templates_raw, list)
    else []
)
```

Pass `message_templates=parsed_message_templates` into the returned `WebUIConfig`. Do not strip non-blank values and do not deduplicate them.

- [ ] **Step 4: Run the parser tests and verify they pass**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_web_ui_config.py -k message_templates -q
```

Expected: PASS for all message-template parser tests.

- [ ] **Step 5: Write the failing `/config` serialization test**

Add this test to `tests/test_runtime_config_limits.py`:

```python
def test_config_endpoint_exposes_backend_validated_message_templates(monkeypatch):
    class FakeService:
        active_backend = "sglang"
        is_ready = True

    current = ConfigLoader().load(reload=True)
    replacement = replace(
        current,
        web_ui=replace(
            current.web_ui,
            message_templates=["first", "second\nline"],
        ),
    )
    monkeypatch.setattr("backend.main.reload_config", lambda: replacement)
    monkeypatch.setattr("backend.main.get_llm_service", lambda: FakeService())

    response = TestClient(app).get("/config")

    assert response.status_code == 200
    assert response.json()["webUI"]["components"]["message_templates"] == {
        "items": ["first", "second\nline"],
    }
```

- [ ] **Step 6: Run the endpoint test and verify it fails**

Run:

```bash
./.sglang/bin/python -m pytest tests/test_runtime_config_limits.py::test_config_endpoint_exposes_backend_validated_message_templates -q
```

Expected: FAIL because the serializer does not expose the new component.

- [ ] **Step 7: Serialize the backend field and add YAML defaults**

Extend `_serialize_web_ui_components()` in `backend/main.py`:

```python
"message_templates": {
    "items": current_config.web_ui.message_templates,
},
```

Add the YAML source in `config/config-webUI.yaml` under `webUI.components`, with comments explaining that the list is the first-load/default source and browser edits do not write back:

```yaml
    message_templates:
      # 首次加载或用户选择“恢复默认”时使用；浏览器编辑不会回写此文件。
      items:
        - "Hello, what is your name?"
        - "Tell me the weather forecast for Changsha."
```

- [ ] **Step 8: Run all backend contract tests**

Run:

```bash
./.sglang/bin/python -m pytest \
  tests/test_web_ui_config.py \
  tests/test_runtime_config_limits.py::test_config_endpoint_exposes_backend_validated_message_templates \
  -q
```

Expected: PASS, with no changes to unrelated Web UI defaults.

## Task 2: Implement typed frontend config and cache precedence helpers

**Files:**

- Modify: `frontend/src/lib/api.ts:475-522`
- Modify: `frontend/src/lib/web-ui-config.ts:1-55,83-95`
- Modify: `frontend/src/lib/web-ui-config.test.mts`

**Interfaces:**

- Consumes: `ConfigResponse.webUI` from Task 1 and the existing localStorage helper conventions.
- Produces: `MESSAGE_TEMPLATES_STORAGE_KEY`.
- Produces: `MESSAGE_TEMPLATE_IMMEDIATE_SEND_STORAGE_KEY` plus boolean read/write helpers that never alter the template-array key.
- Produces: `WebUIMessageTemplateStorageState` with `absent`, `valid`, `invalid`, and `unavailable` states.
- Produces: `getWebUIMessageTemplateDefaults(config?: WebUIConfig): string[]`.
- Produces: `readStoredWebUIMessageTemplates(storage?): WebUIMessageTemplateStorageState`.
- Produces: `resolveWebUIMessageTemplates(yamlDefaults, stored): string[]`.
- Produces: `writeStoredWebUIMessageTemplates(items, storage?): boolean`.
- Produces: `clearStoredWebUIMessageTemplates(storage?): boolean`.

- [ ] **Step 1: Add failing tests for the complete cache-state matrix**

Extend `frontend/src/lib/web-ui-config.test.mts` with a small in-memory storage and tests for the exact precedence contract:

```ts
function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
    values,
  };
}

const yamlDefaults = ["yaml one", "yaml two"];

test("message templates use YAML when the browser key is absent", () => {
  const storage = memoryStorage();
  const stored = readStoredWebUIMessageTemplates(storage);
  assert.equal(stored.kind, "absent");
  assert.deepEqual(resolveWebUIMessageTemplates(yamlDefaults, stored), yamlDefaults);
});

test("valid non-empty cache replaces the complete YAML list", () => {
  const storage = memoryStorage({
    [MESSAGE_TEMPLATES_STORAGE_KEY]: JSON.stringify(["browser only"]),
  });
  const stored = readStoredWebUIMessageTemplates(storage);
  assert.deepEqual(stored, { kind: "valid", items: ["browser only"] });
  assert.deepEqual(resolveWebUIMessageTemplates(yamlDefaults, stored), ["browser only"]);
});

test("valid empty cache intentionally hides YAML templates", () => {
  const storage = memoryStorage({ [MESSAGE_TEMPLATES_STORAGE_KEY]: "[]" });
  const stored = readStoredWebUIMessageTemplates(storage);
  assert.deepEqual(stored, { kind: "valid", items: [] });
  assert.deepEqual(resolveWebUIMessageTemplates(yamlDefaults, stored), []);
});

test("invalid JSON, object, and mixed arrays fall back without rewriting cache", () => {
  for (const encoded of ["{\"items\":[]}", "not-json", "[\"ok\", 4]"]) {
    const storage = memoryStorage({ [MESSAGE_TEMPLATES_STORAGE_KEY]: encoded });
    const stored = readStoredWebUIMessageTemplates(storage);
    assert.equal(stored.kind, "invalid");
    assert.deepEqual(resolveWebUIMessageTemplates(yamlDefaults, stored), yamlDefaults);
    assert.equal(storage.values.get(MESSAGE_TEMPLATES_STORAGE_KEY), encoded);
  }
});

test("message template persistence writes and clears only the dedicated key", () => {
  const storage = memoryStorage({ "llm-chat-settings": "keep" });
  assert.equal(
    writeStoredWebUIMessageTemplates(["first", "second\nline"], storage),
    true,
  );
  assert.equal(
    storage.values.get(MESSAGE_TEMPLATES_STORAGE_KEY),
    JSON.stringify(["first", "second\nline"]),
  );
  assert.equal(storage.values.get("llm-chat-settings"), "keep");
  assert.equal(clearStoredWebUIMessageTemplates(storage), true);
  assert.equal(storage.values.has(MESSAGE_TEMPLATES_STORAGE_KEY), false);
});

test("storage exceptions return safe states instead of escaping", () => {
  const throwingStorage = {
    getItem: () => { throw new Error("blocked"); },
    setItem: () => { throw new Error("blocked"); },
    removeItem: () => { throw new Error("blocked"); },
  };
  assert.equal(readStoredWebUIMessageTemplates(throwingStorage).kind, "unavailable");
  assert.equal(writeStoredWebUIMessageTemplates(["item"], throwingStorage), false);
  assert.equal(clearStoredWebUIMessageTemplates(throwingStorage), false);
});
```

Also test `getWebUIMessageTemplateDefaults()` with missing config and with `components.message_templates.items` containing blank/non-string values.

- [ ] **Step 2: Run the helper tests and confirm failure**

Run from `frontend/`:

```bash
node --test --experimental-strip-types src/lib/web-ui-config.test.mts
```

Expected: FAIL because the new exports do not exist.

- [ ] **Step 3: Extend the frontend config types**

In `frontend/src/lib/api.ts`, add the response shape under `webUI.components`:

```ts
message_templates?: {
  items?: string[];
};
```

In `frontend/src/lib/web-ui-config.ts`, add the same shape to `WebUIConfig.components` and define the storage contract:

```ts
export interface WebUIMessageTemplateStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export const MESSAGE_TEMPLATES_STORAGE_KEY = "llm-message-templates";

export type WebUIMessageTemplateStorageState =
  | { kind: "absent"; items: null }
  | { kind: "valid"; items: string[] }
  | { kind: "invalid"; items: null }
  | { kind: "unavailable"; items: null };
```

Reuse the existing browser-storage fallback convention, but include `removeItem` in the storage type. Do not read `window` at module evaluation time.

- [ ] **Step 4: Implement pure normalization and precedence functions**

Add the following functions in `frontend/src/lib/web-ui-config.ts`:

```ts
export function normalizeWebUIMessageTemplateItems(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  if (!value.every((item) => typeof item === "string" && item.trim().length > 0)) {
    return null;
  }
  return value.slice() as string[];
}

export function getWebUIMessageTemplateDefaults(config?: WebUIConfig): string[] {
  return normalizeWebUIMessageTemplateItems(
    config?.components?.message_templates?.items ?? [],
  ) ?? [];
}

export function resolveWebUIMessageTemplates(
  yamlDefaults: readonly string[],
  stored: WebUIMessageTemplateStorageState,
): string[] {
  return stored.kind === "valid" ? stored.items.slice() : Array.from(yamlDefaults);
}
```

The normalizer must return `[]` for a valid empty array, `null` for a malformed array, and must preserve each non-empty string exactly.

- [ ] **Step 5: Implement safe storage read/write/clear helpers**

Use `window.localStorage` only through a `getDefaultWebUIStorage()` helper. Implement these exact behaviors:

```ts
export function readStoredWebUIMessageTemplates(
  storage: WebUIMessageTemplateStorage | undefined = getDefaultWebUIStorage(),
): WebUIMessageTemplateStorageState {
  if (!storage) return { kind: "unavailable", items: null };
  try {
    const raw = storage.getItem(MESSAGE_TEMPLATES_STORAGE_KEY);
    if (raw === null) return { kind: "absent", items: null };
    const parsed = JSON.parse(raw);
    const items = normalizeWebUIMessageTemplateItems(parsed);
    return items === null
      ? { kind: "invalid", items: null }
      : { kind: "valid", items };
  } catch {
    return { kind: "unavailable", items: null };
  }
}

export function writeStoredWebUIMessageTemplates(
  items: readonly string[],
  storage: WebUIMessageTemplateStorage | undefined = getDefaultWebUIStorage(),
): boolean {
  if (!storage) return false;
  try {
    storage.setItem(MESSAGE_TEMPLATES_STORAGE_KEY, JSON.stringify(Array.from(items)));
    return true;
  } catch {
    return false;
  }
}

export function clearStoredWebUIMessageTemplates(
  storage: WebUIMessageTemplateStorage | undefined = getDefaultWebUIStorage(),
): boolean {
  if (!storage) return false;
  try {
    storage.removeItem(MESSAGE_TEMPLATES_STORAGE_KEY);
    return true;
  } catch {
    return false;
  }
}
```

Treat a missing browser storage object as `unavailable`, while page code may still use YAML defaults in that state. Do not silently remove malformed cache data.

- [ ] **Step 6: Run helper tests and type-check the frontend**

Run:

```bash
node --test --experimental-strip-types src/lib/web-ui-config.test.mts
npm run type-check
```

Expected: all helper tests PASS and TypeScript reports no new errors.

## Task 3: Build and test the shared `MessageTemplateMenu` component

**Files:**

- Create: `frontend/src/components/MessageTemplateMenu.tsx`
- Create: `frontend/src/components/MessageTemplateMenu.test.mjs`
- Consume only: `frontend/src/lib/web-ui-config.ts` helpers from Task 2; keep panel/editor state inside the component and the immediate-send state in each page-level owner.

**Interfaces:**

- Consumes: `templates`, `yamlDefaults`, and persistence callbacks from Task 2/page wiring.
- Produces: a reusable action-bar trigger and expanded panel with stable `data-testid` hooks.
- Required props:

```ts
export interface MessageTemplateMenuProps {
  actionBarVisible: boolean;
  templates: readonly string[];
  yamlDefaults: readonly string[];
  onTemplatesChange: (next: string[]) => void;
  onRestoreDefaults: () => void;
  onInsertTemplate: (text: string) => void;
  onSendTemplate: (text: string) => void | Promise<void>;
  immediateSend: boolean;
  onImmediateSendChange: (enabled: boolean) => void;
  sendDisabled?: boolean;
  storageError?: string | null;
  onDismissStorageError?: () => void;
}
```

- [ ] **Step 1: Write failing rendered-component tests**

Create `frontend/src/components/MessageTemplateMenu.test.mjs` using `createRenderer` and `installTask6TypeScriptHook` from `frontend/src/lib/vision-automation/react-component-renderer.cjs`. Cover these interactions:

Define the test fixture before the tests so every test uses the same complete prop contract:

```js
import assert from "node:assert/strict";
import path from "node:path";
import { createRequire } from "node:module";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createRenderer, installTask6TypeScriptHook } from "../lib/vision-automation/react-component-renderer.cjs";

const require = createRequire(import.meta.url);
const testDir = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(testDir, "../..");
installTask6TypeScriptHook(frontendRoot);
const React = require("react");
const MessageTemplateMenu = require("./MessageTemplateMenu.tsx").default;

function renderMenu(overrides = {}) {
  return createRenderer(React.createElement(MessageTemplateMenu, {
    actionBarVisible: true,
    templates: ["default template"],
    yamlDefaults: ["default template"],
    onTemplatesChange: () => undefined,
    onRestoreDefaults: () => undefined,
    onInsertTemplate: () => undefined,
    onSendTemplate: () => undefined,
    sendDisabled: false,
    ...overrides,
  }));
}
```

Then cover these interactions:

```js
test("trigger opens the panel with one immediate-send switch defaulting off", () => {
  const inserts = [];
  const renderer = renderMenu({
    templates: ["Hello", "Long\nmessage"],
    onInsertTemplate: (value) => inserts.push(value),
  });
  renderer.click(renderer.findOne((node) => node.props["data-testid"] === "message-template-trigger"));
  assert.ok(renderer.findOne((node) => node.props["data-testid"] === "message-template-panel"));
  const switches = renderer.findAll((node) => node.props.role === "switch");
  assert.equal(switches.length, 1);
  assert.equal(switches[0].props["aria-checked"], false);
  renderer.click(renderer.findOne((node) => node.props["data-testid"] === "message-template-item-0"));
  assert.deepEqual(inserts, ["Hello"]);
  renderer.unmount();
});

test("immediate-send switch toggles between send and insert behavior", () => {
  const sent = [];
  const renderer = renderMenu({
    templates: ["Send me"],
    onSendTemplate: (value) => sent.push(value),
  });
  renderer.click(renderer.findOne((node) => node.props["data-testid"] === "message-template-trigger"));
  const immediate = renderer.findOne((node) => node.props.role === "switch");
  renderer.click(immediate);
  assert.equal(renderer.findOne((node) => node.props.role === "switch").props["aria-checked"], true);
  renderer.click(renderer.findOne((node) => node.props["data-testid"] === "message-template-item-0"));
  assert.deepEqual(sent, ["Send me"]);
  renderer.unmount();
});
```

Add tests for editing, cancelling an edit, deleting after confirmation, adding a non-blank item, rejecting a blank save, restoring defaults, not invoking select when edit/delete controls are clicked, and preserving the complete long text only in `aria-label` while the visible label remains truncated.

Mock `globalThis.confirm` to return `true` only in the delete/restore tests; restore the original value in each test.

- [ ] **Step 2: Run the component tests and confirm failure**

Run from `frontend/`:

```bash
node --test --experimental-strip-types src/components/MessageTemplateMenu.test.mjs
```

Expected: FAIL because the component file and stable test IDs do not exist.

- [ ] **Step 3: Implement the component shell and visibility lifecycle**

Implement `MessageTemplateMenu` as a client component. Use local state only for its panel/editor lifecycle:

```ts
const [panelOpen, setPanelOpen] = useState(false);
const [editingIndex, setEditingIndex] = useState<number | "new" | null>(null);
const [draft, setDraft] = useState("");
```

When `actionBarVisible` becomes false, close the panel and discard any active draft. The trigger must have `data-testid="message-template-trigger"`, `aria-expanded`, `aria-label="消息模板"`, and `title="消息模板"`. The panel must have `data-testid="message-template-panel"` and render only while both `actionBarVisible` and `panelOpen` are true.

Return the trigger and panel as sibling cells of the caller's action grid. The trigger occupies one cell and the panel uses `col-span-full`; do not use `display: contents` or add a second independent toolbar state.

- [ ] **Step 4: Implement one controlled immediate-send ToggleSwitch**

Reuse `frontend/src/components/ToggleSwitch.tsx` and render one switch:

```tsx
<ToggleSwitch
  checked={immediateSend}
  onChange={onImmediateSendChange}
  label="选中模板后立即发送"
/>
```

The component receives `immediateSend` and `onImmediateSendChange` from the page. Off selects `onInsertTemplate`; on selects `onSendTemplate`. Both pages own the state, restore it with the shared safe reader, and persist changes with the independent boolean key.

- [ ] **Step 5: Implement item selection, add, edit, delete, and persistence callbacks**

For each template, render a row with:

- main button `data-testid="message-template-item-${index}"`;
- edit button `data-testid="message-template-edit-${index}"`;
- delete button `data-testid="message-template-delete-${index}"`.

Use this selection logic:

```ts
const selectTemplate = (text: string) => {
  if (immediateSend) {
    if (!sendDisabled) void onSendTemplate(text);
    return;
  }
  onInsertTemplate(text);
};
```

The main button is disabled when `sendDisabled` is true only for the immediate-send path; the insert path remains usable unless the parent explicitly disables the whole action bar. Edit/delete handlers must call `event.stopPropagation()` before mutating state.

For save operations, reject `draft.trim().length === 0`, preserve the original non-empty string exactly, append new items, replace edited items by index, call `onTemplatesChange(next)`, and clear the editor. For delete, call `confirm("删除此消息模板？")`; only a confirmed deletion calls `onTemplatesChange(next)`.

Render “新增” with `data-testid="message-template-add"`. Render “恢复默认” with `data-testid="message-template-restore"`; clicking it asks for confirmation and calls `onRestoreDefaults()` without directly mutating the list. Render `storageError` as a non-blocking message with a dismiss control when supplied.

- [ ] **Step 6: Implement truncated visual labels and full accessible names**

Use a single-line `truncate` span for the visual label and `aria-label={template}` for the exact template string. Do not add `role="tooltip"`, `aria-describedby`, or a full-text `title` to template items. Use distinct buttons and `type="button"` for every non-submit control.

- [ ] **Step 7: Run component tests and verify behavior**

Run:

```bash
node --test --experimental-strip-types src/components/MessageTemplateMenu.test.mjs
```

Expected: PASS for insertion, immediate send, editing, adding, deletion, restore, switch behavior, event isolation, and full-text `aria-label` coverage.

## Task 4: Wire the shared helpers and component into the text page

**Files:**

- Modify: `frontend/src/app/page.tsx:164-165,433-495,2180-2265`
- Create: `frontend/src/lib/message-template-pages.test.mjs`

**Interfaces:**

- Consumes: Task 2 storage helpers and Task 3 `MessageTemplateMenuProps`.
- Produces: text-page YAML defaults and effective template state.
- Produces: `onInsertTemplate` that replaces input and focuses `inputRef`.
- Produces: `onSendTemplate` that calls the existing `handleSend(text)` callback exactly once.

- [ ] **Step 1: Write a failing text-page integration contract test**

Create `frontend/src/lib/message-template-pages.test.mjs` and assert the text page source contains the shared component, the config-derived default helper, the effective-list resolver, and both callbacks, while not introducing a second localStorage key:

```js
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";

const root = path.resolve(new URL("..", import.meta.url).pathname, "..");

test("text page wires the shared message-template component and cache resolver", async () => {
  const source = await readFile(path.join(root, "app/page.tsx"), "utf8");
  assert.match(source, /MessageTemplateMenu/);
  assert.match(source, /getWebUIMessageTemplateDefaults/);
  assert.match(source, /resolveWebUIMessageTemplates/);
  assert.match(source, /onInsertTemplate/);
  assert.match(source, /onSendTemplate/);
  assert.doesNotMatch(source, /llm-message-templates\s*['"]/);
});
```

Add a focused assertion that the text page’s insert callback calls `setInput` and that immediate send uses `handleSend`, not a direct `fetch`.

- [ ] **Step 2: Run the source contract test and confirm failure**

Run from `frontend/`:

```bash
node --test --experimental-strip-types src/lib/message-template-pages.test.mjs
```

Expected: FAIL because the page does not import or mount the new component.

- [ ] **Step 3: Add text-page template state and imports**

Import the shared helpers and component, then add two states beside `webUIConfig`:

```tsx
const [yamlMessageTemplates, setYamlMessageTemplates] = useState<string[]>([]);
const [messageTemplates, setMessageTemplates] = useState<string[]>([]);
const [messageTemplateImmediateSend, setMessageTemplateImmediateSend] = useState(false);
const [messageTemplateStorageError, setMessageTemplateStorageError] = useState<string | null>(null);
```

The YAML state is the current `/config` default list; the effective state is the list rendered and mutated by the component.

- [ ] **Step 4: Resolve templates in the existing config-load path**

In `fetchModelInfo`, immediately after applying the received Web UI config, resolve templates from the same response:

```tsx
const yamlTemplates = getWebUIMessageTemplateDefaults(configResponse.webUI);
const storedTemplates = readStoredWebUIMessageTemplates();
setYamlMessageTemplates(yamlTemplates);
setMessageTemplates(resolveWebUIMessageTemplates(yamlTemplates, storedTemplates));
setMessageTemplateImmediateSend(readStoredWebUIMessageTemplateImmediateSend());
```

Do not write YAML values to localStorage during this load. Do not put template values into `settings` or `llm-chat-settings`.

- [ ] **Step 5: Add text-page persistence callbacks**

Add callbacks with explicit storage failure handling:

```tsx
const handleMessageTemplatesChange = useCallback((next: string[]) => {
  setMessageTemplates(next);
  if (!writeStoredWebUIMessageTemplates(next)) {
    setMessageTemplateStorageError("消息模板无法保存到浏览器缓存");
  } else {
    setMessageTemplateStorageError(null);
  }
}, []);

const handleRestoreMessageTemplates = useCallback(() => {
  if (!clearStoredWebUIMessageTemplates()) {
    setMessageTemplateStorageError("无法清除消息模板浏览器缓存");
    return;
  }
  setMessageTemplates(yamlMessageTemplates.slice());
  setMessageTemplateStorageError(null);
}, [yamlMessageTemplates]);

const handleInsertMessageTemplate = useCallback((text: string) => {
  setInput(text);
  requestAnimationFrame(() => inputRef.current?.focus());
}, []);

const handleSendMessageTemplate = useCallback((text: string) => {
  void handleSend(text);
}, [handleSend]);
```

Keep `handleSend` as the sole text-page send pipeline. Do not add a template-specific request body or bypass its loading/queue/session handling.

- [ ] **Step 6: Mount the component in the expanded action area**

Change the existing text action area to `grid grid-cols-2 gap-2 sm:grid-cols-4` and add `MessageTemplateMenu` directly as the fourth equal-width cell. Its panel remains a `col-span-full` sibling before cycle-specific content:

```tsx
<MessageTemplateMenu
  actionBarVisible={showInputActions}
  templates={messageTemplates}
  yamlDefaults={yamlMessageTemplates}
  onTemplatesChange={handleMessageTemplatesChange}
  onRestoreDefaults={handleRestoreMessageTemplates}
  onInsertTemplate={handleInsertMessageTemplate}
  onSendTemplate={handleSendMessageTemplate}
  immediateSend={messageTemplateImmediateSend}
  onImmediateSendChange={handleMessageTemplateImmediateSendChange}
  sendDisabled={isLoading || connectionStatus === 'error' || cycleStatus === 'running'}
  storageError={messageTemplateStorageError}
  onDismissStorageError={() => setMessageTemplateStorageError(null)}
/>
```

The component must remain absent when `showInputActions` is false through its `actionBarVisible` prop. Keep the existing `+` button and other action behavior unchanged.

- [ ] **Step 7: Run text integration checks**

Run:

```bash
node --test --experimental-strip-types src/lib/message-template-pages.test.mjs
npm run type-check
```

Expected: PASS, with no dependency-array or prop-type errors.

## Task 5: Wire the shared feature into vision mode without disturbing image behavior

**Files:**

- Modify: `frontend/src/components/ImageUploader.tsx:29-49,251-399`
- Modify: `frontend/src/app/vision/page.tsx:312-330,719-803,3282-3310`
- Modify: `frontend/src/lib/message-template-pages.test.mjs`

**Interfaces:**

- Consumes: Task 2 helpers, Task 3 component, existing `ImageUploader` `showActionButtons` boundary, and vision `handleSend(text)` callback.
- Produces: vision-page effective template state using the same `llm-message-templates` key.
- Produces: an `ImageUploader` `messageTemplateMenu?: React.ReactNode` slot rendered directly inside the visible five-column action grid.
- Preserves: image upload, camera, automation, cycle, pending-acceptance, and existing vision send lifecycle behavior.

- [ ] **Step 1: Extend the vision/page source test before changing vision code**

Add these assertions to `frontend/src/lib/message-template-pages.test.mjs`:

```js
test("vision page wires the same message-template helpers and passes a toolbar slot", async () => {
  const vision = await readFile(path.join(root, "app/vision/page.tsx"), "utf8");
  const uploader = await readFile(path.join(root, "components/ImageUploader.tsx"), "utf8");
  for (const name of [
    "MessageTemplateMenu",
    "getWebUIMessageTemplateDefaults",
    "readStoredWebUIMessageTemplates",
    "resolveWebUIMessageTemplates",
    "onInsertTemplate",
    "onSendTemplate",
  ]) {
    assert.match(vision, new RegExp(name));
  }
  assert.match(vision, /messageTemplateMenu/);
  assert.match(uploader, /messageTemplateMenu/);
});
```

- [ ] **Step 2: Run the page synchronization test and verify the new assertions fail**

Run from `frontend/`:

```bash
node --test --experimental-strip-types src/lib/message-template-pages.test.mjs
```

Expected: FAIL on the vision-specific assertions.

- [ ] **Step 3: Add an `ImageUploader` toolbar slot**

Extend `ImageUploaderProps` with:

```ts
messageTemplateMenu?: React.ReactNode;
```

Destructure it in the component, change the grid to `grid grid-cols-2 gap-2 sm:grid-cols-5`, and render the node directly in that grid after the four existing actions:

```tsx
{messageTemplateMenu}
```

Do not add message-template state, storage access, or send logic to `ImageUploader`; it only provides the fifth grid cell. Leave its camera-stream cleanup effect and action-state calculation unchanged.

- [ ] **Step 4: Add vision-page state and config resolution**

Import the shared component and helpers, then add the same three template states as text mode. In `fetchConfig`, after applying `configResponse.webUI`, resolve the YAML/default/cache list exactly once:

```tsx
const yamlTemplates = getWebUIMessageTemplateDefaults(configResponse.webUI);
const storedTemplates = readStoredWebUIMessageTemplates();
setYamlMessageTemplates(yamlTemplates);
setMessageTemplates(resolveWebUIMessageTemplates(yamlTemplates, storedTemplates));
setMessageTemplateImmediateSend(readStoredWebUIMessageTemplateImmediateSend());
```

Use the same persistence callback semantics as Task 4. The callbacks must call `setInput(text)` and focus `inputRef` for insertion, and call `void handleSend(text)` for immediate send. The vision `handleSend` callback already accepts explicit text and retains current images, so do not duplicate its image staging or Vision Automation admission logic.

- [ ] **Step 5: Pass the shared menu into the uploader action grid**

Create the node in `vision/page.tsx`:

```tsx
const messageTemplateMenu = (
  <MessageTemplateMenu
    actionBarVisible={showInputActions}
    templates={messageTemplates}
    yamlDefaults={yamlMessageTemplates}
    onTemplatesChange={handleMessageTemplatesChange}
    onRestoreDefaults={handleRestoreMessageTemplates}
    onInsertTemplate={handleInsertMessageTemplate}
    onSendTemplate={handleSendMessageTemplate}
    immediateSend={messageTemplateImmediateSend}
    onImmediateSendChange={handleMessageTemplateImmediateSendChange}
    sendDisabled={visionRequestBusy || connectionStatus === 'error' || cycleStatus === 'running'}
    storageError={messageTemplateStorageError}
    onDismissStorageError={() => setMessageTemplateStorageError(null)}
  />
);
```

Pass it to `ImageUploader`:

```tsx
<ImageUploader
  ...
  showActionButtons={showInputActions}
  messageTemplateMenu={messageTemplateMenu}
  ...
/>
```

Keep the current `+` button as the single owner of `showInputActions`. When it closes, `ImageUploader` must hide the slot and `MessageTemplateMenu` must discard unsaved draft state through `actionBarVisible={false}`.

- [ ] **Step 6: Run vision synchronization and type checks**

Run:

```bash
node --test --experimental-strip-types src/lib/message-template-pages.test.mjs
npm run type-check
```

Expected: PASS. Existing `ImageUploader` props and vision automation compilation must remain valid.

## Task 6: Add cross-layer regression coverage and complete verification

**Files:**

- Modify: `frontend/src/lib/web-ui-config.test.mts`
- Modify: `frontend/src/components/MessageTemplateMenu.test.mjs`
- Modify: `frontend/src/lib/message-template-pages.test.mjs`
- Modify: `tests/test_web_ui_config.py`
- Modify: `tests/test_runtime_config_limits.py`

**Interfaces:**

- Consumes: all Task 1–5 contracts.
- Produces: regression evidence for cache precedence, UI behavior, page parity, and unchanged backend/LLM boundaries.

- [ ] **Step 1: Add exact persistence-sequence tests**

In the component test, verify that the callbacks receive complete lists rather than deltas:

```js
test("add, edit, and delete each emit the complete next list", () => {
  const changes = [];
  const renderer = renderMenu({
    templates: ["one", "two"],
    onTemplatesChange: (next) => changes.push(next),
  });
  renderer.click(renderer.findOne((node) => node.props["data-testid"] === "message-template-trigger"));

  renderer.click(renderer.findOne((node) => node.props["data-testid"] === "message-template-add"));
  const addEditor = renderer.findOne((node) => node.type === "textarea");
  renderer.act(() => addEditor.props.onChange({ target: { value: "three" } }));
  renderer.click(renderer.findOne((node) => node.props["data-testid"] === "message-template-save-new"));

  const edit = renderer.findOne((node) => node.props["data-testid"] === "message-template-edit-0");
  renderer.click(edit);
  const existingEditor = renderer.findOne((node) => node.type === "textarea");
  renderer.act(() => existingEditor.props.onChange({ target: { value: "ONE" } }));
  renderer.click(renderer.findOne((node) => node.props["data-testid"] === "message-template-save-0"));

  const previousConfirm = globalThis.confirm;
  try {
    globalThis.confirm = () => true;
    renderer.click(renderer.findOne((node) => node.props["data-testid"] === "message-template-delete-1"));
  } finally {
    globalThis.confirm = previousConfirm;
  }
  assert.deepEqual(changes, [["one", "two", "three"], ["ONE", "two", "three"], ["ONE", "three"]]);
  renderer.unmount();
});
```

Use a fresh renderer or reset `confirm` around the test so global state cannot leak into other tests.

- [ ] **Step 2: Add a no-prompt-boundary source regression assertion**

Extend `message-template-pages.test.mjs` to ensure neither page adds a template-specific network path and the shared component does not import or call an LLM client:

```js
test("template page wiring does not introduce a template-specific network path", async () => {
  const component = await readFile(path.join(root, "components/MessageTemplateMenu.tsx"), "utf8");
  assert.doesNotMatch(component, /llmClient\./);
  for (const relative of ["app/page.tsx", "app/vision/page.tsx"]) {
    const source = await readFile(path.join(root, relative), "utf8");
    assert.match(source, /onSendTemplate/);
    assert.match(source, /handleSend/);
  }
});
```

- [ ] **Step 3: Run the focused complete test set**

Run backend tests:

```bash
cd ~/Project/LLM-deployment
./.sglang/bin/python -m pytest \
  tests/test_web_ui_config.py \
  tests/test_runtime_config_limits.py \
  -q
```

Run frontend tests:

```bash
cd ~/Project/LLM-deployment/frontend
node --test --experimental-strip-types \
  src/lib/web-ui-config.test.mts \
  src/components/MessageTemplateMenu.test.mjs \
  src/lib/message-template-pages.test.mjs
npm run type-check
```

Expected: all focused tests PASS and type-check completes successfully.

- [ ] **Step 4: Run existing adjacent frontend regressions**

Run:

```bash
cd ~/Project/LLM-deployment/frontend
node --test --experimental-strip-types \
  src/lib/chat-input-alignment.test.mjs \
  src/lib/chat-input-keyboard.test.mts \
  src/lib/voice-input-pages.test.mts \
  src/lib/vision-automation/components-rendered.test.mjs
```

Expected: existing input, voice, vision, and uploader tests remain green.

- [ ] **Step 5: Run the production build verification**

Run:

```bash
cd ~/Project/LLM-deployment/frontend
npm run build:verify
```

Expected: Next.js production build succeeds with both `/` and `/vision` compiling.

- [ ] **Step 6: Perform manual acceptance checks**

With the frontend and backend running, verify in both `/` and `/vision`:

1. Click `+`, open “消息模板”, and see YAML examples on a browser with no `llm-message-templates` key.
2. Confirm “选中模板后立即发送” is off by default.
3. Select a long template and confirm the exact text replaces the input, focus returns to the input, and no request starts.
4. Edit the input and send through the existing Send button; verify the sent user message is ordinary Chat/Agent/Vision input.
5. Switch to “选中模板后立即发送”; select once and verify exactly one existing send request is made.
6. Add, edit, and delete items; inspect localStorage and verify only `llm-message-templates` changes, while `llm-chat-settings` remains unchanged.
7. Reload and confirm the browser list overrides YAML, including an empty cached list.
8. Click “恢复默认”, confirm the action, reload, and verify the current YAML list is restored.
9. Verify a long template remains visually truncated, exposes the exact full text through `aria-label`, and has no custom or native full-text tooltip.
10. In vision mode, select an image, choose a template in insertion mode, and confirm the image remains; immediate send must follow the existing image send behavior.
11. Close `+` during an unsaved edit and confirm the draft is discarded and not stored.

## Completion Handoff

After all tasks and verification pass, report the plan execution choice to the user. Do not run `git status`, `git diff`, `git add`, `git commit`, or any other git command unless the user separately authorizes it under the repository’s `AGENTS.md` rule.
