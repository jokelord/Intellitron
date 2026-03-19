# AGENT.md — LLM-Deployment 工程日志

> 本文档记录本项目的平台背景、历次排障结论与关键决策，供后续 AI Agent / 协作者快速上下文。

---

## 平台与模型

| 项目 | 值 |
|------|-----|
| 硬件 | NVIDIA Jetson AGX Thor（Blackwell SM110，ARM64，UMA 96GB） |
| 模型 | Qwen3-VL-30B-A3B-Instruct-FP8 |
| 推理引擎 | SGLang（端口 30000） |
| 后端框架 | FastAPI（端口 8000） |
| 前端框架 | Next.js + TypeScript（端口 3000） |
| 虚拟环境 | `/home/zk/Project/LLM-deployment/.sglang/` |
| 权重路径 | `/home/zk/Project/LLM-weight/Qwen3-VL-30B-A3B-Instruct-FP8/` |

### SM110 特殊注意事项

- `attention_backend` 必须用 `triton`，FlashInfer 暂不支持 SM110
- `moe_runner_backend` 必须用 `triton`（CUTLASS FP4 内核不支持 SM110）
- `fp8_gemm_backend` 用 `triton`（cutlass 在 SM110 不可用）

---

## Problem Shooting：VLM TTFT 过长（~100s → 目标 ~15s）

### 根本原因分析

```
现象: 上传 2560×1600 PNG/JPEG → SGLang 首帧耗时 100.97s
根因: 2560×1600 = 4,096,000 像素 → visual_tokens ≈ 4,096,000 / 784 ≈ 5224 token
      Jetson Thor prefill 速度 ~52 tok/s → prefill 耗时 ≈ 5224 / 52 ≈ 100s
      核心公式: TTFT ≈ visual_tokens / prefill_throughput
```

**secondary issue**：旧图片处理链有 4 次 Base64 编解码：

```
旧链路:
  前端 FileReader(b64 ①) → canvas.toDataURL(b64 ②, resize 768px)
  → JSON HTTP → 后端 b64_decode(③) → PIL → b64_encode(④) → SGLang

新链路:
  前端 FormData 原始字节 → HTTP multipart
  → 后端 BytesIO → smart resize → JPEG 透传/快速编码 → b64_encode(① 唯一一次) → SGLang
```

---

### 解决方案（已实施）

#### 1. 二进制上传端点 `/v1/process-image-binary`

- **文件**: `backend/api/chat.py`
- **方式**: FastAPI `UploadFile` + `Form`，multipart/form-data
- **前端（文件上传）**: `ImageUploader.tsx` → `api.ts` 中 `LLMClient.processImageBinary(file, format='auto')` 使用 `FormData`，不设 `Content-Type`（浏览器自动设 multipart boundary）
- **前端（拍照）**: `CameraCapture.tsx` 同样迁移至 `processImageBinary()`：`canvas.toBlob()` 获取原始 JPEG 字节 → 包装为 `File` 对象 → `FormData` 上传；降级时直接用 `canvas.toDataURL()` 而不走后端旧端点
- **跳过**: 前端 `FileReader` b64 编码 + 旧 `canvas.toDataURL` base64 中间格式全部省去
- **注意**: `CameraCapture.tsx` 在前端 canvas 阶段预缩至 `camera_max_dimension`（config.ini，默认 768px），后端 `max_image_dimension`（默认 1280px）不会二次触发缩放；而 `ImageUploader` 直接传原始文件，由后端统一缩到 `max_image_dimension`

#### 2. 后端智能缩放（最关键）

- **配置键**: `[model] max_image_dimension = 1280`（0 = 不缩放，适用于文件上传路径）
- **配置键**: `[model] camera_max_dimension = 768`（0 = 不缩放，适用于摄像头拍照路径，前端 canvas 预缩）
- **逻辑**: 若图片任一边 > 1280px，等比缩放到 1280px 以内（LANCZOS）
- **效果**: 2560×1600 → 1280×800，像素减少 75%，visual token 从 ~5224 → ~1305，TTFT 从 ~100s → ~25s
- **代码**: `chat.py` `process_image_binary` 第 3 阶段

#### 3. 智能格式选择（`auto` 模式）

- **JPEG 输入 + 无缩放** → **原始字节透传，0ms 重编码**
- **JPEG 输入 + 有缩放** → 快速 JPEG 重编码（~30ms，quality=85）
- **非 JPEG 输入**（PNG/WebP/...）→ 转 JPEG 编码（~30ms，quality=85）
- **避免**: 原 WebP quality=95 method=6 需 1461ms，节省 >1400ms

#### 4. SGLang `--image-max-pixels`（服务端双重保障）
NOTE BY HUMAN：the current version do not support this parameter; skip this charpter 

