# DSPARK / Speculative Decoding 跟踪备忘录

> 创建日期：2026-09-09  
> 最后复核日期：**2026-10-21**（上游已发布至 v0.5.22）  
> 用途：跟踪 SGLang 官方对 DSPARK 正确性问题的修复进度，并为后续升级、复现和验收保留基线。  
> 当前决策：Agent 与 tool-calling 等正确性敏感场景使用 **no-DSPARK** 配置。  
> 复核结论：**#35150 仍未修复**，修复 PR 链全部未合入，v0.5.18–v0.5.22 中没有任何针对“DSpark 与 Base decode 逐 token 一致”的修复或回归测试。

## 1. 当前项目基线

- 本地 SGLang 版本：`0.5.17`。
- 上游 release 复核基线（2026-10-21）：`v0.5.17`(08-08) → `v0.5.18`(08-22) → `v0.5.19`(09-05) → `v0.5.20`(09-18) → `v0.5.21`(10-02) → **`v0.5.22`(10-19，最新)**。本项目本地版本未随之升级，因此上游结论仅用于决策，不代表本地已具备对应能力。
- 截至 2026-09-11，当前总配置 `config/config-overall.yaml` 只选择：

  ```yaml
  infer: config/qwen3.6-NVIDIA-NVFP4-config-infer.yaml
  ```

- 模型配置已经内嵌在 infer YAML 的 `model` 段，不再由 `config-overall.yaml` 单列 model 文件。当前 active infer 配置中的 `speculative_algorithm` 和 `speculative_draft_model_path` 均为 `null`，因此当前 active 运行配置是 **no-DSPARK**。
- Qwen3.8 文件目前是保留的对比/实验配置，不是 `config/config-overall.yaml` 当前选中的 active model/infer：
  - no-DSPARK 对比配置：`config/qwen3.8-radixArk-no-DSPARK-NVFP4-config-infer.yaml`
  - DSPARK 实验配置：`config/qwen3.8-radixArk-NVFP4-config-infer copy.yaml`
  - 旧独立 model 配置已移入：`config/depricated-archive/qwen3.8-radixArk-NVFP4-config-model.yaml`
- Qwen3.8 DSPARK 实验配置的关键值包括：

  ```yaml
  mamba_ssm_dtype: bfloat16
  speculative_algorithm: DSPARK
  speculative_dspark_block_size: 2
  ```

这些值只描述当前仓库中未激活的实验配置，不代表它已经满足无损解码要求，也不能据此判断当前正在服务的模型是 Qwen3.8。

## 2. 现场观察与证据边界

现场曾观察到：启用 DSPARK/speculative model 后，context 超过约 10k 时，模型更容易输出非项目协议的 XML 风格工具调用，例如：

```xml
<tool_call>
<function=read_file>
<parameter=file_path>/path/to/file</parameter>
</function>
</tool_call>
```

而在不启用 speculative model 的运行中，暂未高频观察到同类现象。

必须保留以下证据边界：

- 这是有价值的相关性观察，但仅凭输出格式异常，不能证明每一次异常都由 DSPARK 导致。
- 工具协议解析器把“正文在前、工具块在后”误当成 final answer，是独立的后端解析边界问题，应由后端 fail-closed 修复；不能用 speculative decoding 解释或掩盖。
- 上游已经存在与 Qwen3.8 DSPARK 解码分叉直接相关的报告，因此在官方修复并通过本项目复现验收前，不应把 DSPARK 用于要求 token-level 正确性的工具调用。

当前本地缓解边界：XML/HTML-like 输出（包括 `<think>`、`<tool_call>`、`<function=...>`、`<parameter=...>`）只作为自然文本，不执行工具，也不触发工具协议错误。唯一可执行的文本工具协议是双尖括号 frame：独立物理行 `<<TOOL_CALL>>`，frame 内一个或多个严格 `{"name":"工具名","arguments":{...}}` JSON objects，最后由 `<<END_TOOL_CALL>>` 行关闭。旧三尖括号协议同样不执行。此项只能阻止错误格式被误执行，不能修复 DSPARK 输出本身；no-DSPARK 决策保持不变。

2026-10-21 复核补充：上游“speculative 与 base greedy 输出不一致”这一类问题不仅仍未关闭，还在新版本上持续被复现（见 §4.1 与 §4.4）。这提高了本项目 XML 风格工具输出与 DSPARK 存在关联的可信度，但仍然不构成“每一次异常都由 DSPARK 导致”的证明；上段的 fail-closed 兜底必须继续保留，不能因为上游进展而放松。

