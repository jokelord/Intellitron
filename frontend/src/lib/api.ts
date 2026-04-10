/**
 * LLM Client API Library
 *
 * 提供与后端 LLM 服务的通信接口，支持：
 * - OpenAI 兼容的聊天 API
 * - Agent 工具调用（SSE 流式）
 * - 图片上传和处理
 * - Cycle 可编程序列执行
 */

// ============================================================
// 类型定义
// ============================================================

/**
 * 聊天消息
 */
export interface ChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string | Array<{ type: string; text?: string; image_url?: { url: string } }>;
  reasoning_content?: string;
}

/**
 * Agent 执行步骤（思考、工具调用、观察结果）
 */
export interface AgentStep {
  step_type: 'thought' | 'tool_call' | 'observation';
  content?: string;
  name?: string;
  input?: string | Record<string, unknown>;
  partial?: boolean;
}

/**
 * Agent 对话请求体
 */
export interface AgentChatRequest {
  session_id?: string;
  message: string;
  images?: string[];
  mode?: string;
}

/**
 * 配置响应
 */
export interface ConfigResponse {
  model?: {
    model_name: string;
    model_type: 'text' | 'vision';
    tensor_parallel_size?: number;
    max_context_length?: number;
  };
  vision?: {
    enabled: boolean;
    max_image_size: number;
    supported_formats: string;
    max_image_tokens: number;
    max_image_dimension?: number;
    camera_max_dimension?: number;
    capture_dimension?: number;
  };
  inference?: {
    temperature: number;
    top_p: number;
    top_k: number;
    max_tokens: number;
    repetition_penalty: number;
    stream: boolean;
  };
  sglang?: {
    chat_template?: string;
  };
  server?: {
    host: string;
    port: number;
  };
  platform?: {
    current: string;
  };
  agent?: {
    enabled: boolean;
    mode: string;
  };
  cycle?: {
    enabled: boolean;
    max_depth: number;
    max_duration: number;
    show_thumbnails: boolean;
    default_sequence: Array<{ type: string;[key: string]: any }>;
  };
}

/**
 * 模型信息
 */
export interface ModelInfo {
  id: string;
  context_length?: number;
}

/**
 * 图片处理响应
 */
export interface ImageProcessResponse {
  image: string;
  width: number;
  height: number;
  original_size: number;
  processed_size: number;
  format: string;
}

/**
 * 聊天流式选项
 */
export interface ChatStreamOptions {
  temperature?: number;
  top_p?: number;
  top_k?: number;
  repetition_penalty?: number;
  max_tokens?: number;
  request_id?: string;
}

// ============================================================
// API 基础配置
// ============================================================

const getApiBaseUrl = (): string => {
  if (typeof window !== 'undefined') {
    const protocol = window.location.protocol;
    const hostname = window.location.hostname;
    const backendPort = 8000;

    if (hostname === 'localhost' || hostname === '127.0.0.1') {
      return `http://localhost:${backendPort}`;
    }

    return `${protocol}//${hostname}:${backendPort}`;
  }

  return 'http://localhost:8000';
};

const API_BASE_URL = getApiBaseUrl();
const REQUEST_TIMEOUT = 300000;
const AGENT_TIMEOUT = 600000;

// ============================================================
// LLM Client 类
// ============================================================

class LLMClient {
  private baseUrl: string;

  constructor(baseUrl?: string) {
    this.baseUrl = baseUrl || API_BASE_URL;
  }

  async healthCheck(): Promise<any> {
    const response = await fetch(`${this.baseUrl}/health`, {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
    });

    if (!response.ok) throw new Error('Health check failed');
    return response.json();
  }

  async getConfig(): Promise<ConfigResponse> {
    const response = await fetch(`${this.baseUrl}/config`, {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
    });

    if (!response.ok) throw new Error('Failed to fetch config');
    return response.json();
  }

