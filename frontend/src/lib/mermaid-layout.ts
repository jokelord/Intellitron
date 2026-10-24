export const MERMAID_CONTAINER_CLASS = "mermaid-diagram my-3 min-w-0 w-full overflow-hidden rounded-lg border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-950";

export const MERMAID_CONTENT_CLASS = "inline-block max-w-none transition-transform duration-150 ease-out";

const MERMAID_ICON_BUTTON_CLASS = "inline-flex h-7 w-7 items-center justify-center rounded-md border border-slate-300 bg-white text-slate-700 shadow-sm transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:hover:bg-slate-700";

const MERMAID_MOVE_BUTTON_OFF = "inline-flex h-7 w-7 items-center justify-center rounded-md border border-slate-300 bg-white text-slate-700 shadow-sm transition hover:bg-slate-100 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100 dark:hover:bg-slate-700";
const MERMAID_MOVE_BUTTON_ON = "inline-flex h-7 w-7 items-center justify-center rounded-md border border-blue-500 bg-blue-100 text-blue-700 shadow-sm transition hover:bg-blue-200 dark:border-blue-400 dark:bg-blue-900 dark:text-blue-200 dark:hover:bg-blue-800";

const MERMAID_MOVE_ICON_CLASS = "h-3.5 w-3.5";

export const MERMAID_MIN_ZOOM = 0.1;
export const MERMAID_MAX_ZOOM = 6;
export const MERMAID_ZOOM_STEP = 0.1;

export function getMermaidViewportClass(moveEnabled: boolean): string {
  return `min-w-0 w-full overflow-hidden p-4 ${moveEnabled ? "cursor-grab touch-none active:cursor-grabbing" : "overflow-auto"}`;
}

export function getMermaidMoveButtonClass(moveEnabled: boolean): string {
  return moveEnabled ? MERMAID_MOVE_BUTTON_ON : MERMAID_MOVE_BUTTON_OFF;
}

export function getMermaidMoveIconClass(): string {
  return MERMAID_MOVE_ICON_CLASS;
}

export function getMermaidIconButtonClass(): string {
  return MERMAID_ICON_BUTTON_CLASS;
}

export function getNextMermaidZoom(current: number, delta: number): number {
  const next = Number((current + delta).toFixed(2));
  return Math.min(MERMAID_MAX_ZOOM, Math.max(MERMAID_MIN_ZOOM, next));
}
