# Web UI Skin Switch And Agent Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Repository-local instructions prohibit git commands without explicit user approval, so this plan intentionally omits commit steps.

**Goal:** Add a robust localStorage-backed skin dropdown to Web UI settings and disable the Web UI system prompt field when Agent mode is enabled.

**Architecture:** Keep backend `/config` as the server default source and apply a validated frontend skin override at render time. Add focused helpers in `frontend/src/lib/web-ui-config.ts`, then wire the same settings UI behavior into both independent pages.

**Tech Stack:** Next.js 14, React 18, TypeScript, Node test runner.

---

### Task 1: Skin Registry And Helper Tests

**Files:**
- Modify: `frontend/src/lib/web-ui-config.test.mts`
- Modify: `frontend/src/lib/web-ui-config.ts`

- [ ] **Step 1: Add failing helper tests**

Add tests that assert known IDs normalize to themselves, unknown IDs fallback to `default-v1`, `applySkinOverride` changes only effective skin metadata, and current skins do not require reload.

- [ ] **Step 2: Run helper tests and verify failure**

Run: `node --test "src/lib/web-ui-config.test.mts"`

Expected: FAIL because registry/helper exports do not exist yet.

- [ ] **Step 3: Implement helper exports**

Add `DEFAULT_WEBUI_SKIN_ID`, `WEBUI_SKINS`, `WEBUI_SKIN_STORAGE_KEY`, `normalizeWebUISkinId`, `getWebUISkin`, `readStoredWebUISkinId`, `writeStoredWebUISkinId`, and `applyWebUISkinOverride`.

- [ ] **Step 4: Run helper tests and verify pass**

Run: `node --test "src/lib/web-ui-config.test.mts"`

Expected: PASS.

### Task 2: Text Page Settings Wiring

**Files:**
- Modify: `frontend/src/app/page.tsx`

- [ ] **Step 1: Add state and effective config flow**

Track `selectedSkinId` and `pendingSkinId`. After `/config`, normalize any stored skin ID, apply it to `webUIConfig`, and render from the effective config.

- [ ] **Step 2: Add settings dropdown and confirm/cancel behavior**

Opening settings copies the current selected skin into `pendingSkinId`; cancel discards it; confirm normalizes, stores, applies, and optionally prompts reload when `requiresReload` is true.

- [ ] **Step 3: Disable system prompt while pending Agent mode is on**

Disable the textarea, add disabled styling, and show explanatory text.

### Task 3: Vision Page Settings Wiring

**Files:**
- Modify: `frontend/src/app/vision/page.tsx`

- [ ] **Step 1: Mirror text page skin state and config flow**

Use the same helpers and behavior as `page.tsx`.

- [ ] **Step 2: Mirror dropdown, confirm/cancel, reload prompt, and disabled system prompt UI**

Keep the two pages in sync per project guidance.

### Task 4: Verification

**Files:**
- Test only.

- [ ] **Step 1: Run frontend helper tests**

Run: `node --test "src/lib/web-ui-config.test.mts"`

Expected: PASS.

- [ ] **Step 2: Run retry and Next distDir tests**

Run: `node --test "src/lib/retry-dynamic-import.test.mts"`

Run: `node --test "src/lib/next-config-dist-dir.test.mjs"`

Expected: PASS.

- [ ] **Step 3: Run TypeScript verification**

Run: `npm run type-check`

Expected: PASS.

- [ ] **Step 4: Run isolated build verification**

Run: `npm run build:verify`

Expected: PASS. Do not run `npm run build`.

## Self-Review

- Spec coverage: skin dropdown, localStorage persistence, robust fallback, refresh prompt hook, both pages, and Agent-mode prompt non-applicability are covered.
- Placeholder scan: no implementation placeholders remain; tasks point to exact files and commands.
- Type consistency: helper names are stable across tasks and use the existing `WebUIConfig` type.
