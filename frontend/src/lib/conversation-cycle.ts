import { CycleRecoveryError, type LLMClient } from './api.ts';
import { createCycleAgentProjection } from './cycle-agent-projection.ts';
import { validateCycleProgram, type CyclePageMode } from './cycle-capability.ts';
import {
  classifyCycleEvent, createCycleEventGuard, selectCycleRecovery,
  writeOwnedCycleBookmark, clearOwnedCycleBookmark, migrateConfirmedVisionCycleBookmark,
  updateCycleVisibility, type CycleStorage, type CycleBookmark,
} from './cycle-page.ts';
import { getBrowserCycleTimezone, type CycleExecutionStatus, type CycleSSEEvent, type CycleStartRequest } from './cycle-types.ts';
import type { Message } from './message-types.ts';

export interface CycleStartGuardLike {
  readonly active: boolean;
  begin(): symbol | null;
  isCurrent(token: symbol): boolean;
  finish(token: symbol): boolean;
  cancel(): void;
}
export function createCycleStartGuard(): CycleStartGuardLike {
  let current: symbol | null = null;
  return {
    get active() { return current !== null; },
    begin() { if (current) return null; current = Symbol('cycle-start'); return current; },
    isCurrent(token) { return current === token; },
    finish(token) { if (current !== token) return false; current = null; return true; },
    cancel() { current = null; },
  };
}
export interface ConversationCycleRuntimeOptions {
  showThumbnails: boolean;
  /** Browser-local input capability; it never enters the Cycle request body. */
  allowPhotos?: boolean;
  getRequestSettings(): Pick<CycleStartRequest, 'agent_mode' | 'enable_thinking' | 'enable_local_memory' | 'tool_configuration'>;
  isStartBlocked?(): boolean;
  beforeCameraStart?(signal: AbortSignal): Promise<void>;
}
export interface ConversationCycleEnvironment {
  pageMode: CyclePageMode;
  storage(): CycleStorage;
  getOptions(): ConversationCycleRuntimeOptions;
  client: Pick<LLMClient, 'startCycle' | 'stopCycle' | 'getCycleStatus' | 'resumeCycle'>;
  setMessages(update: (previous: Message[]) => Message[]): void;
  now(): Date;
  createId(prefix?: string): string;
}
export interface ConversationCycleSnapshot {
  status: CycleExecutionStatus;
  sessionId: string | null;
  events: CycleSSEEvent[];
  validationError: string | null;
  recoveryNotice: string | null;
}
interface CycleOwner {
  runId: string;
  generation: number;
  fingerprint: string;
  cursor: number;
  submitted: boolean;
  token: symbol | null;
  abort: AbortController;
  transport: AbortController | null;
  legacyPending: CycleBookmark | null;
}

