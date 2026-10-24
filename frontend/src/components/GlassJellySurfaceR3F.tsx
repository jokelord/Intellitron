'use client';

import React, { useEffect, useMemo, useRef } from 'react';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import * as THREE from 'three';
import { logGlassJellyDebug } from '@/lib/glass-jelly-debug';
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
  const { gl } = useThree();
  const materialRef = useRef<THREE.ShaderMaterial | null>(null);
  const pointerTargetRef = useRef(new THREE.Vector2(0.5, 0.5));
  const pointerCurrentRef = useRef(new THREE.Vector2(0.5, 0.5));
  const pointerActiveRef = useRef(0);
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
      uDebugBoost: { value: 1.0 },
      uPointer: { value: new THREE.Vector2(0.5, 0.5) },
      uPointerActive: { value: 0 },
    }),
    [
      config.chromatic_aberration,
      config.intensity,
      config.opacity,
      config.refraction_strength,
      config.tint_a,
      config.tint_b,
      zone,
    ],
  );

  useEffect(() => {
    const handlePointerMove = (event: PointerEvent) => {
      const rect = gl.domElement.getBoundingClientRect();
      const inside =
        event.clientX >= rect.left &&
        event.clientX <= rect.right &&
        event.clientY >= rect.top &&
        event.clientY <= rect.bottom;

      if (!inside) {
        pointerActiveRef.current = 0;
        return;
      }

      pointerTargetRef.current.set(
        (event.clientX - rect.left) / Math.max(rect.width, 1),
        1 - (event.clientY - rect.top) / Math.max(rect.height, 1),
      );
      pointerActiveRef.current = 1;
    };

    const handlePointerOut = () => {
      pointerActiveRef.current = 0;
    };

    window.addEventListener('pointermove', handlePointerMove, { passive: true });
    window.addEventListener('pointerleave', handlePointerOut);
    window.addEventListener('blur', handlePointerOut);
    return () => {
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerleave', handlePointerOut);
      window.removeEventListener('blur', handlePointerOut);
    };
  }, [gl]);

  useFrame((state) => {
    if (!materialRef.current) return;
    if (config.animated) {
      materialRef.current.uniforms.uTime.value = state.clock.elapsedTime;
    }
    pointerCurrentRef.current.lerp(pointerTargetRef.current, 0.12);
    materialRef.current.uniforms.uPointer.value.copy(pointerCurrentRef.current);
    materialRef.current.uniforms.uPointerActive.value +=
      (pointerActiveRef.current - materialRef.current.uniforms.uPointerActive.value) * 0.12;
  });

  return (
    <mesh scale={[2.18, 2.42, 1]}>
      <planeGeometry args={[1, 1, 128, 32]} />
      <shaderMaterial
        ref={materialRef}
        transparent
        depthWrite={false}
        depthTest={false}
        uniforms={uniforms}
        vertexShader={`
          varying vec2 vUv;
          varying float vLift;
          uniform float uTime;
          uniform float uIntensity;
          uniform float uZoneSign;

          void main() {
            vUv = uv;
            vec3 p = position;
            float wave = sin((uv.x * 8.0) + uTime * 0.9 + uZoneSign) * 0.026;
            wave += sin((uv.x * 19.0 + uv.y * 5.0) - uTime * 0.72) * 0.014;
            wave += sin((uv.x * 31.0 - uv.y * 9.0) + uTime * 0.42) * 0.006;
            vLift = wave * uIntensity;
            p.y += vLift;
            p.x += sin(uv.y * 9.0 + uTime * 0.5) * 0.008 * uIntensity;
            gl_Position = vec4(p.xy * vec2(2.08, 2.36), 0.0, 1.0);
          }
        `}
        fragmentShader={`
          precision highp float;
          varying vec2 vUv;
          varying float vLift;
          uniform float uTime;
          uniform float uIntensity;
          uniform float uChromatic;
          uniform float uRefraction;
          uniform float uOpacity;
          uniform vec3 uTintA;
          uniform vec3 uTintB;
          uniform float uZoneSign;
          uniform float uDebugBoost;
          uniform vec2 uPointer;
          uniform float uPointerActive;

          float hash(vec2 p) {
            return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
          }

          float noise(vec2 p) {
            vec2 i = floor(p);
            vec2 f = fract(p);
            vec2 u = f * f * (3.0 - 2.0 * f);
            return mix(
              mix(hash(i + vec2(0.0, 0.0)), hash(i + vec2(1.0, 0.0)), u.x),
              mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x),
              u.y
            );
          }

          float blob(vec2 p, vec2 c, float r) {
            float d = length(p - c);
            return smoothstep(r, 0.0, d);
          }

          float heightField(vec2 uv, float time) {
            float h = 0.0;
            h += blob(uv, vec2(0.20 + sin(time * 0.31) * 0.05, 0.25), 0.36) * 0.9;
            h += blob(uv, vec2(0.72 + cos(time * 0.27) * 0.07, 0.70), 0.43) * 0.82;
            h += blob(uv, vec2(0.47 + sin(time * 0.21) * 0.05, 0.44 + cos(time * 0.19) * 0.07), 0.30) * 0.72;
            h += sin((uv.x + uv.y * 0.38) * 58.0 - time * 1.9) * 0.04;
            h += sin((uv.x * 93.0 - uv.y * 41.0) + time * 1.15) * 0.014;
            return h;
          }

          vec3 fakeBackdrop(vec2 uv) {
            vec2 p = clamp(uv, 0.0, 1.0);
            float band = smoothstep(0.0, 1.0, p.x) * 0.55 + smoothstep(0.0, 1.0, p.y) * 0.25;
            float stripe = sin((p.x * 72.0 + p.y * 24.0) + uZoneSign * 1.7) * 0.5 + 0.5;
            float softGrid = smoothstep(0.955, 1.0, stripe) * 0.3;
            vec3 paper = mix(vec3(0.08, 0.12, 0.16), vec3(0.18, 0.28, 0.18), band);
            vec3 accent = mix(uTintA, uTintB, smoothstep(0.15, 1.0, p.x + p.y * 0.35));
            return mix(paper, accent, 0.42) + softGrid;
          }

          float zoneEdge(vec2 uv) {
            float x = smoothstep(0.0, 0.035, uv.x) * smoothstep(1.0, 0.965, uv.x);
            float y = smoothstep(0.0, 0.015, uv.y) * smoothstep(1.0, 0.985, uv.y);
            return x * y;
          }

          void main() {
            vec2 uv = vUv;
            float n1 = noise(uv * vec2(9.0, 4.0) + vec2(uTime * 0.18, -uTime * 0.12));
            float n2 = noise(uv * vec2(22.0, 8.0) - vec2(uTime * 0.23, uTime * 0.1));
            float ripple = sin((uv.x + uv.y * 0.42) * 74.0 - uTime * 2.1 + n1 * 3.2) * 0.5 + 0.5;

            float h = heightField(uv, uTime);
            vec2 e = vec2(0.0035, 0.007);
            vec2 grad = vec2(
              heightField(uv + vec2(e.x, 0.0), uTime) - heightField(uv - vec2(e.x, 0.0), uTime),
              heightField(uv + vec2(0.0, e.y), uTime) - heightField(uv - vec2(0.0, e.y), uTime)
            );
            vec3 normal = normalize(vec3(-grad * vec2(7.0, 4.0), 1.0));
            float body = clamp(h, 0.0, 1.0);
            float mask = zoneEdge(uv);

            float edgeDistance = min(min(uv.x, 1.0 - uv.x) * 1.7, min(uv.y, 1.0 - uv.y) * 3.2);
            float fresnel = pow(1.0 - clamp(edgeDistance, 0.0, 1.0), 2.6);
            float topHighlight = smoothstep(0.86, 1.0, uv.y + sin(uv.x * 9.0 + uTime * 0.35) * 0.03);
            float lowerCaustic = smoothstep(0.0, 0.32, uv.y) * (sin(uv.x * 96.0 + n2 * 5.0 - uTime * 1.35) * 0.5 + 0.5);
            vec2 pointerOffset = (uPointer - 0.5) * vec2(0.34, 0.7) * uPointerActive;
            float helixA = sin((uv.x + pointerOffset.x) * 20.0 + sin((uv.y + pointerOffset.y) * 9.0 + uTime * 0.7) * 2.8 - uTime * 2.2);
            float helixB = sin((uv.x - pointerOffset.x * 0.7) * 34.0 - cos((uv.y - pointerOffset.y) * 12.0 - uTime * 0.55) * 2.2 + uTime * 1.45);
            float ribbonA = smoothstep(0.73, 1.0, helixA * 0.5 + 0.5);
            float ribbonB = smoothstep(0.8, 1.0, helixB * 0.5 + 0.5);
            float mouseGlow = blob(uv, uPointer, 0.55) * uPointerActive;
            float columnCore = clamp(ribbonA * 0.85 + ribbonB * 0.55 + ripple * 0.28 + mouseGlow * 0.65, 0.0, 1.0);
            float verticalFill = 0.78 + 0.22 * (sin(uv.y * 10.0 + uTime * 0.55 + uZoneSign + uPointer.y * 1.8) * 0.5 + 0.5);
            float horizontalSweep = 0.62 + 0.38 * (sin(uv.x * 15.0 - uTime * 1.25 + n1 * 2.2 + uPointer.x * 2.0) * 0.5 + 0.5);

            float refractStrength = (0.035 + uRefraction * 1.8 + body * 0.055) * uIntensity;
            vec2 refractVec = normal.xy * refractStrength;
            float ca = max(uChromatic, 0.018) * 4.0;
            vec3 refracted;
            refracted.r = fakeBackdrop(uv + refractVec * (1.0 + ca)).r;
            refracted.g = fakeBackdrop(uv + refractVec * (0.66 + ca * 0.35)).g;
            refracted.b = fakeBackdrop(uv + refractVec * (0.24 - ca * 0.25)).b;

            vec3 tint = mix(uTintA, uTintB, clamp(body * 0.72 + n1 * 0.24, 0.0, 1.0));
            vec3 darkBase = mix(vec3(0.01, 0.018, 0.028), vec3(0.02, 0.055, 0.03), body * 0.55 + n2 * 0.22);
            vec3 columnLight = mix(vec3(0.36, 0.86, 1.0), uTintB + vec3(0.0, 0.32, 0.02), 0.54);
            vec3 pointerLight = mix(vec3(0.9, 0.55, 1.0), vec3(0.35, 1.0, 0.72), uPointer.x);
            vec3 chroma = vec3(
              max(uChromatic, 0.018) * (fresnel + ribbonA * 0.65 + ripple * 0.2),
              max(uRefraction, 0.045) * (body + ribbonB * 0.45 + n2 * 0.35),
              max(uChromatic, 0.018) * (body + lowerCaustic * 0.42 + mouseGlow * 0.5)
            ) * 8.0;
            vec3 specular = vec3(1.0) * (pow(topHighlight, 2.6) * 0.82 + pow(fresnel, 1.65) * 0.68);
            vec3 caustic = vec3(0.58, 1.0, 0.62) * smoothstep(0.62, 1.0, lowerCaustic) * (0.2 + body) * 0.78;
            vec3 color = darkBase;
            color += columnLight * columnCore * verticalFill * horizontalSweep * 1.08;
            color += pointerLight * mouseGlow * 0.58;
            color += tint * body * 0.08 + chroma + caustic + specular;
            color += refracted * 0.03;
            color = (color - 0.5) * 1.62 + 0.5;
            color = max(color - vec3(0.075), vec3(0.0));

            float darkAlpha = 0.54;
            float lightAlpha = columnCore * verticalFill * horizontalSweep * 0.42 + mouseGlow * 0.18;
            float alpha = clamp((darkAlpha + lightAlpha + abs(vLift) * 2.2) * mask * uOpacity * uIntensity, 0.0, 0.96);
            gl_FragColor = vec4(color, alpha);
          }
        `}
      />
    </mesh>
  );
}

export default function GlassJellySurfaceR3F({ config, zone }: GlassJellySurfaceR3FProps) {
  const dpr = Math.min(Math.max(config.max_dpr, 1.5), 2.5);

  return (
    <Canvas
      dpr={[1, dpr]}
      gl={{ alpha: true, antialias: true, depth: false, stencil: false, powerPreference: 'high-performance' }}
      camera={{ position: [0, 0, 1], near: 0.1, far: 10 }}
      onCreated={({ gl, size }) => {
        logGlassJellyDebug('canvas created', {
          zone,
          width: size.width,
          height: size.height,
          dpr,
          renderer: gl.info.programs?.length ?? 0,
          webgl2: gl.capabilities.isWebGL2,
        });
      }}
    >
      <JellyPlane config={config} zone={zone} />
    </Canvas>
  );
}
