# Agent 层
## 1. 安装 smolagents（PyPI 安装到 .sglang venv，不直接 import smolagents-main）
追加至 requirements_for_sglang.txt

## 2. 创建 backend/agent/local_llm_model.py
继承 smolagents 的 Model 基类，包装现有 LLMService.chat() 方法（直接内部调用，不走 HTTP 回环）。实现 generate(messages, stop_sequences, tools_to_call_from)，负责 smolagents ChatMessage ↔ LLMService 格式的双向转换。

## 3. 创建 backend/agent/agent_runner.py
AgentSession dataclass + 内存 SessionStore（含 TTL 清理）
AgentRunner.build_agent(session_id, mode) — 按配置选 ToolCallingAgent 或 CodeAgent，注入 /tools中 的工具
AgentRunner.run_stream(session_id, message) — async generator，将 smolagents 步骤转换为 SSE 事件：
|   SSE type    |	触发时机    |
|   agent_step (思维 / 工具调用 / 观察)    |   每个 ReAct 子步骤    |
|   content    |   最终答案文本    |
|   done    |   流结束    |