/** One page-owned Cycle lifecycle, with all browser/transport effects injected. */
export function createConversationCycleController(env: ConversationCycleEnvironment) {
  const startGuard = createCycleStartGuard(), eventGuard = createCycleEventGuard();
  let snapshot: ConversationCycleSnapshot = { status: 'idle', sessionId: null, events: [], validationError: null, recoveryNotice: null };
  const listeners = new Set<() => void>();
  const update = (patch: Partial<ConversationCycleSnapshot>) => { snapshot = { ...snapshot, ...patch }; listeners.forEach((listener) => listener()); };
  let owner: CycleOwner | null = null, showSteps = false, userMessageId: string | null = null;
  const projection = createCycleAgentProjection({ setMessages: env.setMessages, now: env.now, createId: (prefix) => env.createId(prefix) });
  const current = (run: CycleOwner) => owner === run && eventGuard.isCurrent(run.generation);
  const photosAllowed = (options: ConversationCycleRuntimeOptions) => env.pageMode === 'vision'
    && (options.allowPhotos === undefined || options.allowPhotos === true);
  const validationMessage = (message: string) => env.pageMode === 'vision'
    ? message.replace('Text 不允许该相机程序', '当前模型不支持该相机程序').replace('请在 Vision 打开', '请使用支持视觉的模型') : message;
  const finishToken = (run: CycleOwner) => { if (run.token) startGuard.finish(run.token); run.token = null; };
  const clearBookmark = (run: CycleOwner) => clearOwnedCycleBookmark(env.storage(), env.pageMode, run.runId);
  const writeBookmark = (run: CycleOwner) => writeOwnedCycleBookmark(env.storage(), {
    version: 2, pageMode: env.pageMode, cycleRunId: run.runId, fingerprint: run.fingerprint, cursor: run.cursor,
  });
  const release = (run: CycleOwner, finalize = true) => {
    if (!current(run)) return;
    finishToken(run); run.abort.abort(); run.transport?.abort();
    eventGuard.invalidate(run.generation); owner = null;
    projection.clear({ finalize }); userMessageId = null; showSteps = false;
  };
  const event = (run: CycleOwner, incoming: CycleSSEEvent) => {
    if (!current(run) || !eventGuard.accept(run.generation, incoming)) return;
    if (Number.isSafeInteger(incoming.event_sequence)) {
      if (incoming.event_sequence! <= run.cursor) return;
      run.cursor = incoming.event_sequence!;
    }
    if (typeof incoming.request_fingerprint === 'string') run.fingerprint = incoming.request_fingerprint;
    if (run.legacyPending) {
      if ((incoming.cycle_run_id ?? incoming.session_id) !== run.runId) return;
      migrateConfirmedVisionCycleBookmark(env.storage(), run.legacyPending, {
        cycleRunId: run.runId, fingerprint: run.fingerprint, cursor: Math.max(run.cursor, run.legacyPending.cursor),
      });
      run.legacyPending = null;
    }
    writeBookmark(run);
    showSteps = updateCycleVisibility(showSteps, incoming);
    update({ events: [...snapshot.events, incoming], sessionId: run.runId });
    const projected = classifyCycleEvent(incoming);
    if (projected.userText !== undefined) userMessageId = projection.beginMessageTurn(projected.userText, showSteps, incoming).userMessageId;
    if (incoming.type === 'photo_captured') {
      if (env.pageMode === 'text') update({ validationError: 'Cycle photo event rejected: Text ownership/capability mismatch' });
      else if (userMessageId && env.getOptions().showThumbnails) {
        const targetId = userMessageId;
        env.setMessages((previous) => previous.map((message) => message.id !== targetId ? message : incoming.image_base64
          ? { ...message, images: [...(message.images ?? []), incoming.image_base64] }
          : incoming.image_omitted ? { ...message, content: `${message.content}\n[历史图片缩略图已省略]` } : message));
      }
    }
    if (incoming.type === 'llm_done') {
      if (!showSteps || !projection.runRef.current) env.setMessages((previous) => [...previous, {
        id: env.createId('cycle-assistant'), role: 'assistant', content: incoming.content ?? '', timestamp: env.now(), bubbleKind: 'cycle',
      }]);
      // The existing projection supplies empty-stream fallback and terminal settlement.
      if (showSteps) void projection.dispatch(incoming, true); else void projection.finish(incoming.session_id);
      userMessageId = null;
    } else if (['agent_step', 'orchestration_plan', 'orchestration_update', 'orchestration_done', 'timeline_event', 'llm_chunk'].includes(incoming.type)) {
      void projection.dispatch(incoming, showSteps);
    } else if (incoming.type === 'step_failed' || incoming.type === 'scheduled_message_failed') {
      void projection.fail(new Error(incoming.error ?? 'Cycle message failed'));
    }
    if (projected.terminal) { clearBookmark(run); release(run); update({ status: projected.terminal }); }
  };
  const failed = (run: CycleOwner, thrown: unknown) => {
    if (!current(run)) return;
    const error = thrown instanceof Error ? thrown : new Error(String(thrown));
    const recovery = error instanceof CycleRecoveryError ? error : null;
    if (!run.submitted || (recovery && !recovery.retryable)) clearBookmark(run);
    release(run);
    const expired = recovery?.code === 'expired';
    update({ status: expired ? 'expired' : 'error', events: [...snapshot.events, {
      type: expired ? 'cycle_expired' : 'cycle_error', error: error.message, reason: recovery?.code ?? 'transport',
    }] });
  };
  const callbacks = (run: CycleOwner) => [
    (incoming: CycleSSEEvent) => event(run, incoming),
    () => { if (current(run)) { finishToken(run); run.transport = null; } },
    (error: Error) => failed(run, error),
  ] as const;
  const newOwner = (runId: string, token: symbol | null): CycleOwner => ({
    runId, generation: eventGuard.begin(runId), fingerprint: '', cursor: 0,
    submitted: false, token, abort: new AbortController(), transport: null, legacyPending: null,
  });
  const cancelStart = () => {
    const run = owner;
    if (run?.submitted) return;
    if (run && !run.submitted) { clearBookmark(run); release(run); update({ status: 'idle', sessionId: null }); }
    else startGuard.cancel();
  };
  const detach = () => { const run = owner; if (run) release(run, false); else startGuard.cancel(); };

  return {
    startGuard,
    getSnapshot: () => snapshot,
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    async start(steps: unknown, scheduledMessages: unknown, shareContext: boolean, showAgentSteps: boolean): Promise<void> {
      const validation = validateCycleProgram(photosAllowed(env.getOptions()) ? 'vision' : 'text', steps, scheduledMessages);
      if (!validation.ok) { update({ validationError: validationMessage(validation.error.message) }); return; }
      if (owner || startGuard.active || env.getOptions().isStartBlocked?.()) return;
      let timezone: string | undefined;
      try { if (validation.program.scheduledMessages.length) timezone = getBrowserCycleTimezone(); }
      catch (error) { update({ validationError: String(error) }); return; }
      const options = env.getOptions();
      let settings: ReturnType<ConversationCycleRuntimeOptions['getRequestSettings']>;
      try { settings = structuredClone(options.getRequestSettings()); }
      catch (error) { update({ validationError: String(error) }); return; }
      const token = startGuard.begin(); if (!token) return;
      const run = newOwner(env.createId(), token); owner = run;
      showSteps = showAgentSteps; projection.clear(); userMessageId = null;
      update({ status: 'running', sessionId: run.runId, events: [], validationError: null, recoveryNotice: null });
      writeBookmark(run);
      try {
        if (validation.hasPhotos && env.pageMode === 'vision') await options.beforeCameraStart?.(run.abort.signal);
        if (!current(run)) return;
        const currentOptions = env.getOptions();
        if (validation.hasPhotos && !photosAllowed(currentOptions)) {
          const rejected = validateCycleProgram('text', validation.program.steps, validation.program.scheduledMessages);
          cancelStart();
          if (!rejected.ok) update({ validationError: validationMessage(rejected.error.message) });
          return;
        }
        if (!startGuard.isCurrent(token) || currentOptions.isStartBlocked?.()) { cancelStart(); return; }
        const request: CycleStartRequest = {
          cycle_run_id: run.runId, steps: validation.program.steps, scheduled_messages: validation.program.scheduledMessages,
          ...(timezone ? { timezone } : {}), agent_mode: settings.agent_mode,
          enable_thinking: settings.enable_thinking, enable_local_memory: settings.enable_local_memory,
          ...(settings.tool_configuration ? { tool_configuration: settings.tool_configuration } : {}),
          share_context: shareContext, show_agent_steps: showAgentSteps,
        };
        run.submitted = true;
        const transport = await env.client.startCycle(request, ...callbacks(run));
        if (current(run)) run.transport = transport; else transport.abort();
      } catch (error) { failed(run, error); }
    },
    async recover(): Promise<void> {
      if (owner) return;
      const selection = selectCycleRecovery(env.storage(), env.pageMode);
      if (selection.kind === 'none') return;
      if (selection.kind === 'foreign-legacy') { update({ recoveryNotice: '相机或旧版 Cycle 请到 Vision 查看/恢复' }); return; }
      const bookmark = selection.bookmark, run = newOwner(bookmark.cycleRunId, null); owner = run;
      // Replay starts at zero to rebuild UI, independently of the durable transport cursor.
      run.fingerprint = bookmark.fingerprint; run.submitted = true;
      update({ status: 'running', sessionId: run.runId, events: [], validationError: null, recoveryNotice: null });
      try {
        const status = await env.client.getCycleStatus(run.runId, run.abort.signal);
        if (!current(run)) return;
        if (status.cycle_run_id !== run.runId || (bookmark.fingerprint && bookmark.fingerprint !== status.request_fingerprint)) {
          throw new CycleRecoveryError('conflict', 'Cycle recovery identity/fingerprint mismatch', 409, false);
        }
        run.fingerprint = status.request_fingerprint;
        if (selection.kind === 'legacy-vision') run.legacyPending = bookmark;
        const transport = env.client.resumeCycle(run.runId, run.fingerprint, 0, ...callbacks(run));
        if (current(run)) run.transport = transport; else transport.abort();
      } catch (error) { failed(run, error); }
    },
    async stop(): Promise<void> {
      const run = owner;
      if (!run) { cancelStart(); return; }
      projection.stop(); clearBookmark(run); release(run, false);
      update({ status: 'stopped', sessionId: null });
      if (run.submitted) {
        try { await env.client.stopCycle(run.runId); }
        catch { /* Local stop is settled; late completion cannot touch a replacement owner. */ }
      }
    },
    cancelStart,
    detach,
    reset({ clearBookmark: clear = false }: { clearBookmark?: boolean } = {}) {
      const run = owner; if (run && clear) clearBookmark(run);
      detach(); projection.clear();
      update({ status: 'idle', sessionId: null, events: [], validationError: null });
    },
  };
}

export type ConversationCycleController = ReturnType<typeof createConversationCycleController>;
