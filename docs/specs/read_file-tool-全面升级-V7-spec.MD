# Read File Tool 产品需求与技术规格

## v7.0 PaddleOCR-VL-1.6 统一视觉解析版

**目标设备**：Jetson Thor，Ubuntu 24.04，ARM64，128GB UMA，NVIDIA Blackwell
**文档状态**：Coding Plan 前置 PRD / Technical Specification
**发布日期**：2026-07-26
**核心模型**：PaddleOCR-VL-1.6
**OCR 推理方式**：PaddleOCR 官方完整 Pipeline，Transformers Engine
**服务方式**：PaddleX 官方完整 API Service
**输出格式**：Markdown 为主，附带页级结构化元数据
**主要优点**：不再判断 PDF 是文本型还是扫描型，所有分页文档均按最终可视效果解析，可直接覆盖混合扫描页、错误隐藏 OCR 层、截图表格和嵌入图片。

---

# 1. 产品目标

升级现有 `read_file` Tool，使其能够稳定读取本地文档、分页办公文件、电子 PDF、扫描 PDF、混合 PDF、图片和电子表格。

新版本取消以下判断：

* PDF 是否为文本型；
* PDF 是否需要 OCR；
* PDF 原生文本层是否可信；
* 页面是否包含扫描内容；
* Office 文档是否包含嵌入图片。

所有 PDF、Word、PowerPoint 和图片类输入统一进入 PaddleOCR-VL-1.6 完整视觉解析管线。

电子表格以单元格结构为核心，继续由 MarkItDown 解析；其中的嵌入图片单独提交 PaddleOCR-VL-1.6，并回填到对应工作表位置。

系统必须正确覆盖以下情况：

* 前几页是电子文本，后面是扫描页；
* 封面只有少量文字，正文全是扫描件；
* PDF 存在损坏、过期或错误的隐藏 OCR 层；
* 隐藏 OCR 层内容与页面图像不一致；
* 页面只有重复页眉和页脚；
* OCR 文本层乱码但字符串长度很长；
* 页面中的表格实际为截图；
* 扫描页和电子页交错；
* DOCX、PPTX 中包含截图、图表、扫描页或文本图片；
* 多栏文档、公式、表格、图表和跨页表格；
* 歪斜、旋转、拍照、轻度弯曲和光照不均的页面。

---

# 2. 文件格式范围

## 2.1 PaddleOCR-VL 统一视觉解析类

以下类别必须统一进入 PaddleOCR-VL-1.6：

| 类别         | 文件格式                                                                     | 预处理                |
| ---------- | ------------------------------------------------------------------------ | ------------------ |
| Word       | `.doc`、`.docx`                                                           | LibreOffice 转 PDF  |
| PowerPoint | `.ppt`、`.pptx`                                                           | LibreOffice 转 PDF  |
| PDF        | `.pdf`                                                                   | 原文件直接进入 OCR；超长文件分块 |
| 常见图片       | `.png`、`.jpg`、`.jpeg`、`.bmp`、`.webp`                                     | 直接输入或标准化           |
| 多页图片       | `.tif`、`.tiff`                                                           | 按页解析               |
| 扩展图片       | `.gif`、`.avif`、`.heic`、`.heif`、`.jp2`、`.j2k`、`.ico`、`.pbm`、`.pgm`、`.ppm` | 转为 PNG 或多页 TIFF    |
| 矢量图片       | `.svg`                                                                   | 安全栅格化为 PNG         |

PaddleOCR-VL 官方完整 Pipeline 的直接输入类型为图片或 PDF；PDF 会逐页解析，多页 TIFF 也可以逐页处理。完整 Pipeline 包含版面分析、区域裁剪、VLM 元素识别、阅读顺序恢复和 Markdown 重组，不得仅调用独立 VLM 模型。

“所有图片格式”在工程上定义为：

> 所有能够通过系统允许的图像解码器安全解码，并能标准化为 PNG、JPEG 或 TIFF 的图片格式。

相机 RAW、专业医学影像、工程 CAD、PSD 工程文件和损坏图片不属于默认支持范围。

## 2.2 MarkItDown 电子表格类

| 文件格式    | 处理链路                               |
| ------- | ---------------------------------- |
| `.xlsx` | MarkItDown                         |
| `.xls`  | LibreOffice 转 `.xlsx` → MarkItDown |
| `.ods`  | LibreOffice 转 `.xlsx` → MarkItDown |

本规格将用户提到但未记住名称的表格软件格式解释为 LibreOffice/OpenOffice Calc 的 `.ods`。

