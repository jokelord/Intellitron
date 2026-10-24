# Agent 工具操作安全边界优化 PRD / SPEC

## 文档状态

- 状态：已实现设计
- 日期：2026-09-13
- 范围：Agent 文件工具、命令工具、浏览器命令、进程生命周期、子 Agent capability、图片与 session 临时路径、相关配置和测试
- 关联专题：docs/remote-safety-issue.md （pending, 暂时搁置，先不管）

## 1. 决策边界

本 SPEC 只处理本次审计中除远程 API 访问控制和“允许根目录过宽”之外的风险与 bug。

明确排除：

- 不实现 API key、Bearer token、监听地址、CORS 或其他远程访问控制；这些内容记录在 docs/remote-safety-issue.md，另行立项。
- 不评价或调整当前已配置的 allowed roots 的覆盖范围。本 SPEC 只要求所有适用工具一致执行已有的配置化路径权限。
- 不修改 SGLang、vLLM 或第三方依赖源码。
- 不把前端展示内容、timeline、工具 observation 或 prompt 文本当作安全权限来源。

本文最初作为设计交付；后续实现由第 10 节列出的 plans 驱动，GUI trusted-launch follow-up 记录在第 7.3.1 节。

## 2. 背景与问题陈述

当前工具层的安全机制是分散的：

- read_file 有独立的路径解析、根目录校验、符号链接和大小验证。
- search 工具有自己的搜索根目录校验。
- write_file 和 edit_file 直接调用 open 写入，没有复用路径权限校验，能够越过搜索和读取边界。
- exec_command 用 Bash 加命令黑名单运行任意 shell 文本。黑名单只覆盖若干字符串形态，不能作为命令隔离。
- 浏览器 session ID 会被拼入 shell 命令并经过 eval，缺少安全的参数边界。
- 超时和用户中止只操作 shell 进程，没有明确的子进程组清理协议；输出收集也没有硬上限。
- Agent child 的可写和可执行范围主要写在 task 提示词中，没有由工具 wrapper 强制执行。
- 图片加载脚本可读取未经过统一路径策略的本地图片，session ID 也直接参与临时目录拼接。
- agent.exec_timeout 被配置解析，但没有可靠传递到实际 exec_command 调用。
- 自定义 command_blacklist 会替换默认规则，可能意外减少保护。

这些问题叠加后，系统更接近“以 backend 进程身份运行的便利工具”，而不是具有明确能力边界的工具执行器。

## 3. 目标与非目标

### 3.1 目标

1. 所有文件写入和编辑都通过统一、配置驱动、可测试的路径 capability。
2. 所有 session 临时目录和图片输入都拒绝路径穿越、非法 session 标识和不受控符号链接。
3. 将命令安全从“只搜索原始字符串黑名单”升级为明确的 command policy 和可控的执行模式。
4. 浏览器命令不再通过未转义的 session ID 或 eval 形成新的 shell 解析边界。
5. 超时、中止、输出、环境变量和子进程生命周期具有硬性、可验证的运行时限制。
6. child Agent 的工具 wrapper 依据 capability 做技术校验，而不是依赖 task 中的自然语言承诺。
7. 配置字段必须真正作用于运行时，并通过配置回归测试锁定。
8. 拒绝原因可诊断，但不得泄露环境中的 secret、完整环境变量或不必要的敏感路径。

### 3.2 非目标

- 不保证任意 shell 文本在不受限模式下可被正则黑名单完整识别。
- 不在本 SPEC 内提供 API 认证或公网部署方案。
- 不将所有 Agent 功能改造成只读；需要保留经 capability 允许的受控写入和命令执行。
- 不通过修改历史 LLM message、系统 prompt 或 UI 文本来实现权限控制。

## 4. 威胁模型与假设

考虑以下输入来源：

- 模型生成的恶意或格式异常工具参数。
- 用户 prompt injection 诱导 Agent 越权。
- child Agent 继承到不应拥有的工具权限。
- 允许目录中的符号链接、竞态替换或恶意文件名。
- 被 shell 解释器重新解析的命令参数、session ID 和路径。
- 产生大量输出、后台子进程或不退出进程的命令。

假设：

- 后端进程不会以 root 运行；若部署以 root 运行，本文所有影响都应按更高等级处理。
- 进程本身仍可能读取和写入 OS 用户拥有的资源，因此 Python 层策略必须和 OS 进程隔离共同工作。
- Agent task 文本是不可信数据，不具备授权语义。
- 现有前端和 Agent API 的工具名称及正常输出尽量保持兼容，但安全拒绝行为可以新增明确错误。

