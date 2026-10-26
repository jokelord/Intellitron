# Prefix Cache Stability v2 — Coding Plan

> 配套 SPEC: `2026-06-02-prefix-cache-stability-v2-design.md`
> 本计划只描述**要修改/删除/新增的代码位置**与**验收测试**。不预先写出代码；具体实现按 plan 任务一步步来。

## 0. 目标回顾

把 LLM 实际输入的 prefix（system + 历史 message）做成 session-pinned 字节级稳定。删除 v1 引入但当前**未被 LLM 消费**的 Tier 2/3 抽象字段（`working_memory` / `unresolved_items` / `agent_contract` / `recent_visible_history` / `user_goal` 注入）。保留 `route_receipt` / `handoff_ledger` 作为 Tier 2 审计，**禁止注入 prompt**。拒绝 Phase 2 LLM 摘要 / 蒸馏器。

## 1. 前置审计（落地前必做）

### 1.1 全后端 `grep` 验证"无消费者"

在动手修改前先用 `grep` 双重确认：

```bash
# 验证 state["context_pack"] 全后端仅写入/清理，无消费
grep -nE "state\[.context_pack.\]|state\.get\(..context_pack|context_pack" backend/

# 验证 working_memory / unresolved_items / tool_ledger 写入点与消费点
grep -nE "working_memory|unresolved_items|tool_ledger" backend/
```

**预期结果**：
- `state["context_pack"]`：命中点应**全部**在 `agent_runner.py` 内，且全部是 `write`（`_activate_profile_agent` 写入）或 `pop`（`_strip_transient_state_fields` 清理）。**零消费**。
- `working_memory` / `unresolved_items` / `tool_ledger`：仅有 `_activate_secondary_handoff`（handoff 时 append working_memory）和 `_compile_context_pack`（读取）两处。

如果 grep 出新消费点（说明存在 v1 隐藏路径），**先停止修改**，回报 v2 设计与实际不符，需要更新 SPEC 后再继续。

### 1.2 全 tests `grep` 识别待改写测试

```bash
grep -nE "working_memory|unresolved_items|tool_ledger|recent_visible_history|agent_contract" tests/
```

按 SPEC §10 重写这些测试的 mock 字段与断言。

## 2. 任务分解

任务用 `T-<n>` 编号。每个任务完成后跑 SPEC §10 验收测试基线一次。

### T-1: 删除 `_compile_context_pack` 对 LLM 无关字段的组装

**位置**: `backend/agent/agent_runner.py:2853-2891`

**操作**:
- 删除 `_compile_context_pack` 返回 dict 中的 `working_memory` / `unresolved_items` / `agent_contract` / `recent_visible_history` / `user_goal` 字段。
- **保留** `routing_receipt` / `session_frame`（**仅**用于审计/Tier 2 metadata），`session_frame` 不带 `session_id` 字段。
- 由于 `state["context_pack"]` 在 `_activate_profile_agent` 中写入，**新返回的 dict 应仅作为审计快照**（如果该方法完全没人读，可直接整方法删除；见 T-2 选项 B）。

**验收**: 静态阅读确认返回 dict 只含审计字段。

### T-2: 删除 `state["context_pack"]` 写入路径

**位置**: `backend/agent/agent_runner.py:3430-3449`（`_activate_profile_agent`）与 `agent_runner.py:2901-2907`（`_strip_transient_state_fields`）

**操作（选项 A，保留 `_compile_context_pack` 作为审计）**:
- 在 `_activate_profile_agent` 末尾不再写 `state["context_pack"]`，**但仍调用** `_compile_context_pack` 把结果存到 `session.behavior_records["last_context_pack"]` 作为本次激活的审计快照。
- `_strip_transient_state_fields` 不再需要 `pop("context_pack")`（因为从未写入 `state`），但保留无害——可选保留该行以防未来误回灌。

**操作（选项 B，整方法删除 — 推荐当 T-1 完成且确认无消费方时）**:
- 删除 `_compile_context_pack` 整个方法。
- `_activate_profile_agent` 不再调用它，改为只 `last_context_pack=None` 写入 `session.behavior_records`。
- `_strip_transient_state_fields` 删除 `pop("context_pack")` 那一行。

**决策依据**:
- 选项 B 更彻底，符合"prefix cache 严格 v2 原则"。
- 选项 A 留个审计出口，但需要确保没有新代码会读 `state["context_pack"]`（建议在落地选项 B 之前先 grep 全后端，再决策）。

