# Selected Python dependency inventory

Observed versions are from the local environments on 2026-10-26; this is not a new lock file. This source release does not ship `.sglang/`, `.paddle-ocr/`, NVIDIA runtimes, downloaded browser binaries, model weights, or the entire installed dependency graph. MIT for project code does not relicense any dependency. See [README.md](README.md) for the entrypoint and full reference license texts.

## Material license boundaries

- **PyMuPDF 1.28.0:** AGPLv3 or an independently obtained Artifex commercial license. `backend/tools/read_file/pdf_chunker.py` directly imports `fitz`, via `service.py` and `file_ops.py`. This is an integrated library independently of the optional OCR service. [Official licensing](https://pymupdf.io/licensing), [versioned source](https://github.com/pymupdf/PyMuPDF/tree/1.28.0). The included wheel `COPYING` is only a short dual-license statement; the complete AGPLv3 text is provided in `texts/AGPL-3.0.txt`. These notices do not waive applicable combined-work obligations.
- **pillow-heif 1.5.0:** BSD Python binding; its installed wheel's `LICENSES_bundled.txt` explicitly labels binary wheels GPLv2 and lists x265 GPLv2 plus libheif/libde265 LGPLv3. The inspected wheel actually contains `pillow_heif.libs/libx265-f173620a.so.216`. The plugin is loaded directly for HEIF/AVIF in `backend/tools/read_file/image_normalizer.py`. [Wrapper source and bundled source references](https://github.com/bigcat88/pillow_heif/tree/v1.5.0). The bundled notice references older component versions than installed library filenames; those upstream references are preserved rather than asserted to be exact binary provenance. Establish the exact build and corresponding sources before redistributing a binary wheel. Complete GPLv2, GPLv3 and LGPLv3 texts are included under `texts/`.
- **CairoSVG 2.9.0:** LGPL-3.0-or-later, directly used for SVG rasterization in `backend/tools/read_file/image_normalizer.py`. [Source](https://github.com/Kozea/CairoSVG/tree/2.9.0). Applicable library source and replacement/relinking rights remain in force. LGPLv3 incorporates GPLv3; both full texts are included.
- **Crawl4AI 0.9.3:** the preserved full LICENSE appends a public-use attribution requirement to Apache-2.0. Imported in `backend/tools/fetch_worker_runtime.py`. Required credit: “This product includes software developed by UncleCode (https://x.com/unclecode) as part of the Crawl4AI project (https://github.com/unclecode/crawl4ai).” Its placement examples include README/NOTICE, website About/Credits and CLI help. [Exact upstream license](https://github.com/unclecode/crawl4ai/blob/v0.9.3/LICENSE).
- **Weak copyleft dependencies:** SGLang declares easydict (LGPLv3) and orjson (MPL-2.0 AND Apache-2.0-or-MIT). Installed Outlines declares pycountry (LGPL-2.1-only). Trafilatura declares Courlan, which declares tld (MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later). Certifi includes Mozilla certificate data under MPL-2.0. Preserve the alternatives and library/file scopes; these are not declarations of the project's overall license.
- **LiteLLM / unclecode-litellm:** their full included licenses exclude any `enterprise/` directory from their MIT grant and refer it to a separate license. No permission for excluded enterprise content is granted here.
- **Optional OCR:** PaddleOCR/PaddleX are Apache-2.0. Selected optional-environment LGPL dependencies and Mozilla certificate notices are included below. Backend OCR requests use HTTP `/layout-parsing` in `backend/tools/read_file/paddleocr_client.py`; the service environment is separately installed. Main-environment chardet 7.6.0 declares 0BSD; optional OCR chardet 5.2.0 is LGPL. Do not conflate these versions.
- **External binaries:** NVIDIA packages carry NVIDIA-specific redistribution conditions. Browser downloads, LibreOffice, inference containers and model weights require their own licenses and inventories. This source inventory supplies no redistribution clearance for those artifacts.

## Preserved package notices

Package files are copied byte for byte from the indicated installed version. `PROVENANCE.tsv` records original repo-relative environment paths and SHA-256 hashes; `SHA256SUMS` supports checking the copies. A wheel without a supplied license file is identified explicitly below; the full Apache reference text is available under `texts/` where relevant. No missing copyright notice is invented. This selected inventory is not a complete SBOM and does not clear every optional feature, transitive dependency, runtime binary, future dependency resolution, or combined-work licensing question.

| Package | Observed version | Environment | Declared / observed license | Source | Included files |
|---|---|---|---|---|---|
| certifi | 2022.12.7 | `.paddle-ocr` | MPL-2.0 | [upstream](https://github.com/certifi/python-certifi) | [LICENSE](packages/certifi-2022.12.7/LICENSE) |
| chardet | 5.2.0 | `.paddle-ocr` | LGPL | [upstream](https://github.com/chardet/chardet) | [LICENSE](packages/chardet-5.2.0/LICENSE) |
| crc32c | 2.8 | `.paddle-ocr` | LGPL-2.1-or-later | [upstream](https://pypi.org/project/crc32c/2.8/) | [LICENSE](packages/crc32c-2.8/licenses/LICENSE)<br>[LICENSE.google-crc32c](packages/crc32c-2.8/licenses/LICENSE.google-crc32c)<br>[LICENSE.slice-by-8](packages/crc32c-2.8/licenses/LICENSE.slice-by-8) |
| cssutils | 2.15.0 | `.paddle-ocr` | LGPL-3.0-or-later | [upstream](https://github.com/jaraco/cssutils) | [LICENSE](packages/cssutils-2.15.0/licenses/LICENSE) |
| encutils | 1.0.0 | `.paddle-ocr` | LGPL-3.0-or-later | [upstream](https://github.com/coherent-oss/encutils) | [LICENSE](packages/encutils-1.0.0/licenses/LICENSE) |
| paddleocr | 3.6.0 | `.paddle-ocr` | Apache License 2.0 | [upstream](https://github.com/PaddlePaddle/PaddleOCR.git) | [LICENSE](packages/paddleocr-3.6.0/LICENSE) |
| paddlex | 3.6.1 | `.paddle-ocr` | Apache-2.0 | [upstream](https://pypi.org/project/paddlex/3.6.1/) | [LICENSE](packages/paddlex-3.6.1/licenses/LICENSE) |
| pypdfium2 | 5.12.1 | `.paddle-ocr` | BSD-3-Clause, Apache-2.0, dependency licenses | [upstream](https://github.com/pypdfium2-team/pypdfium2) | No license file in inspected wheel; consult upstream |
| python-bidi | 0.6.11 | `.paddle-ocr` | See upstream / included license | [upstream](https://github.com/MeirKriheli/python-bidi) | [COPYING](packages/python-bidi-0.6.11/licenses/COPYING)<br>[COPYING.LESSER](packages/python-bidi-0.6.11/licenses/COPYING.LESSER)<br>[LICENSE-THIRD-PARTY.yml](packages/python-bidi-0.6.11/licenses/LICENSE-THIRD-PARTY.yml) |
| CairoSVG | 2.9.0 | `.sglang` | LGPL-3.0-or-later | [upstream](https://github.com/Kozea/CairoSVG/) | [LICENSE](packages/cairosvg-2.9.0/licenses/LICENSE) |
| certifi | 2026.7.22 | `.sglang` | MPL-2.0 | [upstream](https://github.com/certifi/python-certifi) | [LICENSE](packages/certifi-2026.7.22/licenses/LICENSE) |
| chardet | 7.6.0 | `.sglang` | 0BSD | [upstream](https://github.com/chardet/chardet) | [LICENSE](packages/chardet-7.6.0/licenses/LICENSE) |
| charset-normalizer | 3.5.1 | `.sglang` | MIT | [upstream](https://github.com/jawah/charset_normalizer) | [LICENSE](packages/charset-normalizer-3.5.1/licenses/LICENSE) |
| colorama | 0.4.6 | `.sglang` | BSD-3-Clause (included LICENSE) | [upstream](https://pypi.org/project/colorama/0.4.6/) | [LICENSE.txt](packages/colorama-0.4.6/licenses/LICENSE.txt) |
| Crawl4AI | 0.9.3 | `.sglang` | Apache-2.0 | [upstream](https://github.com/unclecode/crawl4ai) | [LICENSE](packages/crawl4ai-0.9.3/licenses/LICENSE) |
| curl_cffi | 0.15.0 | `.sglang` | MIT | [upstream](https://github.com/lexiforest/curl_cffi) | [LICENSE](packages/curl-cffi-0.15.0/licenses/LICENSE) |
| defusedxml | 0.7.1 | `.sglang` | PSFL | [upstream](https://github.com/tiran/defusedxml) | [LICENSE](packages/defusedxml-0.7.1/LICENSE) |
| easydict | 1.13 | `.sglang` | LGPL-3.0 | [upstream](https://github.com/makinacorpus/easydict) | [LICENSE](packages/easydict-1.13/LICENSE) |
| einops | 0.9.0.dev0 | `.sglang` | MIT | [upstream](https://pypi.org/project/einops/0.9.0.dev0/) | [LICENSE](packages/einops-0.9.0.dev0/licenses/LICENSE) |
| fastapi | 0.141.1 | `.sglang` | MIT | [upstream](https://github.com/fastapi/fastapi) | [LICENSE](packages/fastapi-0.141.1/licenses/LICENSE) |
| httpx | 0.28.1 | `.sglang` | BSD-3-Clause | [upstream](https://github.com/encode/httpx) | [LICENSE.md](packages/httpx-0.28.1/licenses/LICENSE.md) |
| huggingface_hub | 1.31.0 | `.sglang` | Apache-2.0 | [upstream](https://github.com/huggingface/huggingface_hub) | [LICENSE](packages/huggingface-hub-1.31.0/licenses/LICENSE) |
| ImageHash | 4.3.2 | `.sglang` | 2-clause BSD License | [upstream](https://github.com/JohannesBuchner/imagehash) | [LICENSE](packages/imagehash-4.3.2/LICENSE) |
| litellm | 1.82.3 | `.sglang` | MIT | [upstream](https://github.com/BerriAI/litellm) | [LICENSE](packages/litellm-1.82.3/LICENSE) |
| markitdown | 0.1.6 | `.sglang` | MIT | [upstream](https://github.com/microsoft/markitdown) | No license file in inspected wheel; consult upstream |
| mcp | 1.26.0 | `.sglang` | MIT | [upstream](https://github.com/modelcontextprotocol/python-sdk) | [LICENSE](packages/mcp-1.26.0/licenses/LICENSE) |
| orjson | 3.12.0 | `.sglang` | MPL-2.0 AND (Apache-2.0 OR MIT) | [upstream](https://github.com/ijl/orjson) | [LICENSE-APACHE](packages/orjson-3.12.0/licenses/LICENSE-APACHE)<br>[LICENSE-MIT](packages/orjson-3.12.0/licenses/LICENSE-MIT)<br>[LICENSE-MPL-2.0](packages/orjson-3.12.0/licenses/LICENSE-MPL-2.0) |
| pillow | 12.3.0 | `.sglang` | MIT-CMU | [upstream](https://github.com/python-pillow/Pillow) | [LICENSE](packages/pillow-12.3.0/licenses/LICENSE) |
| pillow_heif | 1.5.0 | `.sglang` | BSD-3-Clause | [upstream](https://github.com/bigcat88/pillow_heif) | [LICENSE.txt](packages/pillow-heif-1.5.0/licenses/LICENSE.txt)<br>[LICENSES_bundled.txt](packages/pillow-heif-1.5.0/licenses/LICENSES_bundled.txt) |
| playwright | 1.63.0 | `.sglang` | Apache-2.0 | [upstream](https://pypi.org/project/playwright/1.63.0/) | [LICENSE](packages/playwright-1.63.0/licenses/LICENSE) |
| psutil | 7.2.2 | `.sglang` | BSD-3-Clause | [upstream](https://github.com/giampaolo/psutil) | [LICENSE](packages/psutil-7.2.2/LICENSE) |
| pycountry | 26.2.16 | `.sglang` | LGPL-2.1-only | [upstream](https://github.com/pycountry/pycountry) | [LICENSE.txt](packages/pycountry-26.2.16/licenses/LICENSE.txt) |
| pydantic | 2.14.0a1 | `.sglang` | MIT | [upstream](https://github.com/pydantic/pydantic) | [LICENSE](packages/pydantic-2.14.0a1/licenses/LICENSE) |
| pymupdf | 1.28.0 | `.sglang` | Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License | [upstream](https://github.com/pymupdf/pymupdf) | [COPYING](packages/pymupdf-1.28.0/COPYING) |
| python-magic | 0.4.27 | `.sglang` | MIT | [upstream](http://github.com/ahupp/python-magic) | [LICENSE](packages/python-magic-0.4.27/LICENSE) |
| python-multipart | 0.0.32 | `.sglang` | Apache-2.0 | [upstream](https://github.com/Kludex/python-multipart) | [LICENSE.txt](packages/python-multipart-0.0.32/licenses/LICENSE.txt) |
| PyYAML | 6.0.3 | `.sglang` | MIT | [upstream](https://github.com/yaml/pyyaml) | [LICENSE](packages/pyyaml-6.0.3/licenses/LICENSE) |
| readability-lxml | 0.8.4.1 | `.sglang` | Apache License 2.0 | [upstream](http://github.com/buriy/python-readability) | [LICENSE](packages/readability-lxml-0.8.4.1/LICENSE) |
| sentencepiece | 0.2.2 | `.sglang` | Apache-2.0 | [upstream](https://pypi.org/project/sentencepiece/0.2.2/) | No license file in inspected wheel; consult upstream |
| sglang | 0.5.17 | `.sglang` | Apache-2.0 | [upstream](https://pypi.org/project/sglang/0.5.17/) | [LICENSE](packages/sglang-0.5.17/licenses/LICENSE) |
| silero-vad | 6.2.0 | `.sglang` | MIT (included LICENSE) | [upstream](https://pypi.org/project/silero-vad/6.2.0/) | [LICENSE](packages/silero-vad-6.2.0/licenses/LICENSE) |
| smolagents | 1.22.0 | `.sglang` | Apache-2.0 | [upstream](https://pypi.org/project/smolagents/1.22.0/) | [LICENSE](packages/smolagents-1.22.0/licenses/LICENSE) |
| timm | 1.0.16 | `.sglang` | Apache-2.0 | [upstream](https://github.com/huggingface/pytorch-image-models) | [LICENSE](packages/timm-1.0.16/licenses/LICENSE) |
| tld | 0.13.2 | `.sglang` | MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later | [upstream](https://github.com/barseghyanartur/tld/) | [LICENSE.rst](packages/tld-0.13.2/licenses/LICENSE.rst)<br>[LICENSE_GPL2.0.txt](packages/tld-0.13.2/licenses/LICENSE_GPL2.0.txt)<br>[LICENSE_LGPL_2.1.txt](packages/tld-0.13.2/licenses/LICENSE_LGPL_2.1.txt)<br>[LICENSE_MPL_1.1.txt](packages/tld-0.13.2/licenses/LICENSE_MPL_1.1.txt) |
| tokenizers | 0.22.2 | `.sglang` | Apache-2.0 | [upstream](https://github.com/huggingface/tokenizers) | No license file in inspected wheel; consult upstream |
| trafilatura | 2.0.0 | `.sglang` | Apache 2.0 | [upstream](https://github.com/adbar/trafilatura) | [LICENSE](packages/trafilatura-2.0.0/LICENSE) |
| transformers | 5.12.1 | `.sglang` | Apache 2.0 License | [upstream](https://github.com/huggingface/transformers) | [LICENSE](packages/transformers-5.12.1/licenses/LICENSE) |
| unclecode-litellm | 1.81.13 | `.sglang` | MIT | [upstream](https://github.com/unclecode/litellm) | [LICENSE](packages/unclecode-litellm-1.81.13/licenses/LICENSE) |
| uvicorn | 0.52.3 | `.sglang` | BSD-3-Clause | [upstream](https://github.com/Kludex/uvicorn) | [LICENSE.md](packages/uvicorn-0.52.3/licenses/LICENSE.md) |
| zvec | 0.7.0 | `.sglang` | Apache-2.0 | [upstream](https://github.com/alibaba/zvec) | [LICENSE](packages/zvec-0.7.0/licenses/LICENSE)<br>[NOTICE](packages/zvec-0.7.0/licenses/NOTICE) |

The pillow-heif bundled notice also references x265 Release_3.4: its [full upstream COPYING](packages/pillow-heif-1.5.0/x265-COPYING) is preserved. This referenced release is not asserted to identify the exact installed libx265 binary build.
