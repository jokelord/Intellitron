'use client';

import { ChevronDown, ChevronUp, MonitorUp, RefreshCw, Square } from 'lucide-react';
import type { VisionAutomationServerConfig } from '@/lib/vision-automation/config';
import type { SourceDescriptor, TickBundle, VisionAutomationPhase } from '@/lib/vision-automation/types';
import {
  getVisionAutomationPanelState,
  mergeSourceHealthPresentation,
  type VisionAutomationSourceHealth,
} from '@/lib/vision-automation/ui-contracts';
import VisionAutomationStatusRing from './VisionAutomationStatusRing';
import VisionAutomationThumbnailMatrix from './VisionAutomationThumbnailMatrix';

export interface VisionAutomationStats {
  attemptedTicks: number;
  successfulTicks: number;
  failedTicks: number;
  imageCount: number;
  estimatedEncodedBytes: number;
}

export interface VisionAutomationPanelProps {
  phase: VisionAutomationPhase;
  armed: boolean;
  collapsed: boolean;
  cycleRunning: boolean;
  config: VisionAutomationServerConfig;
  availableSources: readonly SourceDescriptor[];
  selectedSourceIds: ReadonlySet<string>;
  activeSources: readonly SourceDescriptor[];
  bundles: readonly TickBundle[];
  sourceHealth: readonly VisionAutomationSourceHealth[];
  stats: VisionAutomationStats;
  error?: string | null;
  onCollapsedChange: (collapsed: boolean) => void;
  onSourceSelectionChange: (sourceId: string, selected: boolean) => void;
  onConfigChange: (patch: Partial<VisionAutomationServerConfig>) => void;
  startDisabled: boolean;
  onStart: () => void;
  onAbort: () => void;
  onRetrySources: () => void;
  onRetryFailedSource: () => void;
  canRetryStatus: boolean;
  canRetrySubmit: boolean;
  onRetryStatus: () => void;
  onRetrySubmit: () => void;
  onRefreshCameras: () => void;
}

function phaseLabel(phase: VisionAutomationPhase) {
  if (phase === 'capturing' || phase === 'capturing_during_model') return '采集中';
  if (phase === 'waiting_for_model') return '等待模型';
  if (phase === 'waiting_for_sources') return '等待视觉源';
  if (phase === 'paused_source_error') return '视觉源异常';
  if (phase === 'freezing') return '冻结发送批次';
  return phase === 'idle' ? '未启动' : '准备中';
}

