'use client';

import React, { useState, useRef, useEffect, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { Send, Trash2, Settings, Bot, User, Loader2, Zap, StopCircle, RefreshCw } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import { llmClient, ChatMessage, AgentStep, AgentChatRequest, stopChat, stopAgent, stopAllAgents, answerNow } from '@/lib/api';
import AgentStepDisplay from '@/components/AgentStepDisplay';
import { ToggleSwitch } from '@/components/ToggleSwitch';
import ImageLightbox from '@/components/ImageLightbox';
import CycleEditor from '@/components/CycleEditor';
import CycleProgress from '@/components/CycleProgress';
import {
  CycleStep,
  CycleSSEEvent,
  CycleExecutionStatus,
} from '@/lib/cycle-types';

/**
 * 对话消息接口（包含UI状态）
 */
interface Message extends ChatMessage {
  id: string;
  timestamp: Date;
  isStreaming?: boolean;
  images?: string[];
  agentSteps?: AgentStep[];
}

/**
 * 聊天设置接口
 */
interface ChatSettings {
  temperature: number;
  topP: number;
  topK: number;
  repetitionPenalty: number;
  maxTokens: number;
  systemPrompt: string;
  showReasoning: boolean;
  agentMode: boolean;
}

/**
 * 默认设置
 */
const DEFAULT_SETTINGS: ChatSettings = {
  temperature: 0.6,
  topP: 0.95,
  topK: 50,
  repetitionPenalty: 1.0,
  maxTokens: 4096,
  systemPrompt: '你是一个有帮助的AI助手，运行在 NVIDIA Jetson Thor 平台上。请用简洁、准确的语言回答问题。',
  showReasoning: true,
  agentMode: false,
};

/**
 * 估算文本的 token 数量（中文近似算法）
 */
function estimateTokenCount(text: string): number {
  // 中文字符大约 1.5-2 个 token，英文单词大约 1 个 token
  const chineseChars = (text.match(/[\u4e00-\u9fff]/g) || []).length;
  const englishWords = text.replace(/[\u4e00-\u9fff]/g, '').split(/\s+/).filter(word => word.length > 0).length;
  const otherChars = text.length - chineseChars - englishWords;

  return Math.ceil(chineseChars * 1.5 + englishWords + otherChars * 0.5);
}

/**
 * 截断消息历史以适应上下文长度限制
 */
function truncateMessagesForContext(
  messages: Message[],
  maxContextLength: number,
  systemPrompt?: string
): ChatMessage[] {
  const chatMessages: ChatMessage[] = [];
  let totalTokens = 0;

  // 预留空间给系统提示和当前响应
  const reservedTokens = 8192; // 预留 8K tokens
  const availableTokens = maxContextLength - reservedTokens;

  // 添加系统提示
  if (systemPrompt) {
    const systemTokens = estimateTokenCount(systemPrompt);
    if (totalTokens + systemTokens <= availableTokens) {
      chatMessages.push({
        role: 'system',
        content: systemPrompt,
      });
      totalTokens += systemTokens;
    }
  }

  // 从最新的消息开始添加，只发送content（不包含推理过程）
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    // 提取文本内容进行 token 估算
    let textContent = '';
    if (typeof msg.content === 'string') {
      textContent = msg.content;
    } else if (Array.isArray(msg.content)) {
      textContent = msg.content
        .filter((c: any) => c.type === 'text')
        .map((c: any) => c.text)
        .join(' ');
    }
    const msgTokens = estimateTokenCount(textContent); // 只估算content的token数

    if (totalTokens + msgTokens <= availableTokens) {
      // 在开头插入以保持顺序，只发送content
      chatMessages.unshift({
        role: msg.role,
        content: msg.content,
      });
      totalTokens += msgTokens;
    } else {
      // 如果连一条消息都放不下，至少保留最后一条用户消息
      if (chatMessages.length === 0 && msg.role === 'user') {
        // 截断文本内容
        const truncatedContent = typeof msg.content === 'string'
          ? msg.content.substring(0, Math.floor(availableTokens * 0.5))
          : msg.content;
        chatMessages.unshift({
          role: msg.role,
          content: truncatedContent,
        });
      }
      break;
    }
  }

  return chatMessages;
}

/**
 * 生成唯一ID
 */
