'use client';

import { useEffect, useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { HoverHint } from './HoverHint';

async function copyAnswer(text: string) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // HTTP deployments and denied permissions can still use the fallback.
    }
  }

  const activeElement = document.activeElement;
  const selection = window.getSelection();
  const ranges = selection
    ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index).cloneRange())
    : [];
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  textarea.style.top = '0';
  document.body.appendChild(textarea);

  try {
    textarea.select();
    if (!document.execCommand('copy')) {
      throw new Error('Copy failed');
    }
  } finally {
    textarea.remove();
    if (activeElement instanceof HTMLElement) {
      activeElement.focus({ preventScroll: true });
    }
    if (selection) {
      selection.removeAllRanges();
      ranges.forEach((range) => selection.addRange(range));
    }
  }
}

export default function AssistantBubbleActions({ body, isStreaming }: {
  body: string;
  isStreaming: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);

  useEffect(() => {
    setCopied(false);
    setCopyFailed(false);
  }, [body]);

  useEffect(() => {
    if (!copied) return;
    const timeout = window.setTimeout(() => setCopied(false), 1400);
    return () => window.clearTimeout(timeout);
  }, [copied]);

  if (isStreaming || !body.trim()) {
    return null;
  }

  const label = copied ? '已复制' : '复制回答';

  return (
    <div className="mt-2 flex items-center gap-2">
      <HoverHint label={label}>
        <button
          type="button"
          aria-label={label}
          onClick={() => {
            setCopyFailed(false);
            void copyAnswer(body).then(() => setCopied(true)).catch(() => {
              setCopied(false);
              setCopyFailed(true);
            });
          }}
          className="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nvidia-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
        >
          {copied ? <Check className="h-4 w-4" aria-hidden="true" /> : <Copy className="h-4 w-4" aria-hidden="true" />}
        </button>
      </HoverHint>
      {copyFailed && <span role="status" className="text-xs text-red-600 dark:text-red-400">复制失败，请重试</span>}
    </div>
  );
}