**默认采用选项 B**。**前提**: 落地前全后端 `grep` 确认无消费者。

**验收**: 任何新代码路径均不会把 `state["context_pack"]` 写入；测试 `test_activate_profile_agent_compiles_context_pack_for_target_agent`（`tests/test_multi_agent_runner.py:366`）和 `test_activate_profile_agent_compiles_context_pack_with_current_turn_user_goal`（同文件 :394）按选项 B 改写：mock `_compile_context_pack` 不再被调用，或者干脆删除测试，改为验证 `state` 不含 `context_pack` 键。

### T-3: 删除 `working_memory` 维护逻辑

**位置**: `backend/agent/agent_runner.py:2636-2644`（`_activate_secondary_handoff`）

**操作**: 删除整段 `if handoff_request.additional_args: working_memory = ... append ...` 块。

**理由**: v2 不再保留 `working_memory` 字段。

**保留**: `route_receipt` 与 `handoff_ledger` 写入块（前者作为本次 handoff 的决策记录，后者作为 handoff 序列追加）。

**验收**: `_activate_secondary_handoff` 不再触碰 `working_memory` 键；`test_activate_secondary_handoff_updates_context_pack_and_handoff_ledger` 中 `additional_args` 路径下不再断言 `state["context_pack"]["working_memory"]` 相关字段。

### T-4: 删除 `_compile_context_pack` 中对 `behavior_records` 的 `working_memory` / `unresolved_items` 读取

**位置**: `backend/agent/agent_runner.py:2886-2890`

**操作**: 若保留该方法，删除读取 `working_memory` / `unresolved_items` / `tool_ledger` 的行；返回 dict 不含这些 key。

**验收**: `test_compile_context_pack_for_coordinator_2_uses_route_receipt_and_unresolved_items`（`tests/test_timeline_projection.py:16`）改写：删去对 `working_memory` / `unresolved_items` / `handoff_ledger` / `tool_ledger` 字段的断言，保留对 `routing_receipt` 字段的断言。

### T-5: 保留 `route_receipt` 与 `handoff_ledger` 作为 Tier 2 审计

**位置**: `backend/agent/agent_runner.py:2627-2634`

**操作**:
- `session.behavior_records["route_receipt"] = route_receipt` **保留**。
- `session.behavior_records["handoff_ledger"] = handoff_ledger` **保留**（append-only）。
- 在 `agent_runner.py` 顶部或新写一个 `docs/superpowers/specs/2026-06-02-prefix-cache-stability-v2-design.md` 引用的"v2 不变量"模块注释，说明这两个字段**严禁**回灌 prompt。

**验收**: 后续 review 时这些字段的写入路径不应被新代码 patch 改成 `state[...]` 注入。

### T-6: 强化 `_build_runtime_context_for_profile` 的 session-pinned 不变量

**位置**: `backend/agent/agent_runner.py:2402-2413`

**操作**:
- 在该方法内加注释，明确写出 v2 原则："session-pinned, 同一 session 内只生成一次；后续 turn 复用 session.runtime_context 的冻结值。"
- 加一处**断言**（debug-only）以防未来被错误重构：
  ```python
  if session is not None and session.runtime_context is not None:
      return session.runtime_context  # 显式早返回，冻结值
  ```
  当前已经走这条路径，**新增注释 + 单元测试覆盖**即可，**不改逻辑**。

**测试**: 在 `tests/test_agent_session_context.py` 内新增一个用例，验证：
- 第一次 `_build_runtime_context_for_profile(profile, session=...)` 后 `session.runtime_context` 非空。
- 第二次调用（用同一个 session）返回**完全相同**的字符串。
- 修改 `session.runtime_context = "x"` 后再次调用，**不会**被覆盖（断言返回 `"x"` 而非新生成的时间字符串）。

### T-7: 强化 `_build_profile_agent` 不引入非确定性

**位置**: `backend/agent/agent_runner.py:2326-2400`

**操作**:
- 阅读确认 `prompt_templates["system_prompt"]` 模板内不含 `{{ timestamp }}` / `{{ turn_id }}` / 任何会随调用变化的占位符。
- 确认 `tools` / `custom_instructions` / `agent_python_executable` / `agent_python_bin_dir` / `prompt_visible_tools` 等变量在同 session 同 profile 下**完全相同**。
- 若发现任何"调用计数器"风格的占位符，**删除**或在 prompt template 中去掉。

