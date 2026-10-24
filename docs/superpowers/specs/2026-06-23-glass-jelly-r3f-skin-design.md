# Glass Jelly R3F Web UI Skin Design

## Goal

Add an opt-in "glass jelly" Web UI skin for the chat application's header and footer zones. The effect must only activate when `config/config-webUI.yaml` selects the new skin; the existing default skin must remain unchanged.

## Background

The current Web UI skin system is driven by `config/config-webUI.yaml`, parsed by `backend/config.py`, exposed through `/config` in `backend/main.py`, and consumed by both frontend pages:

- Text mode: `frontend/src/app/page.tsx`
- Vision mode: `frontend/src/app/vision/page.tsx`

The reference site `https://haoqi.design/` does not implement the relevant jelly effect as reusable CSS. Its current version uses Next.js plus WebGL/Three.js, GLTF geometry, and custom shaders for refraction, chromatic aberration, Fresnel/specular highlights, and tint. The older local Gatsby reference only contains ordinary CSS glass primitives such as `backdrop-filter: saturate(200%) blur(...)`, which are not sufficient for the desired jelly look.

## Requirements

1. Add a new skin id: `glass-jelly-r3f-v1`.
2. The default `default-v1` skin must keep the existing header/footer rendering and must not load WebGL/R3F code.
3. When `glass-jelly-r3f-v1` is configured and the effect is enabled, the frontend should load a WebGL/R3F shader layer for the header and footer zones.
4. The effect applies to the header/footer area surfaces, not to text styling. Text such as `LLM-Test`, model name, status, settings buttons, and input placeholder must stay normal DOM UI text.
5. Both text and vision pages must support the skin consistently.
6. The WebGL layer must be client-only and must not run during SSR.
7. If WebGL is unavailable, initialization fails, or the user/browser prefers reduced motion, the UI must gracefully fall back to a CSS-only translucent surface instead of breaking the page.
8. Initial implementation should avoid GLTF models and should render a shader-based zone material only. This keeps the first experiment small and reversible.
9. Config should expose a small set of tunable effect parameters, with safe defaults.

## Non-Goals

1. Do not replicate the reference site's 3D text/GLTF `hello` or `CRAFT WITH TASTE` implementation.
2. Do not apply artistic gradient/refraction styling to header/footer text.
3. Do not replace the chat message body, agent steps, compact cards, or modal design.
4. Do not make WebGL mandatory for the application.
5. Do not add a runtime skin switcher UI in this phase.

## Configuration Shape

`config/config-webUI.yaml` will keep the current structure and add an optional `effects.glass_jelly` block:

```yaml
webUI:
  skin:
    id: glass-jelly-r3f-v1
    name: Glass Jelly R3F v1

  effects:
    glass_jelly:
      enabled: true
      intensity: 0.72
      blur_px: 18
      chromatic_aberration: 0.018
      refraction_strength: 0.045
      tint_a: "#ffffff"
      tint_b: "#76b900"
      opacity: 0.72
      animated: true
      max_dpr: 1.5
```

Default behavior:

- If `effects.glass_jelly` is absent, no effect runs.
- If `skin.id` is not `glass-jelly-r3f-v1`, no WebGL/R3F code loads even if the effect block exists.
- Invalid numeric values are coerced to safe defaults by the backend parser or frontend style builder.

## Architecture

### Backend Config

Extend `WebUIConfig` in `backend/config.py` to include `effects`. Parse `webUI.effects.glass_jelly` with defaults and expose it through `/config` in `backend/main.py`.

The backend only transports config. It does not decide whether the browser can run WebGL.

### Frontend Config Types

Extend `WebUIConfig` in `frontend/src/lib/web-ui-config.ts` and the `/config` response type in `frontend/src/lib/api.ts` with the new `effects.glass_jelly` fields.

Add helper functions:

