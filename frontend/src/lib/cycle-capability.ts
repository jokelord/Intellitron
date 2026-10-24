import type { CycleStep, ScheduledMessage } from './cycle-types.ts';

export type CyclePageMode = 'text' | 'vision';
export interface CycleProgram { steps: CycleStep[]; scheduledMessages: ScheduledMessage[]; }
export interface CycleProgramError {
  path: string;
  code: 'invalid_program' | 'text_camera_forbidden';
  message: string;
}
export type CycleProgramValidation =
  | { ok: true; program: CycleProgram; hasPhotos: boolean }
  | { ok: false; error: CycleProgramError };

/** Pure preflight and snapshot. No browser state or external resources belong here. */
export function validateCycleProgram(pageMode: CyclePageMode, steps: unknown, scheduledMessages: unknown): CycleProgramValidation {
  return validateProgram(pageMode, steps, scheduledMessages, false);
}

/** Drafts may contain an empty scheduled payload while the user is editing. */
export function validateCycleEditorProgram(steps: unknown, scheduledMessages: unknown, maxDepth: number): CycleProgramValidation {
  return validateProgram('vision', steps, scheduledMessages, true, maxDepth);
}

function validateProgram(pageMode: CyclePageMode, steps: unknown, scheduledMessages: unknown, draft: boolean, maxDepth = Infinity): CycleProgramValidation {
  const invalid = (path: string, reason: string): CycleProgramValidation => ({ ok: false, error: {
    path, code: 'invalid_program', message: `${path}: ${reason}`,
  } });
  if (!Array.isArray(steps)) return invalid('steps', '需要步骤数组');
  if (!Array.isArray(scheduledMessages)) return invalid('scheduled_messages', '需要定时消息数组');
  let hasPhotos = false;
  const photo = (item: Record<string, unknown>, path: string): CycleProgramValidation | null => {
    if (item.photo === undefined || item.photo === null) return null;
    if (!Array.isArray(item.photo) || !Array.from(item.photo).every((index) => Number.isSafeInteger(index) && index >= 0)) {
      return invalid(`${path}.photo`, '需要非负整数相机编号数组');
    }
    if (item.photo.length) {
      if (pageMode === 'text') return { ok: false, error: {
        path: `${path}.photo`, code: 'text_camera_forbidden',
        message: `${path}.photo: Text 不允许该相机程序，整次未启动；请在 Vision 打开`,
      } };
      hasPhotos = true;
    }
    return null;
  };
  const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);
  const clonedSteps: CycleStep[] = [];
  // Explicit traversal avoids call-stack overflow and rejects cyclic containers.
  type Work = { kind: 'array'; source: unknown[]; target: CycleStep[]; path: string; depth: number }
    | { kind: 'step'; source: unknown; target: CycleStep[]; index: number; path: string; depth: number }
    | { kind: 'leave'; source: object };
  const ancestors = new Set<object>();
  const work: Work[] = [{ kind: 'array', source: steps, target: clonedSteps, path: 'steps', depth: 0 }];
  while (work.length) {
    const task = work.pop()!;
    if (task.kind === 'leave') { ancestors.delete(task.source); continue; }
    if (task.kind === 'array') {
      if (ancestors.has(task.source)) return invalid(task.path, '循环引用');
      ancestors.add(task.source);
      work.push({ kind: 'leave', source: task.source });
      for (let index = task.source.length - 1; index >= 0; index--) {
        work.push({ kind: 'step', source: task.source[index], target: task.target, index, path: `${task.path}[${index}]`, depth: task.depth });
      }
      continue;
    }
    const item = task.source;
    if (!record(item)) return invalid(task.path, '需要步骤对象');
    if (ancestors.has(item)) return invalid(task.path, '循环引用');
    if (item.type === 'wait') {
      if (typeof item.wait !== 'number' || !Number.isFinite(item.wait) || item.wait < 0) return invalid(`${task.path}.wait`, '需要有限非负秒数');
      task.target[task.index] = { type: 'wait', wait: item.wait };
    } else if (item.type === 'message') {
      if (typeof item.message !== 'string') return invalid(`${task.path}.message`, '需要文本');
      const error = photo(item, task.path); if (error) return error;
      task.target[task.index] = { type: 'message', message: item.message, ...(Array.isArray(item.photo) ? { photo: [...item.photo] } : {}) };
    } else if (item.type === 'cycle') {
      if (task.depth + 1 > maxDepth) return invalid(`${task.path}.cycle_steps`, '超过允许的嵌套深度');
      if (!Number.isSafeInteger(item.cycle_count) || (item.cycle_count as number) < 1) return invalid(`${task.path}.cycle_count`, '需要正整数');
      if (!Array.isArray(item.cycle_steps)) return invalid(`${task.path}.cycle_steps`, '需要步骤数组');
      const nested: CycleStep[] = [];
      task.target[task.index] = { type: 'cycle', cycle_count: item.cycle_count as number, cycle_steps: nested };
      ancestors.add(item);
      work.push({ kind: 'leave', source: item });
      work.push({ kind: 'array', source: item.cycle_steps, target: nested, path: `${task.path}.cycle_steps`, depth: task.depth + 1 });
    } else return invalid(`${task.path}.type`, '未知步骤类型');
  }
  const schedules: ScheduledMessage[] = [];
  const ids = new Set<string>();
  for (let index = 0; index < scheduledMessages.length; index++) {
    const item = scheduledMessages[index], path = `scheduled_messages[${index}]`;
    if (!record(item)) return invalid(path, '需要定时消息对象');
    if (typeof item.id !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(item.id) || ids.has(item.id)) return invalid(`${path}.id`, '需要唯一合法 ID');
    ids.add(item.id);
    if (typeof item.time !== 'string' || !/^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$/.test(item.time)) return invalid(`${path}.time`, '需要 HH:mm:ss');
    if (item.recurrence !== 'once' && item.recurrence !== 'daily') return invalid(`${path}.recurrence`, '未知重复方式');
    if (!Number.isSafeInteger(item.day_count) || (item.day_count as number) < 1 || (item.recurrence === 'once' && item.day_count !== 1)) return invalid(`${path}.day_count`, '需要有效正整数天数');
    if (typeof item.message !== 'string') return invalid(`${path}.message`, '需要文本');
    const error = photo(item, path); if (error) return error;
    if (!draft && !item.message.trim() && !(Array.isArray(item.photo) && item.photo.length)) return invalid(path, '需要文字或图片');
    schedules.push({ id: item.id, time: item.time, recurrence: item.recurrence, day_count: item.day_count as number, message: item.message,
      ...(Array.isArray(item.photo) ? { photo: [...item.photo] } : {}) });
  }
  return { ok: true, program: { steps: clonedSteps, scheduledMessages: schedules }, hasPhotos };
}