## 5. 设计原则

### 5.1 能力先于工具

工具是否出现在 prompt 或 API schema 中不是最终授权。每次真实执行都必须从当前 AgentSession 取得不可变的 OperationCapability，并在工具边界再次校验。

### 5.2 路径权限必须覆盖实际副作用

读、搜、写、编辑、图片加载和临时文件清理不能各自定义路径规则。路径策略至少需要区分：

- existing read：目标必须存在并为允许的普通文件；
- create：父目录和最终文件都在允许范围内；
- modify：已存在的普通文件在允许范围内；
- cleanup：只能删除由当前 session 创建的固定临时目录。

### 5.3 Shell 解析边界最小化

优先使用 argv 形式执行程序。必须兼容的 shell 文本使用显式策略模式，并在执行前拒绝无法可靠约束的语法，而不是继续扩大正则黑名单。

### 5.4 拒绝安全优先

无法确定路径、命令、session 标识或 capability 是否安全时拒绝执行。拒绝结果应包含稳定的错误码和可操作原因，不回显 secret 或完整命令中的敏感参数。

### 5.5 防御深度

路径策略、工具 wrapper、进程 supervisor 和 OS 运行用户各自提供一层保护。任何一层失效都不应自动获得完整 unrestricted 行为。

## 6. 目标架构

### 6.1 统一 OperationCapability

新增 backend/security/capabilities.py，定义不可变的运行时权限对象。建议接口：

    @dataclass(frozen=True)
    class OperationCapability:
        session_id: str
        read_roots: tuple[str, ...]
        search_roots: tuple[str, ...]
        file_operation_roots: tuple[str, ...]
        image_input_roots: tuple[str, ...]
        session_temp_base_dir: str
        command_mode: str
        allowed_executables: tuple[str, ...]
        max_exec_timeout_seconds: float
        max_output_bytes: int
        max_file_bytes: int
        allow_overwrite_existing: bool
        allow_browser: bool

    def require(capability, operation, target) -> None

AgentSession 创建时绑定 capability；child session 只能继承或收窄，不得从 task 文本扩大权限。所有 file_ops、sys_ops、load_local_images 和 browser wrapper 都通过同一对象校验。

### 6.2 统一 PathPolicy

新增 backend/security/path_policy.py，负责绝对路径化、root containment、符号链接策略、普通文件检查、父目录检查、临时目录 token 和错误码。write/edit/read/search/image 工具通过适配器调用该模块，避免复制规则。

路径策略必须处理：

- 相对路径如何解释；
- 绝对路径是否属于 capability；
- 路径中任意父级符号链接；
- final component 的符号链接；
- 非普通文件、目录、设备、FIFO 和 socket；
- 校验后到打开之间的竞态；
- 创建父目录时不能跨出允许 root。

resolved policy 必须保留 operation-specific root 语义：read_file.allowed_roots 只作为读取和默认文件操作来源，agent.search_whitelist_paths 只作为搜索来源；不得为了统一实现而直接取两者 union。若提供显式 file_operation_roots 或 image_input_roots，则只替换对应 operation 的 root 集合，并在配置诊断中显示最终解析结果。

### 6.3 CommandPolicy 与 ProcessSupervisor

新增 backend/security/command_policy.py 和 backend/security/process_supervisor.py。

CommandPolicy 产生结构化 Decision，至少包含 allow、reason_code、display_reason 和 execution_mode。默认路径使用 argv；shell compatibility 模式仅允许显式配置的安全语法集合，并拒绝 eval、命令替换、source、隐式后台运行、危险重定向、设备写入、提权和未允许的解释器包装。

新增 EnvironmentBuilder 或等效的 runtime environment adapter，负责把当前 agent bin、VIRTUAL_ENV、PATH、NVM_DIR 和必要的 nvm/Node 工具链依赖解析为可测试的执行环境。argv 模式直接使用解析后的可执行文件和环境；必须保留 shell 的兼容路径时，只使用固定 preamble 和受 policy 控制的 command 输入，不能通过 eval 拼接用户参数。browser 命令不依赖 login-shell preamble 才能安全传参。

ProcessSupervisor 负责：

- fresh process group；
- deadline 和 abort；
- 先终止整个进程组，再按 grace 强制 kill；
- 排空和限制 stdout/stderr；
- 返回截断状态、退出码和稳定错误；
- 清理所有已知子进程；
- 关闭和回收 supervisor 资源。

