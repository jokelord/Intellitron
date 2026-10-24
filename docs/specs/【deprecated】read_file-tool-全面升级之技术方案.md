
# 本地文档阅读 Tool 技术方案 (v5 Thor/SGLang 优化版)

> **适用硬件**: Jetson Thor (128GB 统一内存，Blackwell 架构)
> **设计主旨**: 精准显存隔离、拥抱官方 Cookbook 最佳实践、剥离冗余判定、无缝接入现有 Backend

## 1. 核心架构与解析流向

主打“专器专用”，不同格式直接分流到对应处理管线。

```text
[文档入口: file_path]
  │
  ├── 1. 结构化办公文档 (.doc, .docx, .xlsx, .ppt, .pptx)
  │    └── 唯一链路: markitdown 提取 + 本地 VLM （如现在的 qwen3.6）提取图片中的信息并精准插入 markitdown 文本中的正确位置
  │
  ├── 2. 标准文本型 PDF (.pdf)
  │    └── 唯一链路: markitdown 提取文字 + 本地 VLM （如现在的 qwen3.6）提取图片中的信息并精准插入 markitdown 文本中的正确位置
  │
  ├── 3. 扫描件 PDF (.pdf) —— [按需触发或前置解析为空时触发]
  │    └── 唯一链路: Paddle-OCR (SGLang 独立端口服务) 直出
  │
  └── 4. 纯图像文件 (.png, .jpg)
       └── 唯一链路: Paddle-OCR 直出

```

## 2. 引擎矩阵与硬件契合度分析

| 核心组件 | 处理格式 | Thor 专属调优策略 |
| --- | --- | --- |
| **markitdown** | 现代办公文档、文本 PDF | 保持原样。纯 Python 解析，对系统资源几乎零负担。 |
| **LibreOffice** | 老旧 `.doc` | 保持原样。后台静默转换。 |
| **Baidu Unlimited-OCR** | 图像、扫描件 PDF | **作为独立视觉服务运行**。<br>

<br>由于总参数仅 3B（FP16/BF16 权重约占用 6GB），利用官方定制的 R-SWA 算子，在 32K 上下文下显存极度平稳。将显存硬分配压缩至 10GB 以内，绝不挤兑同机部署的大参数 LLM。 |

## 3. 工具内部流转逻辑 (Python 伪代码，仅供参考)

*注：剥离了特定格式的 Return 字典结构，仅返回纯文本（Markdown），外层由你现有的 `read_file` Tool Wrapper 自行封装状态码。*

```python
import os
import subprocess
from pathlib import Path
import pymupdf
# 导入 markitdown 和你封装的 sglang 客户端 (指向 port 30001)

def core_document_parser(file_path: str, force_ocr: bool = False) -> str:
    """
    核心解析逻辑，仅输出原生 Markdown 文本
    """
    path = Path(file_path)
    ext = path.suffix.lower()

    # 1. 图像类 -> 直通 OCR 服务 (port: 30001)
    if ext in [".png", ".jpg", ".jpeg"]:
        return call_unlimited_ocr_via_sglang(file_path, port=30001)

    # 2. 老旧 doc -> 转换后交由 markitdown
    if ext == ".doc":
        docx_path = convert_doc_to_docx_via_libreoffice(path)
        return run_markitdown(docx_path)

    # 3. 现代办公文档 -> 直通 markitdown
    if ext in [".docx", ".xlsx", ".pptx"]:
        return run_markitdown(file_path)

    # 4. PDF 处理逻辑
    if ext == ".pdf":
        if force_ocr:
            # 明确走视觉 OCR（无视页数，直接全本丢入后端处理）
            image_stream = render_pdf_to_images_with_pymupdf(file_path)
            return call_unlimited_ocr_batch(image_stream, port=30001)
        else:
            # 优先用 markitdown 获取带标签的原生文本和表格结构
            text = run_markitdown(file_path)
            # 容错：如果文本极短/为空（通常意味着这是一份未加 OCR 标签的纯图 PDF）
            if not text or len(text.strip()) < 10:
                image_stream = render_pdf_to_images_with_pymupdf(file_path)
                return call_unlimited_ocr_batch(image_stream, port=30001)
            return text

```

## 4. 关键部署参数 (SGLang 双开隔离策略)

为了避免端口冲突（LLM 已占用 `30000`），并防止 SGLang 默认的显存贪婪策略（抢占 90% VRAM）导致你的主 LLM 被挤兑/OOM，必须严格按照官方 Cookbook 配置，并叠加**显存限制参数**。

请使用以下命令作为后台守护进程启动 Unlimited-OCR 服务：

```bash
export TRITON_PTXAS_PATH="/patch/ptxas_wrapper.sh"
export TRITON_PTXAS_BLACKWELL_PATH="/patch/ptxas_wrapper.sh"
export TORCH_CUDA_ARCH_LIST=11.0a
export LD_LIBRARY_PATH=/usr/local/cuda-13.3/lib64:$LD_LIBRARY_PATH
export CPLUS_INCLUDE_PATH=/usr/local/cuda-13.3/targets/sbsa-linux/include:$CPLUS_INCLUDE_PATH
SGLANG_USE_MODELSCOPE=true python -m sglang.launch_server \
  --model-path ~/Project/LLM-weight/Unlimited-OCR \
  --attention-backend flashinfer \
  --page-size 1 \
  --context-length 32768 \
  --enable-custom-logit-processor \
  --disable-radix-cache \
  --host 0.0.0.0 \
  --port 30001 \
  --mem-fraction-static 0.16
```

这两个留作备用参数
--disable-overlap-schedule \
--skip-server-warmup \

### 参数深度解析（Why we do this）：

1. **`--mem-fraction-static 0.08` (新增关键配置)**:
* 128GB × 0.16 ≈ 20.4GB。


2. **`--attention-backend flashinfer`**:
* Jetson Thor 必须使用 flashinfer.  fa3 不受支持


3. **`--disable-radix-cache` & `--page-size 1**`:
* Unlimited-OCR 的 R-SWA 机制不依赖传统的全局 KV Cache 重用（每次解析的文档图像都是全新的独立上下文），因此禁用 Radix Cache 并将页面大小设为 1，能最大化压榨内存效率，避免缓存碎片化。


4. **`--port 30001`**:
* 彻底与常驻 LLM 服务隔离，Tool 代码内将 OCR 请求定向至此端口。