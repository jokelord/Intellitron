# Intellitron

[简体中文（默认）](README.md) | **English**

Sharing my personal local LLM companion, Intellitron, a local LLM deployment and Agent interaction system for NVIDIA Jetson Thor (ARM64 / SM110a), also usable on NVIDIA DGX/ RTX Spark desktop GPUs. It brings visual capture, programmable workflows, desktop interaction, web research, document reading, and voice input into one Chat / Agent interface for everyday reading and practical work.

## Introduction video

[![Watch the Intellitron introduction video on YouTube](https://img.youtube.com/vi/psKzSR9PDQg/hqdefault.jpg)](https://youtu.be/psKzSR9PDQg)

## Highlights

1. **A visual reading companion**: Vision Automation captures a shared screen or camera at intervals and sends the images to the model, allowing an Agent to help you understand material as you turn pages. See the [Vision page guide](frontend/src/app/vision/README.md).
2. **Automatic Vectorization of Local Memory and Its Fast Matching**: Put `.md`/`.txt` notes in `memory/agent-local/`; the backend builds a zvec semantic-vector index and retrieves relevant passages for later Chat / Agent messages. Before first use, follow the [local memory guide](docs/how-to-use-local-memory.md) to prepare the local embedding model, then enable “Local memory” in the web settings.
3. **Automated workflow orchestration**: Cycle combines waits, messages, photos, and nested loops, with scheduled messages for repeatable Chat / Agent workflows. See the [Cycle configuration](config/config-misc.yaml).
4. **Visual desktop interaction**: The built-in [computer-use-vision Skill](backend/skills/computer-use-vision/SKILL.md) lets a vision model locate targets in screenshots and perform actions such as clicking and scrolling. See the [operations guide](docs/computer-use-vision-operations.md).
5. **Batch web search and fetching**: After following the [SearXNG deployment tutorial](deploy/searxng/01_Docker-Compose安装及searXNG拉取教材.md) and [backend-managed setup guide](deploy/searxng/02_searxng-backend-managed-setup.md), `search_and_fetch` can run batch searches and fetch page bodies concurrently. Copy the [example](deploy/searxng/core-config/settings.yml.example) as `settings.yml`; set its key.
6. **Direct document reading**: With PaddleOCR-VL deployed, the Agent's `read_file` can read PDF, Word, and other documents. Office formats such as Word require LibreOffice conversion. See the OCR startup instructions below.
7. **Automatic voice input and dictation**: Deploy the separate Qwen3-ASR service to choose between voice mode, which automatically sends recognized speech, and dictation mode, which produces editable text. Dictation Magic Send exactly matches a configured command at the end of transcribed input, removes it, and triggers sending, reducing manual clicks. See the [ASR configuration](config/config-asr.yaml) and startup instructions below.
8. **Parallel task delegation**: Choose `manager_agent` as the entry profile to delegate independent tasks to multiple `basic_agent` children concurrently, with progress shown in the interface. See the [Agent configuration](config/config-agent-operation.yaml).
9. **Tools selected for each task**: Select the required tools in settings before creating an Agent context. Once its request is accepted, the tool selection and corresponding prompt/API schema are pinned to that context, keeping its capabilities aligned with the task.
10. **GUI launch and diagnostic feedback**: The [running-linux-gui-commands Skill](backend/skills/running-linux-gui-commands/SKILL.md) resolves the Linux graphical session and reports launch status, liveness, exit codes, and logs. Combine it with the desktop vision Skill to inspect the actual interface.


## Architecture

The project has three layers:

```text
Next.js frontend (default 3000) → FastAPI backend (default 8090) → SGLang / vLLM inference service
```

Model, inference engine, server, and UI settings are routed to individual configuration files through [`config/config-overall.yaml`](config/config-overall.yaml). Its `infer` field determines the model and inference configuration in use.

## Feature overview

### Chat / Agent modes

- **Regular Chat** uses `/v1/chat/completions` for single-turn or multi-turn conversations.
- **Agent** uses `/v1/agent/chat` to run a tool-calling loop within a reusable session. The frontend separately displays reasoning, tool calls, and observations, and provides Stop, session restoration, and child Agent event streams.
- Both modes support streaming output. Image context from the Vision page follows the Vision request rules; frontend display content must not be treated directly as model history.

### Agent routing and parallel dispatch

[`config/config-agent-operation.yaml`](config/config-agent-operation.yaml) selects the entry profile through `agent.entry_agent_name`, currently configured as `basic_agent`:

- `manager_agent` interprets tasks, performs necessary read-only research, and delegates to `basic_agent` children through its sole dispatch tool, `dispatching_parallel_agent`.
- Each dispatch call starts one child. Independent children can run concurrently in the same turn, sharing an ordered blackboard. `agent.manager_agent_max_parallel_children` sets the concurrency limit; consult the configuration for its value.
- `basic_agent` handles general execution, including file changes, commands, searches, and other practical work. Child tasks must include complete context. `explorer_agent` and `auditor_agent` are currently disabled placeholder profiles.
- Setting `agent.entry_agent_name` to `coordinator_1` enables an alternative two-stage route: `coordinator_1` chooses between `coordinator_2` and `basic_agent`; `coordinator_2` handles conversations that need no tools and can hand off to `basic_agent` when needed.

### Keyboard / dictation / voice input

The input bar has three modes:

- **Keyboard**: Type and send text manually.
- **Dictation**: ASR turns speech into editable input text for manual sending. With Dictation Magic Send enabled, an exact configured command at the end of the input is removed and triggers sending.
- **Voice**: Continuously capture speech, recognize utterances, and automatically submit them. ASR runs on a separate Qwen3-ASR SGLang service.

Voice input requires a browser secure context. `localhost` works directly; Android Chrome over a LAN generally requires enabling HTTPS in `config/config-asr.yaml`, generating certificates, and installing the local CA. Keyboard input is unaffected.

### Vision Automation

The Vision page offers browser-local visual automation: choose a shared desktop or camera source, capture frames at intervals, and send each capture cycle to Chat or Agent. The panel supports source health, retries, thumbnails, capture during inference, and Abort. Settings are in the `vision_automation` section of `config/config-vision.yaml`.

For the Vision Automation experience, the project recommends selecting `Ubuntu on Wayland` from the login screen's session menu. When using Firefox, setting `media.suspend-background-video.enabled` to `false` in `about:config` can improve background capture.

### Cycle mode

Cycle is a programmable LLM sequence executor. The frontend supports `wait`, `message`, photo, and nested `cycle` steps, with progress streamed over SSE. At startup, choose whether to use Agent mode and share context. It also supports scheduled messages with a timezone. Default sequences and execution limits are in the `cycle` section of `config/config-misc.yaml`.

## Deployment and startup

Run the following commands from the project root. Startup scripts read `config/config-overall.yaml`; start the required services in numerical order, choosing either SGLang or vLLM for the main inference backend.

### 1. Local SGLang environment

The project uses Python 3.12 and uv, with a shared environment at `.sglang`. Dependencies are listed in [`requirements/requirements_for_sglang.txt`](requirements/requirements_for_sglang.txt):

```bash
uv venv .sglang --python 3.12
source .sglang/bin/activate
uv pip install -r requirements/requirements_for_sglang.txt
```

Comments in the requirements file cover CUDA 13.0 PyTorch, the Jetson-specific `sgl-kernel` wheel, and other installation steps that need separate handling. `sgl-kernel` must match the installed SGLang version.

Before using SearXNG web search with the Agent, install Chromium and its Ubuntu system dependencies:

```bash
# Download Chromium matching the installed Python Playwright version
./.sglang/bin/playwright install chromium

# Install Ubuntu libraries required by Chromium (requires sudo)
sudo ./.sglang/bin/playwright install-deps chromium
```

Start the main model:

```bash
bash scripts/1_A_start_sglang.sh
```

The script reads model paths, ports, context length, memory fraction, attention/quantization backends, reasoning/tool parsers, and prefix-cache settings from the selected `*-config-infer.yaml`, then calls `sglang.launch_server`. The first run of a model configuration on Jetson may trigger Triton/FlashInfer compilation. Quantized models such as NVFP4 can cause high temporary UMA memory usage; if needed, lower context length, memory fraction, and compilation concurrency in the inference configuration.

### 2. vLLM NGC container

On Jetson Thor, the supported vLLM entry point is the NVIDIA NGC container. The current scripts use `nvcr.io/nvidia/vllm:26.06-py3` and require Docker, NVIDIA Container Toolkit, and a model directory.

Optionally reclaim UMA memory before starting:

```bash
bash scripts/0_force_cleanup_uma.sh
```

Choose the entry point for your model directory layout:

```bash
# Hugging Face snapshot layout
bash scripts/1_B_start_vllm_container_with_snapshots.sh

# Standalone model directory
bash scripts/1_C_start_vllm_container.sh
```

Both scripts read model and vLLM server settings from the selected inference configuration and start an OpenAI-compatible API, with a default port of `8000`. Normally, run one main inference backend. If both run, use different ports and select the backend explicitly with `LLM_BACKEND`.

Backend auto-detection tries SGLang → vLLM → a configured OpenAI-compatible proxy → mock. SGLang takes priority when both SGLang and vLLM are running. You can also force the provider:

```bash
LLM_BACKEND=sglang bash scripts/4_start_backend.sh
LLM_BACKEND=vllm bash scripts/4_start_backend.sh
```

Confirm the actual provider using `inference.active_backend` in the backend's `/config` response.

### 3. Optional services, backend, and frontend

```bash
# Optional: choose one OCR deployment
bash scripts/2_optional_A_start_OCR_LM_with_SGLang_backend.sh
bash scripts/2_optional_B_start_OCR_LM_with_transformer.sh

# Optional: separate Qwen3-ASR SGLang service
bash scripts/3_optional_start_ASR_sglang.sh

# FastAPI backend
bash scripts/4_start_backend.sh

# Next.js frontend
bash scripts/5_start_frontend.sh
```

`2_optional_A` and `2_optional_B` are alternative OCR paths; choose one. `config/config-asr.yaml` controls ASR enablement, model path, and port. The backend startup script reuses an existing SearXNG instance; if none is available and Docker is installed, it attempts to start one using the project configuration.

The frontend requires Node.js `>=18.17.0`. `5_start_frontend.sh` reads the frontend port and backend API address. If `node_modules` is missing, it reuses shared dependencies or runs `npm install`.

### Basic verification

```bash
curl http://localhost:8090/health
```

Default frontend addresses:

- Text: `http://localhost:3000/`
- Vision: `http://localhost:3000/vision`

## Core tools in Agent mode

Tools are filtered by Agent profile, configuration, and security rules. `basic_agent` is the full execution profile; `manager_agent` includes file reading/writing, commands, search, Skill reading, and child dispatch by default. For either profile, select general business tools in Web settings before creating a context. Once the backend accepts its request, the tool selection, system prompt, and API tool schema are pinned to that context.

| Tool | Purpose |
|------|---------|
| `read_file` | Read text, code, and supported PDF/Office documents, returning content with line numbers |
| `write_file` | Create or overwrite files |
| `edit_file` | Apply targeted text replacements to existing files |
| `glob` / `grep` | Search allowlisted directories by path patterns or content regular expressions |
| `exec_command` | Execute commands subject to security policy |
| `check_process` | Query matching running processes |
| `playwright-browser-skill` | Perform session-isolated browser automation with Playwright in the project's `.sglang` Python environment; invoke the Skill entry point through `exec_command` |
| `list_skills` / `read_skill` | Discover and read built-in Skill procedures |
| `web_fetch` | Fetch a specified web page |
| `search_and_fetch` | Search the web and fetch result bodies concurrently |
| `wikipedia_search_and_fetch` | Optional Wikipedia search and fetching; unchecked by default in the Agent tool catalog and injected only when explicitly selected |
| MCP tools | Integrate external MCP tools through `mcp.servers` in `config-agent-operation.yaml` |
| `dispatching_parallel_agent` | Available only to `manager_agent`; dispatch one `basic_agent` child per call |

Internal controls such as `final_answer` are not user-configurable core tools. Runtime filtering determines which tools actually appear in a profile's prompt and API schema. The legacy `playwright-cli` / `npx playwright-cli` commands are no longer routed through `exec_command`; browser automation must read and use `playwright-browser-skill`.

## Configuration files

`config/config-overall.yaml` routes configuration to the following files:

| File | Settings |
|------|----------|
| `config/*-config-infer.yaml` | Model metadata, sampling, and SGLang/vLLM loading, ports, context length, memory, parallelism, quantization, attention backends, reasoning/tool parsers, and prefix cache. Model metadata now lives in the inference file; legacy `*-config-model.yaml` files are compatibility fallbacks. |
| `config/config-server.yaml` | FastAPI host/port, frontend development port and API address, streaming timeouts, and OCR endpoints/timeouts. |
| `config/config-webUI.yaml` | UI theme, WebGL effects, layout, input/button/avatar sizes, welcome page, and Agent step display. |
| `config/config-misc.yaml` | `/compact`, runtime prompts, debugging/logging, platform settings, and default Cycle sequences. |
| `config/config-agent-operation.yaml` | Agent enablement, entry profile, profiles, concurrency, document-reading limits, MCP, SearXNG, and web fetching. |
| `config/config-agent-prompts.yaml` | System prompts for `coordinator_1`, `coordinator_2`, `manager_agent`, and `basic_agent`; routing/dispatch conventions, VLM instructions, and compact templates. |
| `config/config-asr.yaml` | Browser audio formats, segmentation, Silero VAD, Qwen3-ASR SGLang service, transcription, Dictation Magic Send, and browser HTTPS requirements. |
| `config/config-vision.yaml` | Image upload formats/limits, preprocessing, camera initialization/photos, and Vision Automation capture, compression, timeout, and retry limits. |

`config/config-server.yaml` also references OCR pipeline configuration, including `config/paddleocr-vl-thor.yaml`, `config/paddleocr-vl-thor-sglang.yaml`, and the model manifest.

Typical configuration workflow:

1. Check `config/config-overall.yaml` for the selected inference, server, miscellaneous, and other configuration paths.
2. Edit the corresponding file. Restart inference for model/engine startup parameters and ports, and restart the appropriate backend or frontend script for its settings.
3. Verify through startup logs, health checks, and backend `/config`. Existing browser-cached Web settings take priority over YAML defaults.

## Directory overview

This example uses the local checkout directory name `LLM-deployment`:

```text
LLM-deployment/
├── backend/                 # FastAPI, LLM providers, Agent, Cycle, ASR, and tools
├── frontend/                # Separate Next.js Text / Vision pages
├── config/                  # Configuration router and feature configuration
├── scripts/                 # Numbered inference, auxiliary service, backend, and frontend startup scripts
├── requirements/            # SGLang and OCR dependencies
├── logs/                    # Generated runtime logs
└── .sglang/                 # uv Python environment (usually excluded from version control)
```

[MIT](LICENSE); [third-party terms](THIRD_PARTY_NOTICES.md), including AGPL/GPL, also apply.