浏览器命令使用 argv 或专用 browser adapter，不再用 eval 包裹用户参数。

### 6.4 capability 传播

manager 继续只拥有现有只读工具和 dispatch 工具；basic child 仍可以拥有业务所需工具，但每个副作用都必须通过 child session 的 capability 检查。dispatch 参数保持现有 canonical contract，只含 child_id 和 task；task 文本不承载权限授予。若未来需要更窄的单文件范围，应通过 manager/session 外部的授权状态或显式用户批准建立 capability，而不是增加自然语言字段。

## 7. 功能需求

### 7.1 文件操作需求

- FS-01：write_file 和 edit_file 必须拒绝 capability 之外的路径。
- FS-02：必须拒绝路径穿越和解析后越界。
- FS-03：必须拒绝通过符号链接写入；read/modify/create 的符号链接策略必须明确且有测试。
- FS-04：必须拒绝目录、设备、FIFO、socket 等非普通文件。
- FS-05：创建父目录也必须经过 root 检查，不能只校验最终字符串。
- FS-06：写入应采用临时文件、权限设置、fsync 及原子替换策略；原子替换前必须确认目标仍符合 policy。现有 PermissionError 指数退避只适用于已通过 policy 的暂时性 I/O 失败，不能重试 policy denial；每次失败尝试都必须清理临时文件。
- FS-07：为保持现有 write_file 工作流，授权目录内的已有普通文件默认允许覆盖，allow_overwrite_existing 是显式 capability/config 字段且默认值为 true；strict 配置可将其关闭。edit_file 的修改权限仍单独校验，不能由 prompt 授权。
- FS-08：content 大小必须有配置化上限，拒绝超限输入。
- FS-09：拒绝响应使用稳定错误码，不返回完整 secret-bearing path 或不必要的文件内容。
- FS-10：read、search、file operation 和 image operation 使用各自解析后的 root 集合；统一 policy 不得隐式扩大或缩小既有 operation 的授权范围。

### 7.2 命令需求

- CMD-01：command policy 必须在所有 shell 变换、session 注入和环境包装之前生效，并且变换后的最终执行结构也必须再次校验。
- CMD-02：默认支持 argv 执行；shell compatibility 必须显式选择。
- CMD-03：不能依赖有限正则声称覆盖任意 shell 语义。
- CMD-04：配置 blacklist 与内置保护集合合并，配置项不能降低内置保护。
- CMD-05：browser session ID 仅允许安全字符集，且以独立 argv 参数传递。
- CMD-06：禁止 eval、未经策略允许的 shell 嵌套、命令替换、危险重定向、设备写入、提权和系统级进程控制。
- CMD-07：policy 拒绝必须在启动子进程前完成。
- CMD-08：正常工具输出和现有 SSE 协议尽量不变，但安全错误必须可区分。

### 7.3 进程和资源需求

- PROC-01：每次执行必须创建可定位的进程组或等效 job。
- PROC-02：timeout、Abort 和 supervisor shutdown 必须终止并回收整个进程组。
- PROC-03：timeout 上限、grace、stdout、stderr、并发数和环境变量策略必须配置化且实际生效。
- PROC-04：输出达到上限时停止继续收集，并返回截断标志，不允许无界积累。
- PROC-05：执行环境使用最小 allowlist；不得默认把 token、SSH agent、云凭证等敏感变量传给 Agent 命令。
- PROC-06：资源拒绝或清理失败必须记录结构化诊断，并避免泄露命令中的敏感参数。

#### 7.3.1 Linux GUI trusted launch 例外

`backend/skills/running-linux-gui-commands` 是本 SPEC 的显式内部 trusted launch 例外，边界如下：

