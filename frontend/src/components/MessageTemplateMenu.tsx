"use client";

import { MessageSquareText, Pencil, Plus, RotateCcw, Save, Trash2, X } from "lucide-react";
import { useEffect, useState, type MouseEvent } from "react";
import { ToggleSwitch } from "./ToggleSwitch";

export interface MessageTemplateMenuProps {
  actionBarVisible: boolean;
  templates: readonly string[];
  yamlDefaults: readonly string[];
  onTemplatesChange: (next: string[]) => void;
  onRestoreDefaults: () => void;
  onInsertTemplate: (text: string) => void;
  onSendTemplate: (text: string) => void | Promise<void>;
  immediateSend: boolean;
  onImmediateSendChange: (enabled: boolean) => void;
  sendDisabled?: boolean;
  storageError?: string | null;
  onDismissStorageError?: () => void;
}

export default function MessageTemplateMenu({
  actionBarVisible,
  templates,
  onTemplatesChange,
  onRestoreDefaults,
  onInsertTemplate,
  onSendTemplate,
  immediateSend,
  onImmediateSendChange,
  sendDisabled = false,
  storageError = null,
  onDismissStorageError,
}: MessageTemplateMenuProps) {
  const [panelOpen, setPanelOpen] = useState(false);
  const [editingIndex, setEditingIndex] = useState<number | "new" | null>(null);
  const [draft, setDraft] = useState("");

  const closeEditor = () => {
    setEditingIndex(null);
    setDraft("");
  };

  useEffect(() => {
    if (actionBarVisible) return;
    setPanelOpen(false);
    setEditingIndex(null);
    setDraft("");
  }, [actionBarVisible]);

  if (!actionBarVisible) return null;

  const selectTemplate = (text: string) => {
    if (immediateSend) {
      if (!sendDisabled) void onSendTemplate(text);
      return;
    }
    onInsertTemplate(text);
  };

  const beginEdit = (event: MouseEvent<HTMLButtonElement>, index: number) => {
    event.stopPropagation();
    setEditingIndex(index);
    setDraft(templates[index] ?? "");
  };

  const deleteTemplate = (event: MouseEvent<HTMLButtonElement>, index: number) => {
    event.stopPropagation();
    if (typeof globalThis.confirm !== "function" || !globalThis.confirm("删除此消息模板？")) {
      return;
    }
    onTemplatesChange(templates.filter((_, itemIndex) => itemIndex !== index));
    closeEditor();
  };

  const saveDraft = () => {
    if (draft.trim().length === 0 || editingIndex === null) return;
    const next = Array.from(templates);
    if (editingIndex === "new") next.push(draft);
    else next[editingIndex] = draft;
    onTemplatesChange(next);
    closeEditor();
  };

  const renderEditor = (index: number | "new") => (
    <div className="space-y-2 rounded-lg border border-nvidia-500/40 bg-white/80 p-2 dark:bg-gray-900/80">
      <textarea
        data-testid={`message-template-editor-${index}`}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        rows={4}
        autoFocus
        aria-label={index === "new" ? "新消息模板正文" : "编辑消息模板正文"}
        className="w-full resize-y rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 outline-none focus:border-nvidia-500 focus:ring-1 focus:ring-nvidia-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
      />
      <div className="flex justify-end gap-2">
        <button
          type="button"
          data-testid={`message-template-cancel-${index}`}
          onClick={closeEditor}
          className="inline-flex items-center gap-1 rounded-md px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700"
        >
          <X className="h-4 w-4" aria-hidden="true" />
          取消
        </button>
        <button
          type="button"
          data-testid={`message-template-save-${index}`}
          disabled={draft.trim().length === 0}
          onClick={saveDraft}
          className="inline-flex items-center gap-1 rounded-md bg-nvidia-500 px-3 py-1.5 text-sm text-white hover:bg-nvidia-600 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Save className="h-4 w-4" aria-hidden="true" />
          保存
        </button>
      </div>
    </div>
  );

  return (
    <>
      <button
        type="button"
        data-testid="message-template-trigger"
        aria-expanded={panelOpen}
        aria-label="消息模板"
        title="消息模板"
        onClick={() => {
          if (panelOpen) closeEditor();
          setPanelOpen(!panelOpen);
        }}
        className={`flex w-full items-center justify-center gap-2 rounded-lg border-2 border-dashed px-4 py-2 transition-colors ${
          panelOpen
            ? "border-nvidia-500 bg-nvidia-500/10 text-nvidia-600 dark:text-nvidia-400"
            : "border-gray-300 bg-white text-gray-600 hover:border-nvidia-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-400"
        }`}
      >
        <MessageSquareText className="h-5 w-5" aria-hidden="true" />
        <span className="text-sm">消息模板</span>
      </button>

      {panelOpen && (
        <section
          data-testid="message-template-panel"
          aria-label="消息模板面板"
          className="col-span-full mt-2 space-y-3 rounded-xl border border-gray-200 bg-gray-50/95 p-3 shadow-lg dark:border-gray-700 dark:bg-gray-900/95"
        >
          <ToggleSwitch
            checked={immediateSend}
            onChange={onImmediateSendChange}
            label="选中模板后立即发送"
          />

          {storageError && (
            <div role="alert" className="flex items-center gap-2 rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-950/50 dark:text-amber-200">
              <span className="flex-1">{storageError}</span>
              {onDismissStorageError && (
                <button
                  type="button"
                  data-testid="message-template-error-dismiss"
                  onClick={onDismissStorageError}
                  aria-label="关闭消息模板缓存错误"
                  className="rounded p-1 hover:bg-amber-100 dark:hover:bg-amber-900"
                >
                  <X className="h-4 w-4" aria-hidden="true" />
                </button>
              )}
            </div>
          )}

          <div className="space-y-2">
            {templates.length === 0 && editingIndex !== "new" && (
              <p className="rounded-md border border-dashed border-gray-300 px-3 py-4 text-center text-sm text-gray-500 dark:border-gray-700 dark:text-gray-400">
                暂无消息模板
              </p>
            )}
            {templates.map((template, index) => (
              <div key={index}>
                {editingIndex === index ? renderEditor(index) : (
                  <div className="flex min-w-0 items-center gap-1.5">
                    <div className="min-w-0 flex-1">
                      <button
                        type="button"
                        data-testid={`message-template-item-${index}`}
                        disabled={immediateSend && sendDisabled}
                        onClick={() => selectTemplate(template)}
                        aria-label={template}
                        className="w-full min-w-0 rounded-md border border-gray-200 bg-white px-3 py-2 text-left text-sm text-gray-700 hover:border-nvidia-500 hover:text-nvidia-600 focus:border-nvidia-500 focus:outline-none focus:ring-2 focus:ring-nvidia-500/30 disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-200 dark:hover:text-nvidia-400"
                      >
                        <span className="block truncate">{template}</span>
                      </button>
                    </div>
                    <button
                      type="button"
                      data-testid={`message-template-edit-${index}`}
                      onClick={(event) => beginEdit(event, index)}
                      aria-label={`编辑模板：${template}`}
                      title="编辑"
                      className="rounded-md p-2 text-gray-500 hover:bg-gray-200 hover:text-nvidia-600 dark:hover:bg-gray-700 dark:hover:text-nvidia-400"
                    >
                      <Pencil className="h-4 w-4" aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      data-testid={`message-template-delete-${index}`}
                      onClick={(event) => deleteTemplate(event, index)}
                      aria-label={`删除模板：${template}`}
                      title="删除"
                      className="rounded-md p-2 text-gray-500 hover:bg-red-50 hover:text-red-600 dark:hover:bg-red-950/40 dark:hover:text-red-400"
                    >
                      <Trash2 className="h-4 w-4" aria-hidden="true" />
                    </button>
                  </div>
                )}
              </div>
            ))}
            {editingIndex === "new" && renderEditor("new")}
          </div>

          <div className="flex flex-wrap justify-between gap-2 border-t border-gray-200 pt-3 dark:border-gray-700">
            <button
              type="button"
              data-testid="message-template-add"
              disabled={editingIndex !== null}
              onClick={() => {
                setEditingIndex("new");
                setDraft("");
              }}
              className="inline-flex items-center gap-1 rounded-md px-3 py-1.5 text-sm text-nvidia-600 hover:bg-nvidia-500/10 disabled:cursor-not-allowed disabled:opacity-40 dark:text-nvidia-400"
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
              新增
            </button>
            <button
              type="button"
              data-testid="message-template-restore"
              onClick={() => {
                if (typeof globalThis.confirm === "function" && globalThis.confirm("恢复默认模板？当前浏览器中的新增、编辑和删除结果将被清除。")) {
                  closeEditor();
                  onRestoreDefaults();
                }
              }}
              className="inline-flex items-center gap-1 rounded-md px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-700"
            >
              <RotateCcw className="h-4 w-4" aria-hidden="true" />
              恢复默认
            </button>
          </div>
        </section>
      )}
    </>
  );
}
