# Glass Jelly R3F Skin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in WebGL/R3F glass jelly skin for the Web UI header and footer zones, activated only by `config/config-webUI.yaml`.

**Architecture:** Backend parses and exposes the new skin/effect config. Frontend gates all WebGL/R3F loading behind `skin.id === "glass-jelly-r3f-v1"` and `effects.glass_jelly.enabled === true`, using a client-only dynamic component plus CSS fallback. Text and vision pages share the same config helpers and component boundary.

**Tech Stack:** Python/FastAPI config parsing, Next.js 14, React 18, TypeScript, Tailwind/global CSS, `three`, `@react-three/fiber`, pytest, TypeScript type-checking.

---

## File Structure

- Modify: `config/config-webUI.yaml` - add the new skin/effect example as the active configured skin for local testing.
- Modify: `backend/config.py` - extend `WebUIConfig` and parse `webUI.effects.glass_jelly`.
- Modify: `backend/main.py` - expose `webUI.effects` in `/config`.
- Modify: `tests/test_web_ui_config.py` - cover parsing and defaults.
- Modify: `frontend/package.json` - add `three` and `@react-three/fiber` dependencies.
- Modify: `frontend/src/lib/api.ts` - extend `/config` response type.
- Modify: `frontend/src/lib/web-ui-config.ts` - add frontend config types, gating helper, and fallback CSS variables.
- Create: `frontend/src/lib/web-ui-config.test.ts` or `frontend/src/lib/web-ui-config.test.mts` if the project already supports TS tests; otherwise use type-check coverage only and skip test creation.
- Create: `frontend/src/components/GlassJellySurface.tsx` - safe wrapper and dynamic client-only loader.
- Create: `frontend/src/components/GlassJellySurfaceR3F.tsx` - R3F Canvas and shader implementation.
- Modify: `frontend/src/app/globals.css` - add skin and glass zone CSS fallback classes.
- Modify: `frontend/src/app/page.tsx` - apply skin class and insert header/footer glass surfaces.
- Modify: `frontend/src/app/vision/page.tsx` - mirror text-page changes.

## Task 1: Backend Config Model And Tests

**Files:**
- Modify: `backend/config.py`
- Modify: `backend/main.py`
- Modify: `tests/test_web_ui_config.py`

- [ ] **Step 1: Add failing backend config tests**

Append these tests to `tests/test_web_ui_config.py`:

```python
def test_parse_web_ui_glass_jelly_effect_defaults_when_absent():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {"webUI": {"skin": {"id": "default-v1", "name": "Default"}}}

    cfg = loader._parse_web_ui_config()

    assert cfg.effects == {
        "glass_jelly": {
            "enabled": False,
            "intensity": 0.72,
            "blur_px": 18,
            "chromatic_aberration": 0.018,
            "refraction_strength": 0.045,
            "tint_a": "#ffffff",
            "tint_b": "#76b900",
            "opacity": 0.72,
            "animated": True,
            "max_dpr": 1.5,
        }
    }


def test_parse_web_ui_glass_jelly_effect_values():
    loader = ConfigLoader.__new__(ConfigLoader)
    loader._data = {
        "webUI": {
            "skin": {"id": "glass-jelly-r3f-v1", "name": "Glass Jelly R3F v1"},
            "effects": {
                "glass_jelly": {
                    "enabled": True,
                    "intensity": 0.9,
                    "blur_px": 24,
                    "chromatic_aberration": 0.025,
                    "refraction_strength": 0.06,
                    "tint_a": "#f8fafc",
                    "tint_b": "#38bdf8",
                    "opacity": 0.8,
                    "animated": False,
                    "max_dpr": 1.25,
                }
            },
        }
    }

    cfg = loader._parse_web_ui_config()

    assert cfg.skin_id == "glass-jelly-r3f-v1"
    assert cfg.effects["glass_jelly"] == {
        "enabled": True,
        "intensity": 0.9,
        "blur_px": 24,
        "chromatic_aberration": 0.025,
        "refraction_strength": 0.06,
        "tint_a": "#f8fafc",
        "tint_b": "#38bdf8",
        "opacity": 0.8,
        "animated": False,
        "max_dpr": 1.25,
    }
```

