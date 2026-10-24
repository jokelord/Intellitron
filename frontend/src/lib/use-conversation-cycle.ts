'use client';

import { useEffect, useRef, useSyncExternalStore, type Dispatch, type SetStateAction } from 'react';
import { llmClient } from './api.ts';
import { createConversationCycleController, type ConversationCycleRuntimeOptions } from './conversation-cycle.ts';
import type { CyclePageMode } from './cycle-capability.ts';
import type { Message } from './message-types.ts';

const unavailableStorage = { getItem: () => null, setItem: () => undefined, removeItem: () => undefined };

export interface ConversationCycleOptions extends ConversationCycleRuntimeOptions {
  pageMode: CyclePageMode;
  setMessages: Dispatch<SetStateAction<Message[]>>;
}

/** React owns the snapshot; the controller owns one page's Cycle resources. */
export function useConversationCycle(options: ConversationCycleOptions) {
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const controllerRef = useRef<ReturnType<typeof createConversationCycleController> | null>(null);
  if (!controllerRef.current) controllerRef.current = createConversationCycleController({
    pageMode: options.pageMode,
    getOptions: () => optionsRef.current,
    storage: () => { try { return window.localStorage; } catch { return unavailableStorage; } },
    client: llmClient,
    setMessages: (update) => optionsRef.current.setMessages(update),
    now: () => new Date(),
    createId: () => crypto.randomUUID(),
  });
  const controller = controllerRef.current;
  const snapshot = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const cycleStartGuardRef = useRef(controller.startGuard);
  useEffect(() => {
    void controller.recover();
    return () => controller.detach();
  }, [controller]);

  return {
    cycleStatus: snapshot.status,
    cycleSessionId: snapshot.sessionId,
    cycleEvents: snapshot.events,
    cycleValidationError: snapshot.validationError,
    cycleRecoveryNotice: snapshot.recoveryNotice,
    cycleStartGuardRef,
    handleCycleStart: controller.start,
    handleCycleStop: controller.stop,
    cancelCycleStart: controller.cancelStart,
    resetCycle: controller.reset,
  };
}
