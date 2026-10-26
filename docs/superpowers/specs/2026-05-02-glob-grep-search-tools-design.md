# Glob / Grep Search Tools Design

> 日期: 2026-05-02
> 状态: 已批准，执行中

## 1. 概述

### 1.1 背景

当前项目的 Agent 已具备文件读写、命令执行、联网搜索等能力，但缺少面向本地代码库与白名单目录的专用搜索工具。LLM 在执行搜索任务时，往往只能：

- 先 `read_file` 再猜路径，效率低
- 借助 `exec_command` 调 `find` / `grep` / `rg`，参数容易不稳定，输出也不适合小模型消费
- 在多目录、多文件模式下缺少统一的白名单边界与结果裁剪机制

参考 `ref_src/tools/GrepTool` 与 `ref_src/tools/GlobTool` 后，可以吸收其核心优点：

- 用 `ripgrep` 提供高性能搜索
- 区分“按文件名找文件”和“按内容找文本”两个工具
- 用清晰的参数 schema 和工具说明，直接教会 LLM 如何使用工具
- 结果可分页、可裁剪，避免上下文爆炸

但本仓库不需要引入 TS Tool framework、权限 matcher、UI 渲染等整套重实现。目标是在 Python 后端中落一个面向本仓库 Agent 的定制化、鲁棒版本。

### 1.2 目标

新增两个真正暴露给本地 LLM 的后端工具：

- `glob`: 按路径模式查找文件
- `grep`: 按正则搜索文件内容

并满足以下要求：

- 只允许在 YAML 配置声明的绝对路径白名单中搜索
- 白名单支持多个根路径，子目录自动继承白名单资格
- 默认白名单包含 `/tmp`，且必须显式写在 `config/config-misc.yaml`
- 参数与返回格式针对本仓库的小参数量本地模型优化，强调“易读、稳健、低歧义”
- 优先使用 `ripgrep`，必要时提供 Python 回退，避免环境差异直接导致工具不可用

### 1.3 非目标

本次不做：

- 不复刻 `ref_src` 的 TS Tool 框架
- 不增加前端专门 UI
- 不把这两个工具隐藏出 schema 或 prompt
- 不支持任意本机路径搜索
- 不把 `glob` / `grep` 做成复合编排工具

## 2. 核心决策

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 工具落点 | `backend/tools/search_ops.py` | 保持后端工具层聚合，最小改动接入 Agent |
| 对外工具名 | `glob` / `grep` | 与参考工具语义一致，LLM 易理解 |
| 搜索边界 | 仅 YAML 白名单 | 明确、安全、符合用户要求 |
| 白名单格式 | `config/config-misc.yaml` 中绝对路径数组 | 便于部署时统一调整，不硬编码到 Python 代码 |
| 主实现 | `ripgrep` 子进程 | 性能与稳定性最佳 |
| 回退实现 | Python 标准库 | `rg` 缺失时仍可完成基本搜索 |
| 返回类型 | 面向 LLM 的结构化纯文本 | 与现有工具体系一致，且更适合本地小模型阅读 |
| 参数复杂度 | 吸收参考实现精华，但裁剪到仓库实际需要 | 避免过重 schema 降低模型成功率 |

## 3. 配置设计

配置统一放在 `config/config-misc.yaml` 的 `agent` 段，新增字段：

```yaml
agent:
  search_whitelist_paths:
    - ~/Project/LLM-deployment
    - /tmp
  search_exclude_globs:
    - .git
    - node_modules
    - .next
    - .sglang
    - __pycache__
  glob_default_limit: 100
  glob_max_limit: 1000
  grep_default_head_limit: 200
  grep_max_head_limit: 1000
  grep_max_columns: 400
```

说明：

- `search_whitelist_paths` 必须是绝对路径
- `path` 参数若为空，则默认搜索全部白名单根路径
- `path` 参数若提供，也必须是绝对路径，且位于某个白名单根路径之下
- `search_exclude_globs` 控制噪声目录/文件的默认排除规则，不写死在工具逻辑中

## 4. 工具接口设计

### 4.1 `glob`

建议签名：

```python
def glob_files(pattern: str, path: str | None = None, limit: int | None = None) -> str:
```

参数说明：

