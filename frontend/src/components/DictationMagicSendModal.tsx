"use client";

import {
  useEffect,
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent,
  type MutableRefObject,
} from "react";
import { GripVertical, Plus, Trash2, X } from "lucide-react";
import {
  MIN_DICTATION_MAGIC_SEND_POLL_INTERVAL_MS,
  normalizeDictationMagicSendSubmittedInterval,
  type DictationMagicSendRule,
  type DictationMagicSendSettings,
} from "../lib/dictation-magic-send";

interface DraftRule extends DictationMagicSendRule {
  id: number;
}

const DICTATION_COMMAND_ROW_MIME = "application/x-llm-dictation-command-row";

function createDraftRules(
  rules: readonly DictationMagicSendRule[],
  nextId: MutableRefObject<number>,
): DraftRule[] {
  return rules.map((rule) => ({ ...rule, id: nextId.current++ }));
}

export function DictationMagicSendModal({ open, settings, onConfirm, onCancel }: {
  open: boolean;
  settings: DictationMagicSendSettings;
  onConfirm: (settings: DictationMagicSendSettings) => void;
  onCancel: () => void;
}) {
  const nextRowId = useRef(0);
  const activeDragRowId = useRef<number | null>(null);
  const latestSettings = useRef(settings);
  const wasOpen = useRef(open);
  latestSettings.current = settings;

  const [draftRules, setDraftRules] = useState<DraftRule[]>(
    () => createDraftRules(settings.rules, nextRowId),
  );
  const [pollIntervalDraft, setPollIntervalDraft] = useState(String(settings.pollIntervalMs));

  useEffect(() => {
    if (open && !wasOpen.current) {
      const current = latestSettings.current;
      setDraftRules(createDraftRules(current.rules, nextRowId));
      setPollIntervalDraft(String(current.pollIntervalMs));
    }
    wasOpen.current = open;
  }, [open]);

  if (!open) return null;

  const updateRule = (id: number, text: string) => {
    setDraftRules((current) => current.map((rule) => rule.id === id
      ? { ...rule, text, enabled: text.trim() ? rule.enabled : false }
      : rule));
  };

  const moveRule = (sourceId: number, targetId: number) => {
    setDraftRules((current) => {
      const sourceIndex = current.findIndex((rule) => rule.id === sourceId);
      const targetIndex = current.findIndex((rule) => rule.id === targetId);
      if (sourceIndex < 0 || targetIndex < 0 || sourceIndex === targetIndex) return current;
      const reordered = [...current];
      const [moved] = reordered.splice(sourceIndex, 1);
      reordered.splice(targetIndex, 0, moved);
      return reordered;
    });
  };

  const moveRuleByOffset = (id: number, offset: -1 | 1) => {
    setDraftRules((current) => {
      const sourceIndex = current.findIndex((rule) => rule.id === id);
      const targetIndex = sourceIndex + offset;
      if (sourceIndex < 0 || targetIndex < 0 || targetIndex >= current.length) return current;
      const reordered = [...current];
      [reordered[sourceIndex], reordered[targetIndex]] = [reordered[targetIndex], reordered[sourceIndex]];
      return reordered;
    });
  };

  const handleDragStart = (event: DragEvent<HTMLButtonElement>, id: number) => {
    activeDragRowId.current = id;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData(DICTATION_COMMAND_ROW_MIME, String(id));
  };

  const handleDrop = (event: DragEvent<HTMLButtonElement>, targetId: number) => {
    event.preventDefault();
    const sourceId = activeDragRowId.current;
    activeDragRowId.current = null;
    if (sourceId === null) return;
    moveRule(sourceId, targetId);
  };

  const handleReorderKey = (event: KeyboardEvent<HTMLButtonElement>, id: number) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    moveRuleByOffset(id, event.key === "ArrowUp" ? -1 : 1);
  };

  const controlClass = "rounded-lg border border-gray-300 bg-white text-gray-900 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nvidia-500 dark:border-gray-600 dark:bg-gray-950 dark:text-gray-100";
  const secondaryButtonClass = "rounded-lg border border-gray-300 bg-white text-gray-700 transition-colors hover:border-nvidia-500 hover:text-nvidia-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nvidia-500 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200 dark:hover:border-nvidia-400 dark:hover:bg-gray-800 dark:hover:text-nvidia-300";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" role="presentation">
      <section
        role="dialog"
        aria-modal="true"
        aria-label="Dictation magic send settings"
        className="w-full max-w-xl rounded-2xl border border-gray-200 bg-white p-5 text-gray-950 shadow-xl dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100"
      >
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-lg font-semibold text-gray-950 dark:text-gray-100">Dictation magic send</h2>
          <button
            type="button"
            aria-label="Close dictation magic send settings"
            title="Close"
            onClick={onCancel}
            className={`inline-flex h-9 w-9 items-center justify-center ${secondaryButtonClass}`}
          >
            <X aria-hidden="true" className="h-4 w-4" />
          </button>
        </div>

        <label className="mt-4 block text-sm font-medium text-gray-800 dark:text-gray-200">
          Poll interval (ms)
          <input
            aria-label="Poll interval (ms)"
            type="number"
            min={MIN_DICTATION_MAGIC_SEND_POLL_INTERVAL_MS}
            value={pollIntervalDraft}
            onChange={(event) => setPollIntervalDraft(event.currentTarget.value)}
            className={`mt-1 block w-full p-2 ${controlClass}`}
          />
        </label>

        <div className="mt-4 max-h-64 overflow-y-auto pr-5" aria-label="Dictation commands">
          <div className="sticky top-0 z-10 grid grid-cols-[minmax(0,1fr)_4rem_5rem] items-center gap-3 bg-white pb-1 text-xs font-medium text-gray-600 dark:bg-gray-900 dark:text-gray-300">
            <span>Command</span>
            <span className="text-center">Enabled</span>
            <span className="sr-only">Actions</span>
          </div>
          {draftRules.map((rule, index) => (
            <div
              className="mt-2 grid grid-cols-[minmax(0,1fr)_4rem_5rem] items-center gap-3"
              key={rule.id}
            >
              <input
                aria-label={`Command ${index + 1}`}
                value={rule.text}
                onChange={(event) => updateRule(rule.id, event.currentTarget.value)}
                className={`min-w-0 p-2 ${controlClass}`}
              />
              <span className="flex justify-center px-2">
                <input
                  aria-label={`Enable command ${index + 1}`}
                  type="checkbox"
                  checked={rule.enabled}
                  disabled={!rule.text.trim()}
                  onChange={(event) => setDraftRules((current) => current.map((item) => item.id === rule.id
                    ? { ...item, enabled: event.currentTarget.checked }
                    : item))}
                  className="h-4 w-4 accent-nvidia-500 disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nvidia-500"
                />
              </span>
              <span className="flex items-center justify-end gap-1">
                <button
                  type="button"
                  draggable
                  aria-label={`Reorder command ${index + 1}`}
                  title="Drag to reorder; use Arrow Up or Arrow Down with the keyboard"
                  onDragStart={(event) => handleDragStart(event, rule.id)}
                  onDragEnd={() => { activeDragRowId.current = null; }}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={(event) => handleDrop(event, rule.id)}
                  onKeyDown={(event) => handleReorderKey(event, rule.id)}
                  className={`inline-flex h-8 w-8 cursor-grab items-center justify-center active:cursor-grabbing ${secondaryButtonClass}`}
                >
                  <GripVertical aria-hidden="true" className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  aria-label={`Delete command ${index + 1}`}
                  title="Delete command"
                  onClick={() => setDraftRules((current) => current.filter((item) => item.id !== rule.id))}
                  className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-red-200 bg-white text-red-600 transition-colors hover:bg-red-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500 dark:border-red-900/80 dark:bg-gray-900 dark:text-red-300 dark:hover:bg-red-950/60"
                >
                  <Trash2 aria-hidden="true" className="h-4 w-4" />
                </button>
              </span>
            </div>
          ))}
        </div>

        <button
          type="button"
          onClick={() => setDraftRules((current) => [
            ...current,
            { id: nextRowId.current++, text: "", enabled: false },
          ])}
          className={`mt-3 inline-flex items-center gap-1 px-3 py-2 ${secondaryButtonClass}`}
        >
          <Plus aria-hidden="true" className="h-4 w-4" />
          Add command
        </button>

        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onCancel} className={`px-3 py-2 ${secondaryButtonClass}`}>
            Cancel
          </button>
          <button
            type="button"
            onClick={() => onConfirm({
              rules: draftRules
                .filter((rule) => rule.text.trim())
                .map(({ id: _id, ...rule }) => rule),
              pollIntervalMs: normalizeDictationMagicSendSubmittedInterval(pollIntervalDraft),
            })}
            className="rounded-lg border border-nvidia-700 bg-nvidia-700 px-3 py-2 text-white transition-colors hover:border-nvidia-800 hover:bg-nvidia-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-nvidia-400 focus-visible:ring-offset-2 dark:border-nvidia-700 dark:bg-nvidia-700 dark:text-white dark:hover:border-nvidia-800 dark:hover:bg-nvidia-800 dark:focus-visible:ring-offset-gray-900"
          >
            Confirm
          </button>
        </div>
      </section>
    </div>
  );
}