- **配置键**: `[sglang] image_max_pixels = 501760`（≈ 640 visual token）
- **配置键**: `[sglang] image_min_pixels = 100352`（Qwen3-VL 默认下界）
- **说明**: 在 SGLang visual encoder 内部再次限制像素预算，后端缩放 + SGLang 限制双保险
- **脚本**: `scripts/start_sglang.sh` 自动从 config.ini 读取并传递 `--image-max-pixels` / `--image-min-pixels`

#### 5. Visual Encoder FP16

- **配置键**: `[sglang] dtype = float16`
- **效果**: Jetson Thor Blackwell FP16 吞吐 > bfloat16，visual encoder（ViT，无 FP8 量化）prefill 加速 20-40%
- **安全性**: 仅影响 activations/计算精度；FP8 量化权重会自动 dequant 到 float16 参与运算；无梯度无数值稳定性风险

#### 调参速查

| `image_max_pixels` | visual tokens | 预估 TTFT（@52 tok/s） |
|--------------------|--------------|------------------------|
| 1,003,520          | ~1280         | ~25s                  |
| 589,824            | ~752          | ~14s（推荐平衡）        |
| 501,760            | ~640          | ~12s（当前配置）        |
| 250,880            | ~320          | ~6s（细节损失明显）      |

---

### 排障日志格式约定

所有图片处理端点统一使用如下格式打印 debug 日志（**不得删除，属于运维习惯**）：

```
[process-image-binary] ✅ 完成 | 总耗时: 45.3ms | [读取=0.5 + 解码=2.1 + 缩放=10.2 + 编码=28.4 + b64=4.1]
[process-image-binary] 缩放: 2560x1600 → 1280x800 (像素减少 75.0%) 耗时: 10.2ms
[process-image-binary] ⚡ JPEG 透传: 跳过 PIL 重编码, 直接使用原始字节, 耗时: ~0ms
[process-image-binary] ❌ 失败 | 耗时: 1.2ms | FileNotFoundError | 请检查图片文件是否损坏
```

---

## 关键文件速查

| 文件 | 作用 |
|------|------|
| `config/config.ini` | 所有参数单一来源，包含详细注释 |
| `backend/config.py` | `ModelConfig` dataclass + ini 解析；新增字段在此扩展 |
| `backend/api/chat.py` | 图片处理端点（`/v1/process-image` 旧版 + `/v1/process-image-binary` 新版）|
| `backend/llm_service.py` | SGLang provider，发送 data URL 到 SGLang |
| `frontend/src/lib/api.ts` | `LLMClient` 类，`processImageBinary()` FormData 上传；旧 `processImage()` 保留仅供参考 |
| `frontend/src/components/ImageUploader.tsx` | 图片上传 UI，主流程调用 `processImageBinary(file, 'auto')`；降级路径用 `FileReader` 直接读取 data URL |
| `frontend/src/components/CameraCapture.tsx` | 摄像头拍照 UI，已迁移至 `processImageBinary()`：`canvas.toBlob()` → `File` → FormData 上传；降级用 `canvas.toDataURL()` |
| `scripts/start_sglang.sh` | SGLang 启动脚本，自动从 config.ini 读取并注入所有参数 |
| `scripts/start_backend.sh` | FastAPI 后端启动脚本 |

---

## SGLang 参数注入机制

`start_sglang.sh` 通过 `grep` 从 `config.ini` 的 `[sglang]` 节读取参数并动态构造 `ARGS` 数组：

```bash
# 当前已支持的 config.ini → CLI 参数映射
dtype               → --dtype
image_max_pixels    → --image-max-pixels
image_min_pixels    → --image-min-pixels
attention_backend   → --attention-backend
moe_runner_backend  → --moe-runner-backend
mem_fraction_static → --mem-fraction-static
# ... 更多见脚本
```

**新增 SGLang 参数步骤**：
1. 在 `config.ini` `[sglang]` 节添加键值（带注释）
2. 在 `start_sglang.sh` grep 正则中加入新键名
3. 添加解析 + `[ -n "$VAR" ] && ARGS+=(--flag "$VAR")` 两行

---

## 已知限制 / 待办

- [ ] `image_max_pixels` / `image_min_pixels` 目前在 `config.ini` 中已注释（使用 SGLang 默认值），如需严格控制 token 数请取消注释
- [ ] `reasoning_parser` 和 `chat_template` 根据所用模型按需填写
- [ ] FP4 量化模型（NVFP4）需换用对应的 `config/` 目录下的专用 `.ini` 文件启动

---

## Problem Shooting：多轮 VLM 对话 TTFT 随轮次线性增长

### 根本原因

多轮对话时，前端将**全部历史消息**（含每轮的图片 `image_url`）发给后端。
第 N 轮请求携带 N 张图片 → visual tokens 线性增长 → TTFT ≈ N × 25s。

```
现象: 第1轮 0.58s (SGLang prefix cache 命中)，第2轮 50.93s (2张图, 2871 tokens)
根因: chatMessages 包含所有历史 user 消息的 image_url，未做剥离
```

