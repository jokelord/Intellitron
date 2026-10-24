'use client';

import type { ManagerParallelChildView } from '@/lib/message-types';

interface ManagerParallelDispatchCardProps {
  dispatchId: string;
  children: ManagerParallelChildView[];
  onChildLinkActivate?: () => void;
}

const STATUS_STYLES: Record<ManagerParallelChildView['status'], string> = {
  pending: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200',
  running: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200',
  completed: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200',
  failed: 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200',
  aborted: 'bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-200',
  timeout: 'bg-orange-100 text-orange-800 dark:bg-orange-900/40 dark:text-orange-200',
};

function truncatePreview(value: string | undefined, maxLength: number): string | undefined {
  if (!value) return undefined;
  const normalized = value.replace(/\s+/g, ' ').trim();
  if (normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, maxLength - 1)}…`;
}

export default function ManagerParallelDispatchCard({
  dispatchId,
  children,
  onChildLinkActivate,
}: ManagerParallelDispatchCardProps) {
  if (children.length === 0) return null;

  const terminalCount = children.filter((child) => (
    child.status === 'completed' || child.status === 'failed' || child.status === 'aborted' || child.status === 'timeout'
  )).length;

  return (
    <section className="my-3 overflow-hidden rounded-2xl border border-slate-200 bg-slate-50/80 text-slate-900 shadow-sm dark:border-slate-700 dark:bg-slate-900/50 dark:text-slate-100">
      <div className="flex items-center justify-between gap-3 border-b border-slate-200 px-4 py-3 dark:border-slate-700">
        <div>
          <div className="text-sm font-semibold">Parallel agents</div>
          <div className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">Dispatch {dispatchId}</div>
        </div>
        <div className="rounded-full bg-white px-2.5 py-1 text-xs font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300">
          {terminalCount} / {children.length} finished
        </div>
      </div>
      <div className="divide-y divide-slate-200 dark:divide-slate-700">
        {children.map((child) => {
          const href = child.href ?? `/agent/child/${encodeURIComponent(child.childRunId)}`;
          const assignedTask = truncatePreview(child.assignedTask, 180);
          const finalPreview = truncatePreview(child.finalPreview, 160);
          const error = truncatePreview(child.error, 160);

          return (
            <div key={child.childRunId} className="space-y-2 px-4 py-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium">{child.childName}</div>
                  <div className="truncate text-xs text-slate-500 dark:text-slate-400">{child.childRunId}</div>
                </div>
                <div className="flex items-center gap-2">
                  <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[child.status]}`}>
                    {child.status}
                  </span>
                  <a
                    href={href}
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={onChildLinkActivate}
                    className="rounded-full border border-slate-300 px-3 py-1 text-xs font-medium text-slate-700 transition hover:bg-white dark:border-slate-600 dark:text-slate-200 dark:hover:bg-slate-800"
                  >
                    Open child
                  </a>
                </div>
              </div>
              {assignedTask && (
                <p className="text-xs leading-relaxed text-slate-600 dark:text-slate-300">{assignedTask}</p>
              )}
              {finalPreview && (
                <p className="rounded-xl bg-emerald-50 px-3 py-2 text-xs leading-relaxed text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200">
                  {finalPreview}
                </p>
              )}
              {error && (
                <p className="rounded-xl bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-800 dark:bg-red-950/40 dark:text-red-200">
                  {error}
                </p>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
