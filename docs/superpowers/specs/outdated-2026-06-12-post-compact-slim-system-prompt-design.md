# OUTDATED — Post-Compact Slim System Prompt — 首条 chat 减小 prefill，加速 UX

> **OUTDATED / DO NOT GENERALIZE FOR PREFIX-CACHE WORK (2026-06-14):** This document describes a local first-post-compact UX latency workaround. It deliberately skips runtime context and tool schemas for one call, which changes the normal prompt prefix and can hurt prefix-cache consistency if generalized. Current prefix-cache work must follow `docs/superpowers/specs/2026-06-13-compact-prompt-shape-prefix-cache-prd.md` and `docs/superpowers/plans/2026-06-14-compact-prompt-shape-prefix-cache-plan.md`.

> 版本: v1 (companion to `outdated-2026-06-11-v2-compact-prompt-turn-shape-design.md`)
> 生效范围: `backend/agent/agent_runner.py` · `backend/agent/local_llm_model.py` · `backend/llm_service.py` · `tests/test_compact_post_chat_*.py`
> 配套踩坑记录: `踩坑记录4.md` §5.7

## 1. 背景与本版本要解决的问题

### 1.1 v2 compact 现状（截至 2026-06-12）

v2 compact 解决了"compact 自身 5+ 分钟 stall"问题（prompt 拼法 + language/structure lock + 1 行 backend fix），E2E 测得：

| 阶段 | 耗时 | 备注 |
|---|---|---|
| compact LLM-A | 3 min 23 s | 50k Chinese sci-fi payload，9 chunked batches × 8192 + final |
| LLM-A 输出 | 47 s decode, 3.6k tokens summary | English + 9 sections + XML tags |
| 用户体感 | 3:23 "stuck" | 同步在 `run_stream` 内部 |

### 1.2 用户反馈的"3 min 仍旧不能接受"

用户在 compact 完后的下一条 chat（"我觉得你把小说总结得不错呢"）**仍然 1+ 分钟才有响应**。SGLang 在读完 50k 小说后能立即响应下一条 chat（context 切换到 3-5k 的 envelope + 新消息），但**当前实现没有让 chat LLM-B 走这个快路径**。

### 1.3 当前 LLM-B 慢的真实原因（subagent 诊断）

LLM-B 的 input **不是 3-5k tokens**，而是 **5-10k+ tokens**：

| Component | Tokens | 备注 |
|---|---|---|
| `system_prompt`（含 tool schemas + agent-contract preamble） | 2-5k | session-pinned，**但 SGLang 跨请求不共享 KV** |
| `runtime_context`（时间 / 天气 / session info） | 50-200 | 每条 chat 都注入 |
| `TaskStep(task=envelope)`（preface + summary + suffix） | 2-3k | LLM-A 产出 |
| `coordinator_1` routing call（**额外一次 LLM 调用**） | 5-7k prefill | `agent_runner.py:4748`，timeout 20s |
| new user msg | 10-50 | |

5-10k tokens prefill @ ~3.5k tok/s = 2-3 秒，**但实测 1+ 分钟**——说明隐藏成本来自：
- mamba + attention hybrid 的 prefill 调度不是线性（chunked_prefill_size=4096，5k 切成 2 batch）
- system_prompt 重新 prefill 时**带 tool schemas**（每个 tool JSON 200-500 tokens × N 个 tool）
- `coordinator_1` 是 **2 次 LLM 调用**（routing + handoff to basic_agent）= 双重 prefill

### 1.4 本版本的核心原则

**compact 后的第一条 chat 调用，应该用最简 system_prompt 跳过完整 tool schemas 和 runtime_context。**

理由：
- 用户的"我觉得你把小说总结得不错呢"是 acknowledgement，**99% 不需要 tool calling**
- 跳过 tool schemas + runtime_context，prefill 从 5-10k → 1-2k tokens
- 第二条 chat 起恢复完整 system（如果有需要 tool calling 的真任务）

**这与"compact 后 LLM-B 走新 context window"是同一思路的延伸**——既然 context window 已经重置（50k → envelope + new msg），system_prompt 也应该**首次轻量**，避免重置没做透。

## 2. 设计

### 2.1 状态机

新加 1 个 session flag：`session.first_post_compact_turn: bool`，默认 `False`。

```
[normal run]                          [after compact]                [after first chat]
memory.steps = [...] ─┐                                                   ┌─→ memory.steps = [...]
                     │   _replace_agent_memory                           │
                     └──────────────────────────────────────────────────→│
                                                                          │
                                                                          ├─→ flag = True
                                                                          │
                                                                          ▼
                                                                [coordinator_1 routing + LLM-B]
                                                                          │
                                                                          ▼
                                                                LLM-C 调用入口:
                                                                ContextVar 被消费并清 flag
```

