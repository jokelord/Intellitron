"use client";

import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronUp, RefreshCw } from 'lucide-react';
import CycleStepEditor from './CycleStepEditor';
import {
  type CycleEditorState,
  type CycleExecutionStatus,
  type CycleStep,
  type ScheduledMessage,
  createCycleStep,
  createMessageStep,
  createScheduledMessage,
  createWaitStep,
  resolveCycleEditorState,
  validateScheduledMessages,
} from '@/lib/cycle-types';
import { validateCycleEditorProgram, validateCycleProgram } from '@/lib/cycle-capability';

interface CycleEditorProps {
  isVisionMode: boolean;
  maxDepth: number;
  defaultSteps: CycleStep[];
  maxScheduledMessageDays?: number;
  defaultScheduledMessages: ScheduledMessage[];
  executionStatus: CycleExecutionStatus;
  onStart: (steps: CycleStep[], scheduledMessages: ScheduledMessage[], shareContext: boolean, showAgentSteps: boolean) => void | Promise<void>;
  onStop: () => void;
}

const STORAGE_KEY = 'cycle-editor-state';

export default function CycleEditor({ isVisionMode, maxDepth, defaultSteps, maxScheduledMessageDays, defaultScheduledMessages, executionStatus, onStart, onStop }: CycleEditorProps) {
  const [steps, setSteps] = useState<CycleStep[]>([]);
  const [scheduledMessages, setScheduledMessages] = useState<ScheduledMessage[]>([]);
  const [shareContext, setShareContext] = useState(true);
  const [showAgentSteps, setShowAgentSteps] = useState(false);
  const [isStarting, setIsStarting] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);
  const hasLoadedRef = useRef(false);
  const dirtyRef = useRef(false);
  const [invalidStoredProgram, setInvalidStoredProgram] = useState(false);
  const isRunning = executionStatus === 'running';

  useEffect(() => {
    if (hasLoadedRef.current) return;
    try {
      let raw: string | null = null;
      try { raw = localStorage.getItem(STORAGE_KEY); } catch { /* optional storage; keep configured defaults */ }
      const state = resolveCycleEditorState(raw, defaultSteps, defaultScheduledMessages);
      const saved: unknown = raw ? JSON.parse(raw) : null;
      const candidate = saved && typeof saved === 'object' ? saved as Record<string, unknown> : null;
      const checked = validateCycleEditorProgram(candidate ? candidate.steps : state.steps,
        candidate && 'scheduledMessages' in candidate ? candidate.scheduledMessages : state.scheduledMessages ?? [], maxDepth);
      if (!checked.ok) {
        setValidationError(checked.error.message);
        setInvalidStoredProgram(true);
        hasLoadedRef.current = true;
        return;
      }
      setSteps(checked.program.steps);
      setScheduledMessages(checked.program.scheduledMessages);
      setShareContext(state.shareContext);
      setShowAgentSteps(state.showAgentSteps);
    } catch {
      setValidationError('steps: 无法读取 Cycle 程序；保存内容未修改');
      setInvalidStoredProgram(true);
    }
    hasLoadedRef.current = true;
  }, [defaultSteps, defaultScheduledMessages, maxDepth]);

  useEffect(() => {
    if (!dirtyRef.current || invalidStoredProgram) return;
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ steps, scheduledMessages, shareContext, showAgentSteps } satisfies CycleEditorState)); } catch { /* storage is optional */ }
  }, [steps, scheduledMessages, shareContext, showAgentSteps, invalidStoredProgram]);

  const handleStart = async () => {
    if (invalidStoredProgram) return;
    const program = validateCycleProgram(isVisionMode ? 'vision' : 'text', steps, scheduledMessages);
    if (!program.ok) { setValidationError(program.error.message); return; }
    if (isStarting) return;
    const error = validateScheduledMessages(scheduledMessages, scheduledMessages.length ? Intl.DateTimeFormat().resolvedOptions().timeZone : '', maxScheduledMessageDays);
    if (error) { setValidationError(error); return; }
    setValidationError(null);
    setIsStarting(true);
    try { await onStart(steps, scheduledMessages, shareContext, showAgentSteps); } finally { setTimeout(() => setIsStarting(false), 1000); }
  };
  const edit = () => { dirtyRef.current = true; setInvalidStoredProgram(false); };
  const addStep = (type: CycleStep['type']) => { edit(); setSteps([...steps, type === 'wait' ? createWaitStep() : type === 'message' ? createMessageStep() : createCycleStep()]); };
  const capability = validateCycleProgram(isVisionMode ? 'vision' : 'text', steps, scheduledMessages);
  const photoError = !capability.ok && capability.error.code === 'text_camera_forbidden' ? capability.error.message : null;
  const resetProgram = () => {
    const checked = validateCycleEditorProgram(defaultSteps, defaultScheduledMessages, maxDepth);
    if (!checked.ok) { setValidationError(checked.error.message); return; }
    edit(); setValidationError(null); setSteps(checked.program.steps); setScheduledMessages(checked.program.scheduledMessages); setShareContext(true); setShowAgentSteps(false);
  };

  return <section className="cycle-canvas rounded-lg border">
    <header className="flex items-center justify-between gap-2 px-3 py-2">
      <div className="flex min-w-0 items-center gap-2"><RefreshCw className="h-4 w-4 text-blue-600 dark:text-blue-300" /><span className="truncate text-sm font-medium">编程循环</span>
        <label className="flex items-center gap-1.5 text-xs cycle-muted"><input type="checkbox" checked={shareContext} onChange={(e) => { edit(); setShareContext(e.target.checked); }} disabled={isRunning || invalidStoredProgram} className="rounded" />Share context</label>
        <label className="flex items-center gap-1.5 text-xs cycle-muted"><input type="checkbox" checked={showAgentSteps} onChange={(e) => { edit(); setShowAgentSteps(e.target.checked); }} disabled={isRunning || invalidStoredProgram} className="rounded" />Show agent steps</label>
      </div>
      <div className="flex items-center gap-1.5">{isRunning ? <button onClick={onStop} className="rounded bg-red-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-red-500">Stop</button> : <button onClick={handleStart} disabled={isStarting || (!steps.length && !scheduledMessages.length)} className="rounded bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50">{isStarting ? 'Starting...' : 'Start'}</button>}
        <button type="button" aria-expanded={!collapsed} aria-label={collapsed ? '展开编程循环' : '折叠编程循环'} onClick={() => setCollapsed(!collapsed)} className="rounded-lg p-2 cycle-muted hover:bg-black/5 dark:hover:bg-white/10">{collapsed ? <ChevronDown className="h-4 w-4" /> : <ChevronUp className="h-4 w-4" />}</button>
      </div>
    </header>
    {!collapsed && <div className="space-y-4 border-t cycle-border px-3 py-3">
      <section className="space-y-2"><div className="flex items-center justify-between"><h3 className="text-sm font-medium">普通轨道</h3><span className="text-xs cycle-muted">wait / message / cycle</span></div>
        <div className="max-h-60 space-y-1 overflow-y-auto">{steps.map((step, index) => <CycleStepEditor key={index} step={step} onChange={(next) => { edit(); setSteps((previous) => previous.map((item, i) => i === index ? next : item)); }} onRemove={() => { edit(); setSteps((previous) => previous.filter((_, i) => i !== index)); }} depth={0} maxDepth={maxDepth} isVisionMode={isVisionMode} />)}</div>
        {!isRunning && <div className="flex items-center gap-2"><button disabled={invalidStoredProgram} onClick={() => addStep('wait')} className="cycle-button">+ Wait</button><button disabled={invalidStoredProgram} onClick={() => addStep('message')} className="cycle-button">+ Message</button><button disabled={invalidStoredProgram} onClick={() => addStep('cycle')} className="cycle-button">+ Cycle</button><button onClick={resetProgram} className="ml-auto text-xs cycle-muted hover:underline">Reset</button></div>}
      </section>
      <section className="space-y-2 border-t cycle-border pt-3"><div className="flex items-center justify-between"><div><h3 className="text-sm font-medium">定时轨道</h3><p className="text-xs cycle-muted">固定时刻发送；不随普通循环重复</p></div>{!isRunning && <button disabled={invalidStoredProgram} onClick={() => { edit(); setScheduledMessages((items) => [...items, createScheduledMessage()]); }} className="cycle-button">+ 定时 message</button>}</div>
        <div className="space-y-2">{scheduledMessages.map((item, index) => <ScheduledMessageEditor key={item.id} item={item} isVisionMode={isVisionMode} disabled={isRunning} onChange={(next) => { edit(); setScheduledMessages((items) => items.map((current, i) => i === index ? next : current)); }} onRemove={() => { edit(); setScheduledMessages((items) => items.filter((_, i) => i !== index)); }} />)}</div>
      </section>
      {validationError && <div role="alert" className="rounded bg-red-500/10 px-2 py-1 text-xs text-red-700 dark:text-red-300">{validationError}</div>}
      {photoError && !validationError && <div className="rounded bg-yellow-400/10 px-2 py-1 text-xs text-yellow-700 dark:text-yellow-300">{photoError}</div>}
    </div>}
  </section>;
}