MarkItDown 官方提供 `.xlsx` 和旧版 `.xls` 的可选依赖，但没有列出 `.ods` 转换器，因此 `.ods` 需要先转换为 `.xlsx`。

以下格式暂不纳入：

* Microsoft Access：`.mdb`、`.accdb`
* Excel Binary Workbook：`.xlsb`
* 宏工作簿：`.xlsm`
* 数据库转储文件
* 密码未知的加密工作簿

---

# 3. 总体解析架构

```text
[file_path]
    │
    ├── MIME、扩展名、安全和大小检查
    │
    ├── SHA-256 缓存查询
    │
    └── 格式路由
         │
         ├── .doc / .docx
         │      └── LibreOffice Writer
         │             └── PDF
         │                    └── PaddleOCR-VL-1.6
         │
         ├── .ppt / .pptx
         │      └── LibreOffice Impress
         │             └── PDF
         │                    └── PaddleOCR-VL-1.6
         │
         ├── 所有类型的 .pdf
         │      └── PDF 分块器
         │             └── PaddleOCR-VL-1.6
         │
         ├── 所有可支持图片
         │      └── 图像标准化器
         │             └── PaddleOCR-VL-1.6
         │
         ├── .xlsx
         │      ├── MarkItDown
         │      └── 嵌入图片提取
         │             └── PaddleOCR-VL-1.6
         │
         └── .xls / .ods
                └── LibreOffice Calc
                       └── .xlsx
                              ├── MarkItDown
                              └── 嵌入图片提取
                                     └── PaddleOCR-VL-1.6
```

---

# 4. PaddleOCR-VL 推理与服务规格

## 4.1 服务实现

必须采用 PaddleOCR 官方完整服务部署方式：

```bash
paddlex --serve \
  --pipeline /path/to/paddleocr_vl_thor.yaml \
  --engine transformers \
  --device gpu:0 \
  --host 127.0.0.1 \
  --port 30001
```

官方完整服务端点：

```text
POST /layout-parsing
```

服务仅绑定：

```text
127.0.0.1:30001
```

不得绑定 `0.0.0.0`，除非另行增加身份认证、TLS 和防火墙策略。

PaddleOCR 官方手动服务部署支持通过 `paddlex --serve --pipeline PaddleOCR-VL --engine transformers` 启动完整 Pipeline 服务；该服务与只负责 VLM 推理的底层服务不同。

## 4.2 版本要求

基线依赖：

```text
Python：3.12
PaddleOCR：[doc-parser] >= 3.6.0, < 3.7
Transformers：>= 5.10.0, < 6.0
PyTorch：使用当前 JetPack 对应的 NVIDIA Jetson PyTorch
模型：PaddleOCR-VL-1.6
模型精度：BF16
```

最新 PaddleOCR 推理引擎文档要求 Transformers `>=5.10.0`，并支持通过 `engine_config` 设置 dtype、设备、attention implementation 和 generation config。

所有依赖必须写入锁定文件：

```text
requirements.lock
```

不得使用无版本约束的生产安装命令。

## 4.3 ARM64 启动验收门

目标运行环境不得隐式依赖 x86_64 wheel。

服务启动前必须执行：

```text
1. platform.machine() == "aarch64"
2. torch.cuda.is_available() == True
3. CUDA 设备名称包含 Jetson Thor 对应 GPU
4. PaddleOCRVL Pipeline 能够使用 engine=transformers 初始化
5. 所有实际子模型均运行在 GPU 或明确批准的 CPU 路径
6. 不允许因 GPU 初始化失败而静默回退到 CPU
7. 一张图片和一页 PDF 的 canary 测试必须成功
```

PaddleOCR 在未指定设备或 GPU 不可用时可能选择 CPU，因此服务健康检查必须主动验证实际设备，不能只验证 HTTP 端口可访问。

PaddlePaddle 官方 Linux 安装包当前不支持 ARM64；本方案的目标运行配置必须使用 `engine=transformers`，且启动检查必须确认没有子模型意外解析到 PaddlePaddle Engine。

如果完整 Pipeline 在 Thor ARM64 上仍有模块强制依赖 PaddlePaddle，则该问题定义为 P0 阻断项，不允许以“独立 Transformers VLM 脚本”替代完整 Pipeline。独立 VLM 示例只支持元素级识别和 text spotting，不具备完整页面解析能力。

---

# 5. PaddleOCR-VL 基线配置

## 5.1 Pipeline 配置要求

必须从当前安装版本导出官方 Pipeline YAML，再进行修改，不得手写不匹配当前版本的完整配置。

