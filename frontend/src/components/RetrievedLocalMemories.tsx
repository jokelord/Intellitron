'use client';

import { useId, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import type { RetrievedLocalMemoryChunk } from '@/lib/retrieved-local-memory';

export default function RetrievedLocalMemories({ chunks }: { chunks?: RetrievedLocalMemoryChunk[] }) {
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();
  if (!chunks?.length) return null;
  return (
    <div className="mt-2 border-t border-blue-200/70 pt-2 dark:border-blue-800/70">
      <button
        type="button"
        className="flex items-center gap-1 rounded text-xs text-blue-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 dark:text-blue-300"
        aria-expanded={expanded}
        aria-controls={contentId}
        onClick={() => setExpanded((previous) => !previous)}
      >
        {expanded ? <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" /> : <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />}
        查看本地记忆（{chunks.length}）
      </button>
      {expanded && (
        <div id={contentId} className="mt-2 max-h-72 space-y-2 overflow-auto">
          {chunks.map((chunk, index) => (
            <pre key={index} className="whitespace-pre-wrap break-words rounded bg-white/60 p-2 text-xs text-gray-700 dark:bg-black/20 dark:text-gray-300">
              {chunk.text}
            </pre>
          ))}
        </div>
      )}
    </div>
  );
}
