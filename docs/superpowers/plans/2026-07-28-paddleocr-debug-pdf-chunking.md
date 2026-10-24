# PaddleOCR Debug PDF Chunking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 standalone PaddleOCR debug client 在保留 PaddleX 32 页服务内存上限的同时，完整处理超过 32 页的 PDF，并通过官方 `/restructure-pages` 恢复跨 chunk 表格合并。

**Architecture:** debug client 使用 Poppler 在自动清理的临时目录中把 PDF 拆成单页，再按最多 32 页组成 chunk。超过 32 页时，各 chunk 先以 `restructurePages=false` 调用 `/layout-parsing`，随后把全部有序 `prunedResult` 一次提交给 `/restructure-pages`；图片和不超过 32 页的 PDF 保留单请求路径。

**Tech Stack:** Python 3.12、httpx、PaddleX OpenAPI、Poppler `pdfseparate`/`pdfunite`、pytest、项目现有 PyMuPDF/fitz 测试依赖。

## Global Constraints

- 不启动、停止或替换当前 `8118`/`30002` 服务。
- 不修改 `.paddle-ocr`、`.sglang` 或其他第三方依赖源码。
- 保留 `Serving.extra.max_num_input_imgs=32`、16 GiB PaddleX allocator ceiling 和 16 GiB UMA admission floor。
- 所有 loopback HTTP 调用继续使用 `trust_env=False`。
- 不输出 Base64、完整内部响应、timing 或 benchmark 数据。
- 不引入新的 Python 依赖；Poppler 缺失时返回可操作错误。
- 不执行任何 Git 命令；项目规则要求先获得用户许可。
- 生产 `backend/tools/read_file` 已按最多 24 页 chunk，本文计划不改变其 chunk orchestration。

---

### Task 1: 锁定页数验证和 `/restructure-pages` 协议

**Files:**
- Modify: `tests/test_paddle_ocr_sglang_debug.py`
- Modify: `scripts/debug_OCR_with_SGLang_backend.py`

**Interfaces:**
- Consumes: PaddleX `result.layoutParsingResults`、`result.dataInfo.numPages` 和 OpenAPI paths。
- Produces: `parse_markdown_pages(body: Any, expected_pages: int | None = None) -> list[str]`；`verify_paddlex(client: httpx.Client, require_restructure: bool = False) -> None`。

- [ ] **Step 1: 写页数不匹配和 endpoint 缺失的失败测试**

给 `_DebugHandler` 增加 `expose_restructure = True`；它的 OpenAPI 响应在该值为 true 时包含 `/restructure-pages`。然后增加两个测试：

```python
def test_debug_client_rejects_reported_pdf_page_truncation() -> None:
    client = _load_client()
    body = {
        "errorCode": 0,
        "result": {
            "dataInfo": {"type": "pdf", "numPages": 33, "pages": []},
            "layoutParsingResults": [
                {"markdown": {"text": f"page {index}"}} for index in range(32)
            ],
        },
    }
    with pytest.raises(client.DebugClientError, match="33.*32"):
        client.parse_markdown_pages(body)


def test_debug_client_requires_restructure_endpoint_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _load_client()
    _DebugHandler.expose_restructure = False
    try:
        with _serve(_DebugHandler) as url, client.httpx.Client(
            trust_env=False
        ) as http:
            monkeypatch.setattr(client, "PADDLEX_API_URL", url)
            with pytest.raises(client.DebugClientError, match="restructure-pages"):
                client.verify_paddlex(http, require_restructure=True)
    finally:
        _DebugHandler.expose_restructure = True
```

- [ ] **Step 2: 运行 RED 测试**

Run:

```bash
.sglang/bin/python -m pytest \
  tests/test_paddle_ocr_sglang_debug.py::test_debug_client_rejects_reported_pdf_page_truncation \
  tests/test_paddle_ocr_sglang_debug.py::test_debug_client_requires_restructure_endpoint_when_requested -q
```

