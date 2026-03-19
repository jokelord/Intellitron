# EngineCore cuBLAS CUBLAS_STATUS_NOT_INITIALIZED 故障分析

## 症状

vLLM 启动 Qwen3.5-35B-A3B-FP8 (VLM) 时，EngineCore 子进程在 profiling 阶段崩溃：

```
File ".../vllm/model_executor/layers/conv.py", line 226, in _forward_mulmat
    x = F.linear(
RuntimeError: CUDA error: CUBLAS_STATUS_NOT_INITIALIZED when calling
`cublasLtMatmulAlgoGetHeuristic(...)`
```

崩溃位置：`patch_embed.proj` (Conv3dLayer) → `F.linear()` → cuBLAS matmul heuristic 查询失败。

## 平台

- **设备**: NVIDIA Jetson Thor (SM 110 / Blackwell, UMA 架构, 132GB)
- **PyTorch**: 2.10.0+cu130
- **CUDA**: 13.0
- **vLLM**: 0.17.1+cu130 (源码编译)
- **Python**: 3.10

## 排查过程

### 1. 独立 cuBLAS 测试 — 正常

在独立 Python 进程中 `F.linear(16384, 1176)` 在 float16/float32/bfloat16 下全部通过，说明 cuBLAS + SM110 本身兼容。

### 2. multiprocessing fork 测试 — 复现

```python
# 父进程先初始化 CUDA
_ = torch.randn(1, device='cuda')
# fork 子进程尝试 F.linear
p = mp.Process(target=worker_test)  # fork
# → "Cannot re-initialize CUDA in forked subprocess"
```

fork 后子进程继承父进程 CUDA 状态，cuBLAS 句柄失效 → `CUBLAS_STATUS_NOT_INITIALIZED`。

### 3. vLLM spawn 检测机制分析

vLLM 内置了 `_maybe_force_spawn()` 安全机制（`vllm/utils/system_utils.py`）：

```python
def _maybe_force_spawn():
    if cuda_is_initialized():        # ← torch.cuda.is_initialized()
        os.environ["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"
```

但该检测只调用 `torch.cuda.is_initialized()`，仅跟踪 **PyTorch 层面**的初始化。  
flash_attn / vLLM C 扩展在 import 阶段通过 **C driver API** 直接初始化 CUDA 上下文，  
绕过了 PyTorch 检测 → `_maybe_force_spawn()` 未触发 → 仍用 fork。

## 根因

| 环节 | 说明 |
|------|------|
| vLLM 默认 | `VLLM_WORKER_MULTIPROC_METHOD=fork` (Linux 默认) |
| 父进程 (APIServer) | import flash_attn / vLLM 扩展 → C 层隐式初始化 CUDA driver |
| PyTorch 检测 | `torch.cuda.is_initialized()` → False (未跟踪 C 层初始化) |
| `_maybe_force_spawn()` | 未触发，仍用 fork |
| 子进程 (EngineCore) | 继承失效的 cuBLAS 句柄 |
| 崩溃 | `F.linear()` → `cublasLtMatmulAlgoGetHeuristic` → `CUBLAS_STATUS_NOT_INITIALIZED` |

## 修复

在 `scripts/start_vllm.sh` 中添加环境变量，强制 spawn：

```bash
export VLLM_WORKER_MULTIPROC_METHOD=spawn
```

spawn 让子进程以全新 Python 解释器启动，从零初始化 CUDA/cuBLAS 上下文，不存在状态继承问题。

## 同时修复的次要问题

- `LD_LIBRARY_PATH` 中 `cuda-13.0` 与 `CUDA_HOME=cuda-13.1` 版本不一致（已统一为 13.0）
- `max_image_tokens = 16384` 在 profiling 阶段占用大量显存（config.ini 已有，可按需降低）

---

## 第二次故障：Conv3d custom op dispatch 导致 cuBLAS 失败 (2026-03-13)

### 症状