- `config/config-agent-operation.yaml` 只登记仓库内 `agent-gui-run` 与 `agent-gui-diagnose` 的精确绝对路径；不使用 basename 放行其他目录中的同名文件。仓库迁移后必须同步更新主机级配置。
- 当前 skill 依赖 `command_mode: restricted_shell`：`EnvironmentBuilder` 的 argv 模式会因绝对 launcher `argv[0]` 无法解析而返回 `executable_not_resolved`。切换模式前必须提供独立的可信路径解析和回归测试，不得放宽通用路径规则。
- 外层 `CommandPolicy` 只验证 launcher 自身，无法把 `--` 后的 argv 当作独立命令再次执行完整 policy，也不向内层进程施加 file-operation roots。launcher 因此内置最小高危 deny 集并递归检查 `env` 包装，但这仍是 defense in depth，不是 OS sandbox。
- direct shell interpreter、提权、文件系统格式化、系统电源/服务/进程控制、设备路径访问、递归删除根路径和扩大根路径权限在 graphical-session discovery 前拒绝。Skill 与 Agent prompt 禁止把 `bash -lc`、`sh -c`、pipeline、redirection、`source` 或 `eval` 作为兼容方案。
- `--wait` 不创建分离 session，GUI child 仍处于外层 `ProcessSupervisor` 的运行期监管范围。默认异步模式会通过 `setsid --wait` 或 `nohup` 后台启动，使 GUI 在 launcher 返回后继续存在；外层 timeout/abort/shutdown 不保证回收该分离进程，这是 GUI 持久化语义所需的已登记例外。
- root caller 不能自定义日志。非 root 自定义日志必须是当前真实工作目录或 `/tmp` 下的绝对普通文件路径，且所有现有路径组件都不得为符号链接；该限制缩小 launcher 自身的写入面，但不改变“Python policy 不是 OS sandbox”的总体假设。

### 7.4 Agent 和 session 需求

- AG-01：工具 wrapper 必须在实际执行边界读取 capability。
- AG-02：child capability 只能继承或收窄，不能由 task 文本扩大。
- AG-03：新 session 由后端生成 canonical UUID；客户端提供的 session ID 只有在 canonical 格式或既有的 legacy safe token 格式下才可进入 admission。legacy safe token 只允许复用已存在 session，不能用来新建 session；非法值在 API、session store、临时文件和 cleanup 入口统一拒绝。
- AG-04：session 临时目录必须固定在安全 base directory 下，并通过安全创建方式建立。
- AG-05：load_local_images 的输入路径必须走 image input path policy，并复用统一符号链接和普通文件规则。
- AG-06：cleanup 只能删除当前 session 通过安全分配得到的目录，禁止对拼接后的未经验证字符串调用递归删除。
- AG-07：side-effecting tool 的 session_id 以 wrapper 绑定的 session identity 为准；模型或用户在工具参数中额外提供的 session_id 必须被拒绝，不能覆盖绑定值。

### 7.5 配置需求

- CFG-01：agent.exec_timeout 必须进入实际 exec policy，并由测试证明显式配置值改变运行行为。
- CFG-02：黑名单配置必须校验 pattern、reason 和编译错误；内置规则不能被删除。
- CFG-03：新增安全配置必须有明确默认值、合法范围和启动时诊断。
- CFG-04：配置对象、工具 wrapper、supervisor 使用同一份 resolved policy，不能各自重新读取 YAML。
- CFG-05：file_operation_roots、image_input_roots 等显式 root 配置使用替换对应 operation 的语义；缺省时采用既定 fallback，不做 read/search root union。
- CFG-06：allow_overwrite_existing 默认 true 仅表示允许授权目录内覆盖普通文件，不得绕过任何路径、文件类型、大小或竞态校验。
- CFG-07：agent.exec_timeout、shell mode、agent bin、Node/nvm preamble、work_dir 和输出限制必须由同一 runtime policy 实际消费。

## 8. 错误处理与兼容策略

错误分为 policy_denied、invalid_path、invalid_session_id、unsupported_shell、resource_limit、timeout、aborted、cleanup_failed 和 internal_error。错误返回不应把完整环境或敏感命令原样回显。

建议分两个发布阶段：

1. audit：记录即将被拒绝的原因，但只对明确危险的现有规则执行拒绝；用于收集正常 Agent 工作流需要的命令。
2. enforce：启用完整 capability 和 process policy。未满足 policy 的调用稳定失败，不自动回退到 unrestricted shell。

兼容性边界：

- 工具名称和普通成功输出尽量保持不变。
- 现有 command: str 接口可以保留为 compatibility adapter，但内部必须转换成 policy decision；新内部接口使用 argv。
- 不能通过隐式扩大 root、自动放行失败路径或删除默认保护来恢复兼容。

兼容性决策：