Expected: FAIL，分别因为 `parse_markdown_pages` 未验证 `dataInfo.numPages`，以及 `verify_paddlex` 没有 `require_restructure` 参数。

- [ ] **Step 3: 实现最小协议验证**

在 debug client 中：

```python
RESTRUCTURE_PATH = "/restructure-pages"


def verify_paddlex(
    client: httpx.Client, *, require_restructure: bool = False
) -> None:
    response = client.get(f"{PADDLEX_API_URL.rstrip('/')}/openapi.json")
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError as exc:
        raise DebugClientError("PaddleX /openapi.json returned malformed JSON") from exc
    paths = body.get("paths") if isinstance(body, dict) else None
    if not isinstance(paths, dict) or not isinstance(
        paths.get("/layout-parsing", {}).get("post"), dict
    ):
        raise DebugClientError("PaddleX does not expose POST /layout-parsing")
    if require_restructure and not isinstance(
        paths.get(RESTRUCTURE_PATH, {}).get("post"), dict
    ):
        raise DebugClientError("PaddleX does not expose POST /restructure-pages")
```

将 parser 签名改为：

```python
def parse_markdown_pages(
    body: Any, expected_pages: int | None = None
) -> list[str]:
```

优先使用显式 `expected_pages`；没有显式值时，如果 `dataInfo.type` 为 `pdf` 或 `tiff` 且 `numPages` 是非 bool 正整数，就用它校验 `len(layoutParsingResults)`。错误必须同时包含 expected 和 actual，不打印响应正文。

- [ ] **Step 4: 运行 Task 1 GREEN 测试和现有 debug suite**

Run:

```bash
.sglang/bin/python -m pytest tests/test_paddle_ocr_sglang_debug.py -q
```

Expected: 全部 PASS；原有无 `dataInfo` fake responses 保持兼容。

---

### Task 2: 用 Poppler 生成有序、受限的 PDF chunks

**Files:**
- Modify: `tests/test_paddle_ocr_sglang_debug.py`
- Modify: `scripts/debug_OCR_with_SGLang_backend.py`

**Interfaces:**
- Consumes: PDF `Path`、临时目录 `Path`、系统 `pdfseparate`/`pdfunite`。
- Produces: `prepare_pdf_chunks(path: Path, temp_dir: Path) -> tuple[int, list[tuple[Path, int]]]`，其中 tuple 第一项为源页数，每个 chunk tuple 为 `(chunk_path, chunk_page_count)`。

- [ ] **Step 1: 写真实 Poppler round-trip 失败测试**

测试使用项目已有 `fitz` 创建 33 页小 PDF，跳过条件仅为系统缺少 `pdfseparate` 或 `pdfunite`：

```python
@pytest.mark.skipif(
    shutil.which("pdfseparate") is None or shutil.which("pdfunite") is None,
    reason="Poppler PDF tools are unavailable",
)
def test_prepare_pdf_chunks_preserves_all_pages_in_order(tmp_path: Path) -> None:
    import fitz

    source = tmp_path / "source.pdf"
    document = fitz.open()
    try:
        for width in range(101, 134):
            document.new_page(width=width, height=200)
        document.save(source)
    finally:
        document.close()

    client = _load_client()
    work = tmp_path / "work"
    work.mkdir()
    total, chunks = client.prepare_pdf_chunks(source, work)

    assert total == 33
    assert [count for _, count in chunks] == [32, 1]
    widths = []
    for chunk, _ in chunks:
        chunk_document = fitz.open(chunk)
        try:
            widths.extend(round(page.rect.width) for page in chunk_document)
        finally:
            chunk_document.close()
    assert widths == list(range(101, 134))
```

另加缺失工具测试，通过 monkeypatch `shutil.which` 返回 `None`，断言 `DebugClientError` 包含缺失 executable 名称。

- [ ] **Step 2: 运行 RED 测试**

Run:

```bash
.sglang/bin/python -m pytest \
  tests/test_paddle_ocr_sglang_debug.py::test_prepare_pdf_chunks_preserves_all_pages_in_order -q
```

