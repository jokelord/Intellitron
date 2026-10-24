export const CHAT_INPUT_MAX_ROWS = 6;
export const CHAT_INPUT_LINE_HEIGHT_PX = 24;
export const CHAT_INPUT_VERTICAL_PADDING_PX = 24;
export const CHAT_INPUT_MIN_HEIGHT_PX = CHAT_INPUT_LINE_HEIGHT_PX + CHAT_INPUT_VERTICAL_PADDING_PX;
export const CHAT_INPUT_MAX_HEIGHT_PX =
  CHAT_INPUT_LINE_HEIGHT_PX * CHAT_INPUT_MAX_ROWS + CHAT_INPUT_VERTICAL_PADDING_PX;

type ResizableTextarea = {
  scrollHeight: number;
  style: {
    height: string;
    overflowY: string;
  };
};

export interface ChatInputResizeOptions {
  minHeightPx?: number;
  maxHeightPx?: number;
}

export function resizeChatInputTextarea(
  element: ResizableTextarea,
  options: ChatInputResizeOptions = {},
) {
  const minHeightPx = options.minHeightPx ?? CHAT_INPUT_MIN_HEIGHT_PX;
  const maxHeightPx = options.maxHeightPx ?? CHAT_INPUT_MAX_HEIGHT_PX;

  element.style.height = `${minHeightPx}px`;

  const nextHeight = Math.min(element.scrollHeight, maxHeightPx);
  element.style.height = `${Math.max(nextHeight, minHeightPx)}px`;
  element.style.overflowY = element.scrollHeight > maxHeightPx ? "auto" : "hidden";
}
