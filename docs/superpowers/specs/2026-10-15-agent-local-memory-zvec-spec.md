# Agent 本地记忆与 zvec 检索 SPEC

日期：2026-10-15。状态：用户已批准实施；检索核心、配置与模型准备工具已实现，核心 51 项测试及 Python 编译检查通过。已完成部分的跨模块与真实推理验证状态以交付报告为准；2026-10-16 普通 Chat 扩展已实现并通过专项验证，最终合并验证 backend 497 项通过（2 项 deprecation warnings）、frontend 291 项通过；不沿用先前 Agent 验证结论。

2026-10-16 用户确认修订：新请求的模型引用只含记忆正文，不附加路径、文件名或行号；两页用户气泡下方展示本轮实际选中记忆的可折叠面板。既有已接受尾部保留原字节，不迁移历史。

## 1. 目标与范围

用户在专用目录维护 Markdown（`.md`）或纯文本（`.txt`）记忆，扩展名不区分大小写，支持 UTF-8 及配置契约列出的常见编码。每次启动 `scripts/4_start_backend.sh`，后台增量同步新增、修改、删除的文件。Web 的“本地记忆”热开关默认关闭；开启后，每个普通顶层 Agent 或非 Agent Chat 用户请求由后端检索相关片段，追加到该轮 user 内容。无有效匹配时不注入。

适用范围包含 text、vision 两页的普通 Chat 与 Agent 模式，以及 basic、coordinator、manager 等 Agent 入口 profile。手动、语音、模板、定时/循环及普通 Vision Automation 发送共用请求契约。Vision 只用本轮文本检索，图片及其 provenance、历史省略与 acceptance 规则保持原契约。

child dispatch、工具循环、路由内部调用、Answer Now、manual/auto compact 不单独检索。普通 Chat 复用同一检索服务，在新用户消息准入后准备一次，不新增工具或修改原有 system。路由与 handoff 继承已经准备好的本轮内容，不重复查询。文档由用户维护；不新增 Agent 记忆写入工具，不修改现有提示词、文件权限、工具 schema 或 capability。

本功能的上下文变化仅发生在新普通用户轮次的 user 内容尾部。既有 system/runtime/tool 前缀、历史角色、工具轨迹和已发送正文不得改写。运行时开关关闭、源文件修改或删除，均不得删除已经进入历史的记忆片段。

## 2. 调研结论与选型

### 2.1 zvec 的已核实能力

