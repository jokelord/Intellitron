export type MessageTemplateInputSetter = (value: string) => void;
export type MessageTemplateFocusScheduler = (callback: () => void) => unknown;

function scheduleNextFrame(callback: () => void): number {
  return requestAnimationFrame(callback);
}

export function insertMessageTemplateIntoInput(
  text: string,
  setInput: MessageTemplateInputSetter,
  focusInput: () => void,
  schedule: MessageTemplateFocusScheduler = scheduleNextFrame,
): void {
  setInput(text);
  schedule(focusInput);
}

export function sendMessageTemplateThroughExistingPipeline<Result>(
  text: string,
  send: (text: string) => Result,
): Result {
  return send(text);
}
