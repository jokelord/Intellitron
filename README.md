# LLM-deployment: 大语言模型部署与优化 (Jetson Thor)

<div align="center">

![NVIDIA](https://img.shields.io/badge/NVIDIA-76B900?style=for-the-badge&logo=nvidia&logoColor=white)
![Python](https://img.shields.io/badge/Python_3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)
![uv](https://img.shields.io/badge/uv-Fast_Package_Manager-FF69B4?style=for-the-badge)
![SGLang](https://img.shields.io/badge/SGLang-Inference_Engine-007ACC?style=for-the-badge)

**专为 NVIDIA Jetson Thor (sm_110) 深度优化的 SGLang 部署方案**

</div>

## 📋 项目概述

| 组件 | 技术栈 | 说明 |
|------|--------|------|
| 后端 | Python 3.12 + FastAPI | 转发并增强 SGLang 接口（支持 Reasoning） |
| 前端 | Next.js 14 + TypeScript | 流式对话界面，支持推理过程展示 |
| 推理 | SGLang (v0.4.1+) | 针对 Jetson Thor (sm_110) 优化的推理引擎 |
| 模型 | GLM-4.7-Flash | 支持思考过程的模型 |

*模型配置详见 `config/config.ini` 中的 `[model]` 部分*

**核心依赖版本**：
- **Python**: 3.12 (必需)
- **PyTorch**: 2.9.0+cu130 (CUDA 13.0)
- **SGLang**: 0.5.8 
- **vLLM**: 0.1.dev13739+g4fc447455 (Gadflyii fork，实验性)
- **sgl-kernel**: 0.3.21+ (本地 wheel)
- **Transformers**: 5.0.1.dev0 (源码安装，支持 glm4_moe_lite)
- **FastAPI**: 0.104.0+ (Web 框架)
- **Uvicorn**: 0.24.0+ (ASGI 服务器)
- **Pydantic**: 2.5.0+ (数据验证)
- **Pydantic-settings**: 2.1.0+ (配置管理)
- **python-multipart**: 0.0.6+ (FastAPI 文件上传支持)
- **httpx**: 0.25.0+ (异步 HTTP 客户端)
- **aiofiles**: 23.2.1+ (异步文件操作)
- **colorama**: 0.4.6+ (日志彩色输出)
- **Next.js**: 14.2.21

**架构**：`[用户] → [前端:3000] → [后端:8000] → [SGLang 推理:30000] → [GPU]`

*默认端口配置详见 `config/config.ini` 中的 `[frontend]`, `[server]`, `[sglang]` 部分*

---

## 📋 版本兼容性

### 核心组件版本要求
| 组件 | 版本要求 | 说明 |
|------|----------|------|
| **Python** | 3.12 (必需) | PyTorch/TensorRT-LLM 不支持 3.14 |
| **CUDA** | 13.0+ | Jetson Thor 使用 CUDA 13.0 |
| **PyTorch** | 2.9.0+cu130 | 必须使用 CUDA 13.0 版本 |
| **SGLang** | 0.4.1+ | 支持 Jetson Thor sm_110 架构 |
| **vLLM** | 0.1.dev13739+g4fc447455 | Gadflyii fork，实验性支持 sm_110 |
| **sgl-kernel** | 0.3.21+ | 本地编译 wheel，适配 sm_110 |
| **Transformers** | 4.57.1 | 模型配置和分词器支持 |


### 平台特定要求
- **Jetson Thor**: ARM64, Ubuntu 24.04, JetPack 7
- **Desktop GPU**: x86_64, Ubuntu/CentOS, CUDA 12.0+

---

## 🚀 部署指南

### 1. 准备工作
确保系统已安装 `nvidia-jetpack` (CUDA 13.0+)。

**安装 Node.js (Ubuntu 24.04):**
前端需要 Node.js v18+ (推荐 v20)，若未安装请执行：
```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
```

### 2. 环境初始化
克隆仓库后，使用 `uv` 创建并激活环境：
```bash
uv venv .sglang --python 3.12
source .sglang/bin/activate
```

### 3. 安装依赖
安装支持 CUDA 13.0 的核心包。**注意**：`sgl-kernel` 必须使用针对 sm_110 编译的本地 whl 文件。
```bash
# 设置代理（国内网络必需）
export http_proxy=http://127.0.0.1:10808
export https_proxy=http://127.0.0.1:10808

# 安装特定版本的 Torch 和 SGLang
uv pip install torch==2.9.0+cu130 torchvision torchaudio --index-url https://download.pytorch.org/whl/cu130
uv pip install sglang[all] flashinfer-python

# 安装本地 sgl-kernel (必须，版本: 0.3.21+)
uv pip install /path/to/sgl_kernel-0.3.21+cu130-cp310-abi3-manylinux2014_aarch64.whl

# 安装最新 Transformers (支持 GLM-4.7-Flash)
uv pip install git+https://github.com/huggingface/transformers.git
```

### 4. 启动推理引擎 (SGLang)
使用优化脚本启动。该脚本已集成 `ptxas_wrapper.sh` 补丁，解决 Jetson Thor 的编译兼容性问题，并配置为使用 GLM-4.7-Flash。
```bash
bash scripts/start_sglang.sh
```

### 4.5 vLLM 后端支持 (实验性)

**⚠️ 重要说明**：
- **vLLM 版本**: 0.1.dev13739+g4fc447455 (Gadflyii fork，包含 sm_110 支持)
- **当前状态**: 🚧 **未完成** - NVFP4 格式在 Jetson Thor 上仍有死机问题，无法稳定运行
- **已知问题**: 模型加载后初始化阶段容易死机，推测与 UMA 架构内存管理相关
- **建议**: 暂时禁用 vLLM，使用 SGLang 作为主要推理引擎

**启动命令** (不推荐使用):
```bash
bash scripts/start_vllm.sh
```

**禁用方法**:
- 在 `config/config.ini` 中设置 `backend = sglang`
- 或直接使用 SGLang 启动脚本

**问题排查**:
- 如果遇到 `cudaErrorNoKernelImageForDevice`，说明缺少 sm_110 内核 (已通过源码编译解决)
- 如果死机，通常是显存不足或初始化问题，建议降低 `gpu_memory_utilization` 到 0.4

### 5. 启动后端服务
后端负责将 SGLang 的输出（包含 `reasoning_content`）包装为标准的 API。
```bash
bash scripts/start_backend.sh
```

**注意**：脚本会自动检测端口占用情况，如果端口被占用会尝试清理残留进程，确保服务能正常启动。

### 7. 启动前端界面
```bash
bash scripts/start_frontend.sh
```

---

## LLM参数设置

以下是为不同任务类型推荐的起始参数组合，你可以以此为基准进行调整：

| 任务类型 | 核心目标 | 推荐参数组合 (起始点) | 关键说明 |
|----------|----------|----------------------|----------|
| 复杂推理<br/>(数学、代码、逻辑分析) | 思考严谨，答案精准 | `top_p=0.8, top_k=40,`<br/>`repetition_penalty=1.05, temperature=0.3` | 收紧采样范围(top_p, top_k)，降低随机性(temperature)，轻微惩罚重复，引导模型进行集中、连贯的思考。 |
| 知识问答与摘要<br/>(事实性、信息整合) | 回答可靠，信息凝练 | `top_p=0.9, top_k=60,`<br/>`repetition_penalty=1.1, temperature=0.6` | 稍放宽采样范围以获得更丰富的表达，适度惩罚重复以避免冗余，温度适中保证流畅性。 |
| 创意写作与脑暴<br/>(故事、诗歌、创意生成) | 内容多样，富有新意 | `top_p=0.95, top_k=80,`<br/>`repetition_penalty=1.0, temperature=0.8` | 最大限度放宽候选词范围，关闭或最小化重复惩罚，提高温度以增加随机性和创意。 |

## 🖥️ 验证与测试

### API 健康检查
```bash
curl http://localhost:8000/health
```

### 自动化测试
运行测试脚本验证模型是否能正常回复（含推理过程）：
```bash
# 默认测试健康检查和非流式对话
python3 scripts/test_llm.py

# 开启流式验证 (可选)
python3 scripts/test_llm.py --enable-stream
```

---

## ⚙️ 配置参数传递链条

本项目采用分层配置设计，确保参数从配置文件到运行时的一致性传递：

```
config/config.ini
    ↓ (ConfigLoader 读取)
backend/config.py (AppConfig 类)
    ↓ (注入到服务)
backend/llm_service.py (LLMService 类)
    ↓ (API 调用参数)
SGLang 推理引擎
```

### 关键配置参数

| 参数 | 配置文件位置 | 传递路径 | 说明 |
|------|-------------|----------|------|
| `model_path` | `[model]` | config.ini → ConfigLoader → LLMService → SGLang `--model-path` | 模型路径，支持本地路径或 ModelScope ID |
| `tensor_parallel_size` | `[sglang]` | config.ini → start_sglang.sh → SGLang `--tp-size` | 张量并行大小 |
| `reasoning_parser` | `[model]` | config.ini → ConfigLoader → LLMService → SGLang `--reasoning-parser` | 推理解析器类型 (glm45/glm47 等) |
| `chat_template` | `[model]` | config.ini → ConfigLoader → LLMService → SGLang `--chat-template` | 对话模板格式 |
| `tool_call_parser` | `[model]` | config.ini → ConfigLoader → LLMService → SGLang `--tool-call-parser` | 工具调用解析器 |
| `timeout` | `[inference]` | config.ini → ConfigLoader → LLMService → httpx timeout | 推理请求超时时间 (秒) |
| `temperature` | `[inference]` | config.ini → ConfigLoader → LLMService → API 请求体 | 生成温度参数 |
| `top_p` | `[inference]` | config.ini → ConfigLoader → LLMService → API 请求体 | Top-p 采样参数 |
| `max_tokens` | `[inference]` | config.ini → ConfigLoader → LLMService → API 请求体 | 最大生成 token 数 |

### 配置修改流程

1. **编辑配置文件**: 修改 `config/config.ini` 中的参数
2. **重启服务**: 参数变更需要重启后端服务生效
3. **验证生效**: 通过 API 测试或日志确认参数已正确传递

**注意**: 模型相关参数 (model_path, reasoning_parser, tensor_parallel_size 等) 在 SGLang 启动时读取，重启 SGLang 服务后生效。推理参数 (temperature, top_p 等) 可在运行时动态调整。

---

## 📁 项目结构

```
LLM-deployment/
├── .sglang/               # uv 虚拟环境 (git ignored)
├── backend/               # FastAPI 后端实现
│   ├── api/               # API 路由 (支持推理过程提取)
│   ├── llm_service.py     # SGLang 服务对接
│   └── main.py            # 后端入口
├── frontend/              # Next.js 前端界面
├── scripts/               # 部署与测试脚本
│   ├── start_sglang.sh    # SGLang 启动脚本 (含 GLM-4.7-Flash 优化)
│   ├── start_backend.sh   # 后端启动脚本 (含环境激活)
│   ├── start_frontend.sh  # 前端启动脚本 (自动安装依赖并启动)
│   └── test_llm.py        # 自动化测试工具
├── logs/                  # 日志文件目录 (自动生成)
└── config/                # 配置文件目录
```

---

## 📄 许可证

MIT License