function generateId(): string {
  return `msg-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
}

/**
 * 主页面组件
 * 
 * 提供完整的聊天界面，包括：
 * - 消息列表显示
 * - 流式响应渲染
 * - 设置面板
 */
export default function Home() {
  const router = useRouter();

  // ============================================================
  // 性能优化：预编译 /vision 路由 + sessionStorage 缓存
  // 解决 Next.js 开发模式下按需编译导致的跳转延迟
  // ============================================================
  useEffect(() => {
    // 1. 立即预编译 /vision 页面（与 API 调用并行，消除编译等待）
    router.prefetch('/vision');

    // 2. 检查 sessionStorage 缓存，实现后续访问瞬间跳转
    const cachedModelType = sessionStorage.getItem('llm-model-type');
    if (cachedModelType === 'vision') {
      console.log('[LLM-Test] 命中缓存：vision 模式，瞬间跳转');
      router.replace('/vision');
    }
  }, [router]);

  // 状态管理
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [isStopped, setIsStopped] = useState(false);
  const [messageQueue, setMessageQueue] = useState<string[]>([]);
  const [showSettings, setShowSettings] = useState(false);
  const [settings, setSettings] = useState<ChatSettings>(DEFAULT_SETTINGS);
  const [pendingSettings, setPendingSettings] = useState<ChatSettings | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<'checking' | 'connected' | 'error'>('checking');
  const [modelName, setModelName] = useState<string>('加载中...');
  const [modelInfo, setModelInfo] = useState<{ name: string, description: string } | null>(null);
  const [maxContextLength, setMaxContextLength] = useState<number>(1048576); // 默认1M
  const [serverDefaults, setServerDefaults] = useState<ChatSettings | null>(null);
  const [agentSessionId, setAgentSessionId] = useState<string | undefined>(undefined);
  const [currentRequestId, setCurrentRequestId] = useState<string | undefined>(undefined);

  // Cycle state
  const [cycleStatus, setCycleStatus] = useState<CycleExecutionStatus>('idle');
  const [cycleSessionId, setCycleSessionId] = useState<string | null>(null);
  const [cycleEvents, setCycleEvents] = useState<CycleSSEEvent[]>([]);
  const [cycleAbortController, setCycleAbortController] = useState<AbortController | null>(null);
  const [cycleConfig, setCycleConfig] = useState<{
    enabled: boolean;
    max_depth: number;
    show_thumbnails: boolean;
    default_sequence: CycleStep[];
  }>({ enabled: false, max_depth: 5, show_thumbnails: true, default_sequence: [] });
  const [showCycle, setShowCycle] = useState(false);
  const [lightboxSrc, setLightboxSrc] = useState<string | null>(null);

  // refs
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const isHydratedRef = useRef(false);
  const handleSendRef = useRef<() => Promise<void> | void>(() => {});
  const stopWasClickedRef = useRef(false);
  const isLoadingRef = useRef(false);
  const cycleUserMsgIdRef = useRef<string | null>(null);

  // 客户端挂载后从 localStorage 恢复设置（避免服务端/客户端 hydration 不一致）
  useEffect(() => {
    const saved = localStorage.getItem('llm-chat-settings');
    if (saved) {
      try {
        setSettings({ ...DEFAULT_SETTINGS, ...JSON.parse(saved) });
      } catch {
        // ignore malformed JSON
      }
    }
    isHydratedRef.current = true;
  }, []);

  /**
   * 保存设置到 localStorage
   */
  useEffect(() => {
    if (!isHydratedRef.current) return;
    localStorage.setItem('llm-chat-settings', JSON.stringify(settings));
  }, [settings]);

  /**
   * 滚动到消息底部
   */
  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  /**
   * 获取模型信息和配置
   */
  const fetchModelInfo = useCallback(async () => {
    try {
      // 获取模型信息
      const modelsResponse = await llmClient.listModels();
      if (modelsResponse && modelsResponse.length > 0) {
        const model = modelsResponse[0];
        setModelName(model.id);
        setModelInfo({
          name: model.id,
          description: `这是一个基于 ${model.id} 模型的对话体验程序。`
        });
        // 设置最大上下文长度
        if (model.context_length) {
          setMaxContextLength(model.context_length);
        }
      }

      // 获取配置信息并更新默认设置
      const configResponse = await llmClient.getConfig();

      // 检测是否为视觉模型，如果是则自动跳转到视觉聊天页面
      if (configResponse.model?.model_type === 'vision') {
        sessionStorage.setItem('llm-model-type', 'vision');
        router.replace('/vision');
        return;
      } else {
        sessionStorage.setItem('llm-model-type', configResponse.model?.model_type || 'text');
      }

      if (configResponse && configResponse.inference) {
        const agentEnabled = configResponse.agent?.enabled ?? false;
        const serverDefaults = {
          temperature: configResponse.inference.temperature,
          topP: configResponse.inference.top_p,
          topK: configResponse.inference.top_k,
          repetitionPenalty: configResponse.inference.repetition_penalty,
          maxTokens: configResponse.inference.max_tokens,
          systemPrompt: DEFAULT_SETTINGS.systemPrompt,
          showReasoning: DEFAULT_SETTINGS.showReasoning,
          agentMode: agentEnabled,
        };

        // 更新服务器默认值（用于新用户或重置设置）
        setServerDefaults(serverDefaults);

        // agentMode 始终跟随后端 agent.enabled，用户可在设置中手动关闭
        setSettings((prev: ChatSettings) => {
          const saved = localStorage.getItem('llm-chat-settings');
          const base = saved ? { ...DEFAULT_SETTINGS, ...JSON.parse(saved) } : serverDefaults;
          return { ...base, agentMode: agentEnabled };
        });
      }
    } catch (error) {
      console.error('获取模型信息失败:', error);
      setModelName('AI模型');
      setModelInfo({
        name: 'AI模型',
        description: '这是一个基于AI模型的对话体验程序。'
      });
    }
  }, []);

  /**
   * 检查后端连接状态
   */
  useEffect(() => {
    // 立即检查配置以决定是否跳转
    const checkAndRedirect = async () => {
      try {
        const configResponse = await llmClient.getConfig();
        console.log('[LLM-Test] 获取配置成功:', configResponse);

        // 检测是否为视觉模型，如果是则自动跳转到视觉聊天页面
        if (configResponse?.model?.model_type === 'vision') {
          console.log('[LLM-Test] 检测到视觉模型，自动跳转到 /vision');
          sessionStorage.setItem('llm-model-type', 'vision');
          router.replace('/vision');
          return true;
        } else {
          sessionStorage.setItem('llm-model-type', configResponse?.model?.model_type || 'text');
        }
      } catch (configError) {
        console.error('[LLM-Test] 获取配置失败:', configError);
      }
      return false;
    };

    async function checkConnection() {
      const isVision = await checkAndRedirect();
      if (isVision) return;

      try {
        await llmClient.healthCheck();
        setConnectionStatus('connected');
        fetchModelInfo();
      } catch {
        setConnectionStatus('error');
      }
    }

    checkConnection();

    // 每30秒检查一次连接
    const interval = setInterval(checkConnection, 30000);
    return () => clearInterval(interval);
  }, [fetchModelInfo, router]);

  /**
   * 获取 Cycle 配置
   */
  useEffect(() => {
    llmClient.getConfig()
      .then((data: any) => {
        if (data.cycle?.enabled) {
          setCycleConfig({
            enabled: true,
            max_depth: data.cycle.max_depth || 5,
            show_thumbnails: data.cycle.show_thumbnails !== false,
            default_sequence: data.cycle.default_sequence || [],
          });
        }
      })
      .catch(() => {});
  }, []);

  /**
   * 消息更新时滚动到底部
   */
  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  /**
   * 保存设置到localStorage
   */
  useEffect(() => {
    localStorage.setItem('llm-chat-settings', JSON.stringify(settings));
  }, [settings]);

  /**
   * 发送消息
   */
  const handleSend = useCallback(async () => {
    const trimmedInput = input.trim();
    if (!trimmedInput) return;

    // If currently loading, queue the message for later
    if (isLoading) {
      setMessageQueue((prev) => [...prev, trimmedInput]);
      setInput('');
      return;
    }

    // Reset stop state for new request
    stopWasClickedRef.current = false;
    setIsStopped(false);
    isLoadingRef.current = true;
    console.log('[FRONTEND] handleSend: setting isLoading=true, agentSessionId:', agentSessionId);
    setIsLoading(true);

    // Generate a unique request ID for this chat request
    const requestId = `req_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
    setCurrentRequestId(requestId);

    // 创建用户消息
    const userMessage: Message = {
      id: generateId(),
      role: 'user',
      content: trimmedInput,
      timestamp: new Date(),
    };

    // 创建助手消息占位符
    const assistantMessage: Message = {
      id: generateId(),
      role: 'assistant',
      content: '',
      timestamp: new Date(),
      isStreaming: true,
    };

    // 更新状态 - 同时将之前的助手消息标记为已完成
    setMessages((prev: Message[]) => {
      const updated = prev.map(msg => 
        msg.role === 'assistant' ? { ...msg, isStreaming: false } : msg
      );
      return [...updated, userMessage, assistantMessage];
    });
    setInput('');
    setIsLoading(true);
    setIsStopped(false);

    // 构建消息历史
    const chatMessages: ChatMessage[] = [];

    // 添加系统提示
    if (settings.systemPrompt) {
      chatMessages.push({
        role: 'system',
        content: settings.systemPrompt,
      });
    }

    // 添加历史消息（根据上下文长度动态截断）
    const recentMessages = messages.slice(-20); // 先取最近20条
    const truncatedMessages = truncateMessagesForContext(
      recentMessages,
      maxContextLength,
      settings.systemPrompt
    );

    // 添加截断后的历史消息
    for (const msg of truncatedMessages) {
      if (msg.role !== 'system') { // 系统消息已单独添加
        chatMessages.push({
          role: msg.role,
          content: msg.content,
        });
      }
    }

    // 添加当前用户消息
    chatMessages.push({
      role: 'user',
      content: trimmedInput,
    });

    try {
      if (settings.agentMode) {
        // ====== Agent 模式 ======
        await llmClient.agentChatStream(
          {
            session_id: agentSessionId,
            message: trimmedInput,
          },
          // onStep: 累积 Agent 步骤
          (step: AgentStep) => {
            console.log('[FRONTEND] onStep received:', step.step_type, 'content length:', step.content?.length, 'partial:', step.partial);
            setMessages((prev: Message[]) => {
              const updated = [...prev];
              const lastMessage = updated[updated.length - 1];
              if (lastMessage && lastMessage.role === 'assistant') {
                const steps = lastMessage.agentSteps || [];
                const lastStep = steps.length > 0 ? steps[steps.length - 1] : null;

                if (step.step_type === 'thought') {
                  // 如果上一个步骤是 partial 思考框，则追加内容（支持 token 流式）
                  if (lastStep?.step_type === 'thought' && lastStep.partial) {
                    const merged = {
                      ...lastStep,
                      content: (lastStep.content || '') + (step.content || ''),
                      partial: step.partial ?? false,
                    };
                    lastMessage.agentSteps = [...steps.slice(0, -1), merged];
                  } else {
                    lastMessage.agentSteps = [...steps, step];
                  }
                } else {
                  // 非思考步骤到达时，先把 partial 思考框 finalize（移除光标）
                  if (lastStep?.step_type === 'thought' && lastStep.partial) {
                    const finalized = { ...lastStep, partial: false };
                    lastMessage.agentSteps = [...steps.slice(0, -1), finalized, step];
                  } else {
                    lastMessage.agentSteps = [...steps, step];
                  }
                }
              }
              return updated;
            });
          },
          // onContent: 累积最终答案
          (content: string) => {
            console.log('[FRONTEND] onContent received, length:', content.length);
            setMessages((prev: Message[]) => {
              const updated = [...prev];
              const lastMessage = updated[updated.length - 1];
              if (lastMessage && lastMessage.role === 'assistant') {
                lastMessage.content += content;
              }
              return updated;
            });
          },
          // onComplete
          (sessionId?: string) => {
            console.log('[FRONTEND] onComplete called, sessionId:', sessionId, 'current agentSessionId:', agentSessionId);
            if (sessionId) {
              console.log('[FRONTEND] Setting new agentSessionId:', sessionId);
              setAgentSessionId(sessionId);
            }
            setMessages((prev: Message[]) => {
              const updated = [...prev];
              const lastMessage = updated[updated.length - 1];
              if (lastMessage) {
                lastMessage.isStreaming = false;
                // 兜底：确保完成时不留下 partial 光标
                const steps = lastMessage.agentSteps || [];
                if (steps.length > 0 && steps[steps.length - 1].partial) {
                  lastMessage.agentSteps = [
                    ...steps.slice(0, -1),
                    { ...steps[steps.length - 1], partial: false },
                  ];
                }
              }
              return updated;
            });
            setIsLoading(false);
            // Only reset isStopped if stop was NOT clicked
            if (!stopWasClickedRef.current) {
              setIsStopped(false);
            }
            stopWasClickedRef.current = false;
            setCurrentRequestId(undefined);
          },
          // onError
          (error: Error) => {
            console.error('Agent 错误:', error);
            setMessages((prev: Message[]) => {
              const updated = [...prev];
              const lastMessage = updated[updated.length - 1];
              if (lastMessage && lastMessage.role === 'assistant') {
                // If stop was clicked, don't show error - just keep the content
                if (!stopWasClickedRef.current) {
                  lastMessage.content = `抱歉，Agent 执行出错: ${error.message}`;
                }
                lastMessage.isStreaming = false;
              }
              return updated;
            });
            setIsLoading(false);
            // Only reset isStopped if stop was NOT clicked
            if (!stopWasClickedRef.current) {
              setIsStopped(false);
            }
            stopWasClickedRef.current = false;
            setCurrentRequestId(undefined);
          },
          // onSessionStart: 设置 session_id 以便立即可以停止
          (sessionId: string) => {
            console.log('[FRONTEND] Setting agentSessionId from session_start:', sessionId);
            setAgentSessionId(sessionId);
          }
        );
      } else {
        // ====== 普通聊天模式 ======
        let isFirstChunk = true;
        console.log('[CHAT] Starting chat with requestId:', requestId, 'currentRequestId state:', currentRequestId);

        await llmClient.chatStream(
          chatMessages,
          {
            temperature: settings.temperature,
            top_p: settings.topP,
            top_k: settings.topK,
            repetition_penalty: settings.repetitionPenalty,
            max_tokens: settings.maxTokens,
            request_id: requestId,
          },
          // onChunk: 更新助手消息内容
          (content: string, reasoning?: string) => {
            setMessages((prev: Message[]) => {
              const updated = [...prev];
              const lastMessage = updated[updated.length - 1];
              if (lastMessage && lastMessage.role === 'assistant') {
                if (isFirstChunk && lastMessage.content === '') {
                  isFirstChunk = false;
                }
                if (content) {
                  lastMessage.content += content;
                }
                if (reasoning) {
                  lastMessage.reasoning_content = (lastMessage.reasoning_content || '') + reasoning;
                }
              }
              return updated;
            });
          },
          // onComplete: 标记流式完成
          () => {
            setMessages((prev: Message[]) => {
              const updated = [...prev];
              const lastMessage = updated[updated.length - 1];
              if (lastMessage) {
                lastMessage.isStreaming = false;
              }
              return updated;
            });
            setIsLoading(false);
            // Only reset isStopped if stop was NOT clicked
            if (!stopWasClickedRef.current) {
              setIsStopped(false);
            }
            stopWasClickedRef.current = false;
            setCurrentRequestId(undefined);
          },
          // onError: 处理错误
          (error: Error) => {
            console.error('聊天错误:', error);
            setMessages((prev: Message[]) => {
              const updated = [...prev];
              const lastMessage = updated[updated.length - 1];
              if (lastMessage && lastMessage.role === 'assistant') {
                // If stop was clicked, don't show error - just keep the content
                if (!stopWasClickedRef.current) {
                  lastMessage.content = `抱歉，发生了错误: ${error.message}`;
                }
                lastMessage.isStreaming = false;
              }
              return updated;
            });
            setIsLoading(false);
            // Only reset isStopped if stop was NOT clicked
            if (!stopWasClickedRef.current) {
              setIsStopped(false);
            }
            stopWasClickedRef.current = false;
            setCurrentRequestId(undefined);
          }
        );
      }
    } catch (error) {
      console.error('请求失败:', error);
      setIsLoading(false);
      if (!stopWasClickedRef.current) {
        setIsStopped(false);
      }
      stopWasClickedRef.current = false;
    }
  }, [input, isLoading, messages, settings, agentSessionId, maxContextLength]);

  // Keep handleSendRef updated
  useEffect(() => {
    handleSendRef.current = handleSend;
  }, [handleSend]);

  // 处理消息队列：当加载完成后，自动发送队列中的下一条消息
  useEffect(() => {
    if (!isLoading && messageQueue.length > 0) {
      const nextMessage = messageQueue[0];
      const remainingQueue = messageQueue.slice(1);
      setMessageQueue(remainingQueue);
      // 设置 input 并触发发送
      setInput(nextMessage);
      // 使用 setTimeout 确保状态更新后再发送
      setTimeout(() => {
        handleSendRef.current();
      }, 0);
    }
  }, [isLoading, messageQueue]);

  /**
   * 处理输入框按键
   */
  const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }, [handleSend]);

  /**
   * 清空对话
   */
  const handleClear = useCallback(() => {
    setMessages([]);
    setAgentSessionId(undefined);
    setCurrentRequestId(undefined);
  }, []);

  /**
   * 停止当前聊天（普通模式）
   */
  const handleStopChat = useCallback(async () => {
    console.log('[STOP] handleStopChat called, currentRequestId:', currentRequestId, 'isLoading:', isLoading);
    stopWasClickedRef.current = true;
    setIsStopped(true);
    if (currentRequestId) {
      try {
        console.log('[STOP] Calling stopChat with:', currentRequestId);
        const result = await stopChat(currentRequestId);
        console.log('[STOP] stopChat result:', result);
      } catch (error) {
        console.error('停止聊天失败:', error);
      }
    } else {
      console.log('[STOP] No currentRequestId, cannot stop');
    }
    // Safety timeout: if stream doesn't end in 3s after stop, force reset
    setTimeout(() => {
      if (isLoadingRef.current) {
        console.log('[STOP] Safety timeout: forcing reset');
        isLoadingRef.current = false;
        setIsLoading(false);
        setIsStopped(true);
      }
    }, 3000);
  }, [currentRequestId]);

  /**
   * 停止当前 Agent 执行
   */
  const handleStopAgent = useCallback(async () => {
    console.log('[STOP] handleStopAgent called, agentSessionId:', agentSessionId);
    stopWasClickedRef.current = true;
    setIsStopped(true);
    if (agentSessionId) {
      try {
        console.log('[STOP] Calling stopAgent with:', agentSessionId);
        const result = await stopAgent(agentSessionId);
        console.log('[STOP] stopAgent result:', result);
      } catch (error) {
        console.error('停止 Agent 失败:', error);
      }
    } else {
      // First round - no session_id yet, use abort_all
      console.log('[STOP] No session_id, calling stopAllAgents');
      try {
        const result = await stopAllAgents();
        console.log('[STOP] stopAllAgents result:', result);
      } catch (error) {
        console.error('停止所有 Agent 失败:', error);
      }
    }
    
    // Safety timeout: if stream doesn't end in 3s after stop, force reset
    setTimeout(() => {
      if (isLoadingRef.current) {
        console.log('[STOP] Safety timeout: forcing reset');
        isLoadingRef.current = false;
        setIsLoading(false);
        setIsStopped(true);
      }
    }, 3000);
  }, [agentSessionId]);

  /**
   * 强制当前 Agent 立即返回答案
   */
  const handleAnswerNow = useCallback(async () => {
    if (agentSessionId) {
      try {
        await answerNow(agentSessionId);
      } catch (error) {
        console.error('Answer Now 失败:', error);
      }
    }
  }, [agentSessionId]);

  /**
   * 启动 Cycle 执行
   */
  const handleCycleStart = useCallback(async (steps: CycleStep[], shareContext: boolean) => {
    setCycleStatus('running');
    setCycleEvents([]);
    cycleUserMsgIdRef.current = null;

    const controller = await llmClient.startCycle(
      {
        steps,
        agent_mode: settings.agentMode,
        share_context: shareContext,
      },
      (event) => {
        setCycleEvents((prev) => [...prev, event]);

        // Merge LLM responses into chat messages
        if (event.type === 'llm_done') {
          setMessages((prev) => [
            ...prev,
            {
              id: generateId(),
              role: 'assistant' as const,
              content: event.content,
              timestamp: new Date(),
            },
          ]);
          cycleUserMsgIdRef.current = null;
        }

        // Show message steps in chat (with images placeholder)
        if (event.type === 'step_begin' && event.step_type === 'message') {
          const detail = event.detail as { message?: string };
          const msg = detail.message;
          if (msg) {
            const msgId = generateId();
            cycleUserMsgIdRef.current = msgId;
            setMessages((prev) => [
              ...prev,
              {
                id: msgId,
                role: 'user' as const,
                content: msg,
                timestamp: new Date(),
                images: [],
              },
            ]);
          }
        }

        // Accumulate photo thumbnails into the current user message
        if (
          event.type === 'photo_captured' &&
          'image_base64' in event &&
          event.image_base64 &&
          cycleUserMsgIdRef.current &&
          cycleConfig.show_thumbnails
        ) {
          const targetId = cycleUserMsgIdRef.current;
          const imgSrc = event.image_base64;
          setMessages((prev) =>
            prev.map((m) =>
              m.id === targetId
                ? { ...m, images: [...(m.images || []), imgSrc] }
                : m,
            ),
          );
        }

        // Track session ID
        if (event.type === 'cycle_started') {
          setCycleSessionId(event.session_id);
        }
      },
      () => {
        setCycleStatus('completed');
        setCycleAbortController(null);
        cycleUserMsgIdRef.current = null;
      },
      (error) => {
        setCycleStatus('error');
        setCycleAbortController(null);
        cycleUserMsgIdRef.current = null;
        console.error('Cycle error:', error);
      },
    );

    setCycleAbortController(controller);
  }, [settings.agentMode, cycleConfig.show_thumbnails]);

  /**
   * 停止 Cycle 执行
   */
  const handleCycleStop = useCallback(async () => {
    if (cycleSessionId) {
      await llmClient.stopCycle(cycleSessionId);
    }
    cycleAbortController?.abort();
    setCycleStatus('stopped');
    setCycleAbortController(null);
  }, [cycleSessionId, cycleAbortController]);

  /**
   * 渲染连接状态指示器
   */
  const renderConnectionStatus = () => {
    const statusConfig: Record<typeof connectionStatus, { color: string; text: string }> = {
      checking: { color: 'bg-yellow-500', text: '检查连接中...' },
      connected: { color: 'bg-green-500', text: '已连接' },
      error: { color: 'bg-red-500', text: '连接失败' },
    };

    const { color, text } = statusConfig[connectionStatus];

    return (
      <div className="flex items-center space-x-2 text-sm text-gray-500">
        <span className={`w-2 h-2 rounded-full ${color}`}></span>
        <span>{text}</span>
      </div>
    );
  };

  return (
    <div className="flex flex-col h-screen bg-gray-50 dark:bg-gray-900">
      {/* 头部 */}
      <header className="flex-shrink-0 bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 px-4 py-3">
        <div className="max-w-4xl mx-auto flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="w-10 h-10 bg-nvidia-500 rounded-lg flex items-center justify-center">
              <Bot className="w-6 h-6 text-white" />
            </div>
            <div>
              <h1 className="text-lg font-semibold text-gray-900 dark:text-white">
                LLM-Test
              </h1>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                {modelName} | NVIDIA Jetson Thor
                {settings.agentMode && (
                  <span className="ml-2 inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 text-xs font-medium">
                    <Zap className="w-3 h-3" /> Agent
                  </span>
                )}
              </p>
            </div>
          </div>

          <div className="flex items-center space-x-4">
            {renderConnectionStatus()}

            <button
              onClick={() => {
                setPendingSettings({ ...settings });
                setShowSettings(true);
              }}
              className="p-2 text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700"
              title="设置"
            >
              <Settings className="w-5 h-5" />
            </button>

            <button
              onClick={handleClear}
              className="p-2 text-gray-500 hover:text-red-500 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700"
              title="清空对话"
            >
              <Trash2 className="w-5 h-5" />
            </button>
          </div>
        </div>
      </header>

      {/* 设置对话框 */}
      {showSettings && pendingSettings && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          {/* 遮罩层 */}
          <div 
            className="absolute inset-0 bg-black/50" 
            onClick={() => setShowSettings(false)}
          />
          
          {/* 对话框 */}
          <div className="relative bg-white dark:bg-gray-800 rounded-xl shadow-2xl w-full max-w-lg max-h-[80vh] overflow-hidden mx-4">
            <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h3 className="font-medium text-gray-900 dark:text-white text-lg">设置</h3>
            </div>
            
            <div className="px-6 py-4 overflow-y-auto max-h-[60vh] space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Temperature: {pendingSettings.temperature}
                  </label>
                  <input
                    type="range"
                    min="0"
                    max="2"
                    step="0.1"
                    value={pendingSettings.temperature}
                    onChange={(e: React.ChangeEvent<HTMLInputElement>) => setPendingSettings((prev) => prev ? ({ ...prev, temperature: parseFloat(e.target.value) }) : null)}
                    className="w-full"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Top-P: {pendingSettings.topP}
                  </label>
                  <input
                    type="range"
                    min="0"
                    max="1"
                    step="0.05"
                    value={pendingSettings.topP}
                    onChange={(e: React.ChangeEvent<HTMLInputElement>) => setPendingSettings((prev) => prev ? ({ ...prev, topP: parseFloat(e.target.value) }) : null)}
                    className="w-full"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Top-K: {pendingSettings.topK}
                  </label>
                  <input
                    type="range"
                    min="1"
                    max="100"
                    step="1"
                    value={pendingSettings.topK}
                    onChange={(e: React.ChangeEvent<HTMLInputElement>) => setPendingSettings((prev) => prev ? ({ ...prev, topK: parseInt(e.target.value) }) : null)}
                    className="w-full"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Repetition Penalty: {pendingSettings.repetitionPenalty}
                  </label>
                  <input
                    type="range"
                    min="1.0"
                    max="2.0"
                    step="0.1"
                    value={pendingSettings.repetitionPenalty}
                    onChange={(e: React.ChangeEvent<HTMLInputElement>) => setPendingSettings((prev) => prev ? ({ ...prev, repetitionPenalty: parseFloat(e.target.value) }) : null)}
                    className="w-full"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    最大 Token: {pendingSettings.maxTokens}
                  </label>
                  <input
                    type="range"
                    min="256"
                    max={maxContextLength}
                    step="1024"
                    value={pendingSettings.maxTokens}
                    onChange={(e: React.ChangeEvent<HTMLInputElement>) => setPendingSettings((prev) => prev ? ({ ...prev, maxTokens: parseInt(e.target.value) }) : null)}
                    className="w-full"
                  />
                </div>
              </div>

              <div className="space-y-3">
                <ToggleSwitch
                  checked={pendingSettings.showReasoning}
                  onChange={(checked: boolean) => setPendingSettings((prev) => prev ? ({ ...prev, showReasoning: checked }) : null)}
                  label="显示推理过程"
                />

                <ToggleSwitch
                  checked={pendingSettings.agentMode}
                  onChange={(checked: boolean) => {
                    setPendingSettings((prev) => prev ? ({ ...prev, agentMode: checked }) : null);
                    if (!checked) setAgentSessionId(undefined);
                  }}
                  label={
                    <span className="flex items-center gap-1">
                      <Zap className="w-4 h-4" />
                      Agent 模式（工具调用）
                    </span>
                  }
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  系统提示词
                </label>
                <textarea
                  value={pendingSettings.systemPrompt}
                  onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setPendingSettings((prev) => prev ? ({ ...prev, systemPrompt: e.target.value }) : null)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white resize-none"
                  rows={2}
                />
              </div>

              <button
                onClick={() => {
                  if (serverDefaults) {
                    setPendingSettings(serverDefaults);
                  }
                }}
                disabled={!serverDefaults}
                className="px-4 py-2 bg-gray-500 text-white rounded-lg hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed text-sm"
                title="重置为服务器默认值"
              >
                重置为服务器默认值
              </button>
            </div>
            
            <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700 flex justify-end space-x-3">
              <button
                onClick={() => {
                  setShowSettings(false);
                  setPendingSettings(null);
                }}
                className="px-4 py-2 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
              >
                取消
              </button>
              <button
                onClick={() => {
                  if (pendingSettings) {
                    setSettings(pendingSettings);
                    localStorage.setItem('llm-chat-settings', JSON.stringify(pendingSettings));
                  }
                  setShowSettings(false);
                  setPendingSettings(null);
                }}
                className="px-4 py-2 bg-nvidia-500 text-white rounded-lg hover:bg-nvidia-600 transition-colors"
              >
                确认
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 消息列表 */}
      <main className="flex-1 overflow-y-auto px-4 py-6">
        <div className="max-w-4xl mx-auto space-y-6">
          {messages.length === 0 ? (
            <div className="text-center py-20">
              <div className="w-16 h-16 bg-nvidia-500 rounded-2xl flex items-center justify-center mx-auto mb-4">
                <Bot className="w-8 h-8 text-white" />
              </div>
              <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-2">
                欢迎使用 LLM-Test
              </h2>
              <p className="text-gray-500 dark:text-gray-400 max-w-md mx-auto">
                {modelInfo?.description || '这是一个基于 AI 模型的对话体验程序。'}
                开始输入您的问题吧！
              </p>
            </div>
          ) : (
            messages.map((message) => (
              <div
                key={message.id}
                className={`flex items-start space-x-3 ${message.role === 'user' ? 'flex-row-reverse space-x-reverse' : ''
                  }`}
              >
                {/* 头像 */}
                <div
                  className={`flex-shrink-0 w-8 h-8 rounded-lg flex items-center justify-center ${message.role === 'user'
                    ? 'bg-blue-500'
                    : 'bg-nvidia-500'
                    }`}
                >
                  {message.role === 'user' ? (
                    <User className="w-5 h-5 text-white" />
                  ) : (
                    <Bot className="w-5 h-5 text-white" />
                  )}
                </div>

                {/* 消息内容 */}
                <div
                  className={`flex-1 max-w-[80%] px-4 py-3 rounded-2xl ${message.role === 'user'
                    ? 'bg-blue-500 text-white'
                    : 'bg-white dark:bg-gray-800 text-gray-900 dark:text-white border border-gray-200 dark:border-gray-700'
                    }`}
                >
                  {message.role === 'assistant' ? (
                    <div className="markdown-content">
                      {message.agentSteps && message.agentSteps.length > 0 && (
                        <AgentStepDisplay steps={message.agentSteps} />
                      )}
                      {settings.showReasoning && message.reasoning_content && (
                        <div className="mb-2 text-sm text-gray-500 dark:text-gray-400 italic">
                          {message.reasoning_content}
                        </div>
                      )}
                        <ReactMarkdown
                        remarkPlugins={[remarkGfm, remarkMath]}
                        rehypePlugins={[[rehypeKatex, { throwOnError: false }]]}
                      >
                        {typeof message.content === 'string'
                          ? (message.content 
                              ? (isStopped ? message.content + ' [已停止]' : message.content)
                              : (message.isStreaming && (message.agentSteps?.length ?? 0) > 0 ? '' : (isStopped ? '已停止' : '思考中...')))
                          : (isStopped ? '已停止' : '思考中...')}
                      </ReactMarkdown>
                      {message.isStreaming && (
                        <span className="inline-block w-2 h-4 bg-nvidia-500 animate-pulse ml-1" />
                      )}
                    </div>
                  ) : (
                    <>
                      {/* Cycle 步骤拍照缩略图 */}
                      {message.images && message.images.length > 0 && (
                        <div className="flex gap-2 flex-wrap mb-2">
                          {message.images.map((img, idx) => (
                            <img
                              key={idx}
                              src={img}
                              alt={`拍照 ${idx + 1}`}
                              className="w-16 h-16 object-cover rounded cursor-pointer hover:opacity-80 transition-opacity"
                              onClick={() => setLightboxSrc(img)}
                            />
                          ))}
                        </div>
                      )}
                      <p className="whitespace-pre-wrap">{typeof message.content === 'string' ? message.content : ''}</p>
                    </>
                  )}
                </div>
              </div>
            ))
          )}
          <div ref={messagesEndRef} />
        </div>
      </main>

      {/* 输入区域 */}
      <footer className="flex-shrink-0 bg-white dark:bg-gray-800 border-t border-gray-200 dark:border-gray-700 px-4 py-4">
        <div className="max-w-4xl mx-auto space-y-3">
          {/* 编程循环按钮 */}
          {cycleConfig.enabled && (
            <div>
              <div className="flex gap-2">
                <button
                  onClick={() => setShowCycle(!showCycle)}
                  className={`flex-1 flex items-center justify-center gap-2 
                    px-4 py-2 border-2 border-dashed rounded-lg transition-colors
                    ${showCycle
                      ? 'border-nvidia-500 bg-nvidia-500/10'
                      : cycleStatus === 'running'
                        ? 'border-blue-500 bg-blue-500/10'
                        : 'border-gray-300 dark:border-gray-600 hover:border-nvidia-500 hover:bg-gray-50 dark:hover:bg-gray-800'
                    }`}
                >
                  <RefreshCw className={`w-5 h-5 ${
                    showCycle ? 'text-nvidia-500' 
                    : cycleStatus === 'running' ? 'text-blue-400 animate-spin' 
                    : 'text-gray-500'
                  }`} />
                  <span className={`text-sm ${
                    showCycle ? 'text-nvidia-500' 
                    : cycleStatus === 'running' ? 'text-blue-400'
                    : 'text-gray-600 dark:text-gray-400'
                  }`}>
                    {cycleStatus === 'running' ? '循环运行中' : '编程循环'}
                  </span>
                </button>
              </div>
              {showCycle && (
                <div className="mt-2 bg-gray-800/30 border border-gray-700/50 rounded-lg p-3 space-y-3">
                  <CycleEditor
                    isVisionMode={false}
                    maxDepth={cycleConfig.max_depth}
                    defaultSteps={cycleConfig.default_sequence}
                    executionStatus={cycleStatus}
                    onStart={handleCycleStart}
                    onStop={handleCycleStop}
                  />
                  {cycleStatus !== 'idle' && (
                    <CycleProgress status={cycleStatus} events={cycleEvents} />
                  )}
                </div>
              )}
            </div>
          )}

          <div className="flex items-center space-x-3">
            <div className="flex-1 relative">
              <textarea
                ref={inputRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="输入您的问题... (Shift+Enter 换行)"
                className="w-full px-4 py-3 pr-12 border border-gray-300 dark:border-gray-600 rounded-2xl bg-gray-50 dark:bg-gray-700 text-gray-900 dark:text-white resize-none focus:outline-none focus:ring-2 focus:ring-nvidia-500 focus:border-transparent"
                rows={1}
                style={{
                  minHeight: '48px',
                  maxHeight: '200px',
                }}
                disabled={isLoading || connectionStatus === 'error' || cycleStatus === 'running'}
              />
            </div>

            {isLoading ? (
              <div className="flex-shrink-0 flex space-x-2">
                {/* Stop button - shows in both modes */}
                <button
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    if (settings.agentMode) {
                      handleStopAgent();
                    } else {
                      handleStopChat();
                    }
                  }}
                  className="flex-shrink-0 w-12 px-3 py-3 bg-red-500 text-white rounded-2xl flex items-center justify-center hover:bg-red-600 transition-colors"
                  title="停止"
                >
                  <StopCircle className="w-5 h-5" />
                </button>
                {/* Answer Now button - only in agent mode */}
                {settings.agentMode && (
                  <button
                    onClick={handleAnswerNow}
                    className="flex-shrink-0 w-12 px-3 py-3 bg-orange-500 text-white rounded-2xl flex items-center justify-center hover:bg-orange-600 transition-colors"
                    title="立即返回答案"
                  >
                    <Zap className="w-5 h-5" />
                  </button>
                )}
              </div>
            ) : (
              <button
                onClick={handleSend}
                disabled={!input.trim() || isLoading || connectionStatus === 'error'}
                className="flex-shrink-0 w-12 px-3 py-3 bg-nvidia-500 text-white rounded-2xl flex items-center justify-center hover:bg-nvidia-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                <Send className="w-5 h-5" />
              </button>
            )}
          </div>
        </div>
      </footer>

      {/* 图片全屏预览 */}
      <ImageLightbox src={lightboxSrc} onClose={() => setLightboxSrc(null)} />
    </div>
  );
}
