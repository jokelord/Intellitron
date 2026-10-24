# Web UI Skin Switch And Agent Settings Design

Date: 2026-06-23

## Goal

Add two settings-panel behavior changes to both Web UI pages:

1. Let users switch existing Web UI skins from a dropdown and persist the choice in `localStorage`.
2. Make the Web UI system prompt field not applicable while Agent mode is enabled, because agent prompts are initialized by backend agent configuration rather than user Web UI chat settings.

## Scope

This change affects both independent frontend pages:

- Text mode: `frontend/src/app/page.tsx`
- Vision mode: `frontend/src/app/vision/page.tsx`

The backend remains the source of server defaults through `/config`, but user skin choice is a per-browser frontend preference. No backend write-config endpoint is introduced.

## Skin Registry

The frontend will define a small explicit registry for supported skins:

- `default-v1`: normal existing UI skin.
- `aurora-glow-r3f-v1`: WebGL/R3F aurora glow skin.

Each registry entry includes:

- `id`
- display `label`
- optional short `description`
- `requiresReload`, defaulting to `false`

Current supported skins should switch live without a browser refresh. The `requiresReload` flag exists so future skins can request a refresh prompt if they cannot be safely hot-swapped.

## Persistence And Robust Fallback

The selected skin is stored in a dedicated `localStorage` key, for example `llm-webui-skin-id`. It is intentionally separate from `llm-chat-settings` so old chat setting payloads cannot corrupt UI skin choice.

The frontend must treat `localStorage` as untrusted input:

- Missing value falls back to `default-v1`.
- Empty or non-string-like values fall back to `default-v1`.
- Unknown, removed, or stale skin IDs fall back to `default-v1`.
- Parse/storage errors fall back to `default-v1` and must not break page render.

The fallback target is the frontend program default skin, not the unknown backend value and not the stale local value.

## Config Merge

`/config` continues to provide `webUI.skin`, `webUI.components`, and `webUI.effects`. The frontend applies the selected skin as a local override on top of the fetched config before rendering skin-dependent UI.

The aurora glow effect still loads only when:

- effective `skin.id === "aurora-glow-r3f-v1"`
- `effects.aurora_glow.enabled === true`

If an effect object is absent, helpers use safe defaults and avoid throwing.

## Settings UI

The settings modal on both pages adds a dropdown named `界面皮肤`.

Behavior:

- Opening settings initializes the dropdown from the effective current skin.
- Selecting a skin updates `pendingSkinId` only.
- Clicking `确认` persists the normalized skin ID to `localStorage`, applies it to state, and closes settings.
- Clicking `取消` discards the pending skin change.
- Clicking `重置为服务器默认值` resets chat settings as before and resets pending skin to the frontend default skin.

Refresh prompt behavior:

- If the confirmed skin entry has `requiresReload: true`, show a browser modal prompt after persisting the setting.
- The prompt text should tell the user that this skin requires a page refresh to fully apply.
- If the user accepts, call `window.location.reload()`.
- If the user declines, keep the saved preference and current page state unchanged until the next reload.
- Current `default-v1` and `aurora-glow-r3f-v1` entries do not require reload.

## Agent Mode System Prompt Applicability

The settings modal already contains an Agent mode switch and a system prompt textarea. While pending Agent mode is enabled:

- Disable the system prompt textarea.
- Visually mark it as disabled.
- Show explanatory text: `Agent 模式使用后端 agent 系统提示词，Web UI 系统提示词不适用。`

When pending Agent mode is disabled, the textarea becomes editable again.

This is a UI applicability change, not a backend prompt change. The existing send flow already sends the Web UI system prompt only through the normal chat request path; Agent mode sends messages through `/v1/agent/chat` and does not include the Web UI system prompt.

## Testing

Frontend helper tests should cover:

- Known skin IDs normalize to themselves.
- Unknown/stale skin IDs normalize to `default-v1`.
- Local skin override returns a config with the effective skin ID/name.
- Aurora glow gating still requires both matching skin ID and enabled effect.
- The registry marks current skins as not requiring reload.

Manual/UI verification should cover both `page.tsx` and `vision/page.tsx`:

- Switch from default to aurora glow and see the effect apply.
- Refresh page and confirm skin persists.
- Manually put an invalid localStorage value and confirm fallback to default.
- Toggle Agent mode on and confirm system prompt is disabled with explanatory text.
- Toggle Agent mode off and confirm system prompt is editable.

Build verification should use `npm run build:verify`, not `npm run build`, to avoid interfering with a running dev server.
