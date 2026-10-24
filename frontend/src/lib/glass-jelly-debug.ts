export function shouldLogGlassJellyDebug(): boolean {
  if (typeof window === 'undefined') return false;
  if (window.localStorage.getItem('webuiGlassJellyDebug') === '0') return false;
  return process.env.NODE_ENV !== 'production' || window.localStorage.getItem('webuiGlassJellyDebug') === '1';
}

export function logGlassJellyDebug(label: string, payload?: Record<string, unknown>): void {
  if (!shouldLogGlassJellyDebug()) return;
  console.debug(`[glass-jelly] ${label}`, payload ?? {});
}