基线配置逻辑：

```yaml
pipeline_version: v1.6

engine: transformers

engine_config:
  dtype: bfloat16
  device_type: cuda
  device_id: 0
  trust_remote_code: true
  attn_implementation: sdpa
  generation_config:
    do_sample: false
    temperature: 0.0

use_doc_orientation_classify: true
use_doc_unwarping: false
use_layout_detection: true
use_chart_recognition: true
use_seal_recognition: false
use_ocr_for_image_block: true
format_block_content: true
merge_layout_blocks: true
layout_shape_mode: auto
use_queues: true

markdown_ignore_labels:
  - number
  - header
  - header_image
  - footer
  - footer_image

Serving:
  visualize: false
  extra:
    max_num_input_imgs: 32
```

## 5.2 配置规则

### 版面分析

必须启用：

```text
use_layout_detection=true
```

不得关闭阅读顺序恢复。

### 嵌入图片文字

必须启用：

```text
use_ocr_for_image_block=true
```

用于识别：

* DOCX 内嵌截图；
* PPTX 内嵌扫描页；
* 图片中的标注文字；
* 截图式表格；
* 页面图示内部的文本。

该参数的官方默认值为 `false`，本项目必须显式改为 `true`。

### 图表

默认启用：

```text
use_chart_recognition=true
```

资源保护模式下可以关闭，但不得在普通模式中关闭。

### 印章

默认：

```text
use_seal_recognition=false
```

当请求参数 `recognize_seals=true` 时按请求开启。

### 页眉和页脚

忽略：

```text
header
header_image
footer
footer_image
number
```

不得忽略：

```text
footnote
aside_text
```

官方默认忽略项包含 `footnote` 和 `aside_text`；本项目需要覆盖该默认配置，防止脚注和侧栏正文被丢弃。

### 页面方向

默认开启：

```text
use_doc_orientation_classify=true
```

### 页面矫正

首次解析默认：

```text
use_doc_unwarping=false
```

以下情况触发单页重试并开启矫正：

* 页面结果为空；
* 页面只识别出极少内容；
* 页面报告严重倾斜；
* 页面宽高与内容方向明显冲突；
* 原始输入为手机拍照；
* 用户显式设置 `document_mode="distorted"`。

### 内部流水线队列

必须开启：

```text
use_queues=true
```

官方说明，该模式会将 PDF 渲染、版面分析和 VLM 推理放入异步线程和队列，在多页 PDF 场景下提高效率。

### Attention 实现

Thor 基线：

```text
attn_implementation=sdpa
```

可选优化档：

```text
attn_implementation=flash_attention_2
```

FlashAttention 只能在 ARM64 编译、正确性测试、显存测试和回归测试全部通过后启用，不能作为首个生产版本的硬依赖。

### 高性能推理插件

不设置：

```text
enable_hpi
```

官方引擎配置规则规定，显式设置 `engine` 后，`enable_hpi` 不再生效。

---

# 6. Office 文档视觉化规格

## 6.1 Word 文件

输入：

```text
.doc
.docx
```

处理链路：

```text
Word 文件
    -> 安全检查
    -> LibreOffice Writer 无界面转换
    -> PDF 校验
    -> PDF 分块
    -> PaddleOCR-VL
```

转换必须使用：

```bash
soffice \
  -env:UserInstallation=file:///unique/temp/profile \
  --headless \
  --nologo \
  --nodefault \
  --nolockcheck \
  --nofirststartwizard \
  --convert-to pdf:writer_pdf_Export \
  --outdir /unique/temp/output \
  input.docx
```

LibreOffice 官方支持通过 `--convert-to` 和 `writer_pdf_Export` 在命令行将文档转换为 PDF，并支持指定独立用户配置目录。

输出 PDF 必须包含最终可见版面，包括：

* 正文；
* 图片；
* 文本框；
* 表格；
* 页眉页脚；
* 公式的可视呈现；
* 可见批注标记；
* 当前可见修订结果。

默认不提取：

* 隐藏文字；
* 未显示的修订历史；
* 文件附件；
* OLE 对象内部不可见数据；
* 宏代码。

## 6.2 PowerPoint 文件

输入：

```text
.ppt
.pptx
```

处理链路：

```text
PowerPoint 文件
    -> 安全检查
    -> LibreOffice Impress 无界面转换
    -> PDF 校验
    -> PaddleOCR-VL
```

转换过滤器：

```text
impress_pdf_Export
```

每个 PDF 页面对应一张幻灯片。

默认解析：

