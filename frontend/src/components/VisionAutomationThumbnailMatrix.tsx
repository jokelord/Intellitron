'use client';

import { useEffect, useRef, useState } from 'react';
import type { ImageResource, SourceDescriptor, TickBundle } from '@/lib/vision-automation/types';
import { createImagePreviewURLLease } from '@/lib/vision-automation/ui-contracts';

export interface VisionAutomationThumbnailMatrixProps {
  sources: readonly SourceDescriptor[];
  bundles: readonly TickBundle[];
  onOpenImage?: (resource: ImageResource, source: SourceDescriptor, bundle: TickBundle) => void;
}

function Thumbnail({
  resource,
  label,
  onOpen,
}: {
  resource: ImageResource;
  label: string;
  onOpen?: () => void;
}) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (resource.thumbnailBlob === null || resource.disposed) return;
    const acquired = resource.acquireObjectURL('thumbnail');
    setUrl(acquired);
    return () => {
      resource.releaseObjectURL(acquired);
    };
  }, [resource]);
  if (url === null) return <div className="h-10 w-10 rounded-md bg-gray-800" aria-label={`${label} 缩略图不可用`} />;
  return (
    <button type="button" onClick={onOpen} className="rounded-md focus:outline-none focus:ring-2 focus:ring-blue-400">
      <img src={url} alt={label} className="h-10 w-10 rounded-md object-cover" />
    </button>
  );
}

interface PreviewSelection {
  resource: ImageResource;
  source: SourceDescriptor;
  bundle: TickBundle;
}

function WorkingImagePreview({ selection, onClose }: { selection: PreviewSelection; onClose: () => void }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let lease: ReturnType<typeof createImagePreviewURLLease> | null = null;
    try {
      lease = createImagePreviewURLLease(selection.resource);
      setUrl(lease.url);
    } catch {
      setUrl(null);
    }
    return () => lease?.release();
  }, [selection]);

  return (
    <div role="dialog" aria-modal="true" aria-label="视觉自动化图片预览" className="fixed inset-0 z-[100] flex items-center justify-center bg-black/80 p-4" onClick={onClose}>
      <div className="max-h-full max-w-full rounded-xl bg-gray-950 p-3" onClick={(event) => event.stopPropagation()}>
        <div className="mb-2 flex items-center justify-between gap-4 text-xs text-gray-300">
          <span>{selection.source.stableAlias} · tick {selection.bundle.tickIndex} · {selection.bundle.relativeTimeSeconds.toFixed(2)}s</span>
          <button type="button" onClick={onClose} className="rounded bg-gray-800 px-2 py-1 text-gray-100">关闭</button>
        </div>
        {url ? <img src={url} alt={`${selection.source.stableAlias} 上传图片预览`} className="max-h-[calc(100dvh-6rem)] max-w-[calc(100vw-2rem)] object-contain" /> : <p className="text-sm text-gray-400">预览不可用</p>}
      </div>
    </div>
  );
}

export default function VisionAutomationThumbnailMatrix({
  sources,
  bundles,
  onOpenImage,
}: VisionAutomationThumbnailMatrixProps) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const [followLatest, setFollowLatest] = useState(true);
  const [preview, setPreview] = useState<PreviewSelection | null>(null);

  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller || !followLatest) return;
    scroller.scrollLeft = scroller.scrollWidth;
  }, [bundles.length, followLatest]);

  return (
    <div className="space-y-2">
      {!followLatest && (
        <button type="button" onClick={() => setFollowLatest(true)} className="text-xs text-blue-300 hover:text-blue-200">
          回到最新
        </button>
      )}
      <div
        ref={scrollerRef}
        className="overflow-x-auto pb-2 touch-pan-x"
        onScroll={(event) => {
          const target = event.currentTarget;
          setFollowLatest(target.scrollWidth - target.clientWidth - target.scrollLeft < 8);
        }}
      >
        <div
          className="grid min-w-max gap-2"
          style={{ gridTemplateColumns: `minmax(7rem, auto) repeat(${bundles.length}, 2.5rem)` }}
        >
          {sources.map((source) => (
            <div key={source.sourceId} className="contents">
              <div className="sticky left-0 z-10 flex items-center bg-gray-950/95 pr-2 text-xs text-gray-300">
                {source.stableAlias === 'desktop' ? 'Desktop' : `Camera ${source.stableAlias.slice(7)}`}
              </div>
              {bundles.map((bundle) => {
                const frame = bundle.frames.find((item) => item.source.stableAlias === source.stableAlias);
                return frame ? (
                  <Thumbnail
                    key={`${source.sourceId}-${bundle.tickIndex}`}
                    resource={frame.resource}
                    label={`${source.stableAlias} ${bundle.relativeTimeSeconds.toFixed(2)}s`}
                    onOpen={() => {
                      setPreview({ resource: frame.resource, source, bundle });
                      onOpenImage?.(frame.resource, source, bundle);
                    }}
                  />
                ) : <div key={`${source.sourceId}-${bundle.tickIndex}`} className="h-10 w-10 rounded-md bg-red-950/40" />;
              })}
            </div>
          ))}
        </div>
      </div>
      {preview && <WorkingImagePreview selection={preview} onClose={() => setPreview(null)} />}
    </div>
  );
}
