# Read File v7 Thor Acceptance

This manifest records required cases. Paths are intentionally explicit. Missing hardware fixtures are reported as missing and never converted into synthetic performance data.

<!-- READ_FILE_V7_MANIFEST_BEGIN -->
{
  "schemaVersion": 1,
  "cases": [
    {"id": "pdf-1-page", "format": "pdf", "path": "tests/fixtures/read-file-v7/pdf-1-page.pdf", "pages": 1},
    {"id": "pdf-10-pages", "format": "pdf", "path": "tests/fixtures/read-file-v7/pdf-10-pages.pdf", "pages": 10},
    {"id": "pdf-50-pages", "format": "pdf", "path": "tests/fixtures/read-file-v7/pdf-50-pages.pdf", "pages": 50},
    {"id": "pdf-200-pages", "format": "pdf", "path": "tests/fixtures/read-file-v7/pdf-200-pages.pdf", "pages": 200},
    {"id": "docx-20-pages", "format": "docx", "path": "tests/fixtures/read-file-v7/docx-20-pages.docx", "pages": 20},
    {"id": "pptx-50-slides", "format": "pptx", "path": "tests/fixtures/read-file-v7/pptx-50-slides.pptx", "pages": 50},
    {"id": "xlsx-20-sheets", "format": "xlsx", "path": "tests/fixtures/read-file-v7/xlsx-20-sheets.xlsx", "sheets": 20},
    {"id": "xlsx-100-images", "format": "xlsx", "path": "tests/fixtures/read-file-v7/xlsx-100-images.xlsx", "images": 100},
    {"id": "image-png", "format": "png", "path": "tests/fixtures/read-file-v7/image.png"},
    {"id": "image-rotated-jpeg", "format": "jpeg", "path": "tests/fixtures/read-file-v7/image-rotated.jpg"},
    {"id": "image-multipage-tiff", "format": "tiff", "path": "tests/fixtures/read-file-v7/image-multipage.tiff"},
    {"id": "image-animated-gif", "format": "gif", "path": "tests/fixtures/read-file-v7/image-animated.gif"},
    {"id": "legacy-xls-placeholder", "format": "xls", "path": "tests/fixtures/read-file-v7/legacy.xls", "expectedError": "UNSUPPORTED_FORMAT"},
    {"id": "ods-placeholder", "format": "ods", "path": "tests/fixtures/read-file-v7/legacy.ods", "expectedError": "UNSUPPORTED_FORMAT"}
  ]
}
<!-- READ_FILE_V7_MANIFEST_END -->

## Prerequisites

- NVIDIA Jetson Thor AArch64 with CUDA 13, the P0-attested full PaddleOCR-VL-1.6 PaddleX service at `127.0.0.1:8118`, and its OCR SGLang backend at `127.0.0.1:30002`.
- LibreOffice Writer and Impress, `libmagic1`, Cairo SVG libraries, and the locked `.paddle-ocr` environment.
- Corpus files listed above. Their absence is a fixture gap, not a passing result.
- Production LibreOffice conversion requires an OS-level sandbox (for example, a dedicated low-privilege account plus systemd/AppArmor restrictions) that denies outbound network access and limits filesystem access to the input and per-request temporary directory. The application also uses a unique profile/HOME/temp directory, disables macros and external-link updates, and poisons proxy access, but those portable controls are not a substitute for kernel-enforced isolation.

## Commands

Functional integration:

```bash
RUN_PADDLE_OCR_INTEGRATION=1 PYTHONPATH=. .sglang/bin/pytest tests/test_read_file_v7_integration.py -m paddle_ocr_integration -v
```

Benchmark matrix:

```bash
PYTHONPATH=. .sglang/bin/python scripts/benchmark_read_file_v7.py --manifest docs/read-file-v7-thor-acceptance.md --output logs/read_file_v7_benchmark.json
```

Twenty-request soak (run after corpus installation):

```bash
PYTHONPATH=. .sglang/bin/python scripts/benchmark_read_file_v7.py --manifest docs/read-file-v7-thor-acceptance.md --output logs/read_file_v7_soak.json --soak 20
```

Optional telemetry such as `tegrastats` is recorded only when available. Missing telemetry remains `null` with a warning; zero is never fabricated. SDPA remains the accepted baseline.