* 幻灯片标题；
* 正文；
* 图片；
* 图表；
* SmartArt 的可视结果；
* 表格；
* 公式；
* 截图；
* 形状中的文字。

默认不解析：

* 演讲者备注；
* 隐藏幻灯片；
* 动画中未处于初始显示状态的内容；
* 嵌入附件内部数据。

后续可以通过参数启用：

```text
include_hidden_slides
include_speaker_notes
```

但不属于 v7.0 首版范围。

## 6.3 LibreOffice 转换约束

每次转换必须：

* 使用独立临时目录；
* 使用独立 LibreOffice 用户配置目录；
* 禁止宏执行；
* 禁止自动更新外部链接；
* 设置硬超时；
* 超时后终止完整进程组；
* 校验返回码；
* 校验目标文件存在；
* 校验 PDF 可以打开；
* 校验 PDF 页数大于零；
* 在请求结束后清理临时文件。

默认超时：

```text
单文件转换：180 秒
```

最大允许：

```text
单个 Office 文件：1GB
转换后 PDF：2GB
页面数：1000 页
```

---

# 7. PDF 统一视觉解析规格

## 7.1 基本规则

所有 `.pdf` 均进入 PaddleOCR-VL。

系统不得读取 PDF 原生文本层作为最终内容，也不得根据原生文本长度决定是否 OCR。

允许使用 PyMuPDF 或等价工具执行：

* 页数读取；
* PDF 完整性检查；
* 密码状态检查；
* 页面拆分；
* 页面重新组合；
* 元数据读取。

不得使用 PyMuPDF 提取的文字作为最终正文。

## 7.2 PDF 分块

默认参数：

```text
OCR_PDF_CHUNK_PAGES=24
OCR_PDF_CHUNK_MAX_BYTES=64MB
OCR_PDF_CHUNK_OVERLAP_PAGES=1
OCR_MAX_PDF_PAGES=1000
```

分块同时受页数和字节数约束。

示例：

```text
Chunk 1：第 1–24 页
Chunk 2：第 24–47 页
Chunk 3：第 47–70 页
```

一页重叠用于：

* 捕获跨分块表格；
* 捕获跨页标题关系；
* 捕获上一页延续内容。

合并时按照原始页码去重。

## 7.3 跨页重组

每个分块请求必须设置：

```json
{
  "restructurePages": true,
  "mergeTables": true,
  "relevelTitles": true,
  "prettifyMarkdown": true,
  "showFormulaNumber": true
}
```

PaddleOCR-VL 官方支持通过 `restructure_pages` 合并跨页表格、重建标题层级和整理多页结果。

## 7.4 分块失败恢复

失败层级：

```text
24 页分块失败
    -> 8 页分块重试
        -> 单页重试
            -> 单页开启 unwarping 重试
                -> 返回页面失败占位符
```

任何单页失败不得使整个文件失败。

失败页输出：

```markdown
<!-- page: 17; status: failed; engine: PaddleOCR-VL-1.6 -->

[第 17 页解析失败：OCR_PAGE_FAILED]
```

系统必须继续处理后续页面。

---

# 8. 图片统一解析规格

## 8.1 常见图片

以下格式优先标准化为 RGB PNG：

```text
PNG
JPEG
BMP
WEBP
AVIF
HEIC
HEIF
JPEG 2000
ICO
PBM
PGM
PPM
```

JPEG 可以保持原格式，前提是：

* 文件有效；
* 颜色空间可以正确转换；
* 不包含异常超大尺寸；
* EXIF 方向已应用。

## 8.2 TIFF

单页 TIFF：

```text
直接进入 PaddleOCR-VL
```

多页 TIFF：

```text
按页面处理
```

页数限制与 PDF 一致。

## 8.3 GIF

动画 GIF 按多页文档处理：

```text
每个有效帧 -> 一页
```

默认限制：

```text
最大帧数：200
```

连续重复帧必须按感知哈希去重。

## 8.4 SVG

SVG 必须通过受限制的本地栅格化工具转换为 PNG。

必须禁止：

* 外部 URL；
* 外部字体下载；
* JavaScript；
* 网络资源；
* 文件系统引用；
* 无限递归引用。

默认渲染参数：

```text
DPI：200
最大长边：8192 像素
最大像素：50,000,000
```

## 8.5 超大图片

超过以下任一限制时进入缩放或拒绝流程：

```text
宽度 > 20,000
高度 > 20,000
总像素 > 100,000,000
解码后内存估算 > 1GB
```

页面型图片优先按比例缩放。

不得在不保留纵横比的情况下拉伸图片。

