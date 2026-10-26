# Included upstream source and license texts

These records cover incorporated source/adaptations, not all runtime packages.
See the root [third-party notices](../../THIRD_PARTY_NOTICES.md) for scope and
release conditions. Full license files below were retrieved unchanged from the
listed upstream URLs on 2026-10-26. Existing adaptations do not pin an upstream
commit; current upstream texts are evidence, not proof of the original copying
revision. Preserve any additional notices supplied with the actual source used.

| Included material | Upstream license text | Local full text |
|---|---|---|
| Aider algorithms in `backend/tools/editblock_utils.py`; acknowledged retry logic in `backend/tools/file_ops.py` | [Aider LICENSE.txt](https://raw.githubusercontent.com/Aider-AI/aider/main/LICENSE.txt), Apache-2.0 | [Aider-Apache-2.0.txt](Aider-Apache-2.0.txt) |
| Adapted Qwen3.5-4B template in `config/chat-templates/qwen-native-cache-stable.jinja` | [Qwen3.5-4B LICENSE](https://huggingface.co/Qwen/Qwen3.5-4B/raw/main/LICENSE), Apache-2.0; Copyright 2026 Alibaba Cloud | [Qwen3.5-4B-Apache-2.0.txt](Qwen3.5-4B-Apache-2.0.txt) |
| Upstream context in `patch/sglang-flashinfer-fast-prefill-uniform-q-len.patch` | [SGLang LICENSE](https://raw.githubusercontent.com/sgl-project/sglang/main/LICENSE), Apache-2.0; Copyright 2023-2024 SGLang Team | [SGLang-Apache-2.0.txt](SGLang-Apache-2.0.txt) |
| Upstream SearXNG configuration in `deploy/searxng/core-config/settings.yml.new` | [SearXNG LICENSE](https://github.com/searxng/searxng/blob/master/LICENSE), AGPL-3.0; [upstream settings](https://github.com/searxng/searxng/blob/master/searx/settings.yml) | [AGPL-3.0 full text](../python/texts/AGPL-3.0.txt) |

SHA-256 of the exact downloaded license files:

| File | SHA-256 |
|---|---|
| `Aider-Apache-2.0.txt` | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |
| `Qwen3.5-4B-Apache-2.0.txt` | `bbedc3fda3305820b977265f01b8619d87570a6739de3a5582c3464840f1e57a` |
| `SGLang-Apache-2.0.txt` | `1495e1e757ef4d0925a2350563cf5754bb23c51701a8ec4fb3c5cdcbedae6747` |

The modification descriptions in the root notices identify local adaptations.
Aider's retrieved license contains the Apache appendix's unfilled example
copyright line; this is retained verbatim rather than assigning an invented
owner or year. Upstream root `NOTICE` URLs for Aider, Qwen3.5-4B, and SGLang
returned HTTP 404 on the retrieval date. This is not a claim that every release,
subcomponent, or earlier version lacks additional notices.

SearXNG's upstream settings structure and extensive comments are present in the
`.new` copy. That incorporated material is excluded from the root MIT grant.
For distribution, retain its AGPL terms and fulfill the applicable source and
modification requirements, or omit the unused copy. An attribution inventory
alone cannot authorize relicensing or certify compliance.

## Demo image: permission not established

The repository contains `tests/fixtures/paddleocr/doc_test_rotated.jpg`.
`scripts/paddle_ocr_preflight.py` records its upstream location as
[PaddleX demo image](https://paddle-model-ecology.bj.bcebos.com/paddlex/demo_image/doc_test_rotated.jpg)
and SHA-256
`6fb6b8ea8836a0acb46762a85d2bcf97100e33f0c4b49dcfc0b97f5955f4e534`.
No separate license or redistribution permission for the image has been
established here. Do not infer one from Paddle's software license, public
download access, or this project's MIT license. A release containing it needs
permission evidence; otherwise exclude or replace it with an authorized asset.