设置 `VLLM_WORKER_MULTIPROC_METHOD=spawn` 后，EngineCore 子进程仍在同一位置崩溃：
`patch_embed.proj` (Conv3dLayer) → `forward_native` → `_forward_mulmat` → `F.linear()` → `CUBLAS_STATUS_NOT_INITIALIZED`

### 根因

| 环节 | 说明 |
|------|------|
| 编译配置 | `custom_ops = ['+quant_fp8', 'none', '+quant_fp8']`，`none` 使自定义 ops 默认禁用 |
| conv3d op | 未被显式启用 (`+conv3d` 不在列表中) |
| dispatch_forward | `enabled=False` → 返回 `forward_native` 而非 `forward_cuda` |
| forward_native | `enable_linear=True` → `_forward_mulmat()` → `F.linear()` → cuBLAS |
| cuBLAS 失败 | SM110 上 `cublasLtMatmulAlgoGetHeuristic` 返回 `CUBLAS_STATUS_NOT_INITIALIZED` |

而 `forward_cuda` 在 PyTorch ≥ 2.10 上走 `_forward_conv()` → `F.conv3d()` → cuDNN，完全绕开 cuBLAS。

### 修复

补丁位于 `.vllm/lib/python3.10/site-packages/vllm/model_executor/layers/conv.py`：

```python
# ConvLayerBase.__init__() 中：
super().__init__(enforce_enable=True)  # 原为 super().__init__()
```

`enforce_enable=True` 强制 `dispatch_forward` 走 `forward_cuda` 路径。
这是 vLLM 为 ViT/多模态编码器设计的机制（`compile_mm_encoder=False` 时 MM 编码器不参与 inductor 编译，
但 custom_ops 的全局 `none` 默认值仍会影响到 MM 编码器内部的 Conv 层）。

### 与第一次故障的关系

- 第一次故障（fork）：spawn 修复解决了子进程继承失效 CUDA 句柄的问题
- 第二次故障（dispatch）：spawn 已生效，cuBLAS 在全新子进程中仍然失败，
  原因是 SM110 上 `F.linear()` 在特定 tensor shape/context 下触发 cuBLAS 初始化失败，
  但 `F.conv3d()` (cuDNN) 路径正常工作

---

## 第三次故障：视觉编码器 Linear 层 cuBLASLt 失败 (2026-03-13)

### 症状

Conv3d 修复后，崩溃点后移到视觉编码器 transformer block 的 attention QKV 投影：
```
qwen3_vl.py:576 → blk() → attn() → self.qkv(x)
→ linear.py:576 → quant_method.apply()
→ linear.py:222 → dispatch_unquantized_gemm()
→ utils.py:98 → F.linear(x, weight, bias)
→ RuntimeError: CUBLAS_STATUS_NOT_INITIALIZED
```

### 根因

| 环节 | 说明 |
|------|------|
| 视觉编码器 | 权重为 bfloat16 未量化（FP8 仅量化 LLM 主干） |
| QKV 层 | `QKVParallelLinear` → `dispatch_unquantized_gemm()` → `default_unquantized_gemm()` |
| `F.linear(x, w, bias)` | 当 bias ≠ None 时，PyTorch 内部走 `gemm_and_bias()` → **无条件使用 cuBLASLt** |
| `preferred_blas_library` | 默认值 `Cublas`，但 `gemm_and_bias` 路径不受此设置影响 |
| cuBLASLt 失败 | SM110 上 `cublasLtMatmulAlgoGetHeuristic` → `CUBLAS_STATUS_NOT_INITIALIZED` |

**关键发现**：`torch.preferred_blas_library("cublas")` 仅控制 `gemm()` 函数（无 bias 的 matmul），
但 `F.linear(x, w, bias)` 当 bias ≠ None 时走 `addmm` → `gemm_and_bias()` → 始终使用 cuBLASLt，
无法通过任何设置避开。必须在源码层面拆分 matmul 和 bias 加法。

### 修复