截至调研日，GitHub 最新发布和 PyPI 最新包均为 `0.7.0`：发布说明日期为 2026-08-24，Python wheel 上传于 2026-08-26。PyPI 提供 `cp312-cp312-manylinux_2_28_aarch64`；本机 Python 3.12.3、aarch64、glibc 2.39 与标签条件匹配，仍需实施阶段执行 native import/CRUD smoke。元数据的 `Requires-Python >=3.9` 比官方说明的实际 wheel 范围 3.10–3.14 更宽，部署以实际 wheel 为准。[发布说明](https://github.com/alibaba/zvec/releases/tag/v0.7.0)、[PyPI 文件与元数据](https://pypi.org/project/zvec/0.7.0/)。

zvec 是进程内数据库，支持按 ID insert/upsert/update/delete、标量过滤与持久化；写入结果可立即查询。批量操作返回逐项 Status，不能把“调用没抛异常”当作全部成功。[数据操作](https://zvec.org/en/docs/db/data-operations/)、[删除接口](https://zvec.org/en/docs/db/data-operations/delete/)。

`flush()` 强制落盘；`close()` 刷新并释放文件锁。只读打开持有共享锁，可写打开持有排他锁，因此本设计不假设另一个进程能在可写 owner 存活时读取同一 collection。线程并发能力存在官方测试，但文件版本提交仍由本项目服务锁约束。[Python 的 flush/close 实现](https://github.com/alibaba/zvec/blob/v0.7.0/python/zvec/model/collection.py)、[CollectionImpl::acquire_file_lock](https://github.com/alibaba/zvec/blob/v0.7.0/src/db/collection.cc)、[并发测试](https://github.com/alibaba/zvec/blob/v0.7.0/python/tests/detail/test_collection_concurrency.py)。

特别注意：`COSINE` 的原生 score 为距离，较小更优；`IP` 较大更优。采用 L2 归一化的 FP32 向量和 `MetricType.IP`，对外 `similarity` 就是余弦相似度，按 `similarity >= min_similarity` 筛选，禁止直接混用两种度量的分数。[IsBetterScore 排序实现](https://github.com/alibaba/zvec/blob/v0.7.0/src/db/index/column/vector_column/combined_vector_column_indexer.cc)、[InnerProductMetric::normalize](https://github.com/alibaba/zvec/blob/v0.7.0/src/core/metric/inner_product_metric.cc)。

### 2.2 embedding 与向量管理的分工

zvec 统一承担仓库业务语义向量的存储、索引、过滤和匹配；embedding encoder 只负责将文本编码为向量，不另建 FAISS、Chroma 等索引。SGLang/vLLM 内部 embedding/unembedding 不属于此约束。

选择本地 `intfloat/multilingual-e5-small`：384 维、12 层、最多 512 个输入 token，覆盖中文与英文。使用现有 transformers/torch、masked mean pooling、L2 normalization、CPU FP32；查询使用 `query: `，文档使用 `passage: `，非英语同样保留前缀。固定模型 revision 为 `614241f622f53c4eeff9890bdc4f31cfecc418b3`。[模型与官方用法](https://huggingface.co/intfloat/multilingual-e5-small/blob/614241f622f53c4eeff9890bdc4f31cfecc418b3/README.md)。

该选择避免占用推理服务 GPU，也不依赖远程 embedding API。模型需预先准备为本地完整 artifact；启动和查询使用 local-only 加载，禁止隐式下载。准备过程记录不可变 upstream revision 及 weights/config/tokenizer 等实际文件的 checksum manifest；后台加载前核验，并把实际 artifact digest 纳入 encoder/collection fingerprint。仅向 `from_pretrained(local_path)` 传入 revision 字符串不能证明本地文件属于该 revision。同一路径下文件被替换、checksum 不符或缺少 artifact 时降级，禁止用变更后的 query encoder 查询旧向量。zvec 的默认本地英文 MiniLM、ModelScope 中文替代和云 API wrapper 均不作为本功能的默认 encoder。[zvec embedding 扩展](https://zvec.org/en/docs/db/embedding/)。

实施时将 `zvec==0.7.0` 加入 `requirements/requirements_for_sglang.txt`，使用 uv 管理 `.sglang`。此次只记录要求，不修改 requirements、不安装包、不修改依赖源码；不为使用 zvec 导入其可选云 embedding 依赖。

### 2.3 方案比较

| 方案 | 适配情况与代价 |
|---|---|
| **内嵌 zvec SDK + 仓库自有同步与检索服务（采用）** | 后端单 owner，可精确控制文件版本、阈值、事务发布与 user-tail；无需独立服务。 |
| zvec-grep/CLI | 现成 workspace 搜索，但其扫描、chunk、输出与进程生命周期不适合直接承担本项目的 canonical admission 契约。 |
| 独立检索 daemon | 可隔离进程，但引入 IPC、服务认证、部署与恢复协议；当前规模无需承担这些成本。 |

第一版使用 zvec Flat + IP，不量化，优先保证小型本地记忆的精确匹配与可验证性。索引策略封装在 store adapter 内，调用方不依赖具体索引类型。

## 3. 模块边界与目录

```text
memory/agent-local/                  用户维护的 .md/.txt 源文件
data/agent-local-memory/             生成状态，禁止当作记忆源扫描
  manifest.json                     已提交的文件版本与 collection fingerprint
  journal/                          每文件可恢复的待完成操作
  collections/<fingerprint>/        zvec 数据目录
backend/retrieval/
  contracts.py                      Encoder、VectorStore 的最小协议与结果类型
  embedding.py                      本地 E5 编码；无向量索引职责
  decoding.py                       原始字节到 Unicode；编码校验、BOM 与受限推断
  markdown.py                       结构切分、token 预算、来源行号
  zvec_store.py                     zvec schema/CRUD/过滤/flush
  local_memory.py                   扫描、manifest/journal、检索与尾部准备
```

只保留必要的薄协议，例如 `encode_query/encode_documents`、`search/insert/delete/flush`；不预建多层 factory、provider registry 或额外服务框架。AgentRunner 依赖本地记忆服务接口，不直接操作 zvec。

服务由 FastAPI lifespan 创建和关闭，通过 `app.state` 与 AgentRunner 显式连接。先创建服务，再完成 runner wiring；测试验证真正引用已连接。服务启用时仅允许一个 backend worker 持有该 collection；多 worker 配置必须在启动诊断中明确拒绝该功能，普通 API 可继续。禁止第二个命令行 indexer 与后端同时可写打开同一路径。

所有路径相对项目根解析，禁止 state_dir 等于或位于 source_dir 内。源目录是配置固定的服务读取范围，递归扫描 `.md` 和 `.txt` 普通文件，扩展名不区分大小写（包括 `.MD`、`.TXT`），拒绝越界和符号链接；其他文件格式不转换、不索引。生成路径和父级采用 no-follow、containment 校验及安全原子写入。不改变 Agent 文件工具的既有授权范围。源文本原始字节保持不变；索引和 manifest 是可重建的派生数据，不反向覆盖用户文件。扫描和持久化路径校验必须共用相同扩展名规则，原有 `.md` 文件身份与历史记录不迁移。

## 4. 配置契约

新增 `config/config-local-memory.yaml`，并将 `local_memory` selector 接入 `config/config-overall.yaml` 及 ConfigLoader 的显式子配置加载循环。以下为代码默认值，均需通过配置模型校验；当前用户 YAML 的 `max_chars: 200`、`overlap_chars: 16`、`min_similarity: 0.82`、`timeout_ms: 4000` 保留，以实际服务初始化时加载的值为准：

```yaml
local_memory:
  enabled: true                     # 服务可用性；不是用户热开关
  sync_on_startup: true
  source_dir: memory/agent-local
  state_dir: data/agent-local-memory
  decoding:
    encoding: auto                  # BOM、严格 UTF-8、受支持编码推断；也可显式指定编码
    overrides: {}                   # 相对源目录的精确文件路径到显式编码映射
  embedding:
    model_id: intfloat/multilingual-e5-small
    revision: 614241f622f53c4eeff9890bdc4f31cfecc418b3
    local_model_path: models/embeddings/multilingual-e5-small
    local_files_only: true
    device: cpu
    dimension: 384
    batch_size: 8
  chunking:
    max_chars: 1000                 # Unicode 字符数，便于维护 Markdown
    overlap_chars: 120
    max_tokens: 320                 # tokenizer 安全上限，含 embedding 前缀与 special tokens
  retrieval:
    min_similarity: 0.80
    candidate_k: 16                 # 相似度过滤和种子选择前，zvec 返回的最近向量候选数
    top_k: 4                       # 邻接扩展前的直接命中种子上限
    adjacent_distance: 2           # 同文件同 revision 中前后各扩展的块数；0 禁用扩展
    max_injected_chars: 6000
    timeout_ms: 1500
    max_pending_queries: 8
  indexing:
    max_file_bytes: 2097152
    max_files: 256
  startup:
    gate_timeout_seconds: 30
    searxng_attempt_timeout_seconds: 120
```

`min_similarity` 定义域为 [-1, 1]，top/candidate/batch/queue/file/token 限制必须为合理正值，`candidate_k >= top_k`、`overlap_chars < max_chars`；`adjacent_distance` 为严格非负整数，拒绝布尔值、负数和小数。256 文件、单文件 2MiB 是第一版资源上限的建议值，允许在 YAML 中调节，不能无提示跳过；超过上限记录诊断，且不是“剩余文件不存在”。重要参数不得藏在 shell 或编码器中重复硬编码。

E5 官方说明相似度经常集中在 0.7–1.0；`0.80` 只是待校准起点，阈值无法保证语义正确。使用实际中英文正负例验证误召回率；低于阈值时允许返回零条，禁止为凑 top-k 放宽阈值。[模型 FAQ](https://huggingface.co/intfloat/multilingual-e5-small/blob/614241f622f53c4eeff9890bdc4f31cfecc418b3/README.md)。

Web 用户参数 `enableLocalMemory` 默认 false，保存在现有 `llm-chat-settings`。本配置不覆盖浏览器已有该键；合并仍按键存在性处理 false。encoder、chunk、路径和阈值配置在服务初始化时固定为 runtime snapshot；本版改 YAML 后重启生效，`/config` 必须读取实际 service snapshot，不能展示刚重读但未应用的 YAML 值。

解码配置支持 `auto` 或显式 ASCII、UTF-8/UTF-8-sig、GBK、GB18030、Big5、ISO-8859-1、Shift_JIS、EUC-KR、UTF-16/UTF-16-LE/UTF-16-BE。使用 `codecs.lookup` 规范化受支持别名，拒绝不在白名单内的 codec。`overrides` 必须为字符串到字符串映射；键通过同一 canonical 相对 `.md`/`.txt` 路径校验，大小写与文件路径精确匹配，不支持通配符、绝对路径或 `..`，不读取文件或扩大服务目录；值只能是显式 codec，不能为 `auto`。单文件覆盖优先于全局 encoding，规范化后保留可 deepcopy 的独立字典。

## 5. Markdown/纯文本切分与记录格式

两类文本复用相同的结构与长度切分器：存在 Markdown 结构时优先按标题层级、段落、列表和代码块划分，纯文本按段落和长度处理，过长结构再按 tokenizer 边界细分；大代码块可拆为连续片段并保留来源关系。每块保存相对文件路径、标题层级、原始起止行、chunk ordinal、正文和正文 hash，不把原文摘要替代正文。

先从受限 regular-file 读取原始字节并记录原始 SHA256，再按解码策略转换为 Unicode，最后切分和 embedding。`auto` 优先处理 UTF-8/UTF-16 BOM，再尝试严格 UTF-8，之后使用复用环境的 `charset-normalizer==3.5.1` 在受支持的旧编码候选中推断；不作无条件 Latin-1 兜底。显式 `utf-16` 必须有 BOM，显式 LE/BE 可处理无 BOM 数据。兼容 BOM 移除，冲突、损坏或不受支持的 UTF-32 BOM 直接失败；识别到 BOM 后不得因损坏而改猜其它编码。解码使用 strict，不忽略或制造替换字符；正文已有字面 U+FFFD 及 CRLF 保留。拒绝 NUL 和二进制控制内容，允许正常 tab、CR、LF、form feed。

自动识别无法保证唯一性：短文本、GBK/Big5 等共用合法字节、无 BOM UTF-16 的 ASCII 样式字节可能产生歧义。不得宣称所有无 BOM UTF-16 或旧编码都可可靠识别；使用精确文件 override 明确已知编码。解码失败按单文件错误处理，不发布空版本或乱码替代原文；不改变 prompt/history、编码器模型或已接受记忆引用。

zvec SDK 接收应用生成的文本记录和向量，不负责 Markdown 切分；workspace 解析/切分属于 zvec-grep 的上层能力，本仓库使用自有 splitter。[官方产品职责说明](https://zvec.org/en/docs/)。使用 `.sglang` 已有 transformers tokenizer 和 torch 计算预算与编码，不安装第二套 embedding 框架。`passage: `、可选标题、special tokens 均计入实际最大 512；默认每块最多 1000 个 Unicode 字符、相邻重叠最多 120 字符，并同时满足 320 token 安全上限；仅按字符做一次 overlap，不叠加 token overlap。编码前校验最终长度，不能让 tokenizer 静默截断已经存储的 chunk；超长块重新切分。纯空白文件产生零 chunk。

同样检查查询 token；本版超出 encoder 512 token 上限时跳过检索并记录 `query_too_long`，不得截断发给 Agent 的原始用户消息。vision 纯图片且无文本时跳过检索。

zvec 每条记录包含：

| 字段 | 含义 |
|---|---|
| `id` | `sha256(file_id + revision_id + ordinal + chunk_hash)`；完整确定性 ID |
| `file_id` | 规范相对路径的稳定 hash；重命名按删除+新增处理 |
| `revision_id` | `sha256(file_id + content_hash + fingerprint)`，不同文件或不同版本不共享 revision |
| `source_path/heading/start_line/end_line/ordinal/text` | 来源与可注入正文 |
| `embedding` | 384 维、有限数值、非零且归一化的 FP32 向量 |

`file_id/revision_id` 建标量索引；过滤表达式仅由服务生成的 hex ID 构造，禁止拼接用户文本或路径。字符串 `in (...)` 属于 zvec 支持语法。[标量过滤](https://zvec.org/en/docs/db/data-operations/query/filter/)。

manifest 保存 schema version、collection fingerprint，以及每个文件的相对路径、mtime_ns、size、原始字节 sha256、当前 revision、精确 chunk IDs、indexed_at 和状态。fingerprint 包含 encoder ID/revision、实际 artifact digest、tokenizer/池化/前缀/归一化策略、维度、metric、chunk 算法与参数，以及规范化解码策略、排序后的 overrides、解码算法与探测库版本；时间只用于诊断，不进入向量 ID 或 prompt。准备新的合法 artifact 及 checksum manifest 后，digest 变化按新的 collection generation 处理。解码策略或实现版本变化同样完整重建；等价 codec 别名不重建，只改源文件字节编码则按 raw hash 局部更新。

## 6. 增量同步、一致性与恢复

### 6.1 检测与扫描边界

每次启动比较完整扫描结果与已提交 manifest：新增文件只新增；修改文件只替换该文件记录；已删除文件只精确删除其 chunk IDs。`.md` 与 `.txt` 使用相同增量规则；改名或扩展名变更按旧路径删除、新路径新增处理，文件内容相同也不共享文件身份。记录 mtime_ns 和 size，并计算内容 hash，不能仅比较 `mtime > indexed_at`，否则保留/回退时间戳的修改会漏检。仅 touch 且 hash 相同可更新状态而不重新 embedding。

读取采用打开前后 stat + 字节 hash 的稳定快照；读取中发生变化则有界重试，仍不稳定就跳过该文件，不发布混合版本。一个文件的失败不阻塞其它文件同步。

目录不存在、不可读、遍历中断、超过上限或局部权限错误都不等于所有未见文件已删除：只有确认成功、完整的扫描才允许据“缺席”删除。空 `.md`/`.txt` 是有效新版本，发布空 chunk 集后清除旧 chunks。真实删除先处理，不依赖 encoder 初始化或模型 artifact 是否存在。

第一版无运行期 watcher。修改在下次启动索引；检索先按 committed revisions 及已知隔离版本做 zvec 预过滤，再对候选中不同来源文件进行 stat/hash 核验。新发现变化、删除、越界或不可读来源时排除其 revision，并在同一 deadline 内有界补查至候选预算；不逐请求 hash 全语料。最终只交付已验证的 committed revisions，组装后、冻结前再 stat 检查，变化则舍弃对应整块。验证不足宁可跳过；仅 mtime 相等不能取代 hash。核验只决定本轮资格，不改写历史；冻结后发生的源变化也不撤销已接受内容。

### 6.2 每文件发布协议

不假设 zvec 提供跨多文档事务。manifest 是可见版本的唯一权威，journal 记录待完成操作。任何 native open 或恢复写入之前，须验证 manifest/journal 完整严格 schema、派生 hex IDs、相对来源路径、metadata 与 journal filename；journal 创建/清理也独立拒绝非 hex leaf，损坏状态不允许触发越界写入或删除。所有文件写入使用安全临时文件、fsync、同目录 atomic replace，并 fsync 父目录。

1. 获得稳定文件快照，切分并在提交锁外完成该文件全部 embedding；失败不改已提交 manifest。
2. 持久化 PREPARED journal，记录旧版本、新版本、完整 chunk IDs 与操作类型。
3. 插入新版本的独立 ID，逐项验证 Status；按 zvec SDK 单次写入上限拆分大文件的多批 rows，全部批次成功后 `flush()`。单批写入上限不是单文件块数上限，不能让超过 1024 个块的合法文件因整批提交失败。任何批次失败都不发布完整新文件记录，清理 staged chunks 或保留 journal 供恢复；恢复、删除和 ID 核验仍覆盖完整文件，不能只处理首批。
4. 在短 service 提交锁内原子发布新 manifest，再切换内存中的 active revisions。查询也持该锁取得版本一致的 zvec 结果。
5. 精确删除旧版本 IDs 并 `flush()`，完成后移除 journal。失败可稍后重试；旧版本已被 active revision 过滤，不会重新可见。

删除采用相同协议：持久化删除 journal → 发布 manifest 移除/tombstone → 精确删除旧 IDs → flush → 清理 journal。不先销毁旧数据再发布新版本。所有变更均限定目标 file_id，不全库重算；失败文件不发布部分新版本，旧文件若内容已变则由来源核验排除。

查询在 zvec **预过滤**中指定 committed、来源有效的 revision 集；空集合直接返回零条。禁止仅取全库 top-k 后过滤 staged/obsolete rows，以免它们挤掉有效结果。启动恢复完成前不宣布 ready。

### 6.3 崩溃恢复与 fingerprint

首次创建 collection 前先持久化空 manifest 基线，确保首代构建中断可以自动重建；已经存在 collection 却丢失 manifest 仍属于损坏，不能假定为空库。恢复读取 manifest 与 journal：未发布的新版本清理 staged IDs；已发布的新版本保留并清理旧 IDs；已发布删除继续清理。根据 manifest 检查 active chunk IDs 完整性；缺失则隔离该文件并重新同步，不能当作完整版本检索。必要时用 zvec `iter_docs()` 核验未引用记录，不从向量内容猜源文件状态。

manifest 丢失/损坏不得触发源文件删除或把库误标 ready。记录 degraded，保留数据供诊断，在明确的恢复流程中新建派生库。fingerprint 改变需要新的 collection generation，完整构建并校验成功后再原子切换 manifest；不能混用不同向量空间。该情形及明确的损坏恢复是允许全量重建的边界，普通文件增删改不是。

`max_chars`、`overlap_chars`、`max_tokens` 均已包含在 fingerprint。改动任一参数，在下次 backend 启动且 `sync_on_startup=true` 时重新切分并编码全部源文件；新 manifest、文件 revisions 和 chunk IDs 随新 generation 更新。全部扫描和编码成功前保留旧 manifest 的持久化权威；新 generation 不完整则服务 unavailable，不查询旧配置向量冒充新配置结果。改变切分长度影响块边界、块数及向量值，编码器输出仍为固定 384 维；token 上限可能使某次 max_chars 改动未实际改变部分块，仍按统一 generation 重建。`adjacent_distance` 等检索参数不影响已存储块与向量，不进入 fingerprint；重启应用新检索值即可，无需重新编码。本版不监听运行期 YAML 修改。

## 7. 启动顺序与防阻塞

保留现有“启动 uvicorn → `/health` 可用”的主路径；lifespan 只初始化轻量配置与服务对象，不同步加载模型或执行扫描。后台同步独立于 Web 热开关，默认关闭检索也要同步源文件。

`scripts/4_start_backend.sh` 在项目控制的私有 `mktemp` 运行目录创建 mode 0700 的启动 gate 所在目录，通过 `LOCAL_MEMORY_STARTUP_GATE` 和独立的 `LOCAL_MEMORY_STARTUP_NONCE` 将路径与预期 nonce 传给 backend。健康检查之后，脚本先派发受监督的 SearXNG 启动例程，再原子写入含本次 nonce 的 gate；外部/禁用等跳过分支同样释放。**“派发启动尝试”是前置顺序，“SearXNG healthy”不是向量化条件。**

Docker info、compose/pull、health 各操作及整个例程都有有限预算；后台运行后，Docker 永久卡住也不能阻止 gate 释放和记忆任务启动。SearXNG 成败单独报告。脚本 cleanup 用显式 ownership receipt 记录本次创建的资源及监督进程组，不能依赖 subshell 修改父 shell 变量；退出时回收本次子进程和本次拥有的服务，保留外部已有服务。cleanup 的 docker down/stop 也必须限时，不能在退出阶段重新引入无限等待。

backend 后台任务异步等待本次 gate，核验私有目录、nonce 和内容，拒绝旧 launch 信号。收到 gate 后执行恢复、删除同步、encoder 初始化及新增/修改同步。没有 gate 的直接 uvicorn 启动在 lifespan 就绪后调度同步，不推断 SearXNG 状态；已声明 gate 但超过配置期限仍缺失则标为 degraded，普通 Agent 继续，不能无期限等待或误用旧信号。

扫描、文件 hash、encoder 和 zvec 均不得阻塞 event loop。使用服务拥有的受限 executor/队列，先保证删除处理与状态可查询，再处理 embedding。shutdown 不在仍运行的 worker 下关闭 collection；终止接收任务、丢弃未开始任务并在 worker 完成安全点后 flush/close。

## 8. 检索与 user-tail 契约

### 8.1 一次准备、全链复用

新增请求 scope 内部不可变 `PreparedUserTurn`：包含 `raw_user_text`、`model_user_content`、skip 原因、私有 `sources`，以及追加在 sources 字段后的 `memory_texts: tuple[str, ...] = ()`；这些内部字段不从前端 bubble、timeline、恢复投影或 UI transcript 重建。`memory_texts` 只保存通过去重、整块预算与最终验证的实际注入正文，次序与模型引用相同；off、错误、无匹配等跳过结果为空。它只准备文本，不自行创建 TaskStep；TaskStep 仍由正常接受路径提交，Stop 期间的晚到准备结果不得补写。

先完成既有验证、session admission/reservation、canonical bootstrap 和所需 auto-compact，再在首次 coordinator/Agent 推理前准备一次。bootstrap 的 `current_message` 始终传原始 message；只有本轮模型 task/tail 使用准备好的 `model_user_content`。预准入失败不得检索或提交新历史。

每个 admitted logical request 固定一份准备结果，包括失败、超时和无匹配的“原文发送”结果。相同 request 的重连、幂等重试、SGLang/vLLM fallback、空流非流式重试不得再次查询、改变字节或更换 schema。请求 fingerprint 纳入 `enable_local_memory`，相同 ID 不得以不同开关复用。

所有入口、routing direct answer、passthrough、final handoff 与 manager 顶层执行均传播同一准备结果。已有 `_build_coordinator_turn_message()` 的模板和 canonical 语义保持，仅将准备内容传入其 raw_task；native 历史保留实际接受的完整 routing wrapper，不要求整个消息等同裸用户文本。legacy `_commit_direct_answer_to_shared_memory()` 必须使用真正接受的 model 内容；native `_record_native_routing_call()` 同样保留实际尾部，不再补写一条 raw 用户任务。不能让首轮命中、下一轮历史却回退成未注入文本。

进入 canonical TaskStep/路由消息时遵循现有 acceptance barrier：接受后保存精确注入内容，早期拒绝则不提交；后续 Stop 只改变生命周期，不清除已接受尾部。超时/Stop 后的晚到检索结果只能丢弃，worker 不得直接修改 session 或补写历史。已经停止请求的 worker 退出前不得错误释放可复用 run scope。

compact 使用真实 canonical memory，允许按既有规则替换完整记忆；不得为“清理本地记忆”额外改写历史、重取片段、修改 compact 旋钮或 system 提示。新任务仅正常追加，遵守全局 Prefix-Cache Covenant。

### 8.2 匹配与预算

以原始本轮文本编码 query；不加入 assistant 历史、路由状态、记忆正文或 UI 文本做查询扩写。`candidate_k` 定义 zvec 在相似度过滤和种子选择前返回的最近向量候选数，默认 16；按 `similarity >= min_similarity`（代码默认 0.80）筛选直接命中种子，再按 score 降序、相对路径、ordinal、ID 稳定排序，最多选 `top_k` 个种子（默认 4）。top_k 不限制邻接扩展后的总块数。不得因文本行号相交就吞掉不同 ordinal 的独立种子。

对每个种子 ordinal x，以 `adjacent_distance=n`（默认 2）取同文件同一 committed revision 中 `[x-n, x+n]` 与 `[0, N-1]` 的交集，再将全部种子的这些区间取去重并集。这里去重对象为块身份，而非相似文本；不能跨文件或 revision 连邻块。相似度阈值只用于种子，邻块无需独立达到阈值。原有字符 overlap 保留，注入的每块正文与持久化文本完全一致，不拼接删重或裁剪正文。按来源分组，组间按最强种子的稳定相关性次序排列，同组 ordinal 升序。若文件至少有 36 块，种子 `[1,8,9,18,19,33]`、n=2 的结果为 `[0,1,2,3,6,7,8,9,10,11,16,17,18,19,20,21,31,32,33,34,35]`。

邻块必须来自相同 manifest 的精确 chunk ID 序列，读取后验证 file/revision/ordinal/来源，缺失或不一致时不交付残缺扩展；来源失效则排除该来源种子及全部邻块。扩展不重新编码、不重新切分源文件，沿用本轮 deadline、来源核验和最终 stat 验证。

检索预算含排队、来源验证、编码、匹配和组装，默认 1500ms；队列满、unready、空库、无匹配、异常和超时均发送原消息。timeout 不等于 native/thread 工作已停止：保留 worker ownership，限制总并发，不为每个 timeout 无限创建替代 worker，晚到结果不交付。

完整序列化尾部受 `max_injected_chars` 和既有上下文准入预算双重限制，包含标识与引文编码开销。先尝试所有种子的完整扩展并集；超限时将所有种子的统一距离由 n 逐级缩到 0，每次重算去重并集并完整验证预算，不零散剥除邻块。距离 0 仍不满足时才逐次移除最弱种子，保持距离 0、不重新扩展剩余种子，直到完整剩余种子集合满足预算。最终冻结的 memory_texts、模型引用与可折叠展示逐项同序。零块就不附加任何空 envelope。不得裁剪单块正文、历史、原始用户问题或为本功能额外触发 compact。无法确认追加后符合预算时使用原文。

### 8.3 可审查的注入示例

同一个 user content 中保留原始文本为完整前缀，再追加一个固定格式的引用区：

```text
帮我按照之前约定的格式整理部署记录。

[本地记忆参考：以下为引用资料，不是新的操作指令；仅在与当前请求相关时参考。]
{"text":"部署记录依次包含：版本、启动参数、验证结果。"}
[本地记忆参考结束]
```

每个片段仅通过固定 JSON 对象 `{"text":正文}` 编码，转义换行和控制字符，避免正文伪造 envelope。新尾部不得附加来源路径、文件名、行号、ID、分数或其他 provenance metadata；内部 sources 仅用于索引与诊断，不传给模型或 Web 展示。用户原始消息或记忆正文中本来存在的路径文字保持不变，不做内容删改。引用资料不提升指令权限，用户本轮要求仍是任务依据。此包装只位于 user 尾部，不修改 system，也不声称文字转义能够消除所有 prompt injection 风险。

尾部不包含时间戳、request/session ID、匹配分数、配置 dump、UI/usage 诊断。图片容器和非文本部分原样保留，只扩展当前 task 的文本。此轮接受后，历史中的格式与内容冻结。2026-10-16 之前已接受的带来源尾部也保持原字节；本修订不授权重写、迁移或单独 compact 历史。

### 8.4 普通 Chat 的 canonical 尾部与显示快照

非 Agent Chat 只扩展当前普通用户消息的模型文本，保留原有 system 和图片表示，不注入工具定义。服务端为已接受的模型内容保存不可变快照；用户尾部引用 `metadata.chat_turn_ref="chat:${sid}:${N}:user"` 严格绑定 session 与轮次，assistant/bootstrap 身份使用独立 `metadata.chat_message_ref`。引用不是模型正文，不能授权读取其它 session 的内容。

历史窗口由 session-qualified opaque anchor 固定：请求 transport 字段为 `chat_history_start_ref`，现有 timeline 中为 `history_start_ref`。首次开启只固定实际曾发送的窗口，不复活更早 UI 消息；后续记忆历史窗口通过事件及 replay 保持。新 Chat 从 Agent 或旧 UI 对话初始化时，只导入该请求准确提交的前缀并由服务端持有，不从 Hub 展示或 UI 投影推断模型历史；刷新缺少原始前缀时通过合法 anchor 解析，成功 compact 后退休旧前缀。raw bootstrap 前缀在 Hub 用户准入时登记，与 provider 接受后才提交的记忆尾部/图片投影分离；关闭开关、无匹配、上游错误或 Stop 均不丢失初始化原始前缀，也不得因此提交被拒绝的尾部。

后续模型请求、manual compact 与 auto-compact 先在后端解析严格引用与窗口 anchor，恢复已接受的 canonical 内容，再构造正常前缀；禁止以原始 UI 文本覆盖已注入历史，或重新检索旧轮次。关闭开关、删源和恢复页面不撤销快照。compact 使用原有合法替换边界和参数；live 与 replay 的 compact 边界均停在当前用户消息之前，保留当前用户气泡。

显示使用本轮 PreparedUserTurn 的最终 memory_texts 快照，按精确 Chat 用户 ID 绑定用户气泡，覆盖两页 live 与 Session Hub replay。开关保持默认关闭及实际 POST 快照规则，纯图片无文本时跳过。手动 compact 使用当前 chatSessionId，不能借用 Agent context 身份。上游 HTTP 错误通过 typed internal `transport_error` 区分，禁止把 HTTP 400 的合成错误文本误判为模型成功响应并提交记忆快照。

## 9. Web 与 API

两页在现有 thinking 控件附近提供同款“本地记忆”开关，普通 Chat 与 Agent 模式均可操作，文案说明发送时检索本地 `.md`/`.txt` 文本。开启/关闭无需新建 context 或重启 backend，仅作用于之后实际发送的普通消息；运行中的请求不变。切换模式仍保留该偏好，当前请求使用发送时固定的值。

`ChatSettings` 增加 `enableLocalMemory: boolean`，DEFAULT false。统一 request builder 显式映射为后端严格布尔 `enable_local_memory`，API 默认 false；不得靠 object spread 传输或用字符串真值转换。text/vision、manual/voice/template/cycle 的 wire body 均须检查。

浏览器拥有的排队、模板、语音和自动发送在**实际 POST**时读取开关并冻结到该请求，而不是创建队列项时提前冻结。backend 拥有的 Cycle 与现有 thinking 设置一致：`CycleStartRequest` 在启动循环时固定 `enable_local_memory`，后续每条计划消息使用该值并分别执行一次新检索；运行中的 Cycle 不随浏览器开关变化，新 Cycle 读取新值。本版不增加 live-cycle-control 通道。正常恢复/重试保留原请求的冻结值，不能按恢复页面当前设置替换。前端传原始消息、开关及必要的历史 Chat 引用，不自行拼接命中内容或复制模型快照。

用户气泡、Session Hub 的原始用户展示与 `_turn_started_payload` 保留原始 message；模型历史保留注入版本。两页在对应用户气泡的底部附加可折叠“本地记忆”展示区，交互形式参照 Agent 思考组件中的“查看原始工具调用”，由用户点击展开或收起；只展示该轮冻结的 memory_texts，不显示来源路径、文件名或行号，也不成为额外聊天气泡或 tool observation。后端沿用现有 `timeline_event`，以 `kind=local_memory_retrieved`、`chunks:[{text}]`、`session_id`、`turn_index` 绑定本轮展示。UI 不解析模型引用区、不重查文件、不从来源元数据重建正文；此事件只驱动显示，不进入模型历史。

`/config.local_memory` 使用小型对象：`{default_enabled:false, available:boolean, status, indexed_files, indexed_chunks, last_sync, error_code, applied:{min_similarity, top_k, adjacent_distance}}`。`status` 为 disabled、waiting_startup、indexing、ready、degraded；`last_sync` 为时间或 null，`error_code` 为稳定错误类别或 null，available 表示当前能否执行检索。所有值来自实际服务 snapshot。不得返回记忆正文、绝对目录或堆栈；UI 可提示未就绪/降级但不阻断发送。日志单独记录耗时、命中数、skip 原因和文件同步结果，不将其写入模型上下文；新增展示沿用上述 timeline_event，不建立独立 SSE 协议。

## 10. 实施接入位置

以下为当前仓库函数/类型锚点，后续实施前需再次核对，不以本表代替代码阅读：

| 位置 | 必要接入 |
|---|---|
| `scripts/4_start_backend.sh` | 受监督 SearXNG 派发、启动 gate、ownership receipt 与清理 |
| `backend/main.py` lifespan、`/config` | 轻量服务初始化、runner wiring、后台同步、实际 snapshot 诊断与 shutdown |
| `backend/config.py` ConfigLoader.load | local_memory 配置模型及 selector 显式加载 |
| `backend/api/chat.py` 普通 Chat 准入、发送与 compact；Chat canonical 存储 | 当前 user-tail 准备、历史 chat_turn_ref 解析及不可变模型快照；保留 system |
| `backend/api/agent.py` AgentChatRequest、`_reserve_agent_lifecycle`、`_turn_started_payload` | 严格布尔字段、fingerprint、展示原文 |
| `backend/agent/agent_runner.py` run_stream、run_sync | bootstrap/auto-compact 后统一准备及模型入口传播 |
| 同文件 `_commit_direct_answer_to_shared_memory`、`_record_native_routing_call` | legacy/native canonical 内容与首次接受的尾部一致 |
| `backend/cycle/models.py` CycleStartRequest、`backend/cycle/executor.py` `_execute_message` | 固定循环级布尔设置；直接调用 runner 时显式传入；每条普通计划消息独立准备 |
| `frontend/src/lib/message-types.ts` ChatSettings、DEFAULT_SETTINGS | 默认关闭与现有缓存字段 |
| `frontend/src/lib/api.ts` AgentChatRequest、parseCachedChatSettings、mergeServerDefaultsWithCache、显式请求序列化 | 开关缓存与 wire 映射 |
| `frontend/src/lib/cycle-types.ts` CycleStartRequest、两页 startCycle | 显式序列化循环启动设置 |
| `frontend/src/app/page.tsx`、`frontend/src/app/vision/page.tsx` | 一致 UI 与所有普通发送入口 |
| `requirements/requirements_for_sglang.txt` | 后续实施时 pin zvec 依赖 |

## 11. 验收矩阵

| 领域 | 必须证明的行为 |
|---|---|
| 增量同步 | 首次新增；无变化零编码；单文件改动局部替换；删除精确移除；rename；.md/.txt 和大小写扩展名；md→txt 改名独立身份；空文件；touch；mtime 保持/倒退；同名重建；读取中修改；两个同内容文件独立修改/删除，不通过共享 revision 泄漏失效来源；超过 1024 块大文件分批写入、后批失败不发布且可恢复；仅变更源字节编码局部更新，策略变化全量重建且失败原子保留旧 manifest。 |
| 扫描失败 | missing/unreadable 根目录、局部遍历失败、符号链接、越界、超限、解码失败不会批量删库，不发布伪空版本。 |
| 解码 | 所列 codec 显式策略及别名；BOM 自动识别、严格 UTF-8、受限旧编码探测；短文本歧义使用 overrides；相对路径/codec 严格校验；CRLF/字面 U+FFFD 原样；不忽略或替换错误；损坏/冲突 BOM、UTF-32 和二进制控制内容拒绝；编码信息不进入 prompt；raw hash 不改、源字节不写。 |
| 崩溃恢复 | 在 journal、插入、flush、manifest publish、旧 ID 删除、journal 清理前后注入故障；重启仅暴露完整 committed 版本，无新旧重复，恢复幂等。 |
| 真正 zvec | 真实临时 collection 测 CRUD、逐项 Status、预过滤、flush/reopen、IP 分数方向、相同/正交/反向向量、空 active 集、文件锁与正常关闭；不能全部 mock。 |
| embedding/chunk | 中英文 query/passage 前缀、masked mean、归一化、384 维；512 总预算；标题/代码/长行/重叠/来源；无静默 chunk 截断；缺少本地模型不联网；同路径 tokenizer/weights 被替换时 checksum 失败降级，合法新 artifact digest 触发新 generation，不混用旧向量；max_chars 变化全量重建并原子发布 manifest，失败不发布，无变化重启零编码，adjacent_distance 变化不重新编码。 |
| 相关性/预算 | 新尾部中英文仅含 text，无来源路径/文件名/行号元数据；用户原文中的路径保留；memory_texts 与最终预算选中块逐项一致且不随文件编辑变化；阈值仅筛种子、无匹配不注入、top_k 限种子、同文件同 revision 邻接区间边界与并集去重、原文次序及 overlap 保留；统一半径逐级缩减后才移除最弱种子；字符和 context 预算、长查询跳过、队列满/timeout/晚到结果。 |
| 普通 Chat 扩展 | text/vision、stream/sync 与两 provider；新 user-tail text-only、历史引用解析、开关关闭后旧模型快照不变、manual/auto compact、精确用户气泡 live/Hub replay、session 隔离及纯图片跳过；另覆盖首次开启窗口、bootstrap 精确前缀、刷新 anchor、compact 退休前缀/保留当前用户，以及 HTTP 400 不提交快照；专项及最终合并验证已通过；具体范围和限制见实施计划。 |
| 生产 Agent 路径 | public stream/sync，basic/coordinator direct/passthrough/manager/handoff，legacy/native，SGLang/vLLM；同一 logical request 仅检索一次且后续历史字节稳定。 |
| 生命周期 | 预准入拒绝不污染历史；duplicate request 与开关 fingerprint；Stop 前后；provider/空流 fallback；关闭开关/删源后历史不变；compact 使用真实注入 history。 |
| 两页与发送 | text/vision 的默认关闭、缓存 false/true、yaml 合并；manual/voice/template/queue 实际 POST 快照；Cycle 启动固定设置、运行中浏览器切换无影响、每条消息新检索；图片-only；Vision Automation 与 Computer Use 图片规则独立。 |
| 启动脚本 | 运行真实脚本的受控 stub：Docker info/compose/health 无限挂起、失败、外部服务、禁用分支；均先派发 SearXNG 处理再启动索引，/health 不等待；缺失/旧 gate 有界降级；退出不误清外部服务。 |
| wiring/资源 | lifespan 真正连接 runner；多 worker/collection lock 冲突受控降级；event loop 可响应；worker 未停不 close；重复启动、关停及 journal 恢复不泄漏任务。 |

实施阶段按改动运行 `.sglang/bin/python -m pytest`、Python 编译检查和 frontend type-check；原生 ARM64 smoke 必须使用目标 `.sglang`。核心已运行 51 项测试，覆盖真实 zvec、增量与崩溃恢复等行为；完整矩阵仍按实际集成结果记录，不能将平台标签匹配或核心测试视为全部运行验收。

## 12. 待实测条件与审阅重点

1. 在 Jetson Thor 的现有 `.sglang` 中验证 zvec 0.7.0 wheel 导入、动态链接、真实持久化及多线程行为；不得因此替换现有 Jetson torch 或修改依赖源码。
2. 准备固定 revision 的本地 encoder artifact，验证 CPU 延迟、内存与 batch_size；1500ms 是检索等待预算，不是尚未测量的性能承诺。
3. 用户本人可用实际私人记忆建立中英文相关/无关查询集，校准阈值与 chunk 参数；阈值以上仍可能误召回。开发代理与自动化验证只使用临时目录中的合成语料和独立索引，禁止读取、列出或遍历仓库实际 `memory/agent-local/` 及真实索引目录。
4. 确认默认目录、CPU encoder 和“只在启动同步、运行时变化先抑制陈旧片段”的使用方式符合预期。

用户已授权按本文实施并使用子 agent；后续计划与检查记录仅列必要任务和验收结果，不记录逐 coding step 的阶段小结。
