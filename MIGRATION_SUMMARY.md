# SGLang 迁移总结

## 完成的工作

### 1. 代码修改 ✓

#### 后端代码更新
- **文件**: `backend/llm_service.py`
- **修改内容**:
  - ✅ 添加了 `SGLangProvider` 类，通过 HTTP API 调用 SGLang 推理服务器
  - ✅ 删除了 `VLLMProvider` 和 `TensorRTLLMProvider` 类的实现  
  - ✅ 更新了 `LLMService.initialize()` 方法，默认使用 SGLang 后端

#### 配置文件
- **文件**: `config/config.ini`
- **当前配置**: `backend = sglang`

### 2. 测试脚本创建 ✓

创建了完整的测试脚本套件：

#### Python 测试脚本
- **文件**: `scripts/test_sglang_api.py`
- **功能**:
  - 健康检查（/health 端点）
  - 模型列表获取（/v1/models）
  - 非流式聊天补全测试
  - 流式聊天补全测试
  - 详细的彩色终端输出
  - 自动错误诊断

#### Bash 测试脚本
- **文件**: `scripts/test_sglang.sh`  
- **功能**:
  - 自动调用 Python 测试脚本
  - 将测试输出保存到 `logs/` 目录（带时间戳）
  - 提供故障排查建议

#### SGLang 启动脚本
- **文件**: `scripts/start_sglang.sh`
- **功能**:
  - 自动检查模型路径
  - 下载 tiktoken 编码文件
  - 清理系统缓存
  - 启动 SGLang 服务器

### 3. 日志目录 ✓

- **目录**: `logs/`
- **用途**: 存储所有测试和编译日志，便于 AI Agent 自动分析
- **日志文件**:
  - `sglang_test_*.log` - API 测试日志
  - `sgl_kernel_build_*.log` - 编译日志

### 4. SGLang 环境配置 ✓

#### 依赖安装状态
- ✅ Python 3.12.3 虚拟环境（`.sglang/`）
- ✅ CMake 3.31.1（用于编译）
- ✅ libnuma-dev 和 ninja-build（系统依赖）
- ✅ sglang 0.5.7
- ✅ torch 2.10.0+cu130
- ✅ flashinfer-python 0.5.3

#### 正在进行的任务
- 🔄 **正在编译 sgl-kernel**（从源码）
  - 进程 ID: 38928
  - 日志文件: `logs/sgl_kernel_build_final.log`
  - 预计时间: 10-30 分钟
  - 编译选项:
    - CUDA 架构: SM100A (Blackwell)
    - 启用: BF16, FP8, FP4
    - 禁用: FA3, SM90A, Below SM90

## 为什么需要从源码编译 sgl-kernel？

预编译的 `sgl-kernel` wheel 不包含 `common_ops` 模块，该模块对于 Jetson Thor (Blackwell架构) 是必需的。从源码编译可以：

1. 启用 SM100A (Blackwell) 架构支持
2. 包含所有必需的算子和内核
3. 针对 CUDA 13.0 进行优化
4. 支持 NVFP4 量化格式

## 编译进度监控

### 检查编译是否完成
```bash
# 检查进程状态
ps aux | grep 38928

# 查看实时编译日志
tail -f /home/zk/Project/LLM-deployment/logs/sgl_kernel_build_final.log

# 使用监控脚本
bash /home/zk/Project/LLM-deployment/scripts/monitor_build.sh
```

### 编译成功后的步骤

1. **安装编译好的 wheel**:
```bash
cd /home/zk/Project/LLM-deployment
source .sglang/bin/activate
uv pip install --force-reinstall sglang/sgl-kernel/wheels/sgl_kernel-*.whl
```

2. **验证安装**:
```bash
python -c "from sgl_kernel import common_ops; print('✓ common_ops 模块导入成功')"
```

3. **下载 tiktoken 编码文件**:
```bash
mkdir -p ~/tiktoken_encodings
wget -O ~/tiktoken_encodings/o200k_base.tiktoken \
  "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
wget -O ~/tiktoken_encodings/cl100k_base.tiktoken \
  "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
```

4. **清理内存并启动 SGLang 服务器**:
```bash
sudo sysctl -w vm.drop_caches=3

# 启动服务器
source /home/zk/Project/LLM-deployment/.sglang/bin/activate
python3 -m sglang.launch_server \
  --model-path openai/gpt-oss-120b \
  --host 0.0.0.0 \
  --port 30000 \
  --reasoning-parser gpt-oss \
  --tool-call-parser gpt-oss
```

5. **运行测试脚本**:
```bash
# 在另一个终端中
bash /home/zk/Project/LLM-deployment/scripts/test_sglang.sh
```

6. **启动后端服务**:
```bash
cd /home/zk/Project/LLM-deployment
source .sglang/bin/activate
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

7. **启动前端**:
```bash
cd /home/zk/Project/LLM-deployment/frontend
npm run dev
```

## 关键的 Lesson Learned（来自 NVIDIA 论坛）

1. **环境隔离**: 使用 `uv` 和 Python 3.12 虚拟环境，避免与系统 Python 冲突
2. **CUDA 架构**: Jetson Thor 必须设置 `TORCH_CUDA_ARCH_LIST=11.0a`
3. **内存管理**: 加载 120B 模型前必须清理 UMA 内存
4. **编码文件**: 预先下载 tiktoken 编码文件到本地，避免运行时网络问题
5. **推理参数**: gpt-oss-120b 需要特定的 parser 配置
6. **源码编译**: 预编译的 wheel 不适用于 Blackwell，必须从源码编译

## 架构更改

### 旧架构（vLLM/TensorRT-LLM）
```
前端 (3000) → 后端 FastAPI (8000) → vLLM/TensorRT-LLM (本地加载模型)
```

### 新架构（SGLang）
```
前端 (3000) → 后端 FastAPI (8000) → SGLang Server (30000) → GPU
```

### 优势
- 更好的推理性能（专为长上下文和 NVFP4 优化）
- 更灵活的部署（服务器可以独立重启）
- 更稳定的内存管理
- 支持 gpt-oss-120b 的特殊推理模式

## 故障排查

### 如果编译失败
- 检查日志: `cat logs/sgl_kernel_build_final.log`
- 确认 CUDA 环境: `nvcc --version`
- 确认 CMake 版本: `cmake --version` (需要 >= 3.18)
- 确认编译依赖: `apt list --installed | grep -E "(libnuma-dev|ninja-build)"`

### 如果 SGLang 服务无法启动
- 检查端口占用: `netstat -tulpn | grep 30000`
- 检查模型路径是否正确
- 检查 tiktoken 编码文件是否存在

### 如果测试脚本失败
- 确认 SGLang 服务正在运行: `ps aux | grep sglang`
- 检查服务日志
- 尝试手动 curl 测试: `curl http://localhost:30000/v1/models`

## 文件清单

### 新增/修改的文件
- ✅ `backend/llm_service.py` - 核心后端逻辑
- ✅ `scripts/test_sglang_api.py` - Python 测试脚本
- ✅ `scripts/test_sglang.sh` - Bash 测试脚本
- ✅ `scripts/start_sglang.sh` - SGLang 启动脚本
- ✅ `scripts/monitor_build.sh` - 编译监控脚本
- ✅ `logs/` - 日志目录

### 配置文件
- ✅ `config/config.ini` - 已更新 backend 配置为 sglang

## 下一步行动

等待 sgl-kernel 编译完成后：
1. 安装编译好的 wheel
2. 验证 common_ops 模块
3. 启动 SGLang 服务器
4. 运行自动化测试
5. 验证前端集成