**验收**: 静态阅读 + 在 `tests/test_multi_agent_runner.py` 中加用例，验证同一 session 同一 profile 两次 `_build_profile_agent(...)` 返回的 agent 的 `system_prompt` 字节级相同。

### T-8: 强化图片 byte-stable 不变量

**位置**: `backend/agent/agent_runner.py:4343-4469`（`_decode_request_images`） + `backend/agent/agent_runner.py:146-232`（`_pre_encode_images`） + `backend/llm_service.py:434-533`（`_normalize_and_log_images`）

**操作**:
- 静态阅读确认：
  1. `_decode_request_images` 收到未超 `max_image_dimension` 的图片时设置 `_pre_encoded_b64` / `_pre_encoded_mime` 为**原始 bytes**（`agent_runner.py:4436-4451`）。
  2. `_pre_encode_images` 命中 `_pre_encoded_b64` 时**不再重新 JPEG 编码**，直接 pass-through（`local_llm_model.py:193-202`）。
  3. `_normalize_and_log_images` 仅在**真正超 `max_image_dimension`** 时才重新编码，且**同一尺寸输入**走同一编码路径（`llm_service.py:479-522`）。
- 验证所有历史 `image_url` part 均保留为图片且不转换为文本；正常请求在捕获 source 前至多执行一次确定性图片准备，cache-first 使用捕获的 source 且不执行第二次变换。

**验收**: 加测试，验证：
- 同一图片两次 `_decode_request_images(...)` 返回的 `pil_img._pre_encoded_b64` 字节相同。
- 同一 user 消息连续两次 LLM 调用，最终发给 SGLang 的 `messages_data` 中该图片的 `image_url.url` 字节相同。

### T-9: 拒绝 Phase 2 LLM 摘要 / 蒸馏器

**位置**: 文档 + AGENTS.md

**操作**:
- `docs/superpowers/specs/2026-06-02-prefix-cache-stability-v2-design.md` 已在 §3 Non-Goals 与 §5.3 Historical summary rule 明确"v2 不预留该机制"。
- 同步更新 `AGENTS.md` 陷阱 #14"Phase 1 禁止引入每轮 LLM 总结旧历史"为永久原则（v2 起 Phrase 取消，永久禁止）。
- 同步更新 `UNIFIED_MESSAGE_MODEL_MEMO.md` §5 Phase 1 摘要策略：明确"v2 删除 working_memory / unresolved_items 章节"。

**验收**: 静态阅读三份文档无 `working_memory` / `unresolved_items` / "per-turn LLM 摘要" / Phase 2 持久化机制出现。

### T-10: 改写相关测试

**位置**: `tests/test_timeline_projection.py`, `tests/test_multi_agent_runner.py`, `tests/test_agent_session_context.py`, `tests/test_multi_tool_parsing.py`

**操作（逐测试）**:

1. `tests/test_timeline_projection.py:16-49` (`test_compile_context_pack_for_coordinator_2_uses_route_receipt_and_unresolved_items`):
   - 改写为 `test_compile_context_pack_for_coordinator_2_uses_route_receipt`。
   - 删除 `session.behavior_records` 中 `working_memory` / `unresolved_items` / `handoff_ledger` / `tool_ledger` 的赋值。
   - 删除对 `pack["unresolved_items"]` / `pack["working_memory"]` / `pack["handoff_ledger"]` / `pack["tool_ledger"]` 的断言。
   - 保留对 `routing_receipt["decision"]` / `routing_receipt["reason_summary"]` 的断言。

2. `tests/test_timeline_projection.py:52-86` (`test_compile_context_pack_prefers_behavior_route_receipt_when_present`):
   - 同上：删除 `unresolved_items` 赋值与断言；保留 `route_receipt` 路径。

3. `tests/test_timeline_projection.py:89-117` (`test_compile_context_pack_uses_timeout_fallback_event_for_route_receipt`):
   - 不需改写（无 working_memory / unresolved_items 引用）。

4. `tests/test_timeline_projection.py:120-149` / `:152-178`：不需改写（不引用待删字段）。

5. `tests/test_multi_agent_runner.py:366-426`（两个 `_activate_profile_agent_compiles_context_pack` 测试）:
   - **选项 B 路径**：删除这两个测试，或改写为验证 `_activate_profile_agent` **不**写 `state["context_pack"]`。
   - 同时 `_activate_profile_agent` 调用顺序：去掉对 `_compile_context_pack` 的 mock 期望，改为 mock `_build_profile_agent`。