- [ ] **Step 2: Run backend tests and verify failure**

Run: `pytest tests/test_web_ui_config.py -v`

Expected: FAIL with an error like `AttributeError: 'WebUIConfig' object has no attribute 'effects'`.

- [ ] **Step 3: Extend `WebUIConfig` dataclass**

In `backend/config.py`, update `WebUIConfig` near the existing fields:

```python
    effects: dict[str, dict[str, object]] = field(
        default_factory=lambda: {
            "glass_jelly": {
                "enabled": False,
                "intensity": 0.72,
                "blur_px": 18,
                "chromatic_aberration": 0.018,
                "refraction_strength": 0.045,
                "tint_a": "#ffffff",
                "tint_b": "#76b900",
                "opacity": 0.72,
                "animated": True,
                "max_dpr": 1.5,
            }
        }
    )
```

- [ ] **Step 4: Add coercion helpers if missing**

In `backend/config.py`, inspect existing `_coerce_int`, `_coerce_str`, and `_get_bool` helpers. If no float/bool coercion helper exists for raw nested dictionaries, add these methods to `ConfigLoader`:

```python
    def _coerce_float(self, value, default: float) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def _coerce_bool_value(self, value, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        if value is None:
            return default
        return bool(value)
```

- [ ] **Step 5: Parse effects in `_parse_web_ui_config`**

In `backend/config.py::_parse_web_ui_config`, after parsing `components`, add:

```python
        effects = web_ui.get("effects") or {}
        glass_jelly = effects.get("glass_jelly") or {}
        effect_defaults = default_config.effects["glass_jelly"]
        parsed_effects = {
            "glass_jelly": {
                "enabled": self._coerce_bool_value(glass_jelly.get("enabled"), effect_defaults["enabled"]),
                "intensity": self._coerce_float(glass_jelly.get("intensity"), effect_defaults["intensity"]),
                "blur_px": self._coerce_int(glass_jelly.get("blur_px"), effect_defaults["blur_px"]),
                "chromatic_aberration": self._coerce_float(
                    glass_jelly.get("chromatic_aberration"),
                    effect_defaults["chromatic_aberration"],
                ),
                "refraction_strength": self._coerce_float(
                    glass_jelly.get("refraction_strength"),
                    effect_defaults["refraction_strength"],
                ),
                "tint_a": self._coerce_str(glass_jelly.get("tint_a"), effect_defaults["tint_a"]),
                "tint_b": self._coerce_str(glass_jelly.get("tint_b"), effect_defaults["tint_b"]),
                "opacity": self._coerce_float(glass_jelly.get("opacity"), effect_defaults["opacity"]),
                "animated": self._coerce_bool_value(glass_jelly.get("animated"), effect_defaults["animated"]),
                "max_dpr": self._coerce_float(glass_jelly.get("max_dpr"), effect_defaults["max_dpr"]),
            }
        }
```

Then include `effects=parsed_effects` in the returned `WebUIConfig(...)`.

- [ ] **Step 6: Expose effects in `/config`**

In `backend/main.py`, update the `webUI` response block:

```python
        "webUI": {
            "skin": {
                "id": config.web_ui.skin_id,
                "name": config.web_ui.skin_name,
            },
            "components": {
                "input_bar": config.web_ui.input_bar,
                "action_buttons": config.web_ui.action_buttons,
            },
            "effects": config.web_ui.effects,
        },
```

- [ ] **Step 7: Run backend tests and verify pass**

Run: `pytest tests/test_web_ui_config.py -v`

Expected: PASS.

## Task 2: Frontend Config Types And Gating Helper

**Files:**
- Modify: `frontend/src/lib/api.ts`
- Modify: `frontend/src/lib/web-ui-config.ts`

- [ ] **Step 1: Extend API response type**

In `frontend/src/lib/api.ts`, add `effects` under `webUI?:`:

