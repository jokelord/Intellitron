# Agent 本地记忆 Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. 用户已选择子 agent 执行并授权实施；本计划不要求重复确认。

**Goal:** 默认关闭的 Web 热开关，在普通顶层 Agent 与非 Agent Chat 发送时检索本地 Markdown，并以稳定 user-tail 注入有效片段。

**Architecture:** backend 单 owner 持有 zvec。独立检索模块管理本地 encoder、字符/token 切分、可恢复的文件版本提交；Agent 只依赖冻结的 PreparedUserTurn。两页同步维护，Cycle 固定启动时设置。

**Tech Stack:** Python 3.12、现有 transformers/torch、zvec==0.7.0、FastAPI、React/Next.js。

**Spec:** [SPEC](../specs/2026-10-15-agent-local-memory-zvec-spec.md)

## Global Constraints

- 不执行 Git、stash、worktree/branch，不改依赖源码，不安装第二套 torch/transformers。
- `.sglang` 通过 uv 安装 zvec；embedding 固定 E5 revision、本地 artifact checksum、CPU FP32，无启动网络下载。
- 默认开关 false；仅新 user-tail 变化，保留 system/tool schema、canonical 历史及图片 provenance。
- max_chars=1000、overlap_chars=120、max_tokens=320；IP similarity>=0.80、candidate16/top4、尾部6000字符、查询1500ms。
- 文件版本、journal、manifest 必须可恢复，失效来源不得通过同内容文件的 revision 泄漏。

## Review Focus

- 目录不可读/遍历不完整不能误删全部向量；测试完整扫描判定。
- 同内容不同文件必须隔离 revision；测试单独删除和修改。
- timeout/Stop 后晚到 worker 不提交历史，不提前释放 native owner；测试受阻 encoder。
- 所有 profile/direct answer/Cycle 绕行必须保存实际 accepted tail；测试 public runner/API 路径。
- Docker/cleanup 无限挂起不能阻塞索引启动或 API；运行实际脚本配 stub 验证。

## Task 1: 检索核心与配置（CORE owner）

**Files:** backend/retrieval/{config,contracts,embedding,markdown,zvec_store,local_memory}.py；backend/config.py；config/config-local-memory.yaml；config/config-overall.yaml；requirements/requirements_for_sglang.txt。

**Interfaces:**

- `load_local_memory_config(path, project_root) -> LocalMemoryConfig`：严格验证的 dataclass。
- `LocalMemoryService(config, project_root=...)`，`async start()` 非阻塞调度后台同步，`async shutdown()` 排空拥有的 worker 后 close。
- `async prepare_user_turn(raw_user_text, *, enabled, is_aborted=None, context_budget_chars=None, context_validator=None) -> PreparedUserTurn`。
- `PreparedUserTurn(raw_user_text, model_user_content, status, sources:tuple, memory_texts:tuple[str,...]=())`；`status_snapshot() -> dict`。

- [x] 先写配置、字符/token 切分、同内容文件隔离、增量删除、journal 故障、无联网与 timeout 测试并观察失败。
- [x] 实现固定文件作用域和安全生成写入，逐文件提交、新旧版本预过滤、恢复与来源校验。
- [x] 实现受限 executor/队列、private gate/nonce、不可变准备结果、资源排空。
- [x] 使用真实 zvec 临时库验证 IP score、CRUD、Status、flush/reopen 与 file lock。
- [x] 添加明确的 encoder artifact 准备工具、使用指南和空源目录；禁止后台隐式下载。

## Task 2: Agent/API canonical 接入（Agent owner）

**Files:** backend/main.py、backend/api/agent.py、backend/agent/agent_runner.py、backend/cycle/{models,executor}.py；对应 tests。

**Interfaces:** `AppConfig.local_memory` 输入服务；`runner.local_memory_service`；严格请求 `enable_local_memory=False`；Task1 的 prepare 接口。

- [x] 先写 public stream/sync 与 routing/direct/manager/Cycle 的失败用例。
- [x] lifespan 轻量创建并 wiring；`/config` 用服务实际 snapshot。
- [x] bootstrap/auto-compact 后、首次模型调用前只准备一次；原文给 UI/bootstrap，准备文本给现有 task/routing wrapper。
- [x] legacy/native direct、handoff、retry/fallback 保存相同 accepted 逻辑尾部；拒绝/Stop 不补写晚到内容。
- [x] 请求 fingerprint 纳入开关；内部 tool/child/compact/Answer Now 不检索；Cycle 固定启动设置并每条消息新查询。
- [x] 运行对应 canonical/session/provider/compact 回归，验证关闭开关不重写历史。

## Task 3: Web 与启动脚本（UI/startup owner）

**Files:** frontend/src/lib/{message-types,api,cycle-types}.ts；text/vision page；scripts/4_start_backend.sh；前端与脚本测试。

