"use client";

import React from 'react';
import { summarizeCycleProgress, type CycleExecutionStatus, type CycleSSEEvent } from '@/lib/cycle-types';

export default function CycleProgress({ status, events }: { status: CycleExecutionStatus; events: CycleSSEEvent[] }) {
  if (!events.length) return null;
  const last = events[events.length - 1];
  const summary = summarizeCycleProgress(events);
  const current = [...events].reverse().find((event) => event.type === 'step_begin');
  const labels: Record<CycleExecutionStatus, string> = { idle: '', running: '运行中…', completed: '已完成', stopped: '已停止', error: '错误', expired: '已过期' };
  const colors: Record<CycleExecutionStatus, string> = { idle: 'cycle-muted', running: 'text-blue-600 dark:text-blue-300', completed: 'text-green-700 dark:text-green-300', stopped: 'text-yellow-700 dark:text-yellow-300', error: 'text-red-700 dark:text-red-300', expired: 'text-orange-700 dark:text-orange-300' };
  return <div className="cycle-canvas rounded-lg border p-3 text-sm"><div className="mb-1 flex items-center justify-between"><span className="font-medium">Cycle timeline</span><span className={colors[status]}>{labels[status]}</span></div>
    <div className="space-y-0.5 text-xs cycle-muted"><div>普通轨道完成：{summary.ordinaryCompleted}</div><div>定时轨道：已排队 {summary.scheduledQueued}，已发送 {summary.scheduledDelivered}</div><div>失败：{summary.failed}</div><div>累计延迟：{summary.cumulativeLatenessSeconds.toFixed(1)}s</div>
      {summary.nextScheduledFor && <div>下一次定时：{summary.nextScheduledFor}</div>}
      {typeof last.total_duration === 'number' && <div>总计：{last.total_duration.toFixed(1)}s</div>}
      {status === 'running' && current && <div className="text-blue-600/80 dark:text-blue-300/80">当前：{current.track === 'scheduled' ? '定时 message' : current.step_type} {current.track === 'scheduled' && current.scheduled_for ? `(${current.scheduled_for})` : ''}</div>}
      {last.type === 'journal_truncated' && <div>历史事件已按保留策略截断；已从序列 {last.truncated_through} 继续。</div>}
      {last.image_omitted && <div>重连事件中的图片缩略图已省略。</div>}
      {(last.type === 'cycle_error' || last.type === 'cycle_expired' || last.type === 'scheduled_message_failed' || last.type === 'step_failed') && <div className="mt-1 text-red-700 dark:text-red-300">{last.error || last.reason || 'Cycle 执行失败'}</div>}
      {summary.terminalReason && <div className="mt-1 text-red-700 dark:text-red-300">原因：{summary.terminalReason}</div>}
    </div>
  </div>;
}
