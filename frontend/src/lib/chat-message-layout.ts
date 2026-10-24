export type ChatMessageRole = "user" | "assistant";

export function getChatMessageBubbleWidthClass(role: ChatMessageRole): string {
  return role === "assistant" ? "min-w-0 max-w-[calc(100%-3.75rem)]" : "min-w-0 max-w-[80%]";
}