  async listModels(): Promise<ModelInfo[]> {
    const response = await fetch(`${this.baseUrl}/v1/models`, {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
    });

    if (!response.ok) throw new Error('Failed to fetch models');
    const data = await response.json();
    return data.data || [];
  }

  async chatStream(
    messages: ChatMessage[],
    options: ChatStreamOptions,
    onChunk: (content: string, reasoning?: string) => void,
    onComplete: () => void,
    onError: (error: Error) => void,
  ): Promise<void> {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), REQUEST_TIMEOUT);

    try {
      const response = await fetch(`${this.baseUrl}/v1/chat/completions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: 'qwen3-next',
          messages,
          temperature: options.temperature,
          top_p: options.top_p,
          top_k: options.top_k,
          repetition_penalty: options.repetition_penalty,
          max_tokens: options.max_tokens,
          stream: true,
          request_id: options.request_id,
        }),
        signal: controller.signal,
      });

      if (!response.ok) {
        throw new Error(`Chat request failed: ${response.statusText}`);
      }

      if (!response.body) {
        throw new Error('Response body is empty');
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines[lines.length - 1];

        for (let i = 0; i < lines.length - 1; i++) {
          const line = lines[i].trim();
          if (!line || line === ':ping') continue;

          if (line.startsWith('data: ')) {
            const data = line.slice(6);
            if (data === '[DONE]') continue;

            try {
              const json = JSON.parse(data);
              if (json.choices?.[0]?.delta?.content !== undefined || json.choices?.[0]?.delta?.reasoning_content !== undefined) {
                onChunk(
                  json.choices[0].delta.content || '',
                  json.choices[0].delta.reasoning_content,
                );
              }
            } catch (e) {
              console.warn('Failed to parse SSE message:', e);
            }
          }
        }
      }

      onComplete();
    } catch (error) {
      if (error instanceof Error) {
        if (error.name === 'AbortError') {
          onError(new Error('Request timeout or aborted'));
        } else {
          onError(error);
        }
      } else {
        onError(new Error('Unknown error occurred'));
      }
    } finally {
      clearTimeout(timeoutId);
    }
  }

  async agentChatStream(
    request: AgentChatRequest,
    onStep: (step: AgentStep) => void,
    onContent: (content: string) => void,
    onComplete?: (sessionId?: string) => void,
    onError?: (error: Error) => void,
    onSessionStart?: (sessionId: string) => void,
  ): Promise<void> {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), AGENT_TIMEOUT);

    try {
      const response = await fetch(`${this.baseUrl}/v1/agent/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: request.session_id,
          message: request.message,
          images: request.images,
          stream: true,
        }),
        signal: controller.signal,
      });

      if (!response.ok) {
        throw new Error(`Agent request failed: ${response.statusText}`);
      }

      if (!response.body) {
        throw new Error('Response body is empty');
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let sessionId: string | undefined;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines[lines.length - 1];

        for (let i = 0; i < lines.length - 1; i++) {
          const line = lines[i].trim();
          if (!line || line === ':ping') continue;

          if (line.startsWith('data: ')) {
            const data = line.slice(6);
            if (data === '[DONE]') continue;

            try {
              const json = JSON.parse(data);

              if (json.type === 'session_start') {
                sessionId = json.session_id;
                if (onSessionStart) {
                  onSessionStart(sessionId!);
                }
              } else if (json.type === 'agent_step') {
                const step: AgentStep = {
                  step_type: json.step_type,
                  content: json.content,
                  name: json.name,
                  input: json.input,
                  partial: json.partial,
                };
                onStep(step);
              } else if (json.type === 'content') {
                if (json.content) {
                  onContent(json.content);
                }
              } else if (json.type === 'done') {
                if (onComplete) {
                  onComplete(sessionId || json.session_id);
                }
              } else if (json.type === 'error') {
                if (onError) {
                  onError(new Error(json.error || 'Unknown error'));
                }
              }
            } catch (e) {
              console.warn('Failed to parse SSE message:', e);
            }
          }
        }
      }

      if (onComplete) {
        onComplete(sessionId);
      }
    } catch (error) {
      if (onError) {
        if (error instanceof Error) {
          if (error.name === 'AbortError') {
            onError(new Error('Request timeout or aborted'));
          } else {
            onError(error);
          }
        } else {
          onError(new Error('Unknown error occurred'));
        }
      }
    } finally {
      clearTimeout(timeoutId);
    }
  }

  async processImageBinary(file: File, format: string = 'auto'): Promise<ImageProcessResponse> {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('format', format);

    const response = await fetch(`${this.baseUrl}/v1/process-image-binary`, {
      method: 'POST',
      body: formData,
    });

    if (!response.ok) {
      throw new Error(`Image processing failed: ${response.statusText}`);
    }

    return response.json();
  }

  async stopChat(requestId: string): Promise<any> {
    const response = await fetch(`${this.baseUrl}/v1/chat/stop`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_id: requestId }),
    });

    if (!response.ok) throw new Error('Failed to stop chat');
    return response.json();
  }

  async stopAgent(sessionId: string): Promise<any> {
    const response = await fetch(`${this.baseUrl}/v1/agent/stop`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId }),
    });

    if (!response.ok) throw new Error('Failed to stop agent');
    return response.json();
  }

  async stopAllAgents(): Promise<any> {
    const response = await fetch(`${this.baseUrl}/v1/agent/stop-all`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });

    if (!response.ok) throw new Error('Failed to stop all agents');
    return response.json();
  }

  async answerNow(sessionId: string): Promise<any> {
    const response = await fetch(`${this.baseUrl}/v1/agent/answer-now`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId }),
    });

    if (!response.ok) throw new Error('Failed to trigger answer now');
    return response.json();
  }

  async startCycle(
    request: any,
    onEvent: (event: any) => void,
    onComplete: () => void,
    onError: (error: Error) => void,
  ): Promise<AbortController> {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), AGENT_TIMEOUT);

    try {
      const response = await fetch(`${this.baseUrl}/v1/cycle/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
        signal: controller.signal,
      });

      if (!response.ok) {
        throw new Error(`Cycle request failed: ${response.statusText}`);
      }

      if (!response.body) {
        throw new Error('Response body is empty');
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      (async () => {
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines[lines.length - 1];

            for (let i = 0; i < lines.length - 1; i++) {
              const line = lines[i].trim();
              if (!line || line === ':ping') continue;

              if (line.startsWith('data: ')) {
                const data = line.slice(6);
                if (data === '[DONE]') continue;

                try {
                  const json = JSON.parse(data);
                  onEvent(json);
                } catch (e) {
                  console.warn('Failed to parse SSE message:', e);
                }
              }
            }
          }

          clearTimeout(timeoutId);
          onComplete();
        } catch (error) {
          clearTimeout(timeoutId);
          if (error instanceof Error && error.name !== 'AbortError') {
            onError(error);
          }
        }
      })();
    } catch (error) {
      clearTimeout(timeoutId);
      if (error instanceof Error && error.name !== 'AbortError') {
        onError(error);
      }
    }

    return controller;
  }

  async stopCycle(sessionId: string): Promise<any> {
    const response = await fetch(`${this.baseUrl}/v1/cycle/stop`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId }),
    });

    if (!response.ok) throw new Error('Failed to stop cycle');
    return response.json();
  }
}

// ============================================================
// 导出实例和函数
// ============================================================

export const llmClient = new LLMClient();

export async function stopChat(requestId: string): Promise<any> {
  return llmClient.stopChat(requestId);
}

export async function stopAgent(sessionId: string): Promise<any> {
  return llmClient.stopAgent(sessionId);
}

export async function stopAllAgents(): Promise<any> {
  return llmClient.stopAllAgents();
}

export async function answerNow(sessionId: string): Promise<any> {
  return llmClient.answerNow(sessionId);
}

export default llmClient;
