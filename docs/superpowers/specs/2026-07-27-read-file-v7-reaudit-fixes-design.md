# Read File v7 Re-audit Fixes Design

## Scope

Resolve the seven re-audit findings without changing Agent history, text-file behavior, placeholder spreadsheet behavior, third-party sources, or deployment architecture.

## PDF Ownership and Overlap

Byte splitting operates on an ownership range containing each source page exactly once. An oversized range with more than one owned page is divided into two non-empty, disjoint strict subsets. Overlap is context, not ownership: when a child is materialized, up to `pdf_chunk_overlap_pages` immediately preceding source pages are prepended and recorded in `overlap_source_pages`.

The overlap-expanded PDF is subject to the hard byte cap. If an ownership leaf contains one page and its required overlap makes the PDF exceed the cap, conversion fails with `FILE_TOO_LARGE`. The implementation never drops configured overlap and never emits an oversized chunk. This makes recursion finite while preserving complete, duplicate-free ownership; repeated pages are explicitly overlap only.

## OCR Quality Classification

PaddleOCR response processing has two ordered phases. Protocol validation first checks response shape, page count, page objects, Markdown type, and the exact global page-index convention. Malformed JSON, indexes, or fields remain terminal `OCR_RESPONSE_INVALID` errors.

After protocol validity is established, each page is classified for quality. Empty or shorter-than-configured Markdown and official DocPreprocessor angle evidence at or above the configured severe-angle threshold produce `OCR_PAGE_FAILED`. This code alone enters service-level page splitting and one-page unwarping retry. Tests use the real `PaddleOCRClient` against a local HTTP contract server through `ReadFileService`, including malformed-index cases that must not retry.

## SVG No-fetch Boundary

SVG validation rejects style/script/resource elements, style attributes, CSS escape syntax, external references, and resource-capable presentation attributes containing suspicious escaped or functional values. Rasterization uses CairoSVG's lower-level conversion entry with a URL fetcher that always raises, providing a runtime no-fetch boundary even if static validation misses a representation.

## XLSX Bounds

Workbook and worksheet XML are stream-parsed. Every worksheet `<row r>` and `<c r>` coordinate is validated as it is encountered, in addition to dimensions, aggregate cell count, and sheet count. Invalid, missing, or out-of-range coordinates fail before MarkItDown or openpyxl executes.

## Attestation and Workers

New attestations carry an explicit schema version. Reuse and backend health require that version, `gpuName` identifying Thor, and a parseable Transformers version in `>=5.10.0,<6.0`, in addition to architecture, CUDA, process, socket, config hash, and manifest hash checks. Existing startup-script enforcement remains, and direct application startup validates the configured/environment worker count whenever OCR is enabled.

## Benchmark Telemetry

The benchmark starts one owned `tegrastats` process immediately before executable cases and stops it after all matrix or soak work. A reader thread captures samples throughout the workload. The report includes sample count and explicit maxima/aggregates for available RAM/GPU/temperature/power fields; unavailable or unparseable fields remain null with warnings rather than fabricated values. Smoke mode remains a manifest-only check.

## Agent Contract

The `read_file` tool documentation describes `password` only as an encrypted-PDF password. Office password support is not advertised.

## Verification

Each finding receives a failing regression before production changes. Final verification covers the exact PDF/quality/SVG/XLSX/attestation/telemetry/doc reproductions, the focused Read File/OCR suite, active-suite attribution, Python compilation, shell syntax, live image/PDF/DOCX integration, the 20-request live soak, benchmark smoke, and missing-corpus nonzero acceptance.
