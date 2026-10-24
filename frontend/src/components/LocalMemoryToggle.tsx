'use client';

import { Database } from 'lucide-react';
import { HoverHint } from './HoverHint';

export default function LocalMemoryToggle({ enabled, onChange }: {
  enabled: boolean;
  onChange: (enabled: boolean) => void;
}) {
  const label = enabled ? '发送时检索本地记忆' : '发送时不检索本地记忆';

  return (
    <HoverHint label={label}>
      <button
        type="button"
        aria-label={label}
        aria-pressed={enabled}
        onClick={() => onChange(!enabled)}
        className={`inline-flex items-center rounded-lg p-2 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400 disabled:cursor-not-allowed disabled:opacity-40 ${enabled
          ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
          : 'text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700'}`}
      >
        <Database className="h-5 w-5" aria-hidden="true" />
      </button>
    </HoverHint>
  );
}