---

# 9. 电子表格解析规格

## 9.1 XLSX

处理链路：

```text
XLSX
    ├── MarkItDown：单元格和工作表内容
    └── Embedded Media Extractor
            └── PaddleOCR-VL：嵌入图片内容
```

MarkItDown 是电子表格正文的权威来源。

输出必须保留：

* 工作表名称；
* 单元格数据；
* 表格结构；
* 公式的可见值；
* 工作表顺序。

## 9.2 XLS 和 ODS

处理链路：

```text
XLS / ODS
    -> LibreOffice Calc 转 XLSX
    -> MarkItDown
    -> 嵌入图片提取
    -> PaddleOCR-VL
```

转换完成后，不再直接解析旧格式。

## 9.3 嵌入图片

对标准化后的 XLSX，必须检查：

```text
xl/media/
xl/drawings/
xl/drawings/_rels/
xl/worksheets/_rels/
```

系统需要根据 Drawing Relationship 和 Anchor 信息建立：

```text
图片
    -> 工作表
    -> 起始单元格
    -> 结束单元格
```

每张嵌入图片进入 PaddleOCR-VL。

识别内容插入到对应工作表 Markdown 中：

```markdown
### 嵌入图片：Sheet1!B4:F18

<!-- embedded-image:
source=xl/media/image3.png;
sheet=Sheet1;
anchor=B4:F18
-->

图片识别内容……
```

相同图片根据 SHA-256 去重。

## 9.4 电子表格图表

v7.0 首版要求：

* 保留 MarkItDown 能够提取的标题、数据和文字；
* OCR 所有嵌入的位图图片；
* 不要求将 Excel 原生 Chart 对象重建成 Markdown 图表数据。

后续版本可以增加：

```text
spreadsheet_visual_preview=true
```

将每个工作表渲染为 PDF，再生成独立的视觉补充内容。

---

# 10. 官方服务请求规格

## 10.1 请求方式

小文件使用 Base64：

```text
原始文件 <= 16MB
```

大文件使用仅监听 Loopback 的临时文件服务：

```text
http://127.0.0.1:<internal-port>/objects/<opaque-token>
```

临时 URL 必须：

* 使用不可预测 token；
* 绑定单次请求；
* 设置 10 分钟 TTL；
* 成功读取后可立即销毁；
* 禁止目录遍历；
* 禁止外部网络访问。

官方 `/layout-parsing` 接口支持图片或 PDF 的 URL 和 Base64 输入。

## 10.2 请求模板

```json
{
  "file": "<base64-or-loopback-url>",
  "fileType": 0,
  "useDocOrientationClassify": true,
  "useDocUnwarping": false,
  "useLayoutDetection": true,
  "useChartRecognition": true,
  "useSealRecognition": false,
  "useOcrForImageBlock": true,
  "formatBlockContent": true,
  "mergeLayoutBlocks": true,
  "layoutShapeMode": "auto",
  "prettifyMarkdown": true,
  "showFormulaNumber": true,
  "restructurePages": true,
  "mergeTables": true,
  "relevelTitles": true,
  "returnMarkdownImages": false,
  "visualize": false
}
```

必须设置：

```text
returnMarkdownImages=false
visualize=false
```

避免返回 Base64 可视化图片和 Markdown 附属图片，减少内存、响应体和序列化开销。官方完整服务提供这两个控制字段。

---

# 11. 输出数据规格

## 11.1 内部结果

```python
@dataclass
class ReadFileResult:
    success: bool
    markdown: str
    source_path: str
    source_format: str
    normalized_format: str | None
    engine: str
    model_version: str
    pages: list[PageResult]
    spreadsheet_sheets: list[SheetResult]
    warnings: list[str]
    errors: list[ParseError]
    cache_hit: bool
    elapsed_ms: int
```

页结果：

```python
@dataclass
class PageResult:
    source_page: int
    markdown: str
    status: str
    engine: str
    retries: int
    warnings: list[str]
```

工作表结果：

```python
@dataclass
class SheetResult:
    sheet_name: str
    markdown: str
    embedded_images: list[EmbeddedImageResult]
```

## 11.2 Markdown 页标记

PDF、Word 和 PowerPoint 输出必须包含页标记：

```markdown
<!--
source_page: 12
source_format: pdf
engine: PaddleOCR-VL-1.6
pipeline_version: v1.6
-->

页面内容……
```

PPT 输出增加：

```markdown
<!-- source_slide: 12 -->
```

电子表格输出：

```markdown
<!-- source_sheet: Sheet1 -->
```