6. `tests/test_multi_agent_runner.py:429-...`（`test_activate_secondary_handoff_updates_context_pack_and_handoff_ledger`）:
   - 保留 handoff_ledger / route_receipt 断言。
   - 删除对 `state["context_pack"]["working_memory"]` / `state["context_pack"]["unresolved_items"]` 的断言。
   - 若选项 B 删除 `_compile_context_pack`：删除对 `state["context_pack"]["routing_receipt"]` 的断言；保留 `session.behavior_records["route_receipt"]` / `["handoff_ledger"]` 断言。

7. `tests/test_multi_agent_runner.py:1203-1218`（context_pack 示例常量）:
   - 该测试块是 context_pack 的示例 fixture，删除 `working_memory` / `unresolved_items` / `tool_ledger` / `recent_visible_history` / `agent_contract` 字段。
   - 保留 `routing_receipt` / `user_goal`（若该测试只是 fixture 演示）。

8. `tests/test_multi_agent_runner.py:2068-2074`（handoff 路径下 context_pack 断言）:
   - 改为 `session.behavior_records["route_receipt"]` / `["handoff_ledger"]` 断言。
   - 或删除（如果 T-2 选项 B 删除了整个 context_pack 链路）。

9. `tests/test_agent_session_context.py`: 增加 T-6 描述的 runtime_context session-pinned 单元测试。

10. `tests/test_multi_tool_parsing.py`: 不需改写（与本计划无关）。

**验收**: 全部上述测试在 v2 改动后**通过**。

### T-11: AGENTS.md 同步

**位置**: `AGENTS.md`

**操作**:
- 在"新的多阶段 coordinator 架构"章节中，**保留** v1 提到的 "Tier 1/2/3 与 Context Pack 概念"，但**澄清**："v2 起，Context Pack 仅作为审计快照存在，不进入 LLM 实际输入。"
- 删除或**改写**现有陷阱 #13 中"正确方向是：结构化摘要 + transcript reference + `working_memory` / `unresolved_items` / `handoff_ledger`"——这条 v2 已被取代。改写为"coordinator_2 transcript 仍以 ActionStep.model_output 追加到尾部；不要把 coordinator_2 的 transcript 抽取成 working_memory / unresolved_items 写回前缀。"
- 删除/改写陷阱 #14 的 "Phase 1 禁止引入每轮 LLM 总结旧历史"——v2 改为"永久禁止"。
- **新增陷阱**："任何把 `state["context_pack"]` 注入 prompt 的尝试都是 v2 禁止的回归"。

**验收**: AGENTS.md 全文 `grep "working_memory"` / `"unresolved_items"` / `"context_pack"` 命中点与 v2 SPEC 一致。

### T-12: UNIFIED_MESSAGE_MODEL_MEMO.md 同步

**位置**: `UNIFIED_MESSAGE_MODEL_MEMO.md`

**操作**:
- 删除 `§2 Storage Tier 2` 中 `unresolved_items` 的完整示例（示例 4）。
- 删除 `§4.1` ~ `§4.4` 中所有 `working_memory` / `unresolved_items` / `tool_ledger` / `recent_visible_history` / `agent_contract` 字段。
- `§2 Storage Tier 2` 改为只展示 `route_receipt` / `handoff_ledger` 两类审计记录。
- `§3 Storage Tier 3` "Context Pack" 改为：v2 起 Context Pack 不再注入 LLM prompt 主体；保留为审计快照。
- `§5 Phase 1 摘要策略说明` 中删除 `working_memory` / `unresolved_items` 的逐项维护描述。

**验收**: 全文 `grep "working_memory" / "unresolved_items" / "tool_ledger"` 零命中（除 SPEC / AGENTS.md 引用处）。

### T-13: 跑 SPEC §10 验收基线

**位置**: 根目录

**操作**:
```bash
# Backend 测试
PYTHONPATH=. ~/Project/LLM-deployment/.sglang/bin/python -m pytest \
  tests/test_multi_agent_runner.py \
  tests/test_agent_session_context.py \
  tests/test_multi_tool_parsing.py \
  tests/test_timeline_projection.py \
  -q

# Frontend 测试
node --test --experimental-strip-types "frontend/src/lib/timeline-projection.test.mts"

# Frontend typing
npm run type-check

# Frontend build
npm run build
```