```ts
    effects?: {
      glass_jelly?: {
        enabled?: boolean;
        intensity?: number;
        blur_px?: number;
        chromatic_aberration?: number;
        refraction_strength?: number;
        tint_a?: string;
        tint_b?: string;
        opacity?: number;
        animated?: boolean;
        max_dpr?: number;
      };
    };
```

- [ ] **Step 2: Extend frontend config types**

In `frontend/src/lib/web-ui-config.ts`, add:

```ts
export interface WebUIGlassJellyConfig {
  enabled?: boolean;
  intensity?: number;
  blur_px?: number;
  chromatic_aberration?: number;
  refraction_strength?: number;
  tint_a?: string;
  tint_b?: string;
  opacity?: number;
  animated?: boolean;
  max_dpr?: number;
}
```

Then add to `WebUIConfig`:

```ts
  effects?: {
    glass_jelly?: WebUIGlassJellyConfig;
  };
```

- [ ] **Step 3: Add config helpers**

In `frontend/src/lib/web-ui-config.ts`, add:

```ts
export const GLASS_JELLY_SKIN_ID = "glass-jelly-r3f-v1";

function cssNumber(value: number | undefined, fallback: number): number {
  return Number.isFinite(value) ? value! : fallback;
}

function cssColor(value: string | undefined, fallback: string): string {
  return value && value.trim() ? value : fallback;
}

export function isGlassJellySkin(config?: WebUIConfig): boolean {
  return config?.skin?.id === GLASS_JELLY_SKIN_ID && config.effects?.glass_jelly?.enabled === true;
}

export function getGlassJellyConfig(config?: WebUIConfig): Required<WebUIGlassJellyConfig> {
  const glass = config?.effects?.glass_jelly ?? {};
  return {
    enabled: glass.enabled === true,
    intensity: cssNumber(glass.intensity, 0.72),
    blur_px: cssNumber(glass.blur_px, 18),
    chromatic_aberration: cssNumber(glass.chromatic_aberration, 0.018),
    refraction_strength: cssNumber(glass.refraction_strength, 0.045),
    tint_a: cssColor(glass.tint_a, "#ffffff"),
    tint_b: cssColor(glass.tint_b, "#76b900"),
    opacity: cssNumber(glass.opacity, 0.72),
    animated: glass.animated !== false,
    max_dpr: cssNumber(glass.max_dpr, 1.5),
  };
}
```

- [ ] **Step 4: Add fallback style variables**

In `buildWebUIStyleVars`, compute `const glass = getGlassJellyConfig(config);` and add these properties to the returned object:

```ts
    "--webui-glass-blur": px(glass.blur_px, 18),
    "--webui-glass-opacity": String(glass.opacity),
    "--webui-glass-intensity": String(glass.intensity),
    "--webui-glass-tint-a": glass.tint_a,
    "--webui-glass-tint-b": glass.tint_b,
```

- [ ] **Step 5: Run frontend type-check**

Run: `npm run type-check` from `frontend/`.

Expected: PASS.

## Task 3: Add R3F Dependencies

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json` or `frontend/yarn.lock` if generated by the package manager in use

- [ ] **Step 1: Install dependencies**

Run from `frontend/`:

```bash
npm install three @react-three/fiber
```

Expected: `frontend/package.json` includes `three` and `@react-three/fiber`; lockfile updates if npm is used.

- [ ] **Step 2: Run type-check**

Run from `frontend/`: `npm run type-check`

Expected: PASS.

## Task 4: Create Glass Jelly Components

**Files:**
- Create: `frontend/src/components/GlassJellySurface.tsx`
- Create: `frontend/src/components/GlassJellySurfaceR3F.tsx`

- [ ] **Step 1: Create safe wrapper component**

Create `frontend/src/components/GlassJellySurface.tsx`:

```tsx
'use client';

import dynamic from 'next/dynamic';
import React, { useEffect, useState } from 'react';
import { getGlassJellyConfig, type WebUIConfig } from '@/lib/web-ui-config';