## 11.3 图片引用

默认不返回二进制图片。

识别到纯装饰图片时：

```markdown
[图片]
```

识别到有意义图示时：

```markdown
[图示：模型生成的简短内容描述]
```

不得返回指向临时目录的失效 Markdown 图片链接。

---

# 12. 缓存规格

缓存键：

```text
SHA256(
    source_file_bytes
    + parser_version
    + model_revision
    + pipeline_config_hash
    + request_options
)
```

缓存内容：

* 最终 Markdown；
* 页级结果；
* 工作表结果；
* warnings；
* errors；
* 性能数据；
* 标准化文件元数据。

默认策略：

```text
成功结果 TTL：30 天
部分成功结果 TTL：7 天
完全失败结果 TTL：1 小时
```

模型或配置变化后必须自动失效旧缓存。

---

# 13. 并发与资源控制

Jetson Thor 为 UMA 架构，OCR 服务与主 LLM 共享物理内存。

基线限制：

```text
PaddleX 服务进程：1
服务 Worker：1
同时执行的 read_file OCR 任务：1
等待队列：2
LibreOffice 转换并发：1
图片标准化并发：2
```

不得启动多个 PaddleOCR-VL 模型副本。

服务必须保留：

```text
MAIN_LLM_MEMORY_RESERVE_GB=16
OCR_CUDA_MEMORY_LIMIT_GB=16
```

SGLang 的静态内存池已经包含常驻主 LLM 分配；该参数表示文档任务执行时
必须额外保持的系统可用 UMA 下限。OCR CUDA 参数通过 PyTorch allocator
限制 PaddleX Transformers 进程，不会预分配 16 GiB。启动和每次任务前检查系统可用内存。

当预计无法保留主 LLM 内存时：

```text
返回 RESOURCE_BUSY
```

不得尝试通过无限制 swap 完成任务。

资源保护参数：

```text
OCR_MAX_PDF_PAGES=1000
OCR_MAX_IMAGE_FRAMES=200
OCR_MAX_SOURCE_BYTES=1GB
OCR_MAX_NORMALIZED_BYTES=2GB
OCR_REQUEST_TIMEOUT_SECONDS=3600
OCR_QUEUE_LIMIT=2
```

---

# 14. 错误处理

标准错误码：

| 错误码                          | 含义               |
| ---------------------------- | ---------------- |
| `FILE_NOT_FOUND`             | 文件不存在            |
| `UNSUPPORTED_FORMAT`         | 格式不支持            |
| `FILE_TOO_LARGE`             | 文件过大             |
| `PAGE_LIMIT_EXCEEDED`        | 页数超过限制           |
| `PASSWORD_REQUIRED`          | 文件需要密码           |
| `INVALID_PASSWORD`           | 密码错误             |
| `CORRUPTED_FILE`             | 文件损坏             |
| `OFFICE_CONVERSION_FAILED`   | LibreOffice 转换失败 |
| `IMAGE_NORMALIZATION_FAILED` | 图片标准化失败          |
| `OCR_SERVICE_UNAVAILABLE`    | PaddleOCR 服务不可用  |
| `OCR_TIMEOUT`                | OCR 超时           |
| `OCR_PAGE_FAILED`            | 单页 OCR 失败        |
| `SPREADSHEET_PARSE_FAILED`   | 表格解析失败           |
| `RESOURCE_BUSY`              | 内存或队列资源不足        |
| `SECURITY_REJECTED`          | 安全规则拒绝           |

部分页面失败时：

```text
success=true
warnings 非空
errors 包含失败页
```

只有以下情况返回整体失败：

* 文件无法打开；
* Office 无法转换；
* OCR 服务完全不可用；
* 所有页面均失败；
* 安全校验失败；
* 密码无法解锁；
* 格式不支持。

---

# 15. 安全要求

必须实现：

* 扩展名与 MIME/magic 双重校验；
* 路径白名单；
* 符号链接解析；
* 临时目录隔离；
* LibreOffice 独立 Profile；
* 禁止 Office 宏；
* 禁止更新外部链接；
* ZIP 解压炸弹限制；
* 图片解压炸弹限制；
* SVG 外部资源限制；
* 最大文件、页面、帧数和像素限制；
* subprocess 超时与进程组终止；
* 临时文件自动清理；
* 本地服务仅绑定 Loopback；
* 模型文件固定版本和 revision；
* `trust_remote_code=true` 时必须使用已固定并审计的本地模型副本；
* 运行账户不得拥有项目源代码写权限；
* 不允许 OCR 服务访问互联网。