Expected: FAIL with `AttributeError: ... prepare_pdf_chunks`。

- [ ] **Step 3: 实现无 shell 的 Poppler helper**

新增 imports 和常量：

```python
import shutil
import subprocess

PDF_CHUNK_PAGES = 32
```

实现要求：

```python
def prepare_pdf_chunks(
    path: Path, temp_dir: Path
) -> tuple[int, list[tuple[Path, int]]]:
```

- 用 `shutil.which` 获取两个 executable；任一缺失立即抛 `DebugClientError`。
- 调用 `subprocess.run([pdfseparate, str(path), str(temp_dir / "page-%06d.pdf")], check=True, capture_output=True, text=True, timeout=120)`。
- 用 `temp_dir.glob("page-*.pdf")` 排序；空列表视为无有效页。
- 页数不超过 32 时返回 `[(path, page_count)]`，保持远端单请求使用原文件。
- 超过 32 时按 32 分组；每组调用 `subprocess.run([pdfunite, *map(str, group), str(chunk_path)], ...)`。
- 捕获 `OSError`、`subprocess.SubprocessError`，错误只包含工具名和安全摘要，不包含文档 bytes 或 stderr 全文。

- [ ] **Step 4: 运行 Task 2 GREEN 测试**

Run:

```bash
.sglang/bin/python -m pytest \
  tests/test_paddle_ocr_sglang_debug.py::test_prepare_pdf_chunks_preserves_all_pages_in_order -q
```

Expected: PASS，chunks 为 32+1 且 page width 顺序不变。

---

### Task 3: 编排 chunk OCR 和全局页面重组

**Files:**
- Modify: `tests/test_paddle_ocr_sglang_debug.py`
- Modify: `scripts/debug_OCR_with_SGLang_backend.py`

**Interfaces:**
- Consumes: Task 1 的 parser/probe、Task 2 的 `prepare_pdf_chunks`。
- Produces: `request_document(client: httpx.Client, path: Path) -> Any`，返回可直接传给 `parse_markdown_pages` 的最终 PaddleX body。

- [ ] **Step 1: 扩展 fake server 并写 33 页端到端失败测试**

fake server 必须分别记录 `/layout-parsing` 与 `/restructure-pages` 请求。测试创建 33 页 PDF，并让 server：

- 第一次 layout 请求返回 32 个有序 raw pages 与 `dataInfo.numPages=32`。
- 第二次 layout 请求返回 1 个 raw page 与 `dataInfo.numPages=1`。
- restructure 请求断言收到 33 个 pages，再返回 33 个 Markdown pages。

测试断言：

```python
assert client.main([str(document)]) == 0
assert [request["restructurePages"] for request in layout_requests] == [False, False]
assert [len(request["pages"]) for request in restructure_requests] == [33]
assert "===== Page 33 =====\npage 33\n" in captured.out
```

同时断言每个 layout payload 保留 `maxNewTokens=8192` 及原有 quality switches，final restructure payload 为：

```python
{
    "pages": ordered_pages,
    "mergeTables": True,
    "relevelTitles": True,
    "concatenatePages": False,
    "prettifyMarkdown": True,
    "showFormulaNumber": True,
    "logId": "paddleocr-sglang-debug-restructure",
}
```

- [ ] **Step 2: 运行 RED 端到端测试**

Run:

```bash
.sglang/bin/python -m pytest \
  tests/test_paddle_ocr_sglang_debug.py::test_debug_client_chunks_and_restructures_33_page_pdf -q
```

Expected: FAIL，因为当前 `main` 仍只调用一次 `/layout-parsing`。

- [ ] **Step 3: 实现请求 helper 和全局 restructure**

新增：

```python
def _post_json(client: httpx.Client, path: str, payload: dict[str, Any]) -> Any:
    response = client.post(f"{PADDLEX_API_URL.rstrip('/')}{path}", json=payload)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise DebugClientError(f"PaddleX {path} returned malformed JSON") from exc
```