const GlassJellySurfaceR3F = dynamic(() => import('./GlassJellySurfaceR3F'), {
  ssr: false,
  loading: () => null,
});

interface GlassJellySurfaceProps {
  config?: WebUIConfig;
  zone: 'header' | 'footer';
}

export default function GlassJellySurface({ config, zone }: GlassJellySurfaceProps) {
  const [canAnimate, setCanAnimate] = useState(false);
  const glassConfig = getGlassJellyConfig(config);

  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    setCanAnimate(!media.matches && glassConfig.animated);
    const onChange = () => setCanAnimate(!media.matches && glassConfig.animated);
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, [glassConfig.animated]);

  return (
    <div className="webui-glass-r3f-layer" aria-hidden="true" data-zone={zone}>
      <GlassJellySurfaceR3F config={{ ...glassConfig, animated: canAnimate }} zone={zone} />
    </div>
  );
}
```

- [ ] **Step 2: Create R3F shader component**

Create `frontend/src/components/GlassJellySurfaceR3F.tsx`:

```tsx
'use client';

import React, { useMemo, useRef } from 'react';
import { Canvas, useFrame } from '@react-three/fiber';
import * as THREE from 'three';
import type { WebUIGlassJellyConfig } from '@/lib/web-ui-config';

type RequiredGlassConfig = Required<WebUIGlassJellyConfig>;

interface GlassJellySurfaceR3FProps {
  config: RequiredGlassConfig;
  zone: 'header' | 'footer';
}

function hexToColor(hex: string, fallback: string): THREE.Color {
  try {
    return new THREE.Color(hex);
  } catch {
    return new THREE.Color(fallback);
  }
}

function JellyPlane({ config, zone }: GlassJellySurfaceR3FProps) {
  const materialRef = useRef<THREE.ShaderMaterial | null>(null);
  const uniforms = useMemo(
    () => ({
      uTime: { value: 0 },
      uIntensity: { value: config.intensity },
      uChromatic: { value: config.chromatic_aberration },
      uRefraction: { value: config.refraction_strength },
      uOpacity: { value: config.opacity },
      uTintA: { value: hexToColor(config.tint_a, '#ffffff') },
      uTintB: { value: hexToColor(config.tint_b, '#76b900') },
      uZoneSign: { value: zone === 'header' ? 1 : -1 },
    }),
    [config.chromatic_aberration, config.intensity, config.opacity, config.refraction_strength, config.tint_a, config.tint_b, zone]
  );

  useFrame((state) => {
    if (!materialRef.current || !config.animated) return;
    materialRef.current.uniforms.uTime.value = state.clock.elapsedTime;
  });

  return (
    <mesh scale={[2, 2, 1]}>
      <planeGeometry args={[1, 1, 64, 16]} />
      <shaderMaterial
        ref={materialRef}
        transparent
        depthWrite={false}
        depthTest={false}
        uniforms={uniforms}
        vertexShader={`
          varying vec2 vUv;
          uniform float uTime;
          uniform float uIntensity;
          uniform float uZoneSign;

          void main() {
            vUv = uv;
            vec3 p = position;
            float wave = sin((uv.x * 8.0) + uTime * 0.9 + uZoneSign) * 0.018;
            wave += sin((uv.x * 17.0) - uTime * 0.65) * 0.008;
            p.y += wave * uIntensity;
            gl_Position = vec4(p.xy, 0.0, 1.0);
          }
        `}
        fragmentShader={`
          precision highp float;
          varying vec2 vUv;
          uniform float uTime;
          uniform float uIntensity;
          uniform float uChromatic;
          uniform float uRefraction;
          uniform float uOpacity;
          uniform vec3 uTintA;
          uniform vec3 uTintB;
          uniform float uZoneSign;

          float blob(vec2 p, vec2 c, float r) {
            float d = length(p - c);
            return smoothstep(r, 0.0, d);
          }

          void main() {
            vec2 uv = vUv;
            float wave = sin(uv.x * 10.0 + uTime * 0.7) * 0.5 + 0.5;
            float ripple = sin((uv.x + uv.y * 0.55) * 24.0 - uTime * 1.35) * 0.5 + 0.5;
            float glowA = blob(uv, vec2(0.16 + sin(uTime * 0.35) * 0.04, 0.18), 0.55);
            float glowB = blob(uv, vec2(0.84 + cos(uTime * 0.25) * 0.04, 0.82), 0.48);
            float edge = smoothstep(0.0, 0.08, uv.y) * smoothstep(1.0, 0.88, uv.y);
            vec3 tint = mix(uTintA, uTintB, clamp(glowA * 0.45 + wave * 0.22, 0.0, 1.0));
            vec3 chroma = vec3(uChromatic * ripple, uRefraction * glowA, uChromatic * glowB);
            vec3 color = tint + chroma;
            float alpha = clamp((0.34 + glowA * 0.24 + glowB * 0.16 + ripple * 0.08) * edge * uOpacity * uIntensity, 0.0, 0.86);
            gl_FragColor = vec4(color, alpha);
          }
        `}
      />
    </mesh>
  );
}