**补丁 1** — `.vllm/.../model_executor/layers/utils.py`：
```python
def default_unquantized_gemm(layer, x, weight, bias=None):
    # 拆分 matmul 和 bias，避开 cuBLASLt 的 gemm_and_bias 路径
    out = x.matmul(weight.t())   # → legacy cuBLAS (cublasGemmEx)
    if bias is not None:
        out = out + bias          # → 逐元素加法，不涉及 cuBLAS
    return out
```

**补丁 2** — `.vllm/.../v1/worker/gpu_worker.py`：
```python
# init_device() 中 set_device 后：
try:
    _w = torch.empty(8, 8, device=self.device, dtype=torch.bfloat16)
    torch.mm(_w, _w)  # 强制创建 cuBLAS 句柄
    del _w
except RuntimeError:
    pass
```

**保留的补丁** — `conv.py`：`enforce_enable=True`（第二次故障的修复仍然需要）

### 备份位置

`patch/vllm_backups_20260313_054105/` 包含所有三个文件的原始版本

---

## 第四次故障：3D 张量 + M=65536 触发 cuBLAS batched 路径崩溃 (2026-03-13~14)

### 排查过程

经过多轮迭代排查：

1. **bf16→fp16 转换** — 失败（`CUDA_R_16F` 也 INVALID_VALUE）
2. **fp16→fp32 转换** — 失败（`cublasSgemm` 也 INVALID_VALUE）
3. **独立维度测试** — 所有维度、所有 dtype、所有 API **全部通过**
4. **加同步屏障** — 确认非延迟错误，错误确实来自 matmul 本身

### 关键诊断日志

```
Conv3dLayer.forward_cuda: F.conv3d OK, output=[65536, 1152, 1, 1, 1]
GEMM #1: x=[65536, 1, 1152] stride=[1152, 1152, 1] dtype=torch.bfloat16,
         weight=[3456, 1152] bias=[3456] contig=(True,True)
```

### 根因

| 环节 | 说明 |
|------|------|
| Conv3d 输出 | `[65536, 1152, 1, 1, 1]` → reshape → `[65536, 1, 1152]` **（3D 张量）** |
| 传入 QKV | `self.qkv(x)` 其中 x shape `[65536, 1, 1152]` |
| F.linear / matmul | **3D 输入触发 batched cuBLAS 路径**（65536 个 batch，每个 [1,1152]×[1152,3456]） |
| SM110 cuBLAS | batched cublasGemmEx 对 bf16 + `CUBLAS_GEMM_DEFAULT_TENSOR_OP` 返回 `INVALID_VALUE` |
| 独立测试通过 | 因为测试只用了 **2D** 张量 `[M, K]`，走标准非 batched 路径 |

**为什么 65536？** max_image_tokens = 16384，但 conv3d 输出的 patch 数 = 16384 × spatial_merge_size² = 16384 × 4 = 65536。合并（merger）发生在 transformer blocks 之后。

**为什么 3D？** conv3d 输出 `[B, C, T, H, W]` reshape 为 `[seq_len, batch_size, hidden]` 格式（`[65536, 1, 1152]`），这是 ViT 的标准 `[S, B, C]` layout。

### 修复

**补丁 1（最终版）** — `.vllm/.../model_executor/layers/utils.py`：
```python
def default_unquantized_gemm(layer, x, weight, bias=None):
    orig_shape = x.shape
    # Flatten to 2D: [*, K] → [M, K] — avoids batched cuBLAS path on SM110
    if x.dim() > 2:
        x = x.reshape(-1, x.shape[-1])
    out = torch.mm(x.contiguous(), weight.t().contiguous())
    # Restore original batch dimensions
    if len(orig_shape) > 2:
        out = out.reshape(*orig_shape[:-1], out.shape[-1])
    if bias is not None:
        out = out + bias
    return out
```

核心思路：
- `x=[65536, 1, 1152]` → `reshape(-1, 1152)` → `[65536, 1152]`（2D）
- `torch.mm([65536, 1152], [1152, 3456])` → 标准 cublasGemmEx（非 batched）
- 独立测试已验证此路径对所有 dtype 均工作正常
- 手动分离 bias 加法，避开 cuBLASLt 的 `gemm_and_bias`

