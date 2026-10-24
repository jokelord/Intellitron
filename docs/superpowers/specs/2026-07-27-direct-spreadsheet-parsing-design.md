# Direct Spreadsheet Parsing Design

## Problem

Read File v7 currently parses `.xlsx` directly but classifies `.xls` and `.ods`
as recognized placeholders. The three spreadsheet formats must be readable
without LibreOffice conversion.

MarkItDown 0.1.6 natively registers `XlsxConverter` and `XlsConverter`; it does
not register an ODS converter. Therefore the implementation must not claim that
all three formats are natively supported by MarkItDown. XLSX and XLS use their
official MarkItDown converters, while ODS uses pandas with odfpy directly.

## Goals

- Parse `.xlsx`, `.xls`, and `.ods` into ordered per-sheet Markdown without
  invoking LibreOffice.
- Preserve the existing XLSX formula annotations, archive validation, bounds
  checks, and embedded-image OCR behavior.
- Apply configured sheet, row, column, and cell limits to XLS and ODS.
- Reject malformed or disguised ODS containers before parsing.
- Preserve stable Read File response, cache, abort, and error behavior.

## Non-Goals

- Adding an ODS converter to MarkItDown or modifying dependency source code.
- Converting spreadsheet files to PDF, XLSX, or another intermediate format.
- Adding embedded-image extraction or formula reconstruction for XLS or ODS.
- Reproducing spreadsheet formatting, charts, macros, pivot tables, or formula
  recalculation.
- Renaming persisted `max_xlsx_*` configuration keys; they become the shared
  spreadsheet limits for backward compatibility.

## Routing and Detection

`FileRoute.XLSX` becomes `FileRoute.SPREADSHEET`. The detector routes all three
extensions to this route:

- `.xlsx`: retain the OOXML ZIP signature and required-part validation.
- `.xls`: retain the OLE compound-document signature validation.
- `.ods`: require a ZIP container, a `mimetype` member whose exact decoded value
  is `application/vnd.oasis.opendocument.spreadsheet`, and `content.xml`.

The existing archive safety validator still checks ODS member paths, member
count, expanded bytes, and compression ratio. `render_office_to_pdf()` continues
to reject Calc formats and is never called by the spreadsheet route.

## Parser Architecture

`spreadsheet_parser.py` exposes
`parse_spreadsheet(path, config) -> list[SheetResult]` and dispatches by the
lowercase suffix. Unknown suffixes fail with `UNSUPPORTED_FORMAT`.

### XLSX

The current parser remains the XLSX implementation:

1. Stream-validate OOXML sheet count, coordinates, and cell count before
   MarkItDown.
2. Convert through MarkItDown with `file_extension=".xlsx"`.
3. Read workbook and cached values through openpyxl to preserve sheet order and
   append formula annotations.
4. Keep embedded-media extraction and OCR in the service for XLSX only.

### XLS

XLS is processed without conversion:

1. Open with xlrd in on-demand mode and validate sheet count, each sheet's
   `nrows`/`ncols`, and aggregate `nrows * ncols` against the existing
   `max_xlsx_*` settings.
2. Close the validation workbook.
3. Convert the original file through MarkItDown with
   `file_extension=".xls"`, using its official `XlsConverter`.
4. Split the resulting Markdown by the validated ordered sheet names into
   `SheetResult` values.

### ODS

ODS is processed without conversion:

1. Call `pandas.read_excel(path, sheet_name=None, engine="odf")`.
2. Preserve mapping order as workbook sheet order.
3. Treat the DataFrame columns as the header row and validate sheet count,
   `len(frame.index) + 1` rows when columns exist, column count, and aggregate
   rows multiplied by columns against `max_xlsx_*`.
4. Render each DataFrame with `DataFrame.to_markdown(index=False)`, prefixed by
   `## <sheet name>`, and return ordered `SheetResult` values.

ODS archive expansion is bounded before pandas runs. DataFrame bounds are
validated immediately after odfpy parsing and before Markdown rendering.

## Service Behavior

The service replaces `_read_xlsx` with `_read_spreadsheet`. It always calls
`parse_spreadsheet`. It calls `extract_embedded_images` only when the source
suffix is `.xlsx`; XLS and ODS return their sheet text without OCR calls.

The existing cache key already includes detected format and route, so XLSX,
XLS, and ODS results remain distinct. Line-range rendering and abort checks are
unchanged. The placeholder branch is removed because no supported spreadsheet
extension uses it.

## Dependencies

The SGLang environment requirements change to:

```text
markitdown[xlsx,xls]>=0.1.6,<0.2
odfpy>=1.4.1,<2.0
tabulate>=0.9.0,<1.0
```

The XLS extra provides xlrd for both bounds inspection and MarkItDown's official
XLS converter. odfpy is pandas' ODS engine. tabulate is the documented backend
used by `DataFrame.to_markdown`.

## Errors

- Invalid XLSX structure, invalid XLS/OLE workbook data, invalid ODS structure,
  dependency failures, and parser failures return `SPREADSHEET_INVALID` with a
  format-specific safe message.
- Limit violations return `SPREADSHEET_INVALID` and identify the exceeded
  spreadsheet dimension without exposing workbook data.
- An arbitrary ZIP renamed to `.ods` returns `MIME_MISMATCH`.
- No spreadsheet failure falls back to LibreOffice or OCR.

## Testing

- Routing tests assert all three extensions resolve to
  `FileRoute.SPREADSHEET` and malformed ODS containers are rejected.
- XLSX regressions retain sheet order, Chinese names, formulas, bounds, and
  deduplicated embedded-image OCR.
- XLS parser tests verify ordered sheet splitting, official MarkItDown `.xls`
  dispatch, xlrd bounds enforcement before conversion, and safe parser errors.
- A real ODS fixture verifies ordered sheets, Chinese text, Markdown tables,
  configured bounds, and service output.
- Service tests assert XLS and ODS never call OCR or
  `render_office_to_pdf()`.
- Focused Read File tests and the existing complete Read File suite run after
  implementation.

## Acceptance Criteria

- Valid `.xlsx`, `.xls`, and `.ods` files return table content through
  `ReadFileService.read()`.
- No path for these extensions invokes LibreOffice.
- XLS uses MarkItDown's official converter; ODS uses pandas+odfpy and is not
  described as native MarkItDown support.
- Existing XLSX media OCR and formula behavior remains unchanged.
- Invalid containers and configured-limit violations fail safely.