实现：

```python
def request_document(client: httpx.Client, path: Path) -> Any:
```

数据流必须是：

1. 图片：当前 payload，单次 `/layout-parsing`。
2. PDF：在 `TemporaryDirectory(prefix="paddleocr-debug-")` 中调用 `prepare_pdf_chunks`。
3. PDF 不超过 32 页：当前 payload，单次 `/layout-parsing`，并让 parser 按源页数校验。
4. PDF 超过 32 页：每个 chunk payload 设置 `restructurePages=False`；每个 response 用 chunk known count 校验后，仅收集 `prunedResult` 和 `markdown.images`。
5. 所有 chunk 成功后调用一次 `/restructure-pages`；任何 chunk 失败时不得发 restructure 请求。
6. final response 必须按源总页数校验。

不要并行 chunk 请求；顺序提交可保持确定性，并避免在现有 OCR 并发之外增加内存压力。

- [ ] **Step 4: 将 `main` 切换到 `request_document`**

保留现有 probe、timeout、异常映射和输出循环。仅将直接 `client.post('/layout-parsing', ...)` 替换为：

```python
body = request_document(client, path)
pages = parse_markdown_pages(body)
```

超过 32 页之前调用 `verify_paddlex(client, require_restructure=True)`；短 PDF 与图片只要求 `/layout-parsing`。

- [ ] **Step 5: 运行 Task 3 GREEN 和完整 debug suite**

Run:

```bash
.sglang/bin/python -m pytest tests/test_paddle_ocr_sglang_debug.py -q
.sglang/bin/python -m compileall -q scripts/debug_OCR_with_SGLang_backend.py
```

Expected: 全部 PASS，compileall exit 0。

---

### Task 4: 回归验证和真实 38 页 smoke test

**Files:**
- Test: `tests/test_paddle_ocr_sglang_debug.py`
- Test: `tests/test_read_file_paddleocr_client.py`
- Test: `tests/test_paddle_ocr_sglang_config.py`
- Test: `tests/test_paddle_ocr_startup.py`

**Interfaces:**
- Consumes: 完整 chunking implementation 与当前运行的用户服务。
- Produces: 测试证据和 38 页完整终端输出；不修改服务生命周期。

- [ ] **Step 1: 运行相关自动化回归**

Run:

```bash
.sglang/bin/python -m pytest \
  tests/test_paddle_ocr_sglang_debug.py \
  tests/test_read_file_paddleocr_client.py \
  tests/test_paddle_ocr_sglang_config.py \
  tests/test_paddle_ocr_startup.py -q
```

Expected: 全部 PASS；仅允许仓库已有、与本变更无关的已知 warning。

- [ ] **Step 2: 运行真实 38 页 debug smoke test**

Run:

```bash
.paddle-ocr/bin/python scripts/debug_OCR_with_SGLang_backend.py \
  "/home/zk/Documents/Jetson_Thor_Module_Carrier_Board_Spec_SP-12533-001_v1.2.pdf"
```

Expected:

- exit 0；
- 输出包含 `===== Page 38 =====`；
- Page 21/24 若继续作为合法 merged continuation，显示 merge notice 而非失败；
- 不输出 Base64、timing 或完整内部 JSON；
- 不停止或替换 `8118`/`30002`。

- [ ] **Step 3: 检查最新 SGLang/PaddleX 日志**

确认 smoke test 没有新增 traceback、scheduler exception 或 HTTP 5xx。记录 `finish_reason=length` 只能作为模型质量诊断，不能把合法 merged continuation 误报为空页 OCR 失败。

- [ ] **Step 4: 请求只读代码审查**

审查范围限定为：

```text
scripts/debug_OCR_with_SGLang_backend.py
tests/test_paddle_ocr_sglang_debug.py
```

审查重点：页序、临时文件清理、subprocess 安全、response count、跨 chunk restructure，以及 payload/内部响应泄露风险。
