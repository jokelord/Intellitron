'use client';

import React, { useEffect, useState } from 'react';
import AgentStepDisplay from '@/components/AgentStepDisplay';
import CollapsibleBubbleContent from '@/components/CollapsibleBubbleContent';
import MarkdownRenderer from '@/components/MarkdownRenderer';
import {
  ChildAgentStreamEvent,
  llmClient,
} from '@/lib/api';
import {
  applyChildTranscriptEvent,
  ChildTranscriptState,
} from '@/lib/child-agent-transcript';
import {
  getWebUIBubbleCharacterLimit,
  type WebUIConfig,
} from '@/lib/web-ui-config';

interface ChildAgentPageProps {
  params: {
    childRunId: string;
  };
}

function createInitialTranscriptState(): ChildTranscriptState {
  return {
    childName: 'Child agent',
    turns: [],
    truncationNotice: null,
    status: 'connecting',
    error: null,
  };
}

export default function ChildAgentTranscriptPage({ params }: ChildAgentPageProps) {
  const [transcript, setTranscript] = useState<ChildTranscriptState>(() => createInitialTranscriptState());
  const [isAborting, setIsAborting] = useState(false);
  const [abortError, setAbortError] = useState<string | null>(null);
  const [webUIConfig, setWebUIConfig] = useState<WebUIConfig | undefined>(undefined);
  const bubbleCharacterLimit = getWebUIBubbleCharacterLimit(webUIConfig);

  const abortChild = async () => {
    if (!window.confirm('Abort this child agent run and discard its context?')) return;
    setIsAborting(true);
    setAbortError(null);
    try {
      await llmClient.abortChildAgentRun(params.childRunId);
    } catch (error) {
      setAbortError(error instanceof Error ? error.message : 'Failed to abort child agent.');
    } finally {
      setIsAborting(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    void llmClient.getConfig().then((config) => {
      if (!cancelled) setWebUIConfig(config.webUI);
    }).catch(() => {
      // The default display limit remains available when config cannot be fetched.
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    setTranscript(createInitialTranscriptState());

    const handleEvent = (event: ChildAgentStreamEvent) => {
      if (cancelled) return;
      setTranscript((prev) => applyChildTranscriptEvent(prev, event));
    };

    void llmClient.agentSessionEvents(
      params.childRunId,
      0,
      {
        onEvent: handleEvent,
        onError: (streamError) => {
          if (cancelled) return;
          setTranscript((prev) => ({ ...prev, status: 'failed', error: streamError.message }));
        },
        onComplete: () => {
          if (cancelled) return;
          setTranscript((prev) => ({
            ...prev,
            status: prev.status === 'connecting' || prev.status === 'running' ? 'completed' : prev.status,
          }));
        },
      },
      controller,
    );

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [params.childRunId]);

  return (
    <main className="h-screen overflow-y-auto bg-slate-950 px-4 py-6 text-slate-100">
      <div className="mx-auto max-w-4xl space-y-5">
        <header className="rounded-2xl border border-slate-800 bg-slate-900/80 px-5 py-4 shadow-lg">
          <div className="text-xs uppercase tracking-[0.24em] text-slate-400">Replay/live child transcript</div>
          <h1 className="mt-2 text-2xl font-semibold">{transcript.childName}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-400">
            <span className="rounded-full bg-slate-800 px-2.5 py-1">{params.childRunId}</span>
            <span className="rounded-full bg-slate-800 px-2.5 py-1">{transcript.status}</span>
            {transcript.status === 'running' && (
              <button
                type="button"
                onClick={() => { void abortChild(); }}
                disabled={isAborting}
                className="rounded-full border border-red-700 px-3 py-1 font-medium text-red-200 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {isAborting ? 'Aborting...' : 'Abort child'}
              </button>
            )}
          </div>
        </header>

        {transcript.truncationNotice && (
          <section className="rounded-2xl border border-amber-800/60 bg-amber-950/30 px-5 py-4 text-sm text-amber-100">
            <CollapsibleBubbleContent content={transcript.truncationNotice} limit={bubbleCharacterLimit}>
              {(displayContent) => <>{displayContent}</>}
            </CollapsibleBubbleContent>
          </section>
        )}

        {transcript.turns.length === 0 ? (
          <section className="rounded-2xl border border-blue-900/60 bg-blue-950/30 px-5 py-4">
            <div className="text-sm font-semibold text-blue-200">Manager assigned task</div>
            <p className="mt-2 whitespace-pre-wrap text-sm leading-relaxed text-blue-50">
              Waiting for manager_assigned_task event...
            </p>
          </section>
        ) : transcript.turns.map((turn, index) => (
          <section key={turn.id} className="space-y-4 rounded-2xl border border-slate-800 bg-slate-900/70 px-5 py-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-sm font-semibold text-slate-200">Turn {index + 1}</div>
              <span className="rounded-full bg-slate-800 px-2.5 py-1 text-xs text-slate-400">{turn.status}</span>
            </div>

            <div className="rounded-xl border border-blue-900/60 bg-blue-950/30 px-4 py-3">
              <div className="text-sm font-semibold text-blue-200">Manager assigned task</div>
              <div className="mt-1 text-xs text-blue-300/80">
                {turn.reusedContext ? `Reused context (${turn.priorStepCount ?? 0} prior steps)` : 'Fresh context'}
              </div>
              <CollapsibleBubbleContent
                content={turn.assignedTask || 'No assigned task text was emitted.'}
                limit={bubbleCharacterLimit}
              >
                {(displayContent) => (
                  <p className="mt-2 whitespace-pre-wrap text-sm leading-relaxed text-blue-50">{displayContent}</p>
                )}
              </CollapsibleBubbleContent>
            </div>

            {turn.steps.length > 0 && (
              <div>
                <div className="mb-3 text-sm font-semibold text-slate-200">Agent activity</div>
                <AgentStepDisplay steps={turn.steps} bubbleCharacterLimit={bubbleCharacterLimit} />
              </div>
            )}

            <div>
              <div className="text-sm font-semibold text-slate-200">Final answer stream</div>
              <CollapsibleBubbleContent
                content={turn.content || (turn.status === 'completed' ? 'No final text was emitted.' : 'Waiting for content...')}
                limit={bubbleCharacterLimit}
              >
                {(displayContent) => (
                  <MarkdownRenderer className="markdown-content mt-3 text-sm leading-relaxed text-slate-100">
                    {displayContent}
                  </MarkdownRenderer>
                )}
              </CollapsibleBubbleContent>
            </div>

            {turn.error && (
              <div className="rounded-xl border border-red-900/60 bg-red-950/30 px-4 py-3 text-sm text-red-100">
                <CollapsibleBubbleContent content={turn.error} limit={bubbleCharacterLimit}>
                  {(displayContent) => <>{displayContent}</>}
                </CollapsibleBubbleContent>
              </div>
            )}
          </section>
        ))}

        {transcript.error && (
          <section className="rounded-2xl border border-red-900/60 bg-red-950/30 px-5 py-4 text-sm text-red-100">
            <CollapsibleBubbleContent content={transcript.error} limit={bubbleCharacterLimit}>
              {(displayContent) => <>{displayContent}</>}
            </CollapsibleBubbleContent>
          </section>
        )}
        {abortError && (
          <section className="rounded-2xl border border-red-900/60 bg-red-950/30 px-5 py-4 text-sm text-red-100">
            <CollapsibleBubbleContent content={abortError} limit={bubbleCharacterLimit}>
              {(displayContent) => <>{displayContent}</>}
            </CollapsibleBubbleContent>
          </section>
        )}
      </div>
    </main>
  );
}