**保留的补丁**：
- `conv.py`：`enforce_enable=True`（第二次故障的修复仍然需要）
- `gpu_worker.py`：cuBLAS 句柄预热（保险策略）

### 与之前故障的关系

| 故障 | API | 错误 | 原因 | 修复 |
|------|-----|------|------|------|
| #1 fork | 全部 | NOT_INITIALIZED | fork 继承失效句柄 | spawn |
| #2 conv3d | cuBLASLt | NOT_INITIALIZED | dispatch 走 forward_native | enforce_enable=True |
| #3 QKV+bias | cuBLASLt gemm_and_bias | NOT_INITIALIZED | F.linear(x,w,bias) 强制 cuBLASLt | matmul+bias 拆分 |
| #4 QKV 3D tensor | cublasGemmEx batched | INVALID_VALUE | 3D [65536,1,1152] 触发 batched 路径 | **reshape 2D + torch.mm** |

---

## 第五次故障：2D torch.mm 分块后仍然 INVALID_VALUE (2026-03-14)

### 症状

在第四次修复后，调用链保持不变，但报错点变为 `torch.mm`：

```
qwen3_vl.py:576 → blk() → attn() → self.qkv(x)
→ linear.py:222 → dispatch_unquantized_gemm()
→ utils.py:127 → chunks.append(torch.mm(chunk, weight_t))
→ RuntimeError: CUBLAS_STATUS_INVALID_VALUE
```

当时采用的修复是把 `M=65536` 的大矩阵按行切块，例如分成 4 个 `16384 × 1152` 的 chunk，
分别执行：

```python
torch.mm(chunk, weight_t)
```

但即使 chunk 大小降到 16384，`cublasGemmEx` 仍然返回 `INVALID_VALUE`。

### 根因修正

这说明第四次故障中的“`M=65536` 过大”并不是唯一条件。更准确地说：

| 环节 | 说明 |
|------|------|
| 输入 | 已经从 3D `[65536,1,1152]` 成功 flatten 为 2D `[65536,1152]` |
| 分块 | `[65536,1152]` 被切成多个 `[16384,1152]` 子矩阵 |
| API | 每个 chunk 仍调用 `torch.mm()` → `cublasGemmEx` |
| dtype | `bfloat16` |
| 结果 | **无论是否分块，SM110 上 bf16 `cublasGemmEx` 都返回 `INVALID_VALUE`** |

因此，真正的问题不是 “batched path + 大 M”，而是：

**在 EngineCore 子进程当前上下文中，SM110 上的 bf16 cuBLAS GEMM 路径本身就不可靠。**

### 结论

- 第四次故障中的 `3D → 2D flatten` 仍然是必要的，它成功绕开了 batched cuBLAS 路径
- 但 flatten 之后进入的普通 `cublasGemmEx`（bf16）路径依然会失败
- 仅靠 “切小矩阵” 不能解决问题，必须进一步绕开 bf16 cuBLAS GEMM

---

## 第六次故障：bf16→fp32 提升后 cublasSgemm 仍失败 (2026-03-14)

### 症状

为了绕开 `CUDA_R_16BF` 路径，曾尝试在 `default_unquantized_gemm()` 中做 dtype 提升：

```python
out = torch.mm(x.float(), weight.float().t()).to(torch.bfloat16)
```

预期这会走 `cublasSgemm`（fp32），避开 bf16 的 `cublasGemmEx`。

但新的崩溃日志显示：

```
utils.py:118 → torch.mm(x.float(), weight.float().t())
→ RuntimeError: CUDA error: CUBLAS_STATUS_INVALID_VALUE when calling
`cublasSgemm(...)`
```

### 根因再次收敛

| 尝试路径 | 后端 API | 结果 |
|------|------|------|
| `F.linear(x, w, bias)` | cuBLASLt `gemm_and_bias` | `NOT_INITIALIZED` |
| `x.matmul(w.t())` / `torch.mm(bf16)` | `cublasGemmEx` + `CUDA_R_16BF` | `INVALID_VALUE` |
| `torch.mm(fp32)` | `cublasSgemm` | `INVALID_VALUE` |

