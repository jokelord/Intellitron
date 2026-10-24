export type HistoricalImageSourceAlias = 'manual' | 'desktop' | `camera_${number}`;

export interface HistoricalImageSummaryCardProps {
  count: number;
  sources: readonly HistoricalImageSourceAlias[];
  relativeRange: string | null;
}

export default function HistoricalImageSummaryCard({
  count,
  sources,
  relativeRange,
}: HistoricalImageSummaryCardProps) {
  return (
    <div className="rounded-xl border border-gray-700/70 bg-gray-900/60 px-3 py-2 text-xs text-gray-300">
      <p className="font-medium text-gray-200">历史图片已汇总</p>
      <p>{count} 张 · {sources.length > 0 ? sources.join(', ') : '来源不可用'}</p>
      <p>{relativeRange ?? '时间范围不可用'}</p>
    </div>
  );
}