function ScheduledMessageEditor({ item, isVisionMode, disabled, onChange, onRemove }: { item: ScheduledMessage; isVisionMode: boolean; disabled: boolean; onChange: (item: ScheduledMessage) => void; onRemove: () => void }) {
  const [cameraText, setCameraText] = useState(item.photo?.join(', ') ?? '');
  useEffect(() => setCameraText(item.photo?.join(', ') ?? ''), [item.photo]);
  const commitCameras = () => {
    const photo = cameraText.split(',').map((value) => Number.parseInt(value.trim(), 10)).filter(Number.isFinite);
    onChange({ ...item, ...(photo.length ? { photo } : { photo: undefined }) });
  };
  return <article className="rounded-md border cycle-border bg-black/[0.025] p-2 dark:bg-white/[0.04]"><div className="flex flex-wrap items-center gap-2"><label className="text-xs cycle-muted">时间 <input aria-label="定时时间" type="text" inputMode="numeric" pattern="(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]" title="Use HH:mm:ss (00:00:00–23:59:59)" value={item.time} disabled={disabled} onChange={(e) => onChange({ ...item, time: e.target.value })} className="cycle-input ml-1 w-24" /></label>
    <select aria-label="定时重复方式" value={item.recurrence} disabled={disabled} onChange={(e) => onChange({ ...item, recurrence: e.target.value as ScheduledMessage['recurrence'], day_count: e.target.value === 'once' ? 1 : Math.max(1, item.day_count) })} className="cycle-input"><option value="once">仅1次</option><option value="daily">每天</option></select>
    {item.recurrence === 'daily' && <label className="text-xs cycle-muted">循环天数 <input aria-label="循环天数" type="number" min={1} step={1} value={item.day_count} disabled={disabled} onChange={(e) => onChange({ ...item, day_count: Math.max(1, Number.parseInt(e.target.value, 10) || 1) })} className="cycle-input ml-1 w-16" /></label>}
    {!disabled && <button onClick={onRemove} className="ml-auto text-sm text-red-600 hover:text-red-500 dark:text-red-300">×</button>}</div>
    <textarea value={item.message} disabled={disabled} onChange={(e) => onChange({ ...item, message: e.target.value })} placeholder="指定时刻发送的文字…" rows={2} className="cycle-input mt-2 w-full resize-y" />
    {isVisionMode && <label className="mt-2 flex items-center gap-2 text-xs cycle-muted">Cameras:<input value={cameraText} disabled={disabled} onChange={(e) => setCameraText(e.target.value)} onBlur={commitCameras} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); commitCameras(); } }} placeholder="e.g. 0, 1" className="cycle-input w-32" /></label>}
  </article>;
}

