'use client';

import React, { useEffect, useId, useRef, useState } from 'react';
import { Check, Copy as CopyIcon, Move as MoveIcon, ZoomIn, ZoomOut } from 'lucide-react';
import ReactMarkdown, { type Components } from 'react-markdown';
import rehypeKatex from 'rehype-katex';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import {
  getMermaidIconButtonClass,
  getMermaidMoveButtonClass,
  getMermaidMoveIconClass,
  getNextMermaidZoom,
  getMermaidViewportClass,
  MERMAID_MAX_ZOOM,
  MERMAID_MIN_ZOOM,
  MERMAID_ZOOM_STEP,
  MERMAID_CONTAINER_CLASS,
  MERMAID_CONTENT_CLASS,
} from '@/lib/mermaid-layout';
import { normalizeMermaidTimelineChart } from '@/lib/mermaid-timeline';
import { getMermaidThemeName, subscribeToDarkScheme } from '@/lib/mermaid-theme';

interface MarkdownRendererProps {
  children: string;
  className?: string;
}

interface CodeElementProps {
  children?: React.ReactNode;
  className?: string;
}

const iconButtonClass = getMermaidIconButtonClass();

async function copyToClipboard(text: string) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Fall back for non-secure contexts or browser permission failures.
    }
  }

  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  textarea.style.top = '0';
  document.body.appendChild(textarea);
  textarea.select();

  try {
    document.execCommand('copy');
  } finally {
    document.body.removeChild(textarea);
  }
}

function CopyButton({ text, className = '' }: { text: string; className?: string }) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) {
      return;
    }

    const timeout = window.setTimeout(() => setCopied(false), 1400);
    return () => window.clearTimeout(timeout);
  }, [copied]);

  return (
    <button
      type="button"
      onClick={() => {
        void copyToClipboard(text).then(() => setCopied(true));
      }}
      className={`${iconButtonClass} ${className}`}
      aria-label="Copy code block"
      title={copied ? 'Copied' : 'Copy'}
    >
      {copied ? <Check className="h-3.5 w-3.5" aria-hidden="true" /> : <CopyIcon className="h-3.5 w-3.5" aria-hidden="true" />}
    </button>
  );
}

function CodeBlock({ code, className }: { code: string; className?: string }) {
  return (
    <div className="group relative my-3 overflow-hidden rounded-lg border border-slate-700 bg-slate-950 text-slate-100">
      <div className="absolute right-2 top-2 z-10 opacity-95 transition group-hover:opacity-100">
        <CopyButton text={code} />
      </div>
      <pre className="!m-0 overflow-x-auto p-4 pr-20 text-sm">
        <code className={className}>{code}</code>
      </pre>
    </div>
  );
}

