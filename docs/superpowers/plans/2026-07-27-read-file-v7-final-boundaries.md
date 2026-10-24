# Read File v7 Final Boundaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Execute inline because Git and delegation are prohibited.

**Goal:** Enforce bounded PDF overlap, permit token-proven safe SVG color functions, and correct the impossible-leaf error text.

**Architecture:** Validate PDF overlap at config construction and low-level range generation, preserving the invariant that emitted chunks never exceed `chunk_pages`. Parse SVG presentation values with tinycss2, accepting only safe color functions while rejecting external/escaped URLs and retaining the deny-fetch runtime boundary.

**Tech Stack:** Python 3.12, dataclasses, PyMuPDF, tinycss2, CairoSVG, pytest.

## Global Constraints

- No Git, sudo, delegation, or third-party source edits.
- Require `0 <= pdf_chunk_overlap_pages < pdf_chunk_pages`.
- Preserve 24/1 chunk layout and hard PDF byte caps.
- Preserve static URL rejection and the deny-all SVG fetcher.

### Task 1: PDF Overlap and Emitted-size Invariants

**Files:** `backend/config.py`, `backend/tools/read_file/pdf_chunker.py`, `tests/test_read_file_office_pdf.py`, `tests/test_runtime_config_limits.py`

- [ ] Add RED tests for overlap equal/greater than chunk size at `ReadFileConfig` construction and `_page_ranges`, and assert every 24/1 emitted range has at most 24 pages.
- [ ] Add `ReadFileConfig.__post_init__` validation and low-level validation; assert emitted range size before yielding/materializing.
- [ ] Run exact tests GREEN.

### Task 2: Parsed Safe SVG Colors

**Files:** `backend/tools/read_file/image_normalizer.py`, `tests/test_read_file_images.py`

- [ ] Add RED normalization tests for `rgb()`, `rgba()`, `hsl()`, and `hsla()`, plus plain/escaped external URL attacks and deny-fetch retention.
- [ ] Parse presentation values with tinycss2. Permit only safe color functions for color-capable attributes; fail closed on URL tokens, parse errors, CSS escapes, and non-color functions. Permit `url(#fragment)` only when tokenized as a document-local fragment; the runtime fetcher remains deny-all.
- [ ] Run exact tests GREEN.

### Task 3: Leaf Error and Final Evidence

**Files:** `backend/tools/read_file/pdf_chunker.py`, `tests/test_read_file_office_pdf.py`, `.superpowers/sdd/tasks-2-11-report.md`

- [ ] Add a RED assertion for exact impossible-leaf wording.
- [ ] Change the message to `overlap-expanded PDF leaf exceeds chunk byte limit` and run GREEN.
- [ ] Run the focused Read File/OCR suite, Python compilation, shell syntax, benchmark smoke and missing-corpus negative acceptance.
- [ ] Append exact evidence and remaining concerns to the report.