## 3. 本地 SGLang 0.5.17 的已确认机制

本地源码中可以看到通用 speculative 参数：

- `speculative_accept_threshold_single`
- `speculative_accept_threshold_acc`
- `speculative_use_rejection_sampling`

但在当前 DSPARK 路径中，它们不构成一个可用于“提高主模型放行门槛”的可靠旋钮：

- DSPARK 的 greedy verify 会先对 target logits 做 `argmax`，然后按 draft token 是否与 target 预测 token 连续相等来计算接受长度。
- 当前 DSPARK sampling 调用处把 `threshold_single=1.0`、`threshold_acc=1.0` 和 `deterministic=True` 直接传入。
- DSPARK 参数处理逻辑没有提供一个已经验证可改变上述放行规则、并解决 Qwen3.8 状态漂移的项目级阈值链路。

因此，不应仅因为 SGLang 的通用参数表中存在 accept threshold，就假设它能“加严 DSPARK 的 target release”。在本版本上给项目配置补写这些参数也可能只制造一种已经获得保护的错觉。

更重要的是，上游 Qwen3.8 报告显示：即使 draft token 被强制全部拒绝，target verify 路径仍可能因为 recurrent state 漂移而与普通 Base decode 分叉。这类问题不是提高 draft token 接受阈值就能覆盖的。

本地实现复查位置：

- `.sglang/lib/python3.12/site-packages/sglang/srt/server_args.py`
- `.sglang/lib/python3.12/site-packages/sglang/srt/arg_groups/speculative_hook.py`
- `.sglang/lib/python3.12/site-packages/sglang/kernels/ops/speculative/dspark/dspark_accept.py`
- `.sglang/lib/python3.12/site-packages/sglang/srt/speculative/dflash_utils.py`

这些依赖源码只用于诊断，项目约定禁止未经用户同意直接修改它们。

### 3.1 2026-10-21 上游源码复核（v0.5.22 与 main）

- `dspark_accept.py` 的 `threshold_single=1.0` / `threshold_acc=1.0` / `deterministic=True` 在 **v0.5.22（156-158 行）与 main（158-160 行）中依旧硬编码**，上游从未提供“可调高 target 放行门槛”的 DSpark 参数链路。§3 原结论继续成立。
- GDN target-verify 与 packed decode 的 `beta` 精度语义仍未对齐：`fused_sigmoid_gating_recurrent.py` 在 v0.5.22 与 main 中都还是 `b_beta = 1.0 / (1.0 + tl.exp(-b_b))`。v0.5.22 新增的 `MATCH_CUTEDSL_DECODE` 分支用于 KDA 与 CuTe DSL decode 对齐，**不是** #36014 提出的 target-verify 对齐。
- 上游 main 上没有“spec decode 与 base decode 逐 token 一致”的端到端回归测试：`test/registered/spec/dspark/test_dspark_kernel_parity.py` 只做 triton-vs-torch 的 kernel 对拍，不校验上述不变量。
- v0.5.22 新增的可选 block verification（#42297 / #43012）解决的是**采样分布保真**（block-wise rejection sampling），不是“与 base decode 完全相同”，不能当作 #35150 的修复。

## 4. SGLang 官方跟踪项

### 4.1 #35150：Qwen3.8 forced-reject / GDN state drift（最关键）

