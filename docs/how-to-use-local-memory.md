# Agent 本地记忆使用说明

本地记忆使用 zvec 存储和匹配语义向量，源文件是用户维护的 `.md` 或 `.txt` 文本，扩展名不区分大小写。支持 UTF-8 及下述常见文本编码。Web text/vision 两页普通 Chat 与 Agent 模式共用默认关闭的“本地记忆”开关；开启后只影响之后发送的普通顶层消息，不重写已有历史。普通 Chat 支持已按 2026-10-16 扩展要求实现并通过专项验证。

## 首次准备

本功能复用 `.sglang` 中已有的 transformers、torch、tokenizer；业务向量管理依赖为 `zvec==0.7.0`，编码推断直接依赖为环境已有的 `charset-normalizer==3.5.1`，均列在 `requirements/requirements_for_sglang.txt`。不要为此重新安装 Jetson torch 或另建 sentence-transformers 环境。

需要显式准备一次本地 encoder。项目根目录运行：

```bash
.sglang/bin/python scripts/prepare_local_memory_model.py
```

工具下载固定 revision 的 `intfloat/multilingual-e5-small`，保存到 `config/config-local-memory.yaml` 的 `embedding.local_model_path`，并记录 weights/config/tokenizer 的 SHA256。已有完整且校验通过的 artifact 不重复准备。

如果环境设置了不受当前 HTTP 客户端支持的 SOCKS ALL_PROXY，而 HTTPS_PROXY 已是可用 HTTP 代理，可只在这次准备命令移除 ALL_PROXY：

```bash
env -u ALL_PROXY -u all_proxy .sglang/bin/python scripts/prepare_local_memory_model.py
```

后端启动和用户消息检索均只加载本地文件，不会下载模型。缺少模型、文件校验不符或环境不兼容会显示 degraded，普通 Agent 仍可使用。不能仅修改配置里的 revision 字符串来“确认”未知本地权重；换 encoder 时必须重新准备对应 artifact。

## 编辑与启动

将 `.md` 或 `.txt` 文件放在 `memory/agent-local/`，可以使用子目录；`.MD`、`.TXT` 等大小写形式同样支持。建议每份文档聚焦一类真实偏好、约定或已确认事实，标题简洁清楚，也可放纯文本长文。源目录内所有受支持文本文件都属于检索语料，不要放本功能的说明文档。其他文件格式不自动转换。

支持 ASCII、UTF-8（含 BOM）、GBK、GB18030、Big5、ISO-8859-1、Shift_JIS、EUC-KR、UTF-16 LE/BE。默认 `decoding.encoding: auto` 优先识别 BOM，再尝试严格 UTF-8，之后仅在受支持编码中推断。编码器始终接收解码后的 Unicode 文本，源文件字节保持原样；CRLF、原有字符和正文中的字面 `�` 保留，不用忽略错误或替换字符的方式掩盖解码失败。

自动识别是尽力推断，不能保证区分所有编码，尤其是很短的旧编码文本或不带 BOM 的 UTF-16：同一串字节可能在多个编码下都合法。已知编码时可指定全局策略，或使用精确文件路径覆盖：

```yaml
decoding:
  encoding: auto
  overrides:
    notes/example.txt: gb18030
    notes/another.txt: utf-16-le
```

覆盖路径相对配置的源目录，必须与实际路径大小写完全一致；只允许受支持的 `.md`/`.txt` 相对路径，不支持通配符，也不扩大读取范围。单文件覆盖优先于全局策略；编码别名会规范化。全局显式 `utf-16` 要求 BOM；无 BOM 文件使用 `utf-16-le` 或 `utf-16-be`。兼容的 BOM 会移除，冲突或损坏的 BOM 不回退成另一种编码。检测到二进制控制字符或无法可靠解码时跳过该文件；短文本推断错误时应使用覆盖，不应降低相似度阈值补偿乱码。

```bash
scripts/4_start_backend.sh
```

后端先提供健康服务，脚本派发 SearXNG 启动尝试后开始后台同步；SearXNG 失败或迟迟不健康不阻断记忆索引。首次加载 CPU encoder 与构建索引期间检索会使用原始用户消息，服务就绪后后续消息可检索。直接运行 uvicorn 也会在 lifespan 就绪后后台同步。

