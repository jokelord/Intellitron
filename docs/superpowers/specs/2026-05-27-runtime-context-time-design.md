# Runtime Context Time Injection Design

## Goal

为所有非 `coordinator_1` 的对话路径提供分钟级时间上下文，但不修改 `system prompt` 本体，以避免破坏 prefix cache 命中率。

## Background

当前需求已经从“把时间首行拼进 system prompt”调整为“把时间作为独立 runtime context 传递”。这样可以同时满足：

- 给模型提供本机当前时间参考。
- 避免每次请求都改动 prompt 导致前缀缓存失效。
- `coordinator_1` 保持最小路由 prompt，不注入时间上下文。
- `coordinator_2`、`basic_agent`、以及 text/vision 非 agent 模式仍可使用时间信息。

## Non-Goals

- 不把时间信息重新塞回 `system prompt`。
- 不在 Phase 1 引入 LLM 级别的历史总结。
- 不为 `coordinator_1` 注入时间上下文。

## Product Decision

1. 时间上下文由后端按本机时间生成，粒度为分钟级。
2. 时间上下文以独立 runtime context 的形式传递，而不是前缀化 system prompt。
3. 该 runtime context 对外表现为英文背景信息，仅用于辅助任务，不是任务或目的本身。
4. `coordinator_1` 明确跳过时间上下文注入。
5. 时间上下文功能开关放在 `config/config-misc.yaml`，不是 `config-agent-prompts.yaml`。

## Architecture Overview

### Runtime context shape

建议 runtime context 使用单字段字符串，内容为：

`Current local time: Wednesday, 2026-05-27 14:03`

### Injection points

- 非 agent 模式：前端从 `/config` 读取开关，发送请求时携带 runtime context 元数据；后端在构造最终模型输入时使用它。
- agent 模式：`AgentRunner` 在构建 profile agent 时决定是否为目标 profile 注入 runtime context。
- `coordinator_1`：跳过注入。
- `coordinator_2`、`basic_agent`：接收 runtime context。

### Configuration

时间上下文开关放在 `config/config-misc.yaml`，因为它是跨 profile 的运行时行为开关，而不是 agent prompt 文本的一部分。

## Testing Strategy

- 验证 `config/config-misc.yaml` 能解析时间开关。
- 验证 `/config` 透出该开关给前端。
- 验证 `coordinator_1` 不接收时间上下文。
- 验证 `coordinator_2`、`basic_agent` 接收分钟级 runtime context。
- 验证 `text/vision` 的非 agent 请求不会改写 `systemPrompt`。