function getSvgIntrinsicWidth(svg: string): number | null {
  const viewBoxMatch = svg.match(/viewBox=["']\s*[-\d.]+\s+[-\d.]+\s+([\d.]+)/i);
  if (viewBoxMatch) {
    const width = Number(viewBoxMatch[1]);
    if (Number.isFinite(width) && width > 0) return width;
  }

  const widthMatch = svg.match(/\swidth=["']([\d.]+)(?:px)?["']/i);
  if (widthMatch) {
    const width = Number(widthMatch[1]);
    if (Number.isFinite(width) && width > 0) return width;
  }

  return null;
}

function normalizeMermaidGanttDependencies(line: string): string {
  // Mermaid expects multiple Gantt dependencies as "after b1 b2", while LLMs often emit "after b1,b2".
  return line.replace(/(\bafter\s+)([A-Za-z_][\w-]*(?:\s*,\s*[A-Za-z_][\w-]*)+)(?=\s*,)/g, (_match, prefix: string, deps: string) => {
    return `${prefix}${deps.replace(/\s*,\s*/g, ' ')}`;
  });
}

function normalizeMermaidChart(chart: string): string {
  return normalizeMermaidTimelineChart(chart)
    .split('\n')
    .map((line) => normalizeMermaidGanttDependencies(line)
      .replace(
        /^(\s*[A-Za-z][\w-]*)(\[)([^\]"'\n]*(?:[<>()][^\]"'\n]*)+)(\])(?=\s*(?:::+|[-.=]|$))/,
        (_match, id: string, open: string, label: string, close: string) => {
          const escapedLabel = label.replace(/"/g, '&quot;');
          return `${id}${open}"${escapedLabel}"${close}`;
        },
      ))
    .join('\n');
}

function normalizeMermaidSvg(svg: string): string {
  const width = getSvgIntrinsicWidth(svg);
  if (!width) {
    return svg;
  }

  const withWidth = svg.replace(/\swidth=["'][^"']*["']/i, ` width="${width}px"`);
  if (/\sheight=["'][^"']*["']/i.test(withWidth)) {
    return withWidth;
  }
  return withWidth.replace(/<svg\b/i, '<svg height="auto"');
}

function MermaidDiagram({ chart }: { chart: string }) {
  const reactId = useId();
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [baseZoom, setBaseZoom] = useState(1);
  const [moveEnabled, setMoveEnabled] = useState(false);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [dragStart, setDragStart] = useState<{ pointerId: number; x: number; y: number; panX: number; panY: number } | null>(null);
  const [prefersDark, setPrefersDark] = useState(
    () => typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches === true,
  );

  useEffect(() => {
    const queryList = window.matchMedia('(prefers-color-scheme: dark)');
    return subscribeToDarkScheme(queryList, setPrefersDark);
  }, []);


  const zoomOut = () => setZoom((current) => getNextMermaidZoom(current, -MERMAID_ZOOM_STEP));
  const zoomIn = () => setZoom((current) => getNextMermaidZoom(current, MERMAID_ZOOM_STEP));
  const resetZoom = () => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
    setDragStart(null);
  };

  const handlePointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!moveEnabled) {
      return;
    }

    event.currentTarget.setPointerCapture(event.pointerId);
    setDragStart({ pointerId: event.pointerId, x: event.clientX, y: event.clientY, panX: pan.x, panY: pan.y });
  };

  const handlePointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!moveEnabled || !dragStart || dragStart.pointerId !== event.pointerId) {
      return;
    }

    setPan({
      x: dragStart.panX + event.clientX - dragStart.x,
      y: dragStart.panY + event.clientY - dragStart.y,
    });
  };

  const stopDragging = (event: React.PointerEvent<HTMLDivElement>) => {
    if (dragStart?.pointerId === event.pointerId) {
      setDragStart(null);
    }
  };

  useEffect(() => {
    let cancelled = false;

    async function renderDiagram() {
      setSvg(null);
      setError(null);
      setBaseZoom(1);

      try {
        const mermaid = (await import('mermaid')).default;
        mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: getMermaidThemeName(prefersDark) });
        const safeId = reactId.replace(/[^a-zA-Z0-9_-]/g, '');
        const renderChart = normalizeMermaidChart(chart);
        const result = await mermaid.render(`mermaid-${safeId}`, renderChart);
        if (!cancelled) {
          setSvg(normalizeMermaidSvg(result.svg));
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to render Mermaid diagram');
        }
      }
    }

    void renderDiagram();

    return () => {
      cancelled = true;
    };
  }, [chart, reactId, prefersDark]);

  useEffect(() => {
    if (!svg || !viewportRef.current) {
      return;
    }

    const svgWidth = getSvgIntrinsicWidth(svg);
    if (!svgWidth) {
      return;
    }

    const viewport = viewportRef.current;
    const updateBaseZoom = () => {
      const containerWidth = viewport.clientWidth;
      if (!containerWidth) return;
      const targetWidth = containerWidth * 0.8;
      setBaseZoom(Number((targetWidth / svgWidth).toFixed(4)) || 1);
    };

    updateBaseZoom();
    const observer = new ResizeObserver(updateBaseZoom);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [svg]);

  if (error) {
    return (
      <div className="my-3 rounded-lg border border-red-300 bg-red-50 p-3 text-red-900 dark:border-red-800 dark:bg-red-950/40 dark:text-red-100">
        <div className="mb-2 flex items-center justify-between gap-3">
          <div className="text-xs font-semibold uppercase tracking-wide">Mermaid render failed</div>
          <CopyButton text={chart} />
        </div>
        <div className="mb-3 text-xs opacity-80">{error}</div>
        <pre className="overflow-x-auto rounded bg-slate-950 p-3 text-xs text-slate-100">
          <code>{chart}</code>
        </pre>
      </div>
    );
  }

  if (!svg) {
    return (
      <div className="my-3 rounded-lg border border-slate-200 bg-slate-50 p-4 text-sm text-slate-500 dark:border-slate-700 dark:bg-slate-900/70 dark:text-slate-400">
        Rendering Mermaid diagram...
      </div>
    );
  }

  return (
    <div className={MERMAID_CONTAINER_CLASS}>
      <div className="flex flex-wrap items-center justify-end gap-2 border-b border-slate-200 bg-slate-50 px-3 py-2 dark:border-slate-700 dark:bg-slate-900">
        <CopyButton text={chart} />
        <button
          type="button"
          onClick={() => setMoveEnabled((current) => !current)}
          className={getMermaidMoveButtonClass(moveEnabled)}
          aria-pressed={moveEnabled}
          aria-label="Move Mermaid diagram"
          title={moveEnabled ? 'Move enabled' : 'Move'}
        >
          <MoveIcon className={getMermaidMoveIconClass()} aria-hidden="true" />
        </button>
        <div className="flex items-center gap-1" aria-label="Mermaid zoom controls">
          <button
            type="button"
            onClick={zoomOut}
            className={iconButtonClass}
            disabled={zoom <= MERMAID_MIN_ZOOM}
            aria-label="Zoom out"
            title="Zoom out"
          >
            <ZoomOut className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
          <button
            type="button"
            onClick={resetZoom}
            className="min-w-14 rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-700 shadow-sm transition hover:bg-slate-100 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:hover:bg-slate-700"
            aria-label="Reset zoom"
          >
            {Math.round(zoom * 100)}%
          </button>
          <button
            type="button"
            onClick={zoomIn}
            className={iconButtonClass}
            disabled={zoom >= MERMAID_MAX_ZOOM}
            aria-label="Zoom in"
            title="Zoom in"
          >
            <ZoomIn className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </div>
      </div>
      <div
        ref={viewportRef}
        className={getMermaidViewportClass(moveEnabled)}
        style={{ minHeight: 'var(--webui-mermaid-min-height, 360px)' }}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={stopDragging}
        onPointerCancel={stopDragging}
      >
        <div
          className={MERMAID_CONTENT_CLASS}
          style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${baseZoom * zoom})`, transformOrigin: 'top left' }}
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      </div>
    </div>
  );
}

const markdownComponents: Components = {
  a({ children, ...props }) {
    return (
      <a {...props} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    );
  },
  pre({ children, ...props }) {
    const child = React.Children.only(children);

    if (!React.isValidElement<CodeElementProps>(child)) {
      return <pre {...props}>{children}</pre>;
    }

    const code = String(child.props.children ?? '').replace(/\n$/, '');
    const className = child.props.className;

    if (typeof className === 'string' && /\blanguage-mermaid\b/i.test(className)) {
      const chart = code;
      return <MermaidDiagram chart={chart} />;
    }

    return <CodeBlock code={code} className={className} />;
  },
};

export default function MarkdownRenderer({ children, className }: MarkdownRendererProps) {
  return (
    <div className={className}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[[rehypeKatex, { throwOnError: false }]]}
        components={markdownComponents}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