export default function VisionAutomationPanel(props: VisionAutomationPanelProps) {
  const panelState = getVisionAutomationPanelState({
    armed: props.armed,
    collapsed: props.collapsed,
    cycleRunning: props.cycleRunning,
    selectedSourceCount: props.selectedSourceIds.size,
    bundleCount: props.bundles.length,
    startDisabled: props.startDisabled,
  });
  const sourceHealth = mergeSourceHealthPresentation(
    props.activeSources,
    props.availableSources,
    props.sourceHealth,
  );
  return (
    <section className="rounded-2xl border border-blue-400/20 bg-gray-950/90 shadow-xl shadow-blue-950/20">
      <header className="flex items-center justify-between gap-3 px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <MonitorUp className="h-4 w-4 text-blue-300" />
          <div className="min-w-0">
            <p className="truncate text-sm font-medium text-gray-100">视觉自动化</p>
            <p className="text-xs text-gray-400">{phaseLabel(props.phase)}</p>
          </div>
          <VisionAutomationStatusRing phase={props.phase} armed={props.armed} />
        </div>
        <div className="ml-auto flex min-w-0 flex-wrap items-center justify-end gap-1.5">
          {!props.armed && (
            <button type="button" disabled={!panelState.canStart} onClick={props.onStart} className="rounded bg-blue-600 hover:bg-blue-500 disabled:bg-gray-600 disabled:text-gray-400 disabled:cursor-not-allowed text-white text-sm font-medium px-4 py-1.5">
              start
            </button>
          )}
          {props.phase === 'waiting_for_sources' && (
            <button type="button" onClick={props.onRetrySources} className="flex items-center gap-1 rounded-lg bg-amber-500/20 px-2.5 py-1 text-xs text-amber-200"><RefreshCw className="h-4 w-4" />Retry Sources</button>
          )}
          {props.phase === 'paused_source_error' && (
            <button type="button" onClick={props.onRetryFailedSource} className="flex items-center gap-1 rounded-lg bg-amber-500/20 px-2.5 py-1 text-xs text-amber-200"><RefreshCw className="h-4 w-4" />Retry Failed Source</button>
          )}
          {props.canRetryStatus && (
            <button type="button" onClick={props.onRetryStatus} className="flex items-center gap-1 rounded-lg bg-amber-500/20 px-2.5 py-1 text-xs text-amber-200"><RefreshCw className="h-4 w-4" />Retry Status</button>
          )}
          {props.canRetrySubmit && (
            <button type="button" onClick={props.onRetrySubmit} className="flex items-center gap-1 rounded-lg bg-amber-500/20 px-2.5 py-1 text-xs text-amber-200"><RefreshCw className="h-4 w-4" />Retry Submit</button>
          )}
          {props.armed && (
            <button type="button" onClick={props.onAbort} className="flex items-center gap-1 rounded-lg bg-red-500/20 px-2.5 py-1 text-xs text-red-200"><Square className="h-4 w-4" />Abort</button>
          )}
        </div>
        <button
          type="button"
          aria-expanded={!props.collapsed}
          aria-label={props.collapsed ? '展开视觉自动化' : '折叠视觉自动化'}
          onClick={() => props.onCollapsedChange(!props.collapsed)}
          className="rounded-lg p-2 text-gray-400 hover:bg-gray-800 hover:text-gray-100"
        >
          {props.collapsed ? <ChevronDown className="h-4 w-4" /> : <ChevronUp className="h-4 w-4" />}
        </button>
      </header>

      {!props.collapsed && (
        <div className="space-y-4 border-t border-gray-800 px-4 py-4">
          <fieldset disabled={panelState.controlsLocked} className="space-y-2 disabled:opacity-60">
            <legend className="text-xs font-medium uppercase tracking-wider text-gray-400">视觉源</legend>
            <button
              type="button"
              disabled={panelState.cameraRefreshDisabled}
              onClick={props.onRefreshCameras}
              className="flex items-center gap-1 rounded-lg bg-gray-800 px-2.5 py-1.5 text-xs text-gray-200 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <RefreshCw className="h-3.5 w-3.5" />刷新摄像头
            </button>
            <div className="grid gap-2 sm:grid-cols-2">
              {props.availableSources.map((source) => (
                <label key={source.sourceId} className="flex items-center gap-2 rounded-lg border border-gray-800 px-3 py-2 text-sm text-gray-200">
                  <input
                    type="checkbox"
                    checked={props.selectedSourceIds.has(source.sourceId)}
                    onChange={(event) => props.onSourceSelectionChange(source.sourceId, event.target.checked)}
                  />
                  {source.kind === 'display' ? '桌面共享' : source.label}
                </label>
              ))}
            </div>
          </fieldset>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
            <label className="text-xs text-gray-400">间隔（秒）
              <input disabled={panelState.controlsLocked} type="number" min="0.1" step="0.1" value={props.config.intervalSeconds} onChange={(event) => props.onConfigChange({ intervalSeconds: Number(event.target.value) })} className="mt-1 w-full rounded-lg bg-gray-900 px-2 py-1.5 text-gray-100" />
            </label>
            <label className="text-xs text-gray-400">每源帧数
              <input disabled={panelState.controlsLocked} type="number" min="1" max="250" value={props.config.maxFramesPerSource} onChange={(event) => props.onConfigChange({ maxFramesPerSource: Number(event.target.value) })} className="mt-1 w-full rounded-lg bg-gray-900 px-2 py-1.5 text-gray-100" />
            </label>
            <label className="text-xs text-gray-400">缩略图
              <input disabled={panelState.controlsLocked} type="number" min="16" max="128" value={props.config.thumbnailSizePx} onChange={(event) => props.onConfigChange({ thumbnailSizePx: Number(event.target.value) })} className="mt-1 w-full rounded-lg bg-gray-900 px-2 py-1.5 text-gray-100" />
            </label>
            <label className="flex items-end gap-2 pb-1.5 text-xs text-gray-300">
              <input disabled={panelState.controlsLocked} type="checkbox" checked={props.config.captureDuringInference} onChange={(event) => props.onConfigChange({ captureDuringInference: event.target.checked })} />
              推理期间采集
            </label>
            <label className="flex items-end gap-2 pb-1.5 text-xs text-gray-300">
              <input disabled={panelState.controlsLocked} type="checkbox" checked={props.config.lockFirstBundle} onChange={(event) => props.onConfigChange({ lockFirstBundle: event.target.checked })} />
              锁定首帧
            </label>
            <label className="text-xs text-gray-400">图片预算（MiB）
              <input disabled={panelState.controlsLocked} type="number" min="1" value={Math.floor(props.config.maxEncodedPayloadBytes / 1_048_576)} onChange={(event) => props.onConfigChange({ maxEncodedPayloadBytes: Number(event.target.value) * 1_048_576 })} className="mt-1 w-full rounded-lg bg-gray-900 px-2 py-1.5 text-gray-100" />
            </label>
          </div>

          {props.armed && (
            <div className="grid grid-cols-2 gap-2 text-xs text-gray-400 sm:grid-cols-4">
              <span>尝试 {props.stats.attemptedTicks}</span>
              <span>成功 {props.stats.successfulTicks}</span>
              <span>失败 {props.stats.failedTicks}</span>
              <span>{props.stats.imageCount}/250 张</span>
              <span className="col-span-2">预计 {(props.stats.estimatedEncodedBytes / 1_048_576).toFixed(2)} MiB</span>
            </div>
          )}

          {props.error && <p role="alert" className="rounded-lg bg-amber-950/50 px-3 py-2 text-xs text-amber-200">{props.error}</p>}
          {sourceHealth.length > 0 && (
            <div className="grid gap-2 sm:grid-cols-2" aria-label="视觉源健康状态">
              {sourceHealth.map((source) => (
                <div key={source.sourceId} className="rounded-lg border border-gray-800 px-3 py-2 text-xs text-gray-300">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate">{source.label}</span>
                    <span className={source.status === 'error' ? 'text-amber-300' : source.status === 'healthy' ? 'text-emerald-300' : 'text-gray-400'}>{source.statusLabel}</span>
                  </div>
                  {source.error && <p role="alert" className="mt-1 text-amber-200">{source.error}</p>}
                </div>
              ))}
            </div>
          )}
          {panelState.showThumbnailMatrix && <VisionAutomationThumbnailMatrix sources={props.activeSources} bundles={props.bundles} />}
        </div>
      )}
    </section>
  );
}