密码必须只存在于当前请求内存中，不得写入日志或缓存键明文。

---

# 16. 可观测性

每次解析记录：

```text
request_id
source_format
source_size
normalized_format
page_count
sheet_count
embedded_image_count
cache_hit
office_conversion_ms
normalization_ms
ocr_ms
markitdown_ms
merge_ms
retry_count
failed_pages
peak_process_rss
peak_system_memory_used
result_size
```

不得记录：

* 文档完整正文；
* 密码；
* Base64 文件内容；
* 敏感文件路径中的用户隐私信息。

健康检查必须返回：

```json
{
  "status": "ok",
  "pipeline": "PaddleOCR-VL",
  "pipelineVersion": "v1.6",
  "engine": "transformers",
  "modelDevice": "cuda:0",
  "architecture": "aarch64",
  "cudaAvailable": true,
  "canaryPassed": true,
  "queueDepth": 0
}
```

---

# 17. 功能验收标准

## 17.1 PDF

测试集必须包含：

1. 全文本 PDF；
2. 全扫描 PDF；
3. 文本页与扫描页交错 PDF；
4. 前几页文本、后续扫描 PDF；
5. 错误隐藏 OCR 层 PDF；
6. 乱码隐藏 OCR 层 PDF；
7. 只有页眉页脚文本层的 PDF；
8. 截图式表格 PDF；
9. 多栏论文；
10. 跨页表格；
11. 旋转页面；
12. 手机拍照页面；
13. 200 页以上长 PDF。

验收要求：

* 不得依赖隐藏 OCR 层；
* 所有页面必须有结果或显式失败标记；
* 页序正确；
* 页码标记正确；
* 截图表格输出为结构化内容；
* 页眉页脚不得大量重复；
* 脚注不得被默认删除；
* 单页失败不得中止整份文件；
* 分块重叠页不得重复出现在最终结果中。

## 17.2 Word

测试集：

* DOC；
* DOCX；
* 图片型 DOCX；
* 多栏 DOCX；
* 公式 DOCX；
* 截图表格 DOCX；
* 页眉页脚重复 DOCX；
* 包含文本框的 DOCX。

验收要求：

* 可见内容必须全部进入 PDF；
* 图片中的文字能够识别；
* 页面顺序正确；
* LibreOffice 失败必须返回明确错误。

## 17.3 PowerPoint

测试集：

* PPT；
* PPTX；
* 每页为扫描图片的 PPTX；
* 图表 PPTX；
* 表格 PPTX；
* SmartArt PPTX；
* 截图式技术演示稿。

验收要求：

* 每张幻灯片对应一个页面；
* 图片内文字能够识别；
* 表格和公式保留结构；
* 不输出临时图片链接。

## 17.4 电子表格

测试集：

* XLS；
* XLSX；
* ODS；
* 多工作表；
* 合并单元格；
* 公式；
* 大型表格；
* 工作表内嵌图片；
* 多张重复图片；
* 带中文工作表名称。

验收要求：

* 单元格结构来自 MarkItDown；
* 工作表顺序正确；
* XLS 和 ODS 正确转换为 XLSX；
* 嵌入图片可以映射到工作表；
* 嵌入图片 OCR 结果可以回填；
* 重复图片只执行一次 OCR。

## 17.5 图片

测试集：

* PNG；
* JPEG；
* WEBP；
* BMP；
* TIFF；
* 多页 TIFF；
* GIF；
* HEIC；
* AVIF；
* SVG；
* 旋转图片；
* 透明背景图片；
* 超大图片。

验收要求：

* EXIF 方向正确；
* 动画和多页图片页序正确；
* SVG 不得访问外部资源；
* 超大图片不得造成进程 OOM；
* 不支持的图像必须返回明确错误。

---

# 18. 性能验收

首版不设置未经 Thor 实测的固定页每秒指标。

Coding Plan 必须建立以下基准：

```text
1 页普通扫描件
10 页扫描 PDF
50 页混合 PDF
200 页长 PDF
20 页 DOCX
50 页 PPTX
20 个工作表 XLSX
100 张嵌入图片 XLSX
```

每项记录：

* 冷启动时间；
* 热启动时间；
* 首页面返回时间；
* 总耗时；
* 峰值 UMA；
* GPU 利用率；
* CPU 利用率；
* 临时磁盘用量；
* 输出字符数；
* 重试次数。

生产验收必须满足：

```text
无 OOM
无模型重复加载
无静默 CPU 回退
无页面静默丢失
主 LLM 保留内存达到配置要求
连续处理 20 个请求后无持续内存增长
```