这一步非常关键，因为它证明：

**问题已经不是某一种 dtype 或某一个 GEMM kernel 的 bug，而是 EngineCore 子进程中的 cuBLAS 路径整体不可信。**

换句话说：

- 不是只有 cuBLASLt 坏掉
- 不是只有 bf16 GEMM 坏掉
- 连 fp32 的 `cublasSgemm` 也会在这个上下文中失败

因此结论升级为：

> 对视觉编码器未量化 Linear 层，必须完全绕过 cuBLAS。

---

## 第七次故障：Triton bundled ptxas-blackwell 不支持 sm_110a (2026-03-14)

### 目标

既然 cuBLAS 全线不可靠，下一步是改用 Triton 自定义 GEMM kernel，直接生成 GPU kernel，完全不经过 cuBLAS。

### 初始阻塞

在独立测试脚本 `test/test_triton_gemm.py` 中编写 Triton matmul 后，首次编译失败：

```
Internal Triton PTX codegen error
ptxas-blackwell fatal: Value 'sm_110a' is not defined for option 'gpu-name'
```

复现命令显示 Triton 使用的是内置工具：

```
.../site-packages/triton/backends/nvidia/bin/ptxas-blackwell --gpu-name=sm_110a
```

### 根因

进一步检查发现：

| 项目 | 值 |
|------|------|
| Triton 版本 | `3.6.0` |
| bundled `ptxas-blackwell` | 版本 `12.9` |
| 系统 CUDA ptxas | `/usr/local/cuda-13.0/bin/ptxas` |
| 系统 ptxas 版本 | `CUDA 13.0.48` |

Triton 3.6.0 自带的 `ptxas-blackwell` 是基于 CUDA 12.9 打包的，
**不认识 `sm_110a` 这个目标架构**。

因此 Triton 方案本身没有问题，真正的问题是 **编译器路径错误**。

### 修复

在启动脚本 `scripts/start_vllm.sh` 中添加并统一如下环境变量：

```bash
export TRITON_PTXAS_PATH=/usr/local/cuda-13.0/bin/ptxas
export TRITON_PTXAS_BLACKWELL_PATH=/usr/local/cuda-13.0/bin/ptxas
```

其中：

- `TRITON_PTXAS_PATH` 供通用 NVIDIA Triton 编译流程使用
- `TRITON_PTXAS_BLACKWELL_PATH` 专门覆盖 Triton 在 `arch >= 100` 时默认选择的 `ptxas-blackwell`

### 验证

使用独立脚本 `test/test_triton_gemm.py` 做验证，以下场景全部通过：

```text
Test 1: [256, 1152] x [1152, 3456]     PASSED
Test 2: [65536, 1152] x [1152, 3456]   PASSED
Test 3: [65536, 1152] x [1152, 4608]   PASSED
All Triton GEMM tests passed!
```

其中 `Test 2` 正是此前 cuBLAS 100% 崩溃的关键视觉编码器 QKV 场景。

---

## 最终修复：未量化视觉 Linear 层改为 Triton GEMM，彻底绕过 cuBLAS (2026-03-14)

### 最终根因总结

经过连续多轮排查，问题已经从最初的“fork 继承失效句柄”逐步收敛为两个层次：

| 层次 | 问题 |
|------|------|
| 进程层 | fork 会继承无效 CUDA/cuBLAS 状态，需要强制 `spawn` |
| 算子层 | 即使在 spawn 子进程内，SM110 上视觉编码器未量化线性层涉及的 cuBLAS/cuBLASLt 路径仍然不可靠 |

最终可归纳为：

1. `F.linear(x, w, bias)` 会走 cuBLASLt `gemm_and_bias`，失败
2. `torch.mm(bf16)` 会走 `cublasGemmEx(CUDA_R_16BF)`，失败
3. `torch.mm(fp32)` 会走 `cublasSgemm`，仍失败
4. 只有 **完全绕开 cuBLAS** 才能稳定执行这些 GEMM