- `pattern`: glob 模式，例如 `**/*.py`、`frontend/src/**/*.tsx`
- `path`: 可选；绝对路径。为空时搜索全部白名单根路径
- `limit`: 可选；返回的最大文件数。为空时使用配置默认值，并受最大上限裁剪

行为：

- 主路径校验：仅允许绝对路径，且必须位于白名单内
- 搜索目录校验：`glob` 的 `path` 必须是目录
- 使用 `rg --files -g <pattern>` 扫描文件列表
- 若 `rg` 不可用，则退回 Python `Path.rglob` / `glob` 方案
- 结果按文件修改时间倒序排序，便于把更可能近期相关的文件排前
- 返回绝对路径，方便 LLM 直接传给 `read_file` / `edit_file`
- 结果过多时截断，并告诉 LLM 如何缩小范围

返回格式示例：

```text
Glob search summary
- pattern: **/*.py
- searched roots: 2
- matched files: 143
- showing: 100

Files:
1. ~/Project/LLM-deployment/backend/config.py
2. ~/Project/LLM-deployment/backend/agent/agent_runner.py
...

Note: results truncated. Narrow `pattern` or pass a narrower absolute `path`.
```

### 4.2 `grep`

建议签名：

```python
def grep_search(
    pattern: str,
    path: str | None = None,
    include: str | None = None,
    output_mode: str = "files_with_matches",
    case_insensitive: bool = False,
    multiline: bool = False,
    context_before: int = 0,
    context_after: int = 0,
    context: int | None = None,
    line_numbers: bool = True,
    head_limit: int | None = None,
    offset: int = 0,
) -> str:
```

参数设计来源于参考实现，但做了本仓库定制：

- 保留 `output_mode` / `include` / `offset` / `head_limit` / `multiline`
- 把 `-A` / `-B` / `-C` 改为更适合 JSON schema 的字段名
- 默认 `output_mode` 使用 `files_with_matches`，更适合第一轮探索

`output_mode` 取值：

- `files_with_matches`: 仅返回命中的文件路径
- `content`: 返回命中的文本内容，可带上下文
- `count`: 返回每个文件的命中次数

行为：

- `path` 为空时搜索全部白名单根路径
- `path` 若提供，可为绝对目录或绝对文件路径，但必须位于白名单内
- `include` 用于进一步限制文件范围，例如 `*.py` 或 `*.{ts,tsx}`
- `content` 模式支持 `context_before` / `context_after` / `context`
- `head_limit` 与 `offset` 提供分页能力，避免一次返回过多结果
- 使用 `--max-columns` 防止 minified/base64 等极长行污染输出
- 默认排除配置中的噪声 glob
- 若 `rg` 不可用，则回退到 Python 文本扫描实现

返回格式示例：

`files_with_matches`:

```text
Grep search summary
- pattern: class\s+AgentRunner
- output_mode: files_with_matches
- searched roots: 1
- matched files: 3
- showing files: 3

Files:
1. ~/Project/LLM-deployment/backend/agent/agent_runner.py
2. ~/Project/LLM-deployment/tests/test_agent_session_context.py
3. ~/Project/LLM-deployment/backend/api/agent.py
```

`content`:

```text
Grep search summary
- pattern: tools_and_managed_agents
- output_mode: content
- searched roots: 1
- showing lines: 18
- offset: 0

Content:
~/Project/LLM-deployment/backend/agent/agent_runner.py:785:    def tools_and_managed_agents(self):
~/Project/LLM-deployment/backend/agent/agent_runner.py:1705:                    tools_to_call_from=self.tools_and_managed_agents,
...
```

`count`:

```text
Grep search summary
- pattern: final_answer
- output_mode: count
- searched roots: 2
- matched files: 4

Counts:
12  ~/Project/LLM-deployment/backend/agent/agent_runner.py
4   ~/Project/LLM-deployment/backend/agent/local_llm_model.py
...
```

## 5. LLM 使用提示策略

这两个工具的 docstring 必须直接教本地 LLM 如何正确使用：

### 5.1 `glob` docstring 要点

- 什么时候用：按文件名、路径模式找文件时
- 何时省略 `path`：需要在全部白名单根路径中搜索时
- 何时填写 `path`：已经知道目标目录时，用绝对路径缩小范围
- 输出结果是绝对路径，可直接给 `read_file` / `edit_file`
- 若结果过多，缩小 `pattern` 或收窄 `path`

### 5.2 `grep` docstring 要点