同步处理 `.md` 与 `.txt` 的新增、修改和删除，未变化文件不重新编码。内容 hash 可以检测保留/回退 mtime 的修改。将文件从 `.md` 改名为 `.txt` 视为旧路径删除、新路径新增，旧向量精确移除，已有对话中的引用仍保留。生成的 zvec 索引、manifest 和恢复 journal 位于 `data/agent-local-memory/`，不要手动改其中单个文件；源文本始终保留为用户可维护原文。

较大文本可产生超过 1024 个块，写入 zvec 时会按 SDK 单次写入上限拆批；一个文件全部批次成功并落盘后才发布该文件的新 manifest 记录。拆批不会把未完成文件提前变成可检索版本，失败后沿既有 journal 恢复流程处理。

本版不持续监听文件变化。运行期间编辑/删除的源文件会从新检索中排除，重启 backend 后再同步新内容；已经接受进对话历史的引用不会被撤销。2026-10-16 的纯正文引用修订只适用于新请求，旧尾部保持原字节，不静默迁移。测试完全不含旧来源元数据的上下文时，可新建 context；也可按原有方式手动 compact，但本功能不会为此自动修改历史。

## 热开关与发送

开关保存在现有 `llm-chat-settings` 浏览器缓存。关闭时不做本轮检索；开启时由 backend 根据原始消息文本查询，前端不拼接记忆内容。匹配结果作为当前 user 内容的引用尾部，只编码记忆正文，不附加来源路径、文件名、行号或分数。用户原始问题和记忆正文里本来写有的路径保留。用户聊天气泡仍显示原文，其下方的“本地记忆”面板可展开查看本轮实际注入片段，顺序和内容与通过预算筛选的冻结结果一致。

浏览器队列、语音、模板在实际发送时读取当前开关。由 backend 执行的 Cycle 在启动循环时固定该开关，之后每条计划消息分别检索；运行中的循环不随浏览器开关变化，新循环读取新设置。工具步骤、child dispatch、Answer Now 和 compact 不发起额外检索。

普通 Chat 在后端保存每轮实际接受的模型内容，后续发送及 compact 使用该 canonical 快照，不会从展示气泡重新构造已注入历史。首次开启保留实际发送过的历史窗口，不带回更早的 UI 消息；从 Agent 或旧对话转入新 Chat 时，仅初始化本次准确提交的前缀，刷新后通过服务端窗口引用恢复。用户气泡下方的记忆面板绑定精确 Chat 用户轮次，实时和 Hub 恢复采用同一正文快照。手动 compact 使用当前 Chat context，成功后替换旧前缀并保留当前用户气泡。原始初始化前缀独立保存，关闭记忆、无匹配、请求错误或 Stop 不会令它丢失；未被接受的记忆尾部不会写入历史。

Vision 使用本轮文本查询；没有文本的纯图片消息跳过检索。图片处理和历史图片省略遵守既有规则。E5 查询输入最多 512 token（包含 query 前缀和 special tokens），超长时跳过检索，原始用户消息保持完整。

## 参数与诊断

编辑 `config/config-local-memory.yaml` 后重启 backend；运行中的服务不持续监听 YAML。下表列出当前仓库 YAML 值，省略配置时的代码默认值另行注明：

| 配置 | 当前 YAML 值 | 用途 |
|---|---:|---|
| `enabled` | true | 启用后台服务，与 Web 开关分开 |
| `sync_on_startup` | true | 启动时同步 .md/.txt 文本 |
| `decoding.encoding` | auto | 全局解码策略：自动识别或显式受支持编码 |
| `decoding.overrides` | {} | 相对源目录的精确文件路径到显式编码映射 |
| `chunking.max_chars` | 200 | 每块 Unicode 字符数上限；代码默认 1000 |
| `chunking.overlap_chars` | 16 | 相邻片段字符重叠上限；代码默认 120 |
| `chunking.max_tokens` | 320 | embedding 输入安全 token 上限，包含前缀和 special tokens |
| `retrieval.min_similarity` | 0.82 | 直接命中种子的余弦相似度下限，代码默认 0.80 |
| `retrieval.candidate_k` | 16 | zvec 在相似度过滤和种子选择前返回的最近向量候选数 |
| `retrieval.top_k` | 4 | 邻接扩展前的直接命中种子数上限 |
| `retrieval.adjacent_distance` | 2 | 同文件同版本中向种子两侧各扩展的块数；非负整数，0 仅保留种子 |
| `retrieval.max_injected_chars` | 6000 | 整个引用尾部字符上限 |
| `retrieval.timeout_ms` | 4000 | 包含排队、编码、验证和组装的等待预算；代码默认 1500ms |
| `indexing.max_files` | 256 | 扫描文件上限 |
| `indexing.max_file_bytes` | 2097152 | 单个源文件字节上限 |