### 最终补丁

#### 补丁 1 — `scripts/start_vllm.sh`

```bash
export TRITON_PTXAS_PATH=/usr/local/cuda-13.0/bin/ptxas
export TRITON_PTXAS_BLACKWELL_PATH=/usr/local/cuda-13.0/bin/ptxas
```

作用：确保 Triton 在 Blackwell / SM110a 上使用系统 CUDA 13.0 的 ptxas，而不是 Triton 自带的 12.9 `ptxas-blackwell`。

#### 补丁 2 — `.vllm/.../model_executor/layers/utils.py`

将 `default_unquantized_gemm()` 改为：

- 保留 `3D → 2D flatten`
- 保留 bias 分离加法
- 对未量化 Linear 的 GEMM 使用 Triton 自定义 `_sm110_mm_kernel`
- 仅在 Triton 不可用时才回退到 `torch.mm`

核心思路如下：

```python
if x.dim() > 2:
    x = x.reshape(-1, x.shape[-1])

out = _triton_mm(x.contiguous(), weight.t().contiguous())

if len(orig_shape) > 2:
    out = out.reshape(*orig_shape[:-1], out.shape[-1])
if bias is not None:
    out = out + bias
```

Triton kernel 内部使用 fp32 累加，并根据输入 dtype 回写 bf16/fp16，
因此既能保证数值稳定性，也完全避开 cuBLAS/cuBLASLt。

### 影响范围

| 模块 | 影响 |
|------|------|
| 视觉编码器未量化 QKV / MLP Linear | 改为 Triton GEMM |
| LLM 主干 FP8 量化 Linear | 不受影响，仍走原有 FP8 路径 |
| Conv3d patch embed | 仍依赖 `conv.py` 中 `enforce_enable=True` 修复 |
| 进程模型 | 仍依赖 `VLLM_WORKER_MULTIPROC_METHOD=spawn` 修复 |

### 最终保留的修复集合

| 故障 | API / 机制 | 错误 | 根因 | 最终修复 |
|------|------|------|------|------|
| #1 fork | multiprocessing fork | NOT_INITIALIZED | 子进程继承失效 CUDA/cuBLAS 状态 | `VLLM_WORKER_MULTIPROC_METHOD=spawn` |
| #2 conv3d dispatch | Conv3dLayer `forward_native` | NOT_INITIALIZED | MM encoder Conv 层未启用 custom op | `super().__init__(enforce_enable=True)` |
| #3 QKV+bias | cuBLASLt `gemm_and_bias` | NOT_INITIALIZED | `F.linear(x,w,bias)` 强制走 cuBLASLt | 拆分 matmul + bias |
| #4 3D tensor | batched `cublasGemmEx` | INVALID_VALUE | `[65536,1,1152]` 触发 batched GEMM | flatten 3D → 2D |
| #5 2D bf16 mm | `cublasGemmEx(CUDA_R_16BF)` | INVALID_VALUE | 普通 bf16 cuBLAS GEMM 在子进程中仍不可靠 | 放弃 cuBLAS |
| #6 2D fp32 mm | `cublasSgemm` | INVALID_VALUE | 连 fp32 cuBLAS GEMM 也不可靠 | 放弃 cuBLAS |
| #7 Triton 编译 | bundled `ptxas-blackwell` | 编译失败 | CUDA 12.9 `ptxas-blackwell` 不支持 `sm_110a` | `TRITON_PTXAS_BLACKWELL_PATH=/usr/local/cuda-13.0/bin/ptxas` |
| **最终方案** | Triton 自定义 GEMM | 正常 | 彻底绕过 cuBLAS/cuBLASLt | **Triton GEMM + 系统 ptxas + spawn + conv3d 修复** |

### 备份与测试文件

- 原始备份目录：`patch/vllm_backups_20260313_054105/`
- 新增测试脚本：`test/test_triton_gemm.py`
- 新版 `utils.py` 备份：`patch/vllm_backups_20260313_054105/utils.py.bak_v5_triton`