- 什么时候用：按正则查内容时
- `output_mode` 如何选：先 `files_with_matches`，再按需 `content`
- `include` 如何用：限定文件类型，例如 `*.py`
- `context` / `context_before` / `context_after` 如何用
- 结果过多时用 `offset` 分页，`head_limit` 控制每次返回量
- 路径必须是白名单内绝对路径，若不确定可省略 `path`

此外，`config/config-agent-prompts.yaml` 中也应补一条显式规则：

- 查找文件路径优先使用 `glob`
- 查找文件内容优先使用 `grep`
- 不要为了本地搜索任务调用 `exec_command` 去跑 `find` / `grep` / `rg`

## 6. 关键实现细节

### 6.1 白名单解析与校验

新增统一辅助逻辑：

- 读取 `search_whitelist_paths`
- 规范化为绝对路径
- 去重
- 当 `path` 参数出现时，判断它是否落在任一白名单根路径中
- 若不在白名单中，返回带白名单列表的明确错误信息

判定规则：

```python
candidate == root or root in candidate.parents
```

并使用 `Path.resolve(strict=False)` 规避 `..` 绕过。

### 6.2 `ripgrep` 主实现

优先走 `subprocess.run([...], shell=False)`：

- 防止命令注入
- 便于细粒度构造参数
- 便于区分“无结果”与“执行错误”

`glob` 与 `grep` 都按“每个目标根路径单独执行，再汇总”的方式实现，避免多根路径下输出前缀不一致的问题。

### 6.3 Python 回退

若系统中没有 `rg`：

- `glob`: 使用 `glob` / `pathlib` 扫描
- `grep`: 使用 `os.walk + re` 扫描文本文件

回退实现不追求完全复制 `rg` 的所有边角行为，但必须保证：

- 基本功能可用
- 白名单边界一致
- 分页与结果格式一致

### 6.4 输出裁剪

为适配本地小模型，输出必须避免过量：

- `glob` 用 `limit` 裁剪文件数
- `grep` 用 `head_limit` + `offset` 裁剪
- `content` 模式只裁剪输出行，不做难以理解的二次摘要
- 明确告诉 LLM 当前显示了多少条，以及如何继续分页

## 7. 测试设计

新增测试文件：`tests/test_search_ops.py`

覆盖以下场景：

- `glob` 在白名单目录内能找到文件
- `glob` 越权路径被拒绝
- `glob` 结果截断逻辑正确
- `grep` `files_with_matches` 模式正确
- `grep` `content` 模式正确
- `grep` `count` 模式正确
- `grep` `include` 过滤正确
- `grep` 分页 `offset + head_limit` 正确
- 文件路径 / 目录路径 / 无效路径校验正确
- `_get_tools()` 已注册 `glob` 与 `grep`

优先做纯 Python 回退路径的单元测试，使测试不依赖宿主机必须安装 `rg`；同时主逻辑默认仍优先走 `rg`。

## 8. 变更清单

涉及文件：

- 新增 `backend/tools/search_ops.py`
- 修改 `backend/config.py`
- 修改 `config/config-misc.yaml`
- 修改 `backend/agent/agent_runner.py`
- 修改 `config/config-agent-prompts.yaml`
- 新增 `tests/test_search_ops.py`
- 修改 `tests/test_get_tools_no_load_images.py`

## 9. 风险与应对

### 风险 1: `ripgrep` 在运行环境中不存在

应对：提供 Python 回退实现，并在输出中不暴露内部失败堆栈。

### 风险 2: 白名单配置写错或路径不存在

应对：工具返回明确错误信息，提示当前配置值；测试覆盖空白名单/无效白名单场景。

### 风险 3: 小模型误用参数导致一次返回过多内容

应对：默认 `files_with_matches` + 默认限流 + docstring 教会模型用 `offset` 分页。

### 风险 4: 搜索噪声过多

应对：引入 YAML 可配置 `search_exclude_globs`，默认排除常见噪声目录。

## 10. 最终建议

采用“Python 后端仓库定制版”：

- 保留参考实现最有价值的能力边界与参数设计
- 去掉与本仓库无关的 TS framework 复杂度
- 通过 YAML 白名单与输出裁剪，把工具能力做成适合本地 LLM 的可靠搜索层
- 用清晰 docstring 和 prompt 规则，让 LLM 优先使用专用搜索工具而不是 `exec_command`