- 链接：[sgl-project/sglang#35150](https://github.com/sgl-project/sglang/issues/35150)
- **状态（2026-10-21 复核）：open（未修复）**。2026-08-17 创建，最后一条人工回复为 2026-08-27；无 assignee、无 milestone、无 label。
- 直接相关模型：RadixArk/Qwen3.8-27B-NVFP4 target + Qwen3.8-27B-DSpark draft。
- 报告的核心现象：DSPARK 与 Base decode 可确定性分叉；即使使用 gamma/block size 为 1 的 forced-reject 场景、拒绝每个 draft token 并提交 target argmax，也仍可能发生分叉。
- 报告指向的机制：重复 TARGET_VERIFY 后，GDN recurrent state 逐步漂移；重新走正常 prefill 可以恢复正确 continuation。
- `mamba_ssm_dtype=float32`：报告中只能延迟分叉，没有消除分叉。
- 独立复现（2026-08-27，DGX Spark / GB10，qwen38-27b 专用构建 + DFlash2 overlay）：全部 draft 强制拒绝、width-1 TARGET_VERIFY，在 19/32 冻结用例上与普通 decode 分叉。按“消费的 token”而非轮次对齐后，逐层 trace 显示 request/slot metadata、hidden input、projection、recurrent q/k/v/a/b、A_log、conv output、persistent pre-state 全部 bitwise 相等，**首个差异出现在 selected-step SSM transition 算术本身**，从而排除了 commit/scatter、slot metadata 与 projection。

修复链（三次尝试，全部未落地）：

| PR | 标题 | 结局 |
|---|---|---|
| #35539 | `[Bugfix] Align deterministic DSpark target verify with Triton decode` | 作者自行关闭，改为小范围重写 |
| #35541 | `[Fix] Preserve deterministic DSpark target-verify parity` | 2026-10-11 **关闭但未合并** |
| #36014 | `[Fix] Align GDN target-verify beta semantics with packed decode` | **open**，`mergeable_state=dirty`（与 main 有冲突），2026-09-12 后无更新，0 条 review comment |

即使 #36014 合入也只是**部分修复**：报告者实测它把 batch-8 greedy 错误率从 111/304 降到 37/304，但精确定位用例的首个不匹配点反而从 output index 2 前移到 index 0。上游从未声称该 PR 能消除问题。

结论：这是目前对本项目 Qwen3.8 组合最直接的上游证据，且**在 v0.5.22 上依然未修复**。后续应优先关注 **#36014 是否合入**、以及 main 上是否出现 “spec-vs-base parity” 回归测试，而不是关注某个 release tag。

### 4.2 #34959：DSPARK identifier corruption（已修复，DSV4 专属）

- 链接：[sgl-project/sglang#34959](https://github.com/sgl-project/sglang/issues/34959)
- 报告适用模型：DeepSeek-V4-Flash，**不是 Qwen3.8**。
- **状态（2026-10-21 复核）：closed as completed（2026-08-22）**，由 **#34189「[DSV4] Fix silent KV corruption when speculative draft tokens > 4」**（2026-08-09 合入）修复。这是三个跟踪项中唯一真正落地的修复。
- 复核后更精确的机制：损坏阈值是模型的 **KV 压缩比（`compress_ratios = 4`，c4 层）**，不是单纯的“block size 太大”。实测（4×H200，TP4，temp 0，异构并发 batch）：

  | `--speculative-dspark-block-size` | verify window (γ+1) | 损坏 / grounded identifiers |
  |---|---|---|
  | off | — | 0 / 3506 |
  | 3 | 4 | 0 / 7499（两次独立运行） |
  | 4 | 5 | 4 / 3395 ≈ 0.118% |
  | 6 | 7 | 15 / 2933 ≈ 0.511% |

- 边界：该修复是 DSV4 KV 压缩路径专属，**不能外推为 Qwen3.8 GDN 问题已修复**；但“阈值与模型 KV 压缩比相关”这一结论值得加入本项目的复现矩阵（§6.3）。

### 4.3 #33800：draft depth / block-size 相关输出损坏（未修复，被 bot 关闭）

- 链接：[sgl-project/sglang#33800](https://github.com/sgl-project/sglang/issues/33800)
- 报告适用范围：DeepSeek 相关 DSPARK/SM120 场景，**不是 Qwen3.8 的直接证据**。
- **状态（2026-10-21 复核）：closed（2026-10-08），但由 `github-actions[bot]` 以 “closed due to inactivity” 自动关闭，代码未改动，属于“未修复关闭”。** 按 §6.1 规则不得计入修复。
- 讨论中定位到的两个真实根因（均为 DeepSeek 专属）：
  - SM120：mHC combine 的 einsum 中间张量落在 `use_symmetric_memory` 的 NCCL symmetric pool 内，与在飞 collective buffer 冲突，损坏值直达已提交 token（#34021 只修 SM120 路径）。
  - B300（sm_103a）：write-plan 把 speculative rollback pad 硬编码为 `kMaxMTPDraftTokens = 4`，`num_draft_tokens > 4` 时窗口尾部不会持久化到 compress-state ring，accept-rollback 后读到 stale slot。实测 ndt=4 → 0%、ndt=6 → 41.5%、ndt=7 → 56.5%、ndt=8 → 75%、no-spec → 0%。#34021 在 B300 上是 no-op（TileLang mHC 路径下 torch fallback 不执行）。
- 边界：只能作为 DSPARK 通用风险信号，不能替代 #35150 对 Qwen3.8 的直接复现。注意“block size 越小越安全”也不成立——本案例中 ndt=4 恰好等于硬编码 pad 才干净。

### 4.4 同类未解决问题（2026-10-21 复核，全部 open）

这些 issue 不替换 #35150 的证据地位，但说明“speculative 与 base greedy 不一致”这一类缺陷在最新版本上仍在持续被发现，不能按“个别模型的历史问题”处理。

| Issue | 标题要点 | 与 DSPARK 的关系 |
|---|---|---|
| [#45525](https://github.com/sgl-project/sglang/issues/45525) | DeepSeek-V4-Flash：speculative 与普通 decode 的 greedy 输出不同（2026-10-21 创建，即复核当天） | 同类缺陷仍在最新版本复现 |
| [#36548](https://github.com/sgl-project/sglang/issues/36548) | DFlash2 并发请求下的 state corruption | DSpark 同族（DFlash family）的跨请求状态隔离 |
| [#38009](https://github.com/sgl-project/sglang/issues/38009) | DFlash2 在 thinking 开启时 greedy 输出偏离 target-only **Qwen3.8-27B** | **与本项目模型最接近的旁证**，但属于 DFlash2 而非 DSpark |
| [#39597](https://github.com/sgl-project/sglang/issues/39597) | FlashInfer autotune 开启时 greedy 跨服务重启不可复现（DFLASH） | 说明 deterministic 复现还依赖 autotune/后端选择 |
| [#40144](https://github.com/sgl-project/sglang/issues/40144) | DFLASH draft layout 未校验，anchor-first checkpoint 会静默错位 | draft 权重/layout 的静默错配风险 |
| [#41351](https://github.com/sgl-project/sglang/issues/41351) | 混合 GDN radix-cache 重复分支打分时的 selected-logprob 漂移 | **GDN + 缓存**路径的数值漂移，与 #35150 机制相邻 |

另有一个值得警惕的版本内信号：v0.5.22 窗口内 #45421 刚把 DSpark 的 simulated-acceptance 快路径在 CUDA 上打开，#45433（2026-10-20 合入，晚于 v0.5.22）又把 `SGLANG_DSPARK_SIMULATE_FOLD` / `SGLANG_SIMULATE_ACC_GREEDY` 默认改回 opt-in。说明该区域的正确性边界仍在上游来回调整，近期不宜作为可信基线。

## 5. 当前风险判断与使用建议

1. 对 Agent、工具调用、结构化 JSON、文件名/identifier 等正确性敏感输出，继续使用 no-DSPARK。
2. `speculative_dspark_block_size=1` 只能视为风险缓解实验，不能视为 Qwen3.8 无损保证；#35150 的 forced-reject/gamma=1 结果表明缩到 1 仍不足以证明正确。
3. `mamba_ssm_dtype=float32` 只能视为风险缓解实验；现有 Qwen3.8 上游证据表明它会延迟而非消除分叉。
4. 不把通用 speculative accept threshold 当成 DSPARK 修复开关，除非未来官方明确接通 DSPARK 路径、解释语义，并提供覆盖 Qwen3.8 recurrent-state 问题的测试。
5. 即使未来上游宣称“lossless”，也应在本机 Jetson Thor、当前 target/draft 权重和长上下文工作负载上独立复验。
6. **不把 v0.5.22 高亮的 #43029 当成 #35150 的修复。** 它的触发条件是“结构化/grammar 约束解码 + speculative decoding + prefix cache 复用”导致的 mamba track 边界算错，与普通 greedy TARGET_VERIFY 的累积漂移是两条不同路径。
7. **不把“issue 已关闭”当成修复证据。** #33800 是被 `github-actions[bot]` 以 inactivity 关闭的，代码未变。
8. **不把上游 cookbook 的“已验证”外推到本项目组合。** Qwen3.8 cookbook 的 DSpark preset 指向 `RadixArk/Qwen3.8-2.4T-A95B-DSpark`，且只在 GB300 / B300 的少数 cell 上验证（其中 GB300 DSpark 三格自己标注 Not Verified）；本项目是 27B 权重 + Jetson Thor (SM110a)，不属于任何已验证 cell。

## 6. 每次 SGLang 升级时的检查清单

### 6.1 上游状态

- [ ] 查看 #35150 是否有维护者确认的根因、关联 PR、合并提交和目标 release。
- [ ] **直接检查 #36014 的合并状态**，并核对 `fused_sigmoid_gating_recurrent.py` 中 `beta` 是否已按 packed-decode 的 dtype 语义对齐。这是当前最关键、也最容易验证的落地点。
- [ ] 确认上游 main 是否新增“spec decode 与 base decode 逐 token 一致”的 e2e 回归测试；只有 kernel 级 triton-vs-torch 对拍（`test_dspark_kernel_parity.py`）**不算**。
- [ ] 扫描 §4.4 的同类 open issue，确认是否出现统一根因说明。
- [ ] 查看 #34959、#33800 是否出现共享根因或统一修复说明，但保持模型证据边界。
- [ ] 阅读目标 SGLang release notes 中 DSPARK、GDN/Mamba state、TARGET_VERIFY、forced rejection、ragged verify 相关条目。
- [ ] 不以“issue 已关闭”单独作为修复完成证据；记录关闭原因和实际落入的版本（#33800 即为反面案例）。

### 6.2 实现与参数链路

- [ ] 记录升级后的 SGLang 精确版本、容器/安装来源和构建信息。
- [ ] 复查 DSPARK greedy 与 sampling accept 实现是否发生变化。
- [ ] 复查 `speculative_accept_threshold_single/acc` 是否真正接入 DSPARK；若接入，确认参数方向、默认值和 deterministic 模式的行为。
- [ ] 复查 TARGET_VERIFY 后 GDN/Mamba recurrent state 的提交、回滚、重算和缓存生命周期。
- [ ] 复查 #43029 一类修复（spec mamba track boundary、grammar 截断、prefix cache 复用）是否影响本项目链路；**不要把 grammar 路径的修复当成 greedy verify 的修复**。
- [ ] 确认新增的 DSPARK 开关默认值（例如 `SGLANG_DSPARK_USE_GREEDY_DRAFT`、`SGLANG_DSPARK_SIMULATE_FOLD`、`SGLANG_SIMULATE_ACC_GREEDY`），避免上游把“simulated acceptance”类快路径默认打开后污染正确性测量。
- [ ] 确认项目 infer YAML 到实际 `sglang.launch_server` 参数的生效链路，而不是只看配置文件表面值。

### 6.3 本项目复现矩阵

- [ ] 固定模型权重、prompt、sampling 参数和 seed；保存原始 token IDs，而不只比较肉眼语义。
- [ ] 比较 no-DSPARK 与 DSPARK 的逐 token 输出。
- [ ] 覆盖至少约 8k、10k、16k、32k context，并覆盖多轮连续 TARGET_VERIFY。
- [ ] 覆盖自然语言、长 identifier、路径、严格 JSON、单/多工具调用和 agent hand-off。
- [ ] 在可用时复跑上游 #35150 的 forced-reject/gamma=1 场景，验证“所有 draft 被拒绝”时仍与 Base decode 一致。
- [ ] 分别测试 block size 1 与项目性能候选值；分别测试 `bfloat16` 与 `float32` 的 Mamba/GDN state dtype。
- [ ] 加入“模型 KV 压缩比 / `kMaxMTPDraftTokens` 类硬编码 pad”维度：#34959 与 #33800 都表明损坏阈值由这类常量决定，而不是 block size 的单调关系，扫 block size 时不能只取 1、2 两点。
- [ ] 固定并记录 attention / linear-attention backend 与 FlashInfer autotune 状态，避免把 autotune 差异误判为 DSPARK 分叉（#39597）。
- [ ] 重复运行以排除偶发现象，并保存服务启动参数、请求体、服务日志和首个分叉 token 位置。

## 7. 建议的验收标准

只有同时满足以下条件，才考虑把 DSPARK 重新用于 tool-calling：

1. 官方修复已落入明确的 SGLang release，且能定位到 PR/提交与**针对“spec decode 与 base decode 逐 token 一致”的回归测试**（kernel 级 triton-vs-torch 对拍不算）；仅有 issue 关闭或 release notes 措辞不算。
2. 本项目在相同 prompt 和确定性采样下，DSPARK 与 no-DSPARK 的 token IDs 一致，而不只是最终答案看起来相似。
3. forced-reject/gamma=1 长程测试不再出现 recurrent-state 累积漂移。
4. 10k 以上及更长 context 的工具协议矩阵连续通过，不出现 XML 工具格式、identifier 损坏、JSON 损坏或错误工具名。
5. 多轮测试通过，且停止/重试、compact 后首轮、agent hand-off 等真实链路也通过。
6. 性能收益在上述正确性验收之后再评估；不能用吞吐提升替代正确性证据。

## 8. 升级记录（后续追加）

| 日期 | SGLang 版本/构建 | #35150 状态与关联修复 | 本地复现结果 | 决策 |
|---|---|---|---|---|
| 2026-09-09 | 0.5.17 | 建立跟踪基线 | active 为 `qwen3__6-NVIDIA-NVFP4` model/infer，且 speculation 关闭；Qwen3.8 两套 infer 仅保留作对比/实验 | 保持 no-DSPARK |
| 2026-09-11 | 0.5.17 | 本地配置基线校正 | active 为内嵌 model 段的 `qwen3.6-NVIDIA-NVFP4-config-infer.yaml`，speculation 仍为 null；Qwen3.8 实验文件名与 DSPARK block size 已按仓库现状校正 | 保持 no-DSPARK |
| 2026-10-21 | 上游 v0.5.18–v0.5.22（本地仍 0.5.17） | **#35150 仍 open**。修复链 #35539（作者自关）→ #35541（已关未合）→ #36014（open，`mergeable_state=dirty`，09-12 后无更新）。源码核对：`dspark_accept.py` 仍硬编码 `threshold 1.0/1.0/deterministic`；`fused_sigmoid_gating_recurrent.py` 的 beta 对齐仍未进入 v0.5.22/main；main 无 spec-vs-base parity 测试 | 未在本机复跑；上游无可验收修复 | 保持 no-DSPARK |
| 2026-10-21 | #34959 | **已修复**：由 #34189（08-09 合入）修复，08-22 关闭。阈值 = c4 KV 压缩比（window 4 干净，5/7 递增出错）。DSV4 专属 | 未复验（非本项目 active 模型） | 保持 no-DSPARK |
| 2026-10-21 | #33800 | **未修复关闭**：10-08 被 `github-actions[bot]` 以 inactivity 关闭。讨论给出 SM120 mHC einsum 落入 symmetric pool、B300 `kMaxMTPDraftTokens=4` rollback pad 两个 DeepSeek 专属根因 | 未复验 | 保持 no-DSPARK |
| 2026-10-21 | v0.5.22 高亮修复 #43029 | 只覆盖 grammar/structured output + spec decode + prefix cache 复用下的 mamba track 边界，**不覆盖**普通 greedy TARGET_VERIFY 漂移 | — | 保持 no-DSPARK |

### 8.1 上游逐版本影响评估（v0.5.18 → v0.5.22）

判定标准：是否触及“DSpark 与 Base decode 逐 token 一致”。下表中没有任何一条被标记为 ✅，因此**就 DSPARK 正确性而言，v0.5.18–v0.5.22 都不构成升级理由**。

| 版本 | 日期 | 相关条目 | 是否修复 #35150 |
|---|---|---|---|
| v0.5.18 | 08-22 | DSpark logprobs（#34696/#34478）、GDN verify backend 派发（#34592）、DSpark MegaMoE dp-attn（#34844） | ❌ 功能/性能项 |
| v0.5.19 | 09-05 | #33614 DSpark/DFlash **TP rank 间**状态发散、#36419 DSV4 sample-from-anchor、#36934 重复 draft sample_block、#33431 chunked GDN 跳过 padded state slot | ❌ 均非 GDN target-verify 累积漂移 |
| v0.5.20 | 09-18 | #37165 spec decode 前清理 deferred init metadata、#37818 DFlash Mamba 状态按 checkpoint 边界跟踪、#35544 ReplaySSM checkpoint 摊销 | ❌ |
| v0.5.21 | 10-02 | #39524 fused KDA verify kernel 不再写 conv state、#33722 KDA fused-accept（bit-identical）、#35297 Qwen3.8-MXFP4 DCP 注册 | ❌ |
| v0.5.22 | 10-19 | **#43029** spec mamba track boundary（grammar 触发）、#40001 PP×spec 混合 recurrent-state 提交、#42297/#43012 可选 block verification、#43670 `SGLANG_DSPARK_USE_GREEDY_DRAFT` | ❌ 见 §5 第 6 条 / §3.1 |

> 复核方法：issue/PR 状态取自 GitHub issue timeline 与 PR `mergeable_state`；源码核对直接对比 `v0.5.17` / `v0.5.22` / `main` 的原始文件（`fused_sigmoid_gating_recurrent.py`、`dspark_accept.py`），未修改任何上游依赖源码。
