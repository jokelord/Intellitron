/**
 * 本来想做“玻璃果冻”的皮肤效果，结果最后做成了“欧若拉极光”，索性就在 web UI 里改名叫 “Aurora Glow R3F”，而代码文件名字仍保持“GlassJellySurface”
 */
'use client';

import dynamic from 'next/dynamic';
import React, { Component, useEffect, useMemo, useRef, useState } from 'react';
import { logGlassJellyDebug } from '@/lib/glass-jelly-debug';
import { getGlassJellyConfig, type WebUIConfig } from '@/lib/web-ui-config';
import { retryDynamicImport } from '@/lib/retry-dynamic-import';

const GlassJellySurfaceR3F = dynamic(() => retryDynamicImport(() => import('./GlassJellySurfaceR3F')), {
  ssr: false,
  loading: () => null,
});

interface GlassJellySurfaceProps {
  config?: WebUIConfig;
  zone: 'header' | 'footer';
}

interface ErrorBoundaryState {
  failed: boolean;
}

class GlassJellyErrorBoundary extends Component<React.PropsWithChildren, ErrorBoundaryState> {
  state: ErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { failed: true };
  }

  componentDidCatch(error: Error): void {
    logGlassJellyDebug('r3f error boundary caught error', { message: error.message });
  }

  render() {
    if (this.state.failed) return null;
    return this.props.children;
  }
}

function hasWebGLSupport(): boolean {
  try {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl2') ?? canvas.getContext('webgl');
    if (!gl) return false;
    (gl as WebGLRenderingContext).getExtension('WEBGL_lose_context')?.loseContext();
    return true;
  } catch {
    return false;
  }
}

export default function GlassJellySurface({ config, zone }: GlassJellySurfaceProps) {
  const [canRender, setCanRender] = useState(false);
  const layerRef = useRef<HTMLDivElement | null>(null);
  const glassConfig = useMemo(() => getGlassJellyConfig(config), [config]);

  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const update = () => {
      const webglSupported = hasWebGLSupport();
      const nextCanRender = !media.matches && webglSupported;
      setCanRender(nextCanRender);
      logGlassJellyDebug('wrapper gate evaluated', {
        zone,
        reducedMotion: media.matches,
        webglSupported,
        canRender: nextCanRender,
        config: glassConfig,
      });
    };

    update();
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, [glassConfig, zone]);

  useEffect(() => {
    if (!canRender || !layerRef.current) return;
    const rect = layerRef.current.getBoundingClientRect();
    logGlassJellyDebug('layer mounted', {
      zone,
      width: Math.round(rect.width),
      height: Math.round(rect.height),
      devicePixelRatio: window.devicePixelRatio,
    });
  }, [canRender, zone]);

  if (!canRender) return null;

  return (
    <div ref={layerRef} className="webui-glass-r3f-layer" aria-hidden="true" data-zone={zone}>
      <GlassJellyErrorBoundary>
        <GlassJellySurfaceR3F config={glassConfig} zone={zone} />
      </GlassJellyErrorBoundary>
    </div>
  );
}