export default function GlassJellySurfaceR3F({ config, zone }: GlassJellySurfaceR3FProps) {
  const dpr = Math.min(Math.max(config.max_dpr, 1), 2);

  return (
    <Canvas
      dpr={[1, dpr]}
      gl={{ alpha: true, antialias: true, depth: false, stencil: false, powerPreference: 'high-performance' }}
      camera={{ position: [0, 0, 1], near: 0.1, far: 10 }}
    >
      <JellyPlane config={config} zone={zone} />
    </Canvas>
  );
}
```

- [ ] **Step 3: Run type-check**

Run from `frontend/`: `npm run type-check`

Expected: PASS. If it fails because `three` types are missing, install `@types/three` as a dev dependency and rerun.

## Task 5: Add CSS Fallback And Page Integration

**Files:**
- Modify: `frontend/src/app/globals.css`
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/app/vision/page.tsx`

- [ ] **Step 1: Add CSS classes**

Append to `frontend/src/app/globals.css`:

```css
.webui-glass-zone {
  position: relative;
  isolation: isolate;
  overflow: hidden;
  background-color: color-mix(in srgb, var(--webui-glass-tint-a, #ffffff) calc(var(--webui-glass-opacity, 0.72) * 100%), transparent);
  background-image:
    radial-gradient(circle at 12% 0%, color-mix(in srgb, var(--webui-glass-tint-a, #ffffff) 72%, transparent), transparent 36%),
    radial-gradient(circle at 92% 100%, color-mix(in srgb, var(--webui-glass-tint-b, #76b900) 24%, transparent), transparent 42%);
  -webkit-backdrop-filter: saturate(190%) blur(var(--webui-glass-blur, 18px));
  backdrop-filter: saturate(190%) blur(var(--webui-glass-blur, 18px));
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.72),
    inset 0 -1px 0 rgba(255, 255, 255, 0.24),
    0 16px 48px rgba(15, 23, 42, 0.12);
}

.dark .webui-glass-zone {
  background-color: rgba(17, 24, 39, 0.72);
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.12),
    inset 0 -1px 0 rgba(255, 255, 255, 0.08),
    0 16px 48px rgba(0, 0, 0, 0.28);
}

.webui-glass-r3f-layer {
  position: absolute;
  inset: 0;
  z-index: -1;
  pointer-events: none;
  opacity: var(--webui-glass-opacity, 0.72);
}

.webui-glass-r3f-layer canvas {
  width: 100% !important;
  height: 100% !important;
}

.webui-glass-zone-content {
  position: relative;
  z-index: 1;
}
```

- [ ] **Step 2: Integrate text page**

In `frontend/src/app/page.tsx`, update the import:

```ts
import { buildWebUIStyleVars, getWebUIInputBarResizeOptions, isGlassJellySkin, type WebUIConfig } from '@/lib/web-ui-config';
import GlassJellySurface from '@/components/GlassJellySurface';
```

Before `return`, add:

