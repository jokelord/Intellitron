export interface ChatInputKeyEvent {
  key: string;
  shiftKey?: boolean;
  ctrlKey?: boolean;
  metaKey?: boolean;
  isComposing?: boolean;
}

export function shouldSubmitChatInput(
  event: ChatInputKeyEvent,
  enterToSend: boolean,
): boolean {
  if (event.key !== 'Enter' || event.isComposing) return false;
  if (event.ctrlKey || event.metaKey) return true;
  return enterToSend && !event.shiftKey;
}

export function getChatInputPlaceholder(enterToSend: boolean): string {
  return enterToSend
    ? '输入您的问题... (Enter 发送, Shift+Enter 换行)'
    : '输入您的问题... (Enter 换行, Ctrl+Enter 发送)';
}
