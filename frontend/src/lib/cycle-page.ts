import type { CycleExecutionStatus, CycleSSEEvent } from './cycle-types.ts';
import { updateCycleVisibility } from './cycle-agent-projection.ts';
import type { CyclePageMode } from './cycle-capability.ts';

export { updateCycleVisibility } from './cycle-agent-projection.ts';

export interface CycleEventGuard {
  begin(cycleRunId?: string): number;
  accept(generation: number, event?: CycleSSEEvent): boolean;
  isCurrent(generation: number): boolean;
  invalidate(generation?: number): void;
}

export function createCycleEventGuard(): CycleEventGuard {
  let nextGeneration = 0;
  let activeGeneration = 0;
  let activeCycleRunId: string | undefined;
  return {
    begin(cycleRunId) {
      const generation = ++nextGeneration;
      activeGeneration = generation;
      activeCycleRunId = cycleRunId;
      return generation;
    },
    accept(generation, event) {
      if (generation !== activeGeneration) return false;
      const eventRunId = event?.cycle_run_id ?? event?.session_id;
      return !activeCycleRunId || !eventRunId || eventRunId === activeCycleRunId;
    },
    isCurrent(generation) {
      return generation === activeGeneration;
    },
    invalidate(generation) {
      if (generation !== undefined && generation !== activeGeneration) return;
      activeGeneration = 0;
      activeCycleRunId = undefined;
    },
  };
}

export const CYCLE_BOOKMARK_KEY = 'cycle-active-run';

export interface CycleBookmark { cycleRunId: string; fingerprint: string; cursor: number; }
type StorageLike = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
export type CycleStorage = StorageLike;

export interface OwnedCycleBookmark extends CycleBookmark { version: 2; pageMode: CyclePageMode; }
export type CycleRecoverySelection =
  | { kind: 'owned'; bookmark: OwnedCycleBookmark }
  | { kind: 'legacy-vision'; bookmark: CycleBookmark }
  | { kind: 'foreign-legacy' }
  | { kind: 'none' };

const ownedKey = (pageMode: CyclePageMode) => `${CYCLE_BOOKMARK_KEY}:v2:${pageMode}`;
function parseOwned(storage: CycleStorage, pageMode: CyclePageMode): OwnedCycleBookmark | null {
  try {
    const value = JSON.parse(storage.getItem(ownedKey(pageMode)) ?? 'null');
    return value?.version === 2 && value.pageMode === pageMode
      && typeof value.cycleRunId === 'string' && value.cycleRunId.trim()
      && typeof value.fingerprint === 'string' && Number.isSafeInteger(value.cursor) && value.cursor >= 0
      ? { version: 2, pageMode, cycleRunId: value.cycleRunId, fingerprint: value.fingerprint, cursor: value.cursor } : null;
  } catch { return null; }
}

export function selectCycleRecovery(storage: CycleStorage, pageMode: CyclePageMode): CycleRecoverySelection {
  const owned = parseOwned(storage, pageMode);
  if (owned) return { kind: 'owned', bookmark: owned };
  const legacy = readCycleBookmark(storage);
  if (legacy?.cycleRunId.trim()) return pageMode === 'vision' ? { kind: 'legacy-vision', bookmark: legacy } : { kind: 'foreign-legacy' };
  if (pageMode === 'text' && parseOwned(storage, 'vision')) return { kind: 'foreign-legacy' };
  return { kind: 'none' };
}

export function writeOwnedCycleBookmark(storage: CycleStorage, bookmark: OwnedCycleBookmark): void {
  try {
    const previous = parseOwned(storage, bookmark.pageMode);
    const cursor = previous?.cycleRunId === bookmark.cycleRunId ? Math.max(previous.cursor, bookmark.cursor) : bookmark.cursor;
    storage.setItem(ownedKey(bookmark.pageMode), JSON.stringify({ ...bookmark, cursor }));
  } catch { /* optional storage */ }
}

export function clearOwnedCycleBookmark(storage: CycleStorage, pageMode: CyclePageMode, expectedRunId: string): void {
  try { if (parseOwned(storage, pageMode)?.cycleRunId === expectedRunId) storage.removeItem(ownedKey(pageMode)); } catch { /* optional storage */ }
}

export function migrateConfirmedVisionCycleBookmark(storage: CycleStorage, original: CycleBookmark, confirmed: CycleBookmark): void {
  if (original.cycleRunId !== confirmed.cycleRunId) return;
  try {
    const current = readCycleBookmark(storage);
    if (!current || current.cycleRunId !== original.cycleRunId || current.fingerprint !== original.fingerprint || current.cursor !== original.cursor) return;
    const owned = parseOwned(storage, 'vision');
    if (owned && owned.cycleRunId !== original.cycleRunId) return;
    writeOwnedCycleBookmark(storage, { ...confirmed, cursor: Math.max(original.cursor, confirmed.cursor), version: 2, pageMode: 'vision' });
    const written = parseOwned(storage, 'vision');
    const stillLegacy = readCycleBookmark(storage);
    if (written?.cycleRunId === confirmed.cycleRunId && written.fingerprint === confirmed.fingerprint
      && written.cursor >= Math.max(original.cursor, confirmed.cursor)
      && stillLegacy?.cycleRunId === original.cycleRunId && stillLegacy.fingerprint === original.fingerprint
      && stillLegacy.cursor === original.cursor) storage.removeItem(CYCLE_BOOKMARK_KEY);
  } catch { /* leave the legacy pointer intact on failure */ }
}

export function readCycleBookmark(storage: StorageLike): CycleBookmark | null {
  try {
    const raw = storage.getItem(CYCLE_BOOKMARK_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<CycleBookmark>;
    if (typeof value.cycleRunId !== 'string' || !value.cycleRunId ||
        typeof value.fingerprint !== 'string' ||
        !Number.isSafeInteger(value.cursor) || (value.cursor ?? -1) < 0) return null;
    return value as CycleBookmark;
  } catch { return null; }
}

export function writeCycleBookmark(storage: StorageLike, bookmark: CycleBookmark): void {
  try { storage.setItem(CYCLE_BOOKMARK_KEY, JSON.stringify(bookmark)); } catch { /* storage is optional */ }
}

export function clearCycleBookmark(storage: StorageLike): void {
  try { storage.removeItem(CYCLE_BOOKMARK_KEY); } catch { /* storage is optional */ }
}

export function classifyCycleEvent(event: CycleSSEEvent): { userText?: string; terminal?: CycleExecutionStatus } {
  if (event.type === 'step_begin' && event.step_type === 'message') {
    const message = typeof event.detail?.message === 'string' ? event.detail.message : '';
    const prefix = event.track === 'scheduled' ? `⏰ 定时 message${event.scheduled_for ? ` · ${event.scheduled_for}` : ''}` : '';
    return { userText: prefix ? (message ? `${prefix}\n${message}` : prefix) : message };
  }
  const terminals: Record<string, CycleExecutionStatus> = {
    cycle_completed: 'completed', cycle_stopped: 'stopped',
    cycle_error: 'error', cycle_expired: 'expired',
  };
  return { terminal: terminals[event.type] };
}
