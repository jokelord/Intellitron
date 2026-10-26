# Third-party licenses and notices

The root [MIT license](LICENSE) grants rights in original Intellitron project
material only to the extent that its contributors own or may license those
rights. Third-party code, adaptations, fonts, libraries, models, services, and
other assets retain their own terms. The root license does not replace those
terms or grant rights that an upstream owner has not granted.

This inventory records the inspected source and installed dependencies on
2026-10-26. It is not a certification that every possible installation or
distribution satisfies every license. Resolve the applicable conditions for
the files and artifacts actually shipped, including transitive dependencies,
versions, build options, bundled native libraries, and any later changes.

## Included source and adaptations

| Project material | Upstream source and retained terms |
|---|---|
| `backend/tools/editblock_utils.py` | Core SEARCH/REPLACE algorithms adapted from [Aider's editblock_coder.py](https://github.com/Aider-AI/aider/blob/main/aider/coders/editblock_coder.py); Apache-2.0. Internal Aider dependencies were removed and matching behavior adapted. |
| `backend/tools/file_ops.py` | Write retry logic acknowledges [Aider's io.py](https://github.com/Aider-AI/aider/blob/main/aider/io.py); Apache-2.0 applies to incorporated upstream expression. The project's file policy, tools, and surrounding implementation have been adapted. |
| `config/chat-templates/qwen-native-cache-stable.jinja` | Adapted from [Qwen3.5-4B's chat template](https://huggingface.co/Qwen/Qwen3.5-4B/blob/main/chat_template.jinja); Apache-2.0. Copyright 2026 Alibaba Cloud. Changes preserve historical thinking, JSON scalar values, and tool observation whitespace, and support the repository's raw tool transport. |
| `patch/sglang-flashinfer-fast-prefill-uniform-q-len.patch` | Patch against [SGLang's FlashInfer backend](https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/layers/attention/flashinfer_backend.py); Apache-2.0. Copyright 2023-2024 SGLang Team. Adds an environment-controlled compatibility argument to the planning call. |
| `deploy/searxng/core-config/settings.yml.new` | Contains a full upstream-style [SearXNG settings file](https://github.com/searxng/searxng/blob/master/searx/settings.yml); its original revision is not recorded. Treat the incorporated upstream material under [SearXNG's AGPL-3.0 terms](https://github.com/searxng/searxng/blob/master/LICENSE), rather than the root MIT license. This notice does not supply a missing grant to relicense that file. Preserve the upstream terms and satisfy their source/modification obligations if distributing it; a release may instead omit this unused upstream copy. |

See the [source inventory and full license copies](licenses/source/MANIFEST.md).
Apache-2.0 requires a license copy, retention of applicable notices, prominent
modification notices, and any applicable upstream NOTICE material. MIT for
original contributions does not remove these requirements.

## Frontend packages, fonts, and browser artifacts

The [frontend inventory](licenses/frontend/README.md) records direct package
licenses and bundled assets, including separate KaTeX font terms. Preserve the
applicable notices in distributions of compiled JavaScript, CSS, fonts, or
other browser assets. A package manifest's license label alone does not
describe every asset it distributes.

## Python dependencies and material runtime conditions

The [Python inventory](licenses/python/README.md) includes inspected versions,
license copies, and limitations. In particular:

- **PyMuPDF 1.28.0 / MuPDF:** available under AGPL or a separate commercial
  license. The integrated PDF processing path is not made MIT-only by this
  repository's license. Depending on the combined work and how it is conveyed
  or offered over a network, AGPL obligations can extend to the combined
  application. A compliant AGPL distribution or a suitable commercial license
  is required where those terms apply. Merely adding attribution is insufficient.
- **pillow_heif 1.5.0:** its Python wrapper's permissive license does not cover
  all terms of the inspected binary distribution, which includes GPL codec
  components. An installation or release using that binary must address the
  applicable GPL combination/source obligations or obtain/use a compatible
  alternative artifact and permissions. The root MIT license and this notice
  do not resolve that issue.
- **Crawl4AI 0.9.3:** the installed license is Apache-2.0 with an appended public
  use attribution condition. Preserve the [complete installed license](licenses/python/packages/crawl4ai-0.9.3/LICENSE)
  and satisfy that condition wherever it applies. Its exact required credit is:

  > This product includes software developed by UncleCode (https://x.com/unclecode) as part of the Crawl4AI project (https://github.com/unclecode/crawl4ai).

- **LGPL and MPL dependencies:** library replacement/relinking rights, applicable
  reverse-engineering permissions, notices, and corresponding library source or
  source availability requirements remain applicable. MPL obligations concern
  covered files and modifications; LGPL permits qualifying combinations under
  other terms when its conditions are met. Check the actual version and artifact
  rather than assuming every Python wheel is permissive.

Installing dependencies separately does not automatically settle the license
of an integrated application. Publishing original source under MIT, running a
local installation, offering a network service, and distributing an entire
environment or container are different acts with different requirements.

## Native helper and Qt fixture

The project distributes `bin/linux-aarch64/computer-use-helper`, built from
`backend/computer_use/native/`. Its runtime libraries are dynamically linked;
the shared libraries themselves are not included beside that executable.
These include GLib/GIO/GObject and JSON-GLib under LGPL terms, plus PipeWire,
libei, X11 libraries, and system runtime libraries with their respective terms.
See the [native inventory](licenses/native/README.md) for observed linkage,
notices, and source/replacement obligations. Dynamic linking does not remove
those obligations or require every unrelated project file to use GPL.

The separate `computer_use_fixture/` uses Qt6 Widgets and Test. Its local build
uses dynamically linked Qt6 Widgets/Gui/Core; it is not the shipped Computer Use
helper. If distributing that fixture binary or Qt libraries, comply with the
chosen Qt license, including applicable LGPL source, notices, replacement, and
installation-information requirements, or obtain another valid Qt license.
See [Qt's licensing guidance](https://www.qt.io/development/open-source-lgpl-obligations).

## Separate services, model weights, and NVIDIA software

The deployment starts SearXNG and Valkey as separate services. Communicating
with an independent SearXNG process over HTTP does not by itself relicense
original client code under AGPL. SearXNG's own license and any obligations for
modified/network-served versions or distributed images still apply. The
included upstream settings copy identified above has its own terms regardless
of that process boundary.

Model configurations and download manifests do not grant rights in model
weights, tokenizers, training data, or downloaded model code. For example,
[RadixArk Qwen3.8 NVFP4](https://huggingface.co/RadixArk/Qwen3.8-27B-NVFP4),
[NVIDIA Qwen3.6 NVFP4](https://huggingface.co/nvidia/Qwen3.6-35B-A3B-NVFP4),
[Qwen3.5-4B](https://huggingface.co/Qwen/Qwen3.5-4B), and
[Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) publish Apache-2.0
terms; [multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small)
publishes MIT terms. Verify the exact provider, checkpoint, revision, any
fine-tuning inputs, and accompanying notices before redistributing an artifact.
No model license is changed by the root license.

The [NVIDIA NGC vLLM container](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/vllm)
and CUDA-related components have terms separate from upstream vLLM/SGLang and
this project. NVIDIA currently links its
[Software License Agreement](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-software-license-agreement/)
and [AI Product Specific Terms](https://www.nvidia.com/en-us/agreements/enterprise-software/product-specific-terms-for-ai-products/).
Check the terms supplied with the exact container tag and components being
used or redistributed. Permissive launch-script licensing does not grant a
right to redistribute NVIDIA binaries under MIT.

## Asset with unverified redistribution permission

`tests/fixtures/paddleocr/doc_test_rotated.jpg` is an upstream demo image whose
download URL and checksum are recorded in `scripts/paddle_ocr_preflight.py`.
Its separate redistribution permission has not been established by this audit.
Public availability and an upstream software license do not establish a grant
covering that image. The root MIT license does not cover rights in that image;
before distributing it, establish the required permission or exclude/replace
it with an authorized fixture. See the [source inventory](licenses/source/MANIFEST.md).