**当前实现校正（2026-06-13）**：`_replace_agent_memory()` 设置 `session.first_post_compact_turn = True`。`run_stream()` 在 coordinator_1 routing 完成后设置 `_slim_next_llm_call` ContextVar，让下一次真正的 LLM-C 调用走 slim。`LocalLLMModel._read_slim_flag()` 只消费 ContextVar；它不会单独因为 `session.first_post_compact_turn=True` 就让 routing 调用 slim。该 ContextVar 必须通过 `contextvars.copy_context().run(...)` 传入 worker thread。

### 2.2 Slim 路径

`LocalLLMModel.generate()` 和 `generate_stream()` 通过 `_read_slim_flag()` 读取 `_slim_next_llm_call` ContextVar，决定是否传 `slim=True` 给 `llm_service.chat()`。Session flag 只是持久状态标记，不是直接触发 slim 的唯一信号。

`local_llm_model._inject_runtime_context()` 和 `llm_service` 的 runtime-context 注入在 `slim=True` 时：
- **不**注入 runtime_context（跳过 line 86-93）
- 保留 system_prompt 原样（line 87-93 默认会合并；slim 路径直接 return messages）

Tool schema 剥离发生在 `LocalLLMModel.generate()` / `generate_stream()` 内部：当 `slim=True` 时不把 `tools_to_call_from` 转成 JSON schema，最终传给 LLM service 的 `tools` 为 `None`。

### 2.3 Hard Contract

- ❌ **不**修改 `_replace_agent_memory` 行为（`session.has_run = True`、archive、envelope 拼法）
- ❌ **不**修改 v2 prompt builder（`build_compact_messages_v2`）
- ❌ **不**修改 SGLang config / chunked_prefill_size / mamba_full_memory_ratio
- ❌ **不**引入 per-turn LLM summarization（trap #14）
- ❌ **不**修改 runtime_context 的 session-pinned 性质（trap #20）
- ❌ **不**给 compact LLM-A 路径加 slim（compact 自己用完整 system，per `main_prompt`）
- ❌ **不**让 slim 模式覆盖 normal chat（仅 first post-compact turn）
- ✅ 只加 1 个 session flag + 1 个 slim 分支（`if first_post_compact_turn`）

### 2.4 边界情况

| 场景 | 行为 |
|---|---|
| 用户在 compact 跑完前发新消息（不常见，但可能） | 走 normal path（flag 还是 True 但 chat 还没开始），第一条 chat 走 slim |
| 用户的首条 post-compact chat 触发 tool calling（罕见） | slim 模式不暴露 tool schema，模型通常会输出纯文本；不要承诺一定显示固定的 "I cannot use tools" 文案。 |
| 用户连续 2 次 `/compact`（首条 chat 没发） | flag 在第二次 `_replace_agent_memory` 末尾重新设 True（幂等） |
| 异常 / 失败 / 用户取消 | flag 仍然在 session 上下次 run_stream 时被消费；下下条 chat 不再 slim |
| 跨 session | flag 是 session-scoped，**不**污染其他 session |

### 2.5 测试

- 单元：`tests/test_post_compact_slim_flag.py`
  - `_replace_agent_memory` 末尾 `first_post_compact_turn = True`
  - `run_stream()` 在 coordinator routing 完成后设置 `_slim_next_llm_call` ContextVar
  - `LocalLLMModel.generate()` / `generate_stream()` 在 ContextVar=True 时传 `slim=True` 到 llm_service 并清除 session flag
  - `llm_service._inject_runtime_context` 在 `slim=True` 时跳过注入
  - LLM-C 调用入口消费 `_slim_next_llm_call` 并清除 `first_post_compact_turn`
- E2E：50k Chinese sci-fi payload + 10-token follow-up，验证：
  - compact LLM-A 3 min（不变）
  - LLM-B < 30 s（**新指标**，从 1+ min 降）
  - LLM-B 输出是合理的纯文本回答（不是 tool error）
  - 第二条 chat（`/clear` 后重新发 "你好"）走 normal path（flag 已清）

## 3. 替代方案对比

### 方案 A：slim system_prompt（**推荐**）
- 优点：prefill 5-10k → 1-2k，时间 30-60s → 5-15s
- 缺点：首条 chat 无 tool calling（acceptable）
- 复杂度：低（1 个 flag + 1 个分支 + 2-3 处小改）

### 方案 B：跳过 coordinator_1，直接 basic_agent
- 优点：省 routing 双重 prefill
- 缺点：失去 routing 能力（用户已选"保留 routing"）
- 复杂度：低

### 方案 C：slim + 跳过 routing（组合）
- 优点：极限优化
- 缺点：与用户选择矛盾（"普通路由"）
- 复杂度：中

**选 A**。

## 4. 实施拆分

参见配套 plan `docs/superpowers/plans/outdated-2026-06-12-post-compact-slim-system-prompt-plan.md`。

预计 4 task，2-3 commit，1 天实施 + 0.5 天 E2E。