- `isGlassJellySkin(config)` returns true only when `skin.id === "glass-jelly-r3f-v1"` and `effects.glass_jelly.enabled === true`.
- `buildWebUIStyleVars(config)` emits CSS fallback variables for header/footer glass surfaces.

### Frontend Rendering

Create a dedicated component boundary:

- `frontend/src/components/GlassJellySurface.tsx`
- `frontend/src/components/GlassJellySurfaceR3F.tsx`

`GlassJellySurface.tsx` is the safe wrapper used by both pages. It dynamically imports the R3F implementation with SSR disabled. It also renders the CSS fallback class so the UI remains usable if WebGL fails.

`GlassJellySurfaceR3F.tsx` owns all Three/R3F imports and shader code. This file must not be imported directly by server-rendered code.

Header/footer DOM remains normal. The component should sit inside the header/footer as an absolutely positioned background layer with `pointer-events: none` and a lower z-index than controls.

### Styling

Add skin classes to `frontend/src/app/globals.css`:

- `.webui-skin-glass-jelly-r3f-v1`
- `.webui-glass-zone`
- `.webui-glass-zone-content`
- `.webui-glass-r3f-layer`

The CSS fallback should use translucent backgrounds, noise/highlight gradients, and `backdrop-filter`. The R3F layer should add animated refraction-like ripples, color separation, and soft specular blobs.

### Shader Scope

The first shader should be a lightweight plane shader, not a scene full of models. It should render into the header/footer surface bounds and use time, resolution, mouse position, and config uniforms to create a subtle jelly refraction/highlight layer.

The shader is decorative only. It must not affect hit testing or layout.

## Data Flow

1. User edits `config/config-webUI.yaml` to select `glass-jelly-r3f-v1`.
2. Backend parses config into `config.web_ui`.
3. `/config` returns `webUI.skin` and `webUI.effects`.
4. Text/vision page stores `webUIConfig`.
5. Page root receives skin CSS class and CSS variables.
6. Header/footer render `GlassJellySurface` only if `isGlassJellySkin(webUIConfig)` is true.
7. `GlassJellySurface` loads `GlassJellySurfaceR3F` client-side and falls back to CSS-only if unavailable.

## Error Handling And Fallback

- Missing config: default skin behavior.
- Unknown skin id: default skin behavior.
- Effect disabled: no WebGL layer.
- Dynamic import failure: keep CSS fallback.
- WebGL unavailable: keep CSS fallback.
- Reduced motion: disable animation or render static shader/fallback.
- Runtime R3F error: catch at wrapper boundary and keep DOM controls usable.

## Performance Constraints

- WebGL/R3F dependencies load only for `glass-jelly-r3f-v1`.
- The Canvas uses `frameloop="demand"` or throttled animation where possible.
- `dpr` is capped by `max_dpr` from config, default `1.5`.
- Shader should render only two small surfaces, not the full page.
- The effect should not block typing, scrolling, streaming, or image upload interactions.

## Testing Requirements

Backend tests:

- Parse default config with no effects.
- Parse `glass_jelly` config and coerce defaults.
- `/config` includes `webUI.effects.glass_jelly`.

Frontend tests/type checks:

- `isGlassJellySkin` returns false for default skin.
- `isGlassJellySkin` returns false when effect is disabled.
- `isGlassJellySkin` returns true for `glass-jelly-r3f-v1` with enabled effect.
- `buildWebUIStyleVars` emits fallback variables.
- `npm run type-check` passes.

Manual verification:

- Default skin does not load WebGL layer.
- Glass skin shows header/footer area material while text remains normal.
- Text and vision pages both render the effect.
- Browser with reduced motion or WebGL failure still shows a usable page.

## Open Implementation Notes

- Adding `three` and `@react-three/fiber` is expected.
- `@react-three/drei` is not needed for the first implementation.
- If the first shader does not feel jelly enough, tune shader parameters and CSS surface composition before adding GLTF/model-based complexity.
