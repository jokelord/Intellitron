"""
轻量级意图路由器

在进入完整 Agent 流程前，用极少的 token 判断当前消息是否需要调用工具。
若不需要工具，直接走普通流式对话，避免 Agent 格式噪音（Calling tools: [...]）。

设计原则：
- 路由请求不携带任何工具描述，极简系统提示，max_new_tokens=5
- 只需模型回复 YES / NO
- 失败时保守回退（认为需要工具），不影响主流程
"""

import logging
from backend.llm_service import LLMService, Message

logger = logging.getLogger(__name__)

# ── 路由判断极简提示（不含任何工具描述）────────────────────────────────────
_ROUTER_SYSTEM = (
    "You are a routing classifier. "
    "Decide if the user request requires any of these operations: "
    "file read/write/edit, shell command execution, web search/fetch. "
    "Reply ONLY with YES (tools needed) or NO (can answer directly). "
    "No explanation."
)

# ── 直接回答路径使用的简洁系统提示 ──────────────────────────────────────────
DIRECT_CHAT_SYSTEM = (
    "你是一个智能 AI 助手，请用简洁、友好的自然语言回答用户问题。"
)


class IntentRouter:
    """轻量级意图路由器，决定当前对话是否需要走完整 Agent 流程。"""

    def __init__(self, llm_service: LLMService):
        self.llm_service = llm_service

    def needs_tools(self, message: str, recent_history: list[Message] | None = None) -> bool:
        """
        判断当前用户消息是否需要调用工具。

        Args:
            message: 当前用户消息文本
            recent_history: 最近对话历史（Message 对象列表），至多取最后 4 条

        Returns:
            True  → 需要工具，走完整 Agent 流程
            False → 无需工具，走直接回答流程
        """
        messages: list[Message] = [Message(role="system", content=_ROUTER_SYSTEM)]

        # 包含最近 1-2 轮历史，帮助判断上下文相关意图（如"把上面的答案写入文件"）
        if recent_history:
            for m in recent_history[-4:]:
                messages.append(m)

        messages.append(Message(role="user", content=message))

        try:
            result = self.llm_service.chat(
                messages=messages,
                max_new_tokens=5,
                temperature=0.0,
                stream=False,
            )
            # stream=False 时 chat() 返回 GenerationResult
            from backend.llm_service import GenerationResult
            if not isinstance(result, GenerationResult):
                logger.warning("[IntentRouter] 意外的返回类型，保守回退到 Agent")
                return True
            answer = (result.text or "").strip().upper()
            needs = answer.startswith("YES")
            logger.info(
                f"[IntentRouter] '{message[:60]}' → {'TOOLS' if needs else 'DIRECT'} "
                f"(raw={answer!r})"
            )
            return needs
        except Exception as e:
            logger.warning(f"[IntentRouter] 判断失败，保守回退到 Agent 模式: {e}")
            return True