```ts
  const glassJellyEnabled = isGlassJellySkin(webUIConfig);
  const appSkinClass = glassJellyEnabled ? 'webui-skin-glass-jelly-r3f-v1' : '';
  const glassZoneClass = glassJellyEnabled ? 'webui-glass-zone border-transparent' : 'bg-white dark:bg-gray-800 border-gray-200 dark:border-gray-700';
```

Update the root div className:

```tsx
    <div className={`app-shell flex flex-col overflow-hidden bg-gray-50 dark:bg-gray-900 ${appSkinClass}`} style={buildWebUIStyleVars(webUIConfig)}>
```

Update the header opening and add the surface:

```tsx
      <header className={`flex-shrink-0 border-b px-4 py-3 ${glassZoneClass}`}>
        {glassJellyEnabled && <GlassJellySurface config={webUIConfig} zone="header" />}
        <div className="webui-glass-zone-content max-w-4xl mx-auto flex items-center justify-between">
```

Update the footer opening and inner wrapper:

```tsx
      <footer className={`chat-input-footer flex-shrink-0 border-t px-4 pt-4 ${glassZoneClass}`}>
        {glassJellyEnabled && <GlassJellySurface config={webUIConfig} zone="footer" />}
        <div className="webui-glass-zone-content max-w-4xl mx-auto space-y-3">
```

- [ ] **Step 3: Integrate vision page with the same pattern**

Apply the same import, constants, root class, header wrapper, and footer wrapper changes to `frontend/src/app/vision/page.tsx`.

- [ ] **Step 4: Run type-check**

Run from `frontend/`: `npm run type-check`

Expected: PASS.

## Task 6: Configure The New Skin

**Files:**
- Modify: `config/config-webUI.yaml`

- [ ] **Step 1: Update skin config**

Change the top of `config/config-webUI.yaml` to:

```yaml
webUI:
  skin:
    # 皮肤唯一标识；default-v1 为默认皮肤，glass-jelly-r3f-v1 启用 WebGL/R3F 玻璃果冻页眉/页脚。
    id: glass-jelly-r3f-v1
    # 皮肤显示名称；用于日志、调试或未来 UI 中展示。
    name: Glass Jelly R3F v1

  effects:
    glass_jelly:
      # 仅当 skin.id 为 glass-jelly-r3f-v1 且 enabled=true 时，前端才会加载 WebGL/R3F shader 层。
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

Keep the existing `layout` and `components` blocks below unchanged.

- [ ] **Step 2: Run backend config test**

Run: `pytest tests/test_web_ui_config.py -v`

Expected: PASS.

## Task 7: Verification

**Files:**
- No code changes unless verification reveals failures.

- [ ] **Step 1: Backend tests**

Run: `pytest tests/test_web_ui_config.py -v`

Expected: PASS.

- [ ] **Step 2: Frontend type-check**

Run from `frontend/`: `npm run type-check`

Expected: PASS.

- [ ] **Step 3: Frontend build**

Run from `frontend/`: `npm run build`

Expected: PASS. If build fails due to dependency version mismatch, resolve package versions instead of disabling checks.

- [ ] **Step 4: Manual browser verification**

Start the app normally with the project scripts. Verify:

- `/` header/footer show glass jelly zone effect.
- `/vision` header/footer show glass jelly zone effect.
- Header/footer text remains normal, readable DOM text.
- Buttons remain clickable.
- Typing and streaming are not noticeably degraded.
- Setting `webUI.skin.id: default-v1` and `effects.glass_jelly.enabled: false` removes the WebGL/R3F layer.

## Self-Review

- Spec coverage: config gating, backend transport, text/vision parity, dynamic client-only loading, CSS fallback, default-skin no-op, reduced motion fallback, and verification are covered.
- Placeholder scan: no implementation step relies on an undefined placeholder.
- Type consistency: `WebUIGlassJellyConfig`, `isGlassJellySkin`, `getGlassJellyConfig`, `GlassJellySurface`, and `GlassJellySurfaceR3F` names are consistent across tasks.
