/**
 * 视觉聊天页面
 * 
 * 提供多模态对话功能，支持图片上传和摄像头拍照。
 */

'use client';

import React, { useState, useRef, useEffect, useCallback } from 'react';
import { Send, Trash2, Settings, Bot, User, Loader2, Image as ImageIcon, Zap } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import { llmClient, ChatMessage, ConfigResponse, AgentStep, AgentChatRequest } from '@/lib/api';
import ImageUploader from '@/components/ImageUploader';
import ImagePreview from '@/components/ImagePreview';
import AgentStepDisplay from '@/components/AgentStepDisplay';

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
 * 生成唯一ID
 */
function generateId(): string {
  return `msg-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
}

/**
 * 估算文本的 token 数量
 */
function estimateTokenCount(text: string): number {
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
  const reservedTokens = 8192;
  const availableTokens = maxContextLength - reservedTokens;

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

  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    let contentForTokenCount = '';

    if (typeof msg.content === 'string') {
      contentForTokenCount = msg.content;
    } else if (Array.isArray(msg.content)) {
      contentForTokenCount = msg.content
        .filter((c: any) => c.type === 'text')
        .map((c: any) => c.text)
        .join(' ');
    }

    const msgTokens = estimateTokenCount(contentForTokenCount);

    if (totalTokens + msgTokens <= availableTokens) {
      chatMessages.unshift({
        role: msg.role,
        content: msg.content,
      });
      totalTokens += msgTokens;
    } else {
      if (chatMessages.length === 0 && msg.role === 'user') {
        chatMessages.unshift({
          role: msg.role,
          content: typeof msg.content === 'string'
            ? msg.content.substring(0, Math.floor(availableTokens * 0.5))
            : msg.content,
        });
      }
      break;
    }
  }

  return chatMessages;
}

/**
 * 主页面组件
 */
export default function VisionChat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [images, setImages] = useState<string[]>([]);
  const [imagesKey, setImagesKey] = useState(0);
  const [isLoading, setIsLoading] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [agentSessionId, setAgentSessionId] = useState<string | undefined>(undefined);

  // 监听图片被清空时，强制 remount ImageUploader（重置 file input 等内部状态）
  useEffect(() => {
    if (images.length === 0) {
      setImagesKey(prev => prev + 1);
    }
  }, [images.length]);

  const [settings, setSettings] = useState<ChatSettings>(() => {
    if (typeof window !== 'undefined') {
      const saved = localStorage.getItem('llm-chat-settings');
      return saved ? { ...DEFAULT_SETTINGS, ...JSON.parse(saved) } : DEFAULT_SETTINGS;
    }
    return DEFAULT_SETTINGS;
  });
  const [connectionStatus, setConnectionStatus] = useState<'checking' | 'connected' | 'error'>('checking');
  const [modelName, setModelName] = useState<string>('加载中...');
  const [maxContextLength, setMaxContextLength] = useState<number>(1048576);
  const [serverDefaults, setServerDefaults] = useState<ChatSettings | null>(null);
  const [visionConfig, setVisionConfig] = useState<{
    maxImageSize: number;
    supportedFormats: string[];
    maxImageTokens: number;
    cameraDimension: number;
  } | null>(null);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  /**
   * 保存设置到 localStorage
   */
  useEffect(() => {
    localStorage.setItem('llm-chat-settings', JSON.stringify(settings));
  }, [settings]);

  /**
   * 滚动到消息底部
   */
  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  /**
   * 获取配置信息
   */
  const fetchConfig = useCallback(async () => {
    try {
      const configResponse = await llmClient.getConfig();

      // 设置模型信息
      if (configResponse.model) {
        setModelName(configResponse.model.model_name);
        if (configResponse.model.max_context_length) {
          setMaxContextLength(configResponse.model.max_context_length);
        }
      }

      // 设置视觉配置
      if (configResponse.vision) {
        setVisionConfig({
          maxImageSize: configResponse.vision.max_image_size,
          supportedFormats: configResponse.vision.supported_formats.split(','),
          maxImageTokens: configResponse.vision.max_image_tokens,
          cameraDimension: configResponse.vision.camera_max_dimension ?? 768,
        });
      }

      // 设置服务器默认值
      if (configResponse.inference) {
        const agentEnabled = (configResponse as any).agent?.enabled ?? false;
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
        setServerDefaults(serverDefaults);

        // agentMode 始终跟随后端 agent.enabled
        setSettings((prev) => {
          const saved = localStorage.getItem('llm-chat-settings');
          const base = saved ? { ...DEFAULT_SETTINGS, ...JSON.parse(saved) } : serverDefaults;
          return { ...base, agentMode: agentEnabled };
        });
      }
    } catch (error) {
      console.error('获取配置失败:', error);
      setModelName('AI模型');
    }
  }, []);

  /**
   * 检查后端连接状态
   */
  useEffect(() => {
    async function checkConnection() {
      try {
        await llmClient.healthCheck();
        setConnectionStatus('connected');
        fetchConfig();
      } catch {
        setConnectionStatus('error');
      }
    }

    checkConnection();
    const interval = setInterval(checkConnection, 30000);
    return () => clearInterval(interval);
  }, [fetchConfig]);

  /**
   * 消息更新时滚动到底部
   */
  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  /**
   * 构建多模态消息内容
   */
  const buildMultimodalContent = useCallback((text: string, imageList: string[]): ChatMessage['content'] => {
    if (imageList.length === 0) {
      return text;
    }

    const content: any[] = [];

    // Qwen3-VL 要求图片先于文字，先添加图片
    for (const img of imageList) {
      content.push({
        type: 'image_url',
        image_url: { url: img },
      });
    }

    // 再添加文本
    if (text.trim()) {
      content.push({ type: 'text', text });
    }

    return content;
  }, []);

  /**
   * 发送消息
   */
  const handleSend = useCallback(async () => {
    const trimmedInput = input.trim();

    // 检查是否可以发送
    if ((!trimmedInput && images.length === 0) || isLoading) {
      return;
    }

    // 保存当前图片副本（用于消息）
    const currentImages = [...images];

    // 创建用户消息
    const userMessage: Message = {
      id: generateId(),
      role: 'user',
      content: buildMultimodalContent(trimmedInput, currentImages),
      timestamp: new Date(),
      images: currentImages.length > 0 ? currentImages : undefined,
    };

    // 创建助手消息占位符
    const assistantMessage: Message = {
      id: generateId(),
      role: 'assistant',
      content: '',
      timestamp: new Date(),
      isStreaming: true,
    };

    // 使用函数式更新确保使用最新状态
    setMessages((prev: Message[]) => [...prev, userMessage, assistantMessage]);
    setInput('');
    // 清空图片
    setImages([]);
    setIsLoading(true);

    // 构建消息历史
    const chatMessages: ChatMessage[] = [];

    if (settings.systemPrompt) {
      chatMessages.push({
        role: 'system',
        content: settings.systemPrompt,
      });
    }

    const recentMessages = messages.slice(-20);
    const truncatedMessages = truncateMessagesForContext(
      recentMessages,
      maxContextLength,
      settings.systemPrompt
    );

    for (const msg of truncatedMessages) {
      if (msg.role !== 'system') {
        chatMessages.push({
          role: msg.role,
          content: msg.content,
        });
      }
    }

    // 添加当前用户消息
    chatMessages.push({
      role: 'user',
      content: buildMultimodalContent(trimmedInput, currentImages),
    });

    try {
      if (settings.agentMode) {
        // ====== Agent 模式 ======
        const agentText = trimmedInput || '(图片已附加，请分析)';
        await llmClient.agentChatStream(
          {
            session_id: agentSessionId,
            message: agentText,
            images: currentImages.length > 0 ? currentImages : undefined,
          },
          (step: AgentStep) => {
            setMessages((prev: Message[]) => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (last && last.role === 'assistant') {
                last.agentSteps = [...(last.agentSteps || []), step];
              }
              return updated;
            });
          },
          (content: string) => {
            setMessages((prev: Message[]) => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (last && last.role === 'assistant') {
                last.content = (last.content as string) + content;
              }
              return updated;
            });
          },
          (sessionId?: string) => {
            if (sessionId) setAgentSessionId(sessionId);
            setMessages((prev: Message[]) => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (last) last.isStreaming = false;
              return updated;
            });
            setIsLoading(false);
          },
          (error: Error) => {
            console.error('Agent 错误:', error);
            setMessages((prev: Message[]) => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (last && last.role === 'assistant') {
                last.content = `抱歉，Agent 执行出错: ${error.message}`;
                last.isStreaming = false;
              }
              return updated;
            });
            setIsLoading(false);
          }
        );
      } else {
        // ====== 普通聊天模式 ======
        let isFirstChunk = true;

        await llmClient.chatStream(
          chatMessages,
          {
            temperature: settings.temperature,
            top_p: settings.topP,
            top_k: settings.topK,
            repetition_penalty: settings.repetitionPenalty,
            max_tokens: settings.maxTokens,
          },
          (content: string, reasoning?: string) => {
            setMessages((prev: Message[]) => {
              const updated = [...prev];
              const lastMessage = updated[updated.length - 1];
              if (lastMessage && lastMessage.role === 'assistant') {
                if (isFirstChunk && lastMessage.content === '') {
                  isFirstChunk = false;
                }
                if (content) {
                  lastMessage.content = (lastMessage.content as string) + content;
                }
                if (reasoning) {
                  lastMessage.reasoning_content = (lastMessage.reasoning_content || '') + reasoning;
                }
              }
              return updated;
            });
          },
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
          },
          (error: Error) => {
            console.error('聊天错误:', error);
            setMessages((prev: Message[]) => {
              const updated = [...prev];
              const lastMessage = updated[updated.length - 1];
              if (lastMessage && lastMessage.role === 'assistant') {
                lastMessage.content = `抱歉，发生了错误: ${error.message}`;
                lastMessage.isStreaming = false;
              }
              return updated;
            });
            setIsLoading(false);
          }
        );
      }
    } catch (error) {
      console.error('请求失败:', error);
      setIsLoading(false);
    }
  }, [input, images, isLoading, messages, settings, maxContextLength, buildMultimodalContent, agentSessionId]);

  /**
   * 处理输入框按键
   */
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  /**
   * 清空对话
   */
  const handleClear = useCallback(() => {
    setMessages([]);
    setImages([]);
  }, []);

  /**
   * 处理图片变化（支持函数式更新，透传给 React setState 以保证拿到最新 state）
   */
  const handleImagesChange = useCallback((newImages: string[] | ((prev: string[]) => string[])) => {
    setImages(newImages);
  }, []);

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
            <div className="w-8 h-8 bg-nvidia-500 rounded-lg flex items-center justify-center">
              <ImageIcon className="w-5 h-5 text-white" />
            </div>
            <div>
              <h1 className="text-base font-semibold text-gray-900 dark:text-white">
                LLM-Test 视觉模式
              </h1>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                {modelName} | NVIDIA Jetson Thor
              </p>
            </div>
          </div>

          <div className="flex items-center space-x-4">
            {renderConnectionStatus()}

            <button
              onClick={() => setShowSettings(!showSettings)}
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

      {/* 设置面板 */}
      {showSettings && (
        <div className="flex-shrink-0 bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 px-4 py-4">
          <div className="max-w-4xl mx-auto space-y-4">
            <h3 className="font-medium text-gray-900 dark:text-white">设置</h3>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Temperature: {settings.temperature}
                </label>
                <input
                  type="range"
                  min="0"
                  max="2"
                  step="0.1"
                  value={settings.temperature}
                  onChange={(e) => setSettings((prev) => ({ ...prev, temperature: parseFloat(e.target.value) }))}
                  className="w-full"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Top-P: {settings.topP}
                </label>
                <input
                  type="range"
                  min="0"
                  max="1"
                  step="0.05"
                  value={settings.topP}
                  onChange={(e) => setSettings((prev) => ({ ...prev, topP: parseFloat(e.target.value) }))}
                  className="w-full"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Top-K: {settings.topK}
                </label>
                <input
                  type="range"
                  min="1"
                  max="100"
                  step="1"
                  value={settings.topK}
                  onChange={(e) => setSettings((prev) => ({ ...prev, topK: parseInt(e.target.value) }))}
                  className="w-full"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Repetition Penalty: {settings.repetitionPenalty}
                </label>
                <input
                  type="range"
                  min="1.0"
                  max="2.0"
                  step="0.1"
                  value={settings.repetitionPenalty}
                  onChange={(e) => setSettings((prev) => ({ ...prev, repetitionPenalty: parseFloat(e.target.value) }))}
                  className="w-full"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  最大 Token: {settings.maxTokens}
                </label>
                <input
                  type="range"
                  min="256"
                  max={maxContextLength}
                  step="1024"
                  value={settings.maxTokens}
                  onChange={(e) => setSettings((prev) => ({ ...prev, maxTokens: parseInt(e.target.value) }))}
                  className="w-full"
                />
              </div>

              <div className="flex items-center space-x-2">
                <input
                  type="checkbox"
                  id="showReasoning"
                  checked={settings.showReasoning}
                  onChange={(e) => setSettings((prev) => ({ ...prev, showReasoning: e.target.checked }))}
                  className="rounded"
                />
                <label htmlFor="showReasoning" className="text-sm font-medium text-gray-700 dark:text-gray-300">
                  显示推理过程
                </label>
              </div>

              <div className="flex items-center space-x-2">
                <input
                  type="checkbox"
                  id="agentMode"
                  checked={settings.agentMode}
                  onChange={(e) => {
                    setSettings((prev) => ({ ...prev, agentMode: e.target.checked }));
                    if (!e.target.checked) setAgentSessionId(undefined);
                  }}
                  className="rounded"
                />
                <label htmlFor="agentMode" className="text-sm font-medium text-gray-700 dark:text-gray-300">
                  <span className="flex items-center gap-1">
                    <Zap className="w-4 h-4" />
                    Agent 模式（工具调用）
                  </span>
                </label>
              </div>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                系统提示词
              </label>
              <textarea
                value={settings.systemPrompt}
                onChange={(e) => setSettings((prev) => ({ ...prev, systemPrompt: e.target.value }))}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg bg-white dark:bg-gray-700 text-gray-900 dark:text-white resize-none"
                rows={2}
              />
            </div>

            <div className="flex justify-end space-x-2">
              <button
                onClick={() => {
                  if (serverDefaults) {
                    setSettings(serverDefaults);
                    localStorage.setItem('llm-chat-settings', JSON.stringify(serverDefaults));
                  }
                }}
                disabled={!serverDefaults}
                className="px-4 py-2 bg-gray-500 text-white rounded-lg hover:bg-gray-600 disabled:opacity-50 disabled:cursor-not-allowed text-sm"
              >
                重置为服务器默认值
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
                <ImageIcon className="w-8 h-8 text-white" />
              </div>
              <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-2">
                欢迎使用视觉聊天
              </h2>
              <p className="text-gray-500 dark:text-gray-400 max-w-md mx-auto">
                上传图片或使用摄像头拍照，询问关于图片的问题。
              </p>
            </div>
          ) : (
            messages.map((message) => (
              <div
                key={message.id}
                className={`flex items-start space-x-3 ${message.role === 'user' ? 'flex-row-reverse space-x-reverse' : ''}`}
              >
                {/* 头像 */}
                <div
                  className={`flex-shrink-0 w-8 h-8 rounded-lg flex items-center justify-center ${message.role === 'user' ? 'bg-blue-500' : 'bg-nvidia-500'
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
                  {/* 用户消息中的小图标的图片预览 */}
                  {message.role === 'user' && message.images && message.images.length > 0 && (
                    <div className="flex gap-1 mb-2">
                      {message.images.map((img, idx) => (
                        <img
                          key={idx}
                          src={img}
                          alt={`图片 ${idx + 1}`}
                          className="w-8 h-8 object-cover rounded"
                        />
                      ))}
                    </div>
                  )}

                  {/* 文本内容 */}
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
                        {typeof message.content === 'string' ? message.content || '思考中...' : '思考中...'}
                      </ReactMarkdown>
                      {message.isStreaming && (
                        <span className="inline-block w-2 h-4 bg-nvidia-500 animate-pulse ml-1" />
                      )}
                    </div>
                  ) : (
                    /* 用户消息：提取文本内容 */
                    <p className="whitespace-pre-wrap">
                      {(() => {
                        const content = message.content;
                        if (typeof content === 'string') return content;
                        if (Array.isArray(content)) {
                          const textPart = content.find((c: any) => c.type === 'text');
                          return textPart && typeof textPart === 'object' ? (textPart as any).text || '' : '';
                        }
                        return '';
                      })()}
                    </p>
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
          {/* 图片上传区域 */}
          <ImageUploader
            key={`uploader-${imagesKey}`}
            images={images}
            onImagesChange={handleImagesChange}
            maxImages={9}
            maxFileSize={visionConfig?.maxImageSize || 10 * 1024 * 1024}
            acceptedFormats={visionConfig?.supportedFormats || ['png', 'jpg', 'jpeg', 'gif', 'webp']}
            cameraDimension={visionConfig?.cameraDimension || 768}
          />

          {/* 已选图片预览 - 仅显示小缩略图 */}
          {images.length > 0 && (
            <div key={`preview-${imagesKey}`} className="flex gap-2 overflow-x-auto pb-2">
              {images.map((img, idx) => (
                <div key={idx} className="relative group flex-shrink-0">
                  <img
                    src={img}
                    alt={`待发送图片 ${idx + 1}`}
                    className="w-16 h-16 object-cover rounded-lg border border-gray-300 dark:border-gray-600"
                  />
                  {/* 删除按钮 */}
                  <button
                    onClick={() => setImages(imgs => imgs.filter((_, i) => i !== idx))}
                    className="absolute -top-2 -right-2 w-5 h-5 bg-red-500 text-white rounded-full 
                      flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* 文本输入框 */}
          <div className="flex items-end space-x-3">
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
                disabled={isLoading || connectionStatus === 'error'}
              />
            </div>

            <button
              onClick={handleSend}
              disabled={(!input.trim() && images.length === 0) || isLoading || connectionStatus === 'error'}
              className="flex-shrink-0 w-12 h-12 bg-nvidia-500 text-white rounded-2xl flex items-center justify-center hover:bg-nvidia-600 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {isLoading ? (
                <Loader2 className="w-5 h-5 animate-spin" />
              ) : (
                <Send className="w-5 h-5" />
              )}
            </button>
          </div>
        </div>
      </footer>
    </div>
  );
}