**附加字节级稳定验收**（手工跑 + 后续自动化）：
- 在 session 1 内连续 2 次 LLM 调用，对比两次的 `[MSG-DUMP]` 日志中：
  - system message 字符串字节相同
  - 第 0 ~ N-2 条 user/assistant/tool message 字节相同（仅最后一条 user 消息因 dynamic tail 增长而变化）
- 跨 turn（第 1 轮 vs 第 2 轮），第 1 轮的所有 message 字符串在第 2 轮前缀部分字节相同。

**验收**: 全部命令通过。

## 3. 任务依赖与顺序

| 任务 | 依赖 | 说明 |
|---|---|---|
| T-1 | — | 数据结构调整 |
| T-2 | T-1 | 删除写入路径 |
| T-3 | — | 删除 working_memory 维护 |
| T-4 | T-1, T-2 | 改写 `_compile_context_pack` |
| T-5 | — | 保留 route_receipt / handoff_ledger |
| T-6 | — | session-pinned 强化（加测试） |
| T-7 | — | 系统 prompt 不引入非确定性 |
| T-8 | — | 图片 byte-stable |
| T-9 | T-1, T-2, T-3, T-4 | 文档同步前置 |
| T-10 | T-1, T-2, T-3, T-4 | 测试改写 |
| T-11 | T-9 | AGENTS.md 同步 |
| T-12 | T-9 | MEMO 同步 |
| T-13 | 全部 | 验收基线 |

**建议落地顺序**: T-1 → T-2 → T-3 → T-4 → T-5 → T-6 → T-7 → T-8 → T-10 → T-11 → T-12 → T-13。

T-9 与 T-11/T-12 在文档上合并执行：T-1~T-4 完成后一次性改文档。

## 4. 落地时的风险（实施期）

### 风险 A: 现有代码有隐藏消费 `state["context_pack"]` 的路径

**缓解**: T-1 前用 `grep` 全后端扫一遍；T-2 落地时先保留 `state["context_pack"]` 写入但加一个"DEPRECATED"日志，commit 后下一轮再彻底删除。

### 风险 B: 测试 mock 改写不完整

**缓解**: T-10 一次性把所有 `test_*` 中对 v1 字段的引用列出；按 SPEC §10 验收。

### 风险 C: `_strip_tool_call_text` 清洗不一致

**缓解**: 在 `_step_stream` 入口处加 `assert`（仅 dev 模式），断言 `memory_step.model_output` 一旦被清洗就保持稳定（即下一次 `_step_stream` 调用看到的就是这个值）。

### 风险 D: 误引入新的"调用计数器"占位符

**缓解**: T-7 增加 prompt template 静态扫描校验（在 CI 测试中 grep `{{ turn` / `{{ timestamp` / `{{ counter`）。

### 风险 E: 用户上传图片的 base64 因 `_normalize_and_log_images` 重新编码而 byte-flicker

**缓解**: T-8 加测试覆盖；如发现 flicker，**回退**到 `_pre_encoded_b64` 优先路径（即强制不在 LLM 入口处重新编码）。

## 5. 验收汇总（SPEC §10 复述）

T-13 完成后，验证 SPEC §10 全部验收项：

- 同一 session 内 system message 字节级稳定 ✓ (T-6, T-7)
- 同一 session 内历史 message 字节级稳定 ✓ (T-7, T-8)
- runtime_prompts.enable_runtime_time_context 关闭后不含 "Current local time" ✓ (T-6 既有行为)
- state["context_pack"] 写入后零消费 ✓ (T-2)
- session.behavior_records 不再写 working_memory / unresolved_items / tool_ledger ✓ (T-3, T-4)
- _compile_context_pack 返回结构不含 LLM 注入字段 ✓ (T-1, T-4)
- 全部测试通过 ✓ (T-10, T-13)

## 6. 落地后的回滚预案

如果 T-13 出现以下情况：

- 测试无法通过且不能短时间修复 → **回滚到 v1 行为**：恢复 `_activate_profile_agent` 中 `state["context_pack"]` 写入；恢复 `_compile_context_pack` 返回全部字段；**保留** working_memory / unresolved_items 的写入；保留现有测试断言。
- 系统 prompt 或动态尾部引入 byte-flicker → 立刻定位 flicker 来源（多数为 `_inject_runtime_context` / 正常请求的非确定性图片准备 / cache-first 对捕获 source 的第二次变换）；修复前**回滚**对应 T-7 / T-8 改动。

回滚时**只回滚代码**，不删除 SPEC 文档。SPEC 文档保留作为后续修复指引。