- write_file 在已授权且为普通文件的目标上默认保持覆盖行为；将来可通过 allow_overwrite_existing=false 切换为 create-only，而不是在本次安全改造中默认破坏正常开发工作流。
- EnvironmentBuilder 必须先复现现有 agent bin、VIRTUAL_ENV、PATH 和 nvm/Node 依赖，再切换 argv 执行。无法迁移的旧 shell 调用必须进入显式 shell compatibility policy，不得通过重新引入 eval 恢复兼容。
- 新请求不提供 session_id 时继续由 backend 生成 canonical UUID。已存在且符合 canonical 或 legacy safe token 的 session 可复用；未知的 legacy safe token 和任何非法 ID 都不能创建或清理路径，并返回迁移可诊断错误。
- read_file.allowed_roots、agent.search_whitelist_paths、file_operation_roots 和 image_input_roots 的解析结果按 operation 分开保存；缺少显式 file_operation_roots 时，file operation 使用 read_file.allowed_roots 的解析值，不与 search roots 做 union。

## 9. 验收标准

### 必须通过的负向测试

- write_file/edit_file 对 root 外路径、..、绝对越界路径、父目录符号链接、最终符号链接、目录和设备文件拒绝。
- 写入目标在校验后被替换为符号链接时，不能写到 root 外。
- 超大 content 被拒绝；默认兼容 policy 允许授权目录内覆盖普通文件，allow_overwrite_existing=false 时覆盖被拒绝。
- PermissionError 的暂时性重试保留在原子写生命周期内；policy denial 不重试，临时文件不残留。
- 黑名单配置只增加规则时，默认高危规则仍然有效。
- shell 绕过样例不能在 restricted mode 启动子进程。
- 含 shell 元字符的 session ID 被拒绝，browser adapter 不产生 eval。
- timeout、abort 和 shutdown 后不存在由该调用遗留的子进程。
- stdout/stderr 超限时内存占用有硬上限且响应标记为 truncated。
- 执行环境不包含默认 secret 类变量。
- 非法 session ID 不能穿越到 agent_images base directory 外，也不能触发越界 cleanup。
- load_local_images 对 policy 外图片路径拒绝。
- child task 文本声明拥有额外目录或命令权限时，实际工具调用仍被 capability 拒绝。
- GUI trusted launcher 在配置中仅以精确路径放行；内层高危命令、`env` 包装和 shell wrapper 在启动前拒绝，越界或 symlink 自定义日志不会被截断。

### 必须通过的回归测试

- read_file、search、manager allowlist、正常 write/edit、正常浏览器流程保持既有预期。
- agent.exec_timeout 显式配置值被实际执行路径使用。
- 现有 SSE 错误和普通成功事件仍可被前端解析。
- 配置解析失败时给出明确错误，而不是静默恢复为 unrestricted 行为。
- argv 执行与现有 agent bin、VIRTUAL_ENV、PATH、nvm/Node preamble 的运行时依赖均有测试；browser 不依赖未受控的 login-shell 或 eval。
- canonical session、新建 legacy ID、复用既有 legacy safe session 和非法 session ID 的行为均有测试。

## 10. 计划拆分与依赖

按独立子系统拆分为三个 implementing plan：

1. docs/superpowers/plans/2026-09-13-filesystem-operation-boundaries.md
   - 统一路径 capability、write/edit、图片输入和 session 临时路径。
2. docs/superpowers/plans/2026-09-13-command-execution-isolation.md
   - command policy、browser 参数边界、进程组、输出上限、环境和 timeout。
3. docs/superpowers/plans/2026-09-13-agent-capability-config-integration.md
   - AgentSession/child capability、tool wrapper 接入、配置生效与跨链路回归。

依赖顺序：计划 1 和计划 2 可并行设计和实现；计划 3 依赖两者提供的 capability 与 supervisor 接口。

## 11. 风险与开放决策

- 现有 Agent 工作流可能依赖 shell 管道、重定向和解释器包装；需要先在 audit 阶段收集调用样本，再决定 restricted mode 的最小语法集合。
- 若业务确实需要任意脚本执行，Python policy 不能单独提供强隔离，后续仍可能需要容器、user namespace、seccomp 或专用 worker。
- GUI launcher 的异步持久化语义有意越过普通 process-group 回收边界；需要 timeout/abort 可覆盖运行期的任务必须使用 `--wait`，需要更强保证时应新增由 backend 持有 PID/PGID 的专用 GUI supervisor，而不是扩张 launcher 例外。
- 原子写入会改变部分文件权限、inode 和 watcher 行为，需在实现测试中确认对开发工作流的影响。
- 浏览器 profile 本身包含登录态；browser capability 和 profile 生命周期应作为独立安全评审项，不因命令注入修复而默认扩大访问。