**Interfaces:** `enableLocalMemory=false` 缓存；显式 wire `enable_local_memory`；CycleStartRequest；gate 路径/nonce 环境变量由 Task1 消费。

- [x] 先写缓存 false、两页请求映射、Cycle 开关及 Docker 无限阻塞 stub 的失败用例。
- [x] 增加同款热开关；普通浏览器发送实际 POST 快照，Cycle 启动快照；恢复保持原请求设置。
- [x] 健康后派发 bounded SearXNG 后台处理再释放 private gate；ownership receipt 支持有限 cleanup。
- [x] 运行 frontend 测试/type-check 和真实启动脚本 stub，验证失败/禁用/外部服务/挂起分支。

## Task 4: 合并验证与交付（root 协调）

- [x] 最小 uv 安装 zvec wheel，保留现有 torch/transformers；目标 ARM64 import 与真实临时库 smoke。
- [x] 运行新增 local-memory、Agent、Cycle、frontend 和 shell 测试；明确记录失败/未验证项。
- [x] 核对全部配置链路、pending worker、fingerprint、prefix 与启动 sequencing，修复跨 owner 接口。
- [x] 静态编译 Python、frontend type-check；审阅最终修改；用户指南注明模型准备命令、目录、阈值及运行限制。
- [x] 报告实际通过的检查与 encoder artifact/真实推理仍需验证的条件，不宣称未运行测试成功。

先前 Agent 范围最终定向验证（不覆盖新增普通 Chat 扩展）：backend 318 项通过（2 项 deprecation warnings），frontend 170 项通过；type-check、全部变更 Python 的 py_compile、启动脚本 bash -n 均通过，独立审阅无未解决 P1/P2。真实本地 CPU E5 + native zvec 临时语料 smoke 通过：命中 1 块、text-only JSON 与冻结 memory_texts 完全一致、无来源文件名、关闭开关发送原文、删除来源后抑制陈旧命中。此轮未新增真实 LLM transport/browser E2E；此前广覆盖旧套件仍有未解决失败，尚未通过基线对比确认是否既存，不代表整个仓库全绿。

## 2026-10-16 已确认需求修订

- [x] 新模型尾部仅编码 `{"text":正文}`；私有来源不进入模型，用户正文内原有路径不删改；旧已接受尾部不迁移。
- [x] `PreparedUserTurn.memory_texts` 追加在 sources 后，保存最终选中整块正文；失败/关闭/无匹配为空，51 项核心测试通过。
- [x] 现有 timeline_event 增加 local_memory_retrieved（chunks:[{text}]、session_id、turn_index），仅驱动独立 UI 显示快照。
- [x] text/vision 用户气泡下方的可折叠记忆面板显示实际快照；验证恢复、去重、Stop 及原文气泡保持。

## 2026-10-16 普通 Chat 扩展

- [x] Chat 保留原 system、不新增工具，仅本轮 user-tail 使用同一冻结检索结果，纯图片跳过。
- [x] 服务端保存已接受模型内容；严格 chat_turn_ref、独立 chat_message_ref 及窗口 anchor 解析先于后续模型/manual/auto compact；首次开启不复活旧 UI，bootstrap 只导入准确提交前缀并在 Hub 准入时独立登记，error/Stop/off/no-match 不丢失原始初始化且不提交拒绝尾部；刷新复用 anchor，compact 退休旧前缀。
- [x] 两页两模式共用默认关闭开关并固定实际 POST 值；精确用户记忆快照和 history_start_ref 覆盖 live/replay；请求携带 chat_history_start_ref，手动 compact 使用当前 chatSessionId，边界排除当前用户。
- [x] typed internal transport_error 区分上游 HTTP 错误，验证 HTTP 400 合成文本不会提交模型快照。
- [x] 新增 Chat/backend/frontend/compact/session 隔离及 anchor 验证并独立记录结果；318/170 的旧证据不冒充本次验证。

普通 Chat 新范围验证：Chat 专项 47 项、owner 定向 128 项、frontend 291 项通过，type-check 和 5 个变更 Python 文件编译通过；独立审阅无未解决 P1/P2。真实 CPU E5 + native zvec + 实际推理 tokenizer 的 Chat API smoke 通过，保持原 system、原始用户气泡及 tools 为空，后续关闭开关仍保留已接受历史尾部。未新增 live LLM transport/browser E2E；最终合并结果单独记录如下，不将多次定向运行相加冒充唯一测试总数。

- [x] 汇总 root 最终合并测试计数与验证限制。

最终新鲜合并验证：backend 497 项通过，2 项 deprecation warnings，66.32s，exit 0；frontend 291 项与 type-check 通过，5 个变更 Python 文件编译通过。真实 CPU E5/zvec/实际 tokenizer 的 Chat API smoke 通过，但 LLM transport 使用 mock，未执行真实浏览器 E2E；上述结果不代表整个仓库所有广覆盖套件全绿。