max_chars 和 max_tokens 同时生效，中文与英文的实际 chunk 长度可能不同。不会以静默截断的向量代表一段更长的存储正文。改变 max_chars 会影响切分边界、块数和向量值，E5 的输出仍为固定 384 维。切分参数、规范化后的解码策略及解码器版本均纳入索引 fingerprint；下次启动检测到变化，会重新切分并编码全部源文件，构建新 collection generation。编码别名变为等价名称不重建；只变更单个文件的原始字节编码而策略不变时，按内容 hash 局部更新该文件。全部成功后才原子发布完整新 manifest；构建失败保留旧 manifest，新配置下检索暂不可用，不混用两代数据。仅改 adjacent_distance、阈值或其他检索参数不重新编码。相同配置再次启动会复用未变化文件。

邻接扩展只沿同一源文件当前 revision 的原文 ordinal（从 0 开始）进行，边界限制在 0 到该文件最后一块；各区间取去重并集，不跨文件。例如命中 `[1,8,9,18,19,33]`、距离为 2 时，若该文件至少有 36 块，扩展结果为 `[0,1,2,3,6,7,8,9,10,11,16,17,18,19,20,21,31,32,33,34,35]`。阈值只筛选直接命中种子，邻块作为上下文一起加入；同一块只注入一次，不改写块正文，也不裁掉切分时已有的字符重叠。不同来源按最强种子的相关性分组，同组按原文顺序排列。

预算会计入整个引用尾部、JSON 编码和模型上下文。超限时先对全部种子统一把扩展距离由 n 逐级降到 0，每次尝试完整的扩展并集；仍不满足才移除最弱种子，保持距离 0，不重新扩展剩余种子。最终气泡下的展示片段逐项对应实际注入内容。不会裁剪原始问题、历史或单块正文，零块就发送原文。

代码默认 `0.80` 是初始阈值，当前 YAML 已设为 `0.82`；建议用实际中英文相关/无关问题调节。E5 的分数经常集中在 0.7–1.0。低于阈值、空库、无匹配、队列满、超时或上下文预算不允许时直接发送原文，不强行凑满种子数。路由模式需要验证后续 profile 的真实提示词与工具 schema 预算；若未来 MCP schema 无法预先确认，则保守跳过本轮记忆注入，普通 Agent 照常执行。

`/config.local_memory` 显示实际服务 snapshot 的 `available/status/indexed_files/indexed_chunks/last_sync/error_code/applied`。状态包括 disabled、waiting_startup、indexing、ready、degraded；不返回记忆正文或绝对路径。具体错误类别和统计在 backend 日志，不注入模型。记忆面板使用独立显示快照，不解析模型提示词或重新读取文件，也不显示内部来源元数据；该显示快照只为新轮次生成，不从旧历史反推补齐。前端展示内容不会再次进入模型消息。

源目录缺失/不可读、扫描不完整或超过资源上限不会被解释为所有文件删除。符号链接、越界路径、解码失败和读取中变化的文件会跳过。多 backend worker 不能共享可写 zvec collection；保持单 worker。

变更 encoder artifact、切分参数或解码策略会建立新的 collection generation；普通文件增删改只局部更新。损坏 manifest/collection 会降级并保留数据供诊断，不自动删除源文件。

自动化开发与验证只使用临时目录中的合成 Markdown/纯文本和独立测试索引，不读取、列出或遍历仓库实际 `memory/agent-local/` 及真实索引目录。需要用私人记忆校准相似度与切分参数时，由用户本人执行和观察结果。