### 解决方案（已实施）

**文件**: `backend/llm_service.py` → `_strip_old_images()`

- 在发送给 SGLang 前，扫描 `messages_data`，找到**最后一条 user 消息**
- 对所有更早的 user 消息，将 `image_url` 项替换为文字占位符 `[图片×N]`
- LLM 已在先前回复中描述过这些图片，文本上下文足以保持连续性
- **效果**: 每轮只发送当前图片，TTFT 恒定 ≈ 25s，不随轮次增长

### 附：第1轮 0.58s 的 "奇迹" 解释

SGLang 的 RadixAttention 前缀缓存机制：同一张图片 + 同一 system prompt 在上次调试中已进过 prefill，KV cache 未过期，因此命中缓存直接返回，表现为极快的 TTFT。第2轮因历史序列变化而 cache miss。

---

## CUDAGraph 与 ViT Encoder 说明

### CUDAGraph 当前状态

- **SM110 bug 根因**：CUDAGraph capture 阶段触发 Triton JIT 编译 → `sm_110a` ptxas 不支持 → 编译报错（见踩坑纪录.md Section 10/20）,目前已打补丁：patch/ptxas_wrapper.sh


### CUDAGraph 对 ViT Encoder 无效

- SGLang/vLLM 的 CUDAGraph 只捕获 **LLM decoder 的 decode 阶段**（固定 batch size 的纯 decode forward pass）
- ViT/visual encoder 因输入形状随图片分辨率动态变化，**从不在 CUDAGraph 捕获范围内**，始终以 eager 方式运行
- 因此"启用 CUDAGraph 加速 ViT encoder"的方案**在框架层面不可行**；SM110 bug 仅影响 LLM decode 阶段

### 已实施的 ViT TTFT 优化（完整列表）

| 优化 | 实现位置 | 效果 |
|------|----------|------|
| 图片缩小到 1280px（`max_image_dimension`，文件上传） | `config.ini` + `chat.py process_image_binary` | visual token ~1305，TTFT ~25s |
| 拍照预缩到 768px（`camera_max_dimension`，摄像头） | `config.ini` → 前端 `CameraCapture.tsx` canvas | 摄像头路径前端直接截断，减少传输量 |
| Visual Encoder FP16（`dtype=float16`） | `config.ini` | prefill +20-40% |
| 多轮对话历史图片剥离（`_strip_old_images`） | `backend/llm_service.py` | TTFT 不随轮次增长 |
| 前端二进制上传（去除 b64 往返） | `ImageUploader.tsx` + `CameraCapture.tsx` | 节省 ~68ms 编码开销（原 WebP 链路） |

**尚未实施**：并行 image patch 处理（vLLM 上游有 `vit_attn_wrappers.py` torch.compile，但与当前 SGLang 部署路径无关）

---
### 开启ViT CUDA Graph: 添加sglang attention backend白名单 以及 参考issue #16883 bug

（1）问题原因：SGLang 抛出 Not supported ViT attention backend，是因为开发团队在代码里写死了白名单（通常只信任 FlashInfer/FlashAttn 这种显式优化过显存指针的后端）。但 Triton 算子在很多情况下其实是兼容 CUDA Graph 的，只是 SGLang 官方没有做充分测试，为了安全直接给 Ban 了。
解决方案：在.sglang/lib/python3.12/site-packages/sglang/srt/layers/rotary_embedding.py 中注释掉如下装饰（0.5.8版本第2752行；0.5.9版本第3598行）
@torch.compile(dynamic=True, backend=get_compiler_backend())

（2）问题原因：SGLang 的底层在 vision.py 中对 apply_rotary_pos_emb 这个算子硬编码加上了 @torch.compile(dynamic=True) 装饰器。
当引擎处于 CUDA Graph 捕获阶段（cudaStreamCaptureStatusActive）时，这个被 torch.compile 包裹的函数首次执行触发了 TorchDynamo 的重编译（JIT 编译阶段）。在编译期间，Dynamo 试图获取当前的随机数生成器状态（get_rng_state），但这在 CUDA Graph 捕获期间是被 CUDA 驱动严格禁止的操作，从而导致了 InternalTorchDynamoError 崩溃。
解决方案：在.sglang/lib/python3.12/site-packages/sglang/srt/multimodal/vit_cuda_graph_runner.py 中加triton的白名单（第162行）
                # if override_backend == "triton_attn":
                if override_backend in ["triton_attn", "triton", "trt", "triton_attention"]:
                    cu_seq_len_ws = [cu_seqlens_now, cu_seqlens_kk_now, max_len]
                elif override_backend == "fa3":
                    cu_seq_len_ws = [cu_seqlens_now, max_len]
                else:
                    # raise RuntimeError("Not supported ViT attention backend")
                    cu_seq_len_ws = [cu_seqlens_now, cu_seqlens_kk_now, max_len]