---

# 19. 代码模块边界

Coding Plan 应至少包含：

```text
read_file/
├── router.py
├── file_validator.py
├── mime_detector.py
├── office_renderer.py
├── pdf_chunker.py
├── image_normalizer.py
├── spreadsheet_parser.py
├── spreadsheet_media_extractor.py
├── paddleocr_client.py
├── result_assembler.py
├── cache.py
├── resource_guard.py
├── models.py
├── errors.py
└── tests/
```

职责：

```text
router.py
    格式路由，不执行具体解析

office_renderer.py
    DOC/DOCX/PPT/PPTX -> PDF
    XLS/ODS -> XLSX

pdf_chunker.py
    PDF 按页数、大小和重叠策略切分

image_normalizer.py
    所有图片 -> PaddleOCR 可接受格式

spreadsheet_parser.py
    MarkItDown 表格解析

spreadsheet_media_extractor.py
    提取 XLSX 嵌入图片和锚点

paddleocr_client.py
    调用官方 /layout-parsing 服务

result_assembler.py
    页码、工作表、图片 OCR 和 Markdown 合并

resource_guard.py
    内存、并发和队列控制
```

---

# 20. 实施顺序

## P0：Thor 环境可行性

完成：

* ARM64 Python 环境；
* Jetson PyTorch；
* Transformers Engine；
* PaddleOCR-VL-1.6 完整 Pipeline 初始化；
* GPU 设备验证；
* 官方完整服务启动；
* 图片、PDF canary 测试；
* 记录峰值 UMA。

P0 未通过时不得进入业务编码。

## P1：统一视觉主链路

完成：

* DOC/DOCX 转 PDF；
* PPT/PPTX 转 PDF；
* 所有 PDF 分块；
* 常见图片标准化；
* PaddleOCR API 调用；
* Markdown 页级合并；
* 错误恢复。

## P2：电子表格

完成：

* XLSX MarkItDown；
* XLS/ODS 转 XLSX；
* 嵌入图片提取；
* 图片 OCR 回填；
* 工作表元数据。

## P3：可靠性

完成：

* 缓存；
* 资源保护；
* 超时；
* 重试；
* 长文档；
* 监控；
* 安全限制；
* 压力测试。

## P4：性能优化

按测试结果选择：

* SDPA 参数调整；
* FlashAttention 2；
* VLM 像素参数；
* PDF 分块大小；
* 图片并行解码；
* Pipeline 队列参数；
* 模型常驻与启动优化。

---

# 21. 首版非目标

v7.0 不包含：

* Access `.mdb/.accdb`；
* Excel `.xlsb`；
* 宏内容解析；
* Office 附件提取；
* PowerPoint 动画逐帧解析；
* 演讲者备注；
* 隐藏幻灯片；
* 电子表格原生图表数据重建；
* CAD；
* PSD 图层；
* 相机 RAW；
* 视频 OCR；
* 手写批注语义分析；
* 云端 OCR；
* 多用户高并发 OCR；
* OCR 模型微调。

---

# 22. 最终路由定义

```python
VISUAL_OFFICE_EXTENSIONS = {
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
}

PDF_EXTENSIONS = {
    ".pdf",
}

SPREADSHEET_EXTENSIONS = {
    ".xlsx",
    ".xls",
    ".ods",
}

DIRECT_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
}

NORMALIZED_IMAGE_EXTENSIONS = {
    ".gif",
    ".avif",
    ".heic",
    ".heif",
    ".jp2",
    ".j2k",
    ".ico",
    ".pbm",
    ".pgm",
    ".ppm",
    ".svg",
}
```

最终规则：

```text
.doc / .docx
    -> LibreOffice PDF
    -> PaddleOCR-VL-1.6

.ppt / .pptx
    -> LibreOffice PDF
    -> PaddleOCR-VL-1.6

.pdf
    -> PaddleOCR-VL-1.6

所有受支持图片
    -> 标准化
    -> PaddleOCR-VL-1.6

.xlsx
    -> MarkItDown
    -> 嵌入图片 PaddleOCR-VL-1.6

.xls / .ods
    -> LibreOffice XLSX
    -> MarkItDown
    -> 嵌入图片 PaddleOCR-VL-1.6
```

该路由不再包含：

```text
PDF 文本型/扫描型判断
PDF 原生文本优先
OCR 空文本触发
隐藏 OCR 层可信度判断
MarkItDown 解析 Word/PowerPoint
PyMuPDF 正文提取
SGLang
vLLM
```
