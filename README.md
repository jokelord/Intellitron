# Intellitron

**简体中文（默认）** | [English](README.en.md)

分享自用本地 LLM 伴侣——Intellitron，其为面向 NVIDIA Jetson Thor（ARM64 / SM110a）的本地大语言模型部署与 Agent 交互系统，也可在桌面 GPU 上运行。它将视觉采集、可编程工作流、桌面操作、网络检索、文档阅读与语音输入接入同一套 Chat / Agent 界面，适合把日常阅读和实际工作串起来。

## 介绍视频

[![Intellitron 哔哩哔哩介绍视频预览](docs/assets/intellitron-bilibili-preview.svg)](https://www.bilibili.com/video/BV1mGJR6HExE/)

## 特色

1. **视觉阅读伴侣**：Vision Automation 按间隔采集共享屏幕或摄像头画面并发送给模型，让 Agent 随着你翻页持续辅助理解；参见 [Vision 页面说明](frontend/src/app/vision/README.md)。
2. **自动工作流编排**：Cycle 模式组合等待、消息、拍照与嵌套循环，并支持定时消息，可复用重复的 Chat / Agent 工作流程；参见 [Cycle 配置](config/config-misc.yaml)。
3. **桌面视觉交互**：内置 [computer-use-vision Skill](backend/skills/computer-use-vision/SKILL.md)，让视觉模型通过桌面截图定位目标，再执行点击、滚动等操作；参见 [操作指南](docs/computer-use-vision-operations.md)。
4. **批量网络搜索与抓取**：按 [SearXNG 部署教程](deploy/searxng/01_Docker-Compose安装及searXNG拉取教材.md) 和 [后端托管配置指南](deploy/searxng/02_searxng-backend-managed-setup.md) 部署后，`search_and_fetch` 可批量搜索并并发抓取网页正文。 将[示例](deploy/searxng/core-config/settings.yml.example)复制为同目录的 `settings.yml`，并填写密钥。
5. **直接阅读文档**：部署 PaddleOCR-VL 后，Agent 的 `read_file` 可阅读 PDF、Word 等文档；Word 等 Office 格式需要 LibreOffice 转换。OCR 启动方式见下方「部署与启动」。
6. **自动语音输入与听写**：部署独立 Qwen3-ASR 服务后，可选择识别后自动发送的语音模式，或转为可编辑文本的听写模式。Dictation Magic Send 可在转写输入末尾精确匹配配置的口令，减少手动点击发送。参见 [ASR 配置](config/config-asr.yaml) 与下方启动说明。
7. **并行任务分工**：可将入口设为 `manager_agent`，让它把相互独立的任务分发给多个 `basic_agent` 并行执行，并在界面查看进度；参见 [Agent 配置](config/config-agent-operation.yaml)。
8. **按任务选择工具**：新建 Agent context 前可在设置中勾选所需工具；请求被接受后，工具选择及对应提示词/API schema 固定在该 context 中，便于保持任务所需的能力范围。
9. **GUI 启动与诊断反馈**：[running-linux-gui-commands Skill](backend/skills/running-linux-gui-commands/SKILL.md) 自动定位 Linux 图形会话，提供启动状态、存活探测、退出码与日志反馈；结合桌面视觉 Skill 可进一步检查实际界面。
10. **本地记忆**：将 `.md`/`.txt` 笔记放入 `memory/agent-local/`，后端会建立 zvec 语义向量索引，并为后续 Chat / Agent 消息检索相关片段。首次使用请按[本地记忆说明](docs/how-to-use-local-memory.md)准备本地向量模型，并在网页设置中开启“本地记忆”。

## 项目架构

项目由三层组成：

```text
前端 Next.js（默认 3000） → 后端 FastAPI（默认 8090） → SGLang / vLLM 推理服务
```

模型、推理引擎、服务端和 UI 参数均由 [`config/config-overall.yaml`](config/config-overall.yaml) 路由到子配置文件。实际使用的模型与推理配置以该文件的 `infer` 字段为准。

## 功能概览

### Chat / Agent 模式

- **普通 Chat** 使用 `/v1/chat/completions`，直接进行单轮或多轮对话。
- **Agent** 使用 `/v1/agent/chat`，复用同一 session 运行工具调用循环；前端会分别展示思考、工具调用和观察结果。Agent 还提供 Stop、session 恢复及子 Agent 事件流等控制能力。
- 两种模式都可以使用流式输出；Vision 页面的图片上下文会按 Vision 请求规则传输，不能把前端显示内容直接当作模型历史。

### Agent 路由与并行调度

当前 [`config/config-agent-operation.yaml`](config/config-agent-operation.yaml) 通过 `agent.entry_agent_name` 选择入口，当前配置为 `basic_agent`：

- `manager_agent` 负责理解任务、做必要的只读查找，并通过唯一的 `dispatching_parallel_agent` 工具分发 `basic_agent` 子 Agent。
- 每个 dispatch call 只启动一个 child；相互独立的 child 可在同一轮并行执行，共享有序 blackboard。并发上限由 `agent.manager_agent_max_parallel_children` 控制，以配置中的值为准。
- `basic_agent` 是通用执行 Agent，负责文件修改、命令执行、搜索和其他实际工作；child 任务必须包含完整背景。`explorer_agent` 和 `auditor_agent` 目前只是未启用的占位 profile。
- 如将 `agent.entry_agent_name` 改为 `coordinator_1`，则启用另一套双阶段路由：`coordinator_1` 先在 `coordinator_2` 与 `basic_agent` 之间选择；`coordinator_2` 处理无需工具的普通聊天，必要时再切换到 `basic_agent`。

### 键盘 / 听写 / 语音输入

输入栏有三种模式：

- **键盘**：手动输入文本并发送。
- **听写（dictation）**：使用 ASR 将语音转成可编辑的输入框文本，再手动发送；启用 Dictation Magic Send 后，输入末尾精确匹配配置的口令即可移除口令并触发发送。
- **语音（voice）**：持续采集语音，按 utterance 识别并进入自动发送流程；ASR 使用独立的 Qwen3-ASR SGLang 服务。

语音输入需要浏览器 secure context：`localhost` 可直接使用；局域网 Android Chrome 通常需要在 `config/config-asr.yaml` 开启 HTTPS、生成证书并安装本地 CA。键盘输入不受该限制。

### Vision Automation

Vision 页提供浏览器本地视觉自动化：选择桌面共享或摄像头等视觉源，按间隔采集图像帧，组成 capture cycle 后发送给 Chat 或 Agent。面板支持源健康状态、失败重试、缩略图、推理期间采集和 Abort；相关参数位于 `config/config-vision.yaml` 的 `vision_automation` 段。
强烈建议在用户登陆界面选择右下角的 `Ubuntu on Wayland` 来体验 Vision Automation 功能。
如使用 Firefox, 将 about:config 中的 media.suspend-background-video.enabled 设为 false ，可更好体验 Vision Automation 。

### Cycle 模式

Cycle 是可编程的 LLM 序列执行器，前端可编辑 `wait`、`message`、拍照和嵌套 `cycle` 步骤，并通过 SSE 展示进度。启动时可选择是否使用 Agent、是否共享上下文；还支持带时区的定时消息；相关默认序列和执行限制位于 `config/config-misc.yaml` 的 `cycle` 段。

## 部署与启动

以下命令均在项目根目录执行。启动脚本会读取 `config/config-overall.yaml`，按数字顺序按需启动服务；推理后端只选择 SGLang 或 vLLM 其中之一。

### 1. 本地 SGLang 环境

项目使用 Python 3.12 和 uv，共享环境目录为 `.sglang`。依赖入口是 [`requirements/requirements_for_sglang.txt`](requirements/requirements_for_sglang.txt)：

```bash
uv venv .sglang --python 3.12
source .sglang/bin/activate
uv pip install -r requirements/requirements_for_sglang.txt
```

文件中的注释说明了 CUDA 13.0 版 PyTorch、Jetson 专用 `sgl-kernel` wheel 以及其他需要单独处理的安装步骤；`sgl-kernel` 必须与当前 SGLang 版本匹配。

agent 首次使用 searXNG 网络搜索，还需要安装 Chromium 浏览器文件和 Ubuntu 系统依赖：

```bash
# 下载与当前 Python Playwright 版本匹配的 Chromium
./.sglang/bin/playwright install chromium

# 安装 Chromium 运行所需的 Ubuntu 系统库（需要 sudo）
sudo ./.sglang/bin/playwright install-deps chromium
```


启动主模型：

```bash
bash scripts/1_A_start_sglang.sh
```

脚本从选中的 `*-config-infer.yaml` 读取模型路径、端口、上下文长度、显存比例、注意力/量化后端、reasoning/tool parser、prefix-cache 等参数，并调用 `sglang.launch_server`。Jetson 首次运行某个模型配置时可能触发 Triton/FlashInfer 编译；NVFP4 等量化模型会出现较高的 UMA 临时内存峰值，必要时先在 infer 配置中降低上下文、显存比例和编译并发。

### 2. vLLM NGC 容器

Jetson Thor 的 vLLM 入口是 NVIDIA NGC 容器，不是本地 uv/source vLLM 环境。当前脚本使用 `nvcr.io/nvidia/vllm:26.06-py3`，需要可用的 Docker、NVIDIA Container Toolkit 和模型目录。

运行前可清理 UMA：

```bash
bash scripts/0_force_cleanup_uma.sh
```

按模型目录形式选择一个入口：

```bash
# Hugging Face snapshot 布局
bash scripts/1_B_start_vllm_container_with_snapshots.sh

# 普通模型目录
bash scripts/1_C_start_vllm_container.sh
```

两个脚本都会从选中的 infer 配置读取模型和 vLLM 服务参数，并启动 OpenAI-compatible API，默认端口为 `8000`。通常只启动一个主推理后端；若两者并存，应确保端口不同，并用 `LLM_BACKEND` 明确后端选择。

后端自动探测顺序是 SGLang → vLLM → 配置启用的 OpenAI-compatible proxy → mock。若 SGLang 与 vLLM 同时运行，SGLang 会优先；也可以用环境变量强制选择：

```bash
LLM_BACKEND=sglang bash scripts/4_start_backend.sh
LLM_BACKEND=vllm bash scripts/4_start_backend.sh
```

以启动后端返回的 `/config` 中 `inference.active_backend` 为准确认实际 provider，不要只根据端口或启动脚本推断。

### 3. 按需启动辅助服务、后端和前端

```bash
# 可选：OCR 服务，按部署方式选择其一
bash scripts/2_optional_A_start_OCR_LM_with_SGLang_backend.sh
bash scripts/2_optional_B_start_OCR_LM_with_transformer.sh

# 可选：独立 Qwen3-ASR SGLang 服务
bash scripts/3_optional_start_ASR_sglang.sh

# FastAPI 后端
bash scripts/4_start_backend.sh

# Next.js 前端
bash scripts/5_start_frontend.sh
```

`2_optional_A` 和 `2_optional_B` 是两种 OCR 路径，按需选择，不需要同时开启。ASR 是否启用、模型路径和端口由 `config/config-asr.yaml` 决定。后端启动脚本还会复用已有的 SearXNG；没有可用实例且 Docker 可用时，会尝试按项目配置启动它。

前端要求 Node.js `>=18.17.0`。`5_start_frontend.sh` 会读取前端端口和后端 API 地址；缺少 `node_modules` 时会复用共享依赖或执行 `npm install`。

### 基本验证

```bash
curl http://localhost:8090/health
```

前端默认地址：

- Text：`http://localhost:3000/`
- Vision：`http://localhost:3000/vision`

## Agent 模式的基础工具

工具会按 Agent profile、配置和安全过滤规则裁剪。`basic_agent` 是完整执行 profile，`manager_agent` 默认包含文件读取/写入、命令执行、搜索、技能读取和 child dispatch 能力；两者都可在新建 context 前通过 Web 设置选择一般业务工具；请求被后端接受后，工具选择、系统提示词与 API tool schema 会固定在该 context 中。

| 工具 | 基本用途 |
|------|----------|
| `read_file` | 读取文本、代码及受支持的 PDF/Office 等文档，并返回带行号内容 |
| `write_file` | 创建或覆盖文件 |
| `edit_file` | 对已有文件做定点文本替换 |
| `glob` / `grep` | 按路径模式或内容正则搜索白名单目录 |
| `exec_command` | 执行受安全策略约束的 shell 命令 |
| `check_process` | 查询匹配的运行中进程 |
| `playwright-browser-skill` | 通过项目 `.sglang` Python 环境中的 Playwright 做会话隔离浏览器自动化；通过 `exec_command` 调用 skill 入口 |
| `list_skills` / `read_skill` | 查询并读取项目内置 Skill 的 SOP |
| `web_fetch` | 抓取指定网页 |
| `search_and_fetch` | 搜索网页并并发抓取结果正文 |
| `wikipedia_search_and_fetch` | 可选的 Wikipedia 搜索与抓取；在 Agent 工具目录中默认不勾选，显式勾选后才注入 |
| MCP tools | `config-agent-operation.yaml` 配置 `mcp.servers` 后接入外部 MCP 工具 |
| `dispatching_parallel_agent` | 仅 `manager_agent` 使用；每次分发一个 `basic_agent` child |

`final_answer` 等内部控制能力不是用户可配置的基础工具。工具是否真正出现在某个 profile 的提示词和 API schema 中，以运行时过滤结果为准。
旧 `playwright-cli` / `npx playwright-cli` 命令不会再由 `exec_command` 路由执行；浏览器自动化必须读取并使用 `playwright-browser-skill`。

## 配置文件

`config/config-overall.yaml` 是配置路由入口。它当前引用以下文件：

| 文件 | 调整方向 |
|------|----------|
| `config/*-config-infer.yaml` | 当前模型信息、采样参数，以及 SGLang/vLLM 的模型加载、端口、上下文、显存、并行、量化、注意力后端、reasoning/tool parser 和 prefix-cache 等推理参数。当前模型段已合并到 infer 文件；旧的 `*-config-model.yaml` 只用于兼容回退，不是默认路由入口。 |
| `config/config-server.yaml` | FastAPI 服务地址/端口、前端开发端口和 API 地址、流式超时、OCR 服务端点与相关超时。 |
| `config/config-webUI.yaml` | UI 皮肤、WebGL 效果、布局、输入框/按钮/头像尺寸、欢迎页和 Agent step 显示。 |
| `config/config-misc.yaml` | `/compact`、运行时提示、调试/日志、平台设置和 Cycle 默认序列。 |
| `config/config-agent-operation.yaml` | Agent 总开关/入口/profile/并发限制、文档读取限制、MCP、SearXNG/web 抓取。 |
| `config/config-agent-prompts.yaml` | `coordinator_1`、`coordinator_2`、`manager_agent`、`basic_agent` 等 profile 的 system prompt、路由/调度约定、VLM 说明和 compact prompt 模板。 |
| `config/config-asr.yaml` | 浏览器音频格式、录音分段、Silero VAD、Qwen3-ASR SGLang 服务、转写策略、Dictation Magic Send 和浏览器 HTTPS 安全要求。 |
| `config/config-vision.yaml` | 图片上传格式/大小/预处理缩放、摄像头初始化与拍照参数，以及 Vision Automation 的采集、压缩、超时和重试限制。 |

相关 OCR pipeline 配置由 `config/config-server.yaml` 再引用，包括 `config/paddleocr-vl-thor.yaml`、`config/paddleocr-vl-thor-sglang.yaml` 和模型 manifest。

修改配置的一般顺序：

1. 在 `config/config-overall.yaml` 确认实际使用的 infer、server、misc 等路径。
2. 修改对应子配置；模型/引擎启动参数和端口需要重启推理服务，后端或前端配置需要重启相应脚本。
3. 用启动日志、健康检查和后端 `/config` 验证；Web 设置面板中已有的浏览器缓存值会优先于 YAML 默认值。

## 目录速览

以下以本地检出目录名 `LLM-deployment` 为例：

```text
LLM-deployment/
├── backend/                 # FastAPI、LLM provider、Agent、Cycle、ASR 与工具
├── frontend/                # Next.js Text / Vision 两个页面
├── config/                  # 总配置及各功能子配置
├── scripts/                 # 按数字顺序的推理、辅助服务、后端、前端启动脚本
├── requirements/            # SGLang 与 OCR 依赖入口
├── logs/                    # 运行日志（自动生成）
└── .sglang/                 # uv Python 环境（通常不纳入版本控制）
```

许可证：MIT
