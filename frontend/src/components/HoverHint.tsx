'use client';

import type React from 'react';

export function HoverHint({
  label,
  children,
  className = '',
}: {
  label: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <span className={`group relative inline-flex ${className}`}>
      {children}
      <span className="pointer-events-none absolute left-1/2 top-full z-50 mt-1.5 -translate-x-1/2 whitespace-nowrap rounded-md bg-gray-900 px-2 py-1 text-xs font-medium text-white shadow-lg dark:bg-gray-100 dark:text-gray-900 opacity-0 group-hover:opacity-100">
        {label}
      </span>
    </span>
  );
}
