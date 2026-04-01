# LLM-Deployment 项目 — Copilot 上下文指南

## 项目架构

本项目是一个完整的 LLM 推理部署系统，运行在 **NVIDIA Jetson Thor** (SM110a) 上，包含三层：

| 层级 | 路径 | 技术栈 | 启动脚本 |
|------|------|--------|----------|
| 推理引擎 | SGLang 服务 | SGLang + Qwen3 系列模型 | `scripts/1_start_sglang.sh` |
| 后端 API | `backend/` | Python + FastAPI + smolagents | `scripts/2_start_backend.sh` |
| 前端 UI | `frontend/` | Next.js 14 + React + Tailwind | `scripts/3_start_frontend.sh` |

## Python Env
Path: ./.sglang
Note: 使用uv管理环境

## 前端两种模式（重要！）

前端有 **两个独立页面**，对应两种交互模式：

### 1. Text 模式 — `frontend/src/app/page.tsx`
- 路径: `/`
- 功能: 纯文本对话 + Agent 模式（可切换）
- 无图片上传能力
- Agent 模式下支持流式思考组件

### 2. Vision 模式 — `frontend/src/app/vision/page.tsx`
- 路径: `/vision`
- 功能: 视觉多模态对话 + Agent 模式（支持图片上传 / 摄像头拍照）
- 上传的图片在后端经 `max_image_dimension` 缩放后传给 VLM
- Agent 模式下同样支持流式思考组件
- **启用条件**: `config/config-model.yaml` 中 `model_type: vision`


## Agent 系统架构

### 核心文件
| 文件 | 职责 |
|------|------|
| `backend/agent/agent_runner.py` | SmartToolCallingAgent、Session 管理、SSE 事件流 |
| `backend/agent/local_llm_model.py` | smolagents Model 适配器，桥接 LLMService |
| `backend/llm_service.py` | SGLang/vLLM 推理调用（同步+流式） |
| `backend/api/agent.py` | Agent API 端点 `/v1/agent/chat` |
| `backend/api/chat.py` | 普通聊天端点 `/v1/chat/completions` |
| `config/config-agent-prompts.yaml` | Agent 提示词模板（系统提示 + 指令） |


### SmartToolCallingAgent 三层处理
1. **纯文本** → 自动包装为 `final_answer`（模型无需显式调用）
2. **final_answer 工具调用** → 静默拦截处理
3. **真实工具调用** → 正常执行工具并返回观察结果

### VLM + Agent 注意事项
- 当 `model_type: vision` 时，agent 系统需要在指令中明确告知模型"你是 VLM，能直接看到图片"
- 否则模型会误判为"没有图片处理工具"，拒绝执行涉及图片的任务
- VLM 指令在 `_build_agent()` 中根据 `config.model.model_type` 动态追加

## 前端组件
| 组件 | 路径 | 功能 |
|------|------|------|
| AgentStepDisplay | `frontend/src/components/AgentStepDisplay.tsx` | 思考/工具/观察卡片渲染 |
| LLMClient | `frontend/src/lib/api.ts` | SSE 流式请求封装 |

## 配置文件
| 文件 | 内容 |
|------|------|
| `config/config-model.yaml` | 模型路径、model_type、图片参数 |
| `config/config-infer.yaml` | 推理参数、SGLang 启动参数、tool_call_parser |
| `config/config-agent-prompts.yaml` | Agent 提示词模板 |
| `config/config-server.yaml` | 后端服务器配置 |

## 常见陷阱
1. **两个页面不同步**: text 和 vision 页面的 Agent 逻辑需要同步维护

2. **VLM 无工具调用**: 缺少 VLM 身份指令时模型不知道自己能看图。检查 `_build_agent()` 中的 VLM 指令追加逻辑

3. **Qwen3-VL 在 SGLang 的 auto + stream 模式下可能返回空增量**: 已实际复现“非流式有 `<tool_call>` 文本，但流式只有空首帧和 stop，没有 content / reasoning_content / tool_calls”的情况。此时前端会看到空思考框，后端会把空字符串收敛成 final answer。修复点在 `backend/agent/local_llm_model.py`，需要对“流式空响应”回退到一次非流式解析。

4. **vision 页面与 text 页面是两套独立前端页面**: [frontend/src/app/page.tsx](frontend/src/app/page.tsx) 对应 text， [frontend/src/app/vision/page.tsx](frontend/src/app/vision/page.tsx) 对应 vision。构建产物、页面逻辑、上传能力不同，排障时不要把两边行为混为一条链路。

5. **Agent 链路与普通聊天链路不同**: Agent 走 `/v1/agent/chat`，普通聊天走 `/v1/chat/completions`。vision/text 页面虽然都能进 agent 模式，但问题要先区分是“前端页面差异”还是“后端 agent/tool calling 差异”。

6. **smolagents `@tool` 装饰器 JSON Schema 生成失败**: 使用自定义 wrapper 包装工具时，`@tool` decorator 在装饰时（decoration time）就需要完整的 docstring。错误信息：`Cannot generate JSON schema for wrapper because the docstring has no description for the argument 'args'`。**关键点**：
   - docstring 必须在 `@tool` 装饰器**内部**（作为 wrapper 函数的文档字符串）
   - 不能用 `wrapper.__doc__ = ...` 在装饰**之后**赋值
   - Args 中的参数名要用 `args` 和 `kwargs`（无 `*` 前缀），不是 `*args` 和 `**kwargs`
   - 参考 `backend/agent/agent_runner.py` 中的 `_make_session_aware_wrapper` 和 `_create_tool_wrapper`
