# 历史工具反馈文本剥离

在 `config/config-overall.yaml` 选择的 `agent_operation` YAML 中配置（仓库默认路径为 `config/config-agent-operation.yaml`）：

```yaml
agent:
  tool_feedback_omission:
    enabled: false
    threshold_chars: 10000
```

默认关闭，保持已有上下文。启用后，下一条普通用户请求被后端成功接纳时，当前顶层 Agent 上下文中已有的超阈值工具反馈会在模型消息中替换为固定占位文本：

`[Tool feedback omitted by configuration; this placeholder is not a summary.]`

当前任务内的新反馈始终完整，到再下一条用户请求才成为候选。适用于 Text/Vision Agent、SGLang/vLLM 和共享记忆的 Agent profiles；不处理普通 Chat、child/sibling 上下文。Stop、Answer Now、compact、profile 切换与单纯读取/序列化均不触发剥离；请求接纳前失败不会改变历史。

`threshold_chars` 必须为正整数，严格大于才剥离。使用 Python 内置 `len(str)` 统计 Unicode 字符（码点），空白和标点也计数；不做英文分词，也不是模型 token 数。已有 `backend/compact/tokenizer.py` 的 `TokenCounter` 统计 token；`backend/compact/source_budget.py` 在 `reliable_position1` 分支限制 compact 总源预算，可能移除其他消息，因而不用于这里的逐工具字数判断。

`qwen35-native` 按每条工具结果独立判断，结构化内容只替换文本并保留图片等非文本部分及 metadata。`legacy_text` 按原有合并观察块判断，不猜测拆分工具输出。显式 error/parser 字段与失败、超时或 skipped 状态的结果保留；legacy 合并块若含这些状态，整块保留。native 共享 dispatch blackboard 若含失败 sibling，也整块保留，即使承载它的最后一次 dispatch 成功。部分工具把操作错误作为普通字符串返回并标记 completed，此类文本仍按字数判断；不通过匹配文本猜测错误状态。工具调用及参数/ID、用户消息、模型输出、图片和 provenance 均保留。

剥离是已提交的模型上下文投影，原始工具反馈仍可用于 UI/timeline，关闭配置不会恢复已剥离文本。模型无法逐字回忆被剥离内容；这会打破替换位置后的旧 prefix cache。实际发生时记录 `tool_feedback_historical_text_omission`。旧 compact 缓存快照同时失效，随后从投影记忆重建，防止完整反馈重新进入上下文；若此时入口 profile 已切换，compact 使用当前 profile 前缀，不再复用旧 profile 快照。compact 控制、fallback 和性能配置不变。

修改 YAML 后重启后端使配置生效。
