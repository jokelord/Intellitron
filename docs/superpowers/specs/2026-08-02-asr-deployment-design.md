# Qwen3-ASR 本地语音输入部署设计

**日期：** 2026-08-02  
**状态：** 已通过分节设计评审，等待书面规范复核  
**范围：** ASR MVP v1  
**上游需求：** `docs/ASR-部署-PRD.md`  
**目标平台：** NVIDIA Jetson Thor、桌面 Chromium、Android Chrome  

## 1. 摘要

本设计为现有 Text 与 Vision Web UI 增加 Keyboard/Voice 输入模式切换。浏览器负责麦克风权限、轻量 PCM 采集和 SPL 波形展示；Jetson Thor 后端负责软件增益、声压检测、pre-trigger ring buffer、Silero VAD v6.2、录音状态机、WAV 编码和 Qwen3-ASR 推理。

Qwen3-ASR-1.7B 必须通过独立 SGLang 服务推理。项目不使用 `qwen-asr`、Transformers 或 vLLM 作为 ASR 推理后端。默认模型路径为 `/home/zk/Project/LLM-weight/Qwen3-ASR-1.7B`，默认 SGLang 端口为 `30003`。

浏览器与 FastAPI 通过 session-local WebSocket 传输 Float32 PCM。默认完成整句录音后一次性调用 SGLang `/v1/audio/transcriptions`，`transcription.streaming=false`。识别文本默认自动进入当前 Chat/Agent 发送链路；用户可在 YAML 中将 `auto_send` 改为 `false`，改为识别后手动点击 Send。

所有重要 ASR 参数集中在 `config/config-asr.yaml`，由严格 Schema 校验。不得在业务代码或启动脚本复制一套隐藏运行参数。

## 2. 已核实的仓库与运行时事实

1. 当前 Python/uv 环境位于项目根目录 `.sglang`，Python 3.12。
2. 已安装 SGLang `0.5.15`。该版本包含 `Qwen3ASRForConditionalGeneration`、Qwen3-ASR transcription adapter、`POST /v1/audio/transcriptions` 和 realtime ASR 代码。
3. 模型目录存在，`model.safetensors.index.json` 声明的两个 shard 均存在；`config.json` 和 tokenizer 文件存在。
4. `.sglang` 已有 ONNX Runtime CPU provider、NumPy、SciPy、SoundFile 和 TorchAudio；缺少 `silero-vad` 包。
5. 当前主 SGLang 常用端口为 `30000`，OCR SGLang 使用 `30002`。ASR 使用 `30003`，不使用历史占用过的 `30001`。
6. FastAPI 后端监听 `8090`，Next.js 前端监听 `3000`。浏览器 API 调用通常经同源 `/api/backend` 转发。
7. Text 页为 `frontend/src/app/page.tsx`，Vision 页为 `frontend/src/app/vision/page.tsx`。两页需同步接入语音输入。
8. 用户经常通过 Android Chrome 访问 `http://192.168.1.88:3000` 或 `http://192.168.2.54:3000`。这些 HTTP LAN origin 可运行文本功能，但不是麦克风 API 所需的 secure context。

## 3. 目标与非目标

### 3.1 目标

1. 两页顶部提供 Layout A 样式的 Keyboard/Voice 分段开关。
2. Voice 模式下完全隐藏文本输入栏，以实时 SPL-time 波形和状态面板替代。
3. 用户首次点击 Voice 时才请求麦克风权限，不在加载页面时请求。
4. 浏览器持续采集 PCM，后端维护固定容量 pre-trigger ring buffer。
5. 声压触发候选、Silero 确认人声、VAD-only 无声超时结束（`recording.end_no_voice_duration_ms`）、尾部收集后调用 SGLang。
6. `audio.input_gain_db` 默认 `+12 dB`，放大后的同一 PCM 用于 SPL、ring、VAD 和 ASR。
7. Voice 切回 Keyboard 会中止当前采集、候选、录音或推理，并等待 session-local worker 清理。
8. 识别后默认自动发送；YAML 可切换为手动发送。
9. 自动发送后暂停麦克风，当前 Chat/Agent 完成后仍处 Voice 模式则自动恢复。
10. Android Chrome 通过受信任 HTTPS 使用麦克风；不安全 origin 给出明确诊断。
11. ASR 单次错误不得终止主聊天后端或永久停止当前语音会话。
12. 所有重要参数通过 `config/config-asr.yaml` 到达启动脚本、后端和前端安全配置。

### 3.2 非目标

1. TTS、回声消除算法或 JetPack 音频驱动排障。
2. 说话人分离、声纹、唤醒词、会议转写。
3. 自动环境噪声校准或动态阈值。
4. 语义话轮结束判断。
5. 将 partial transcript 写入聊天历史。
6. 在浏览器运行 Silero、WAV 编码或 Qwen3-ASR。
7. 修改 SGLang、Silero 或其他依赖包源码。
8. 云端 ASR 或 vLLM/Transformers ASR fallback。
9. 自动把本地 CA 安装到 Android；系统信任操作必须由用户确认。

## 4. 架构

```text
Android/Desktop Browser
  explicit Voice click
        |
        v
getUserMedia + AudioContext(16 kHz) + AudioWorklet
  - PCM capture only
  - lightweight waveform estimate
        |
        | WebSocket: Float32 mono PCM
        v
FastAPI /v1/asr/stream (port 8090)
  - strict frame validation
  - +12 dB software gain and clipping
  - RMS dBFS
  - pre-trigger ring buffer
  - Silero VAD v6.2 ONNX on CPU
  - recording state machine
  - session-local abort and queues
        |
        | completed in-memory WAV, multipart HTTP
        v
Dedicated SGLang Qwen3-ASR (port 30003)
  POST /v1/audio/transcriptions
        |
        v
Final transcript
        |
        v
Existing Text/Vision handleSend -> Chat or Agent API
```

### 4.1 进程边界

| 进程 | 职责 | 不负责 |
|---|---|---|
| Next.js | UI、权限手势、AudioWorklet、波形、状态投影 | VAD、录音决策、ASR |
| FastAPI backend | 配置、WebSocket、DSP、ring、VAD、状态机、SGLang client | 加载 Qwen3-ASR 权重 |
| SGLang ASR | Qwen3-ASR-1.7B 推理 | 麦克风、VAD、Chat/Agent 历史 |

### 4.2 代码边界

新增 `backend/asr/` 聚焦模块：

```text
backend/asr/
  config.py              strict schema and config loading
  models.py              AudioFrame, Utterance, AsrResult, protocol types
  gain.py                dB gain and clipping statistics
  level.py               RMS dBFS detector and hysteresis
  ring_buffer.py         fixed pre-trigger sample ring
  vad.py                 Silero v6.2 ONNX stateful adapter
  state_machine.py       BUFFERING/CANDIDATE/RECORDING/FINALIZING
  sglang_client.py       multipart transcription and cancellation
  session.py             one WebSocket session lifecycle
  service.py             admission, health, shutdown
```

新增 `backend/api/asr.py`，保持 FastAPI 路由层薄。`backend/main.py` 只负责 lifespan wiring、router 注册、`/config` 和 `/health` 暴露。

前端共享模块负责大部分逻辑：

```text
frontend/src/lib/voice-input.ts
frontend/src/lib/voice-input-state.ts
frontend/src/components/VoiceModeToggle.tsx
frontend/src/components/VoiceInputPanel.tsx
frontend/public/asr-pcm-worklet.js
```

Text 与 Vision 页面只维护页面现有 send/image/Agent 状态的 glue，不各自实现一套麦克风或 WebSocket 客户端。

## 5. 配置契约

`config/config-overall.yaml` 新增：

```yaml
asr: config/config-asr.yaml
```

`config/config-asr.yaml` 初始配置：

```yaml
version: 1
enabled: true

audio:
  sample_rate_hz: 16000
  channels: 1
  sample_format: float32
  capture_frame_ms: 20
  input_gain_db: 12.0
  clip_limit: 0.999
  browser_constraints:
    echo_cancellation: false
    noise_suppression: false
    auto_gain_control: false

recording:
  pretrigger_buffer_ms: 1000
  posttrigger_tail_ms: 250
  max_utterance_ms: 30000
  min_utterance_ms: 200
  candidate_confirmation_timeout_ms: 900
  # VAD-only 结束：瞬时人声概率连续低于 vad.threshold 达到该时长即进入 FINALIZING。
  end_no_voice_duration_ms: 1800
  prevent_segment_overlap: true

# SPL 只服务开始路径；release_threshold_dbfs 字段已从 schema 整体删除，
# 出现即按未知字段拒绝（runtime.fail_on_unknown_config=true）。
audio_trigger:
  enabled: true
  metric: rms_dbfs
  start_threshold_dbfs: -29.0
  min_trigger_duration_ms: 40
  require_vad_confirmation: true

vad:
  engine: silero
  version: "6.2"
  backend: onnx
  model_path: package://silero_vad/data/silero_vad.onnx
  device: cpu
  threshold: 0.50
  min_silence_duration_ms: 500
  speech_pad_ms: 30
  frame_samples: 512
  # Silero 推理计算门控（必填，schema 严格校验；语义见 10.1.1）。
  compute_gating_enabled: true
  compute_wake_threshold_dbfs: -35.0
  compute_suspend_after_quiet_ms: 500
  compute_warmup_context_ms: 100

sglang:
  host: 127.0.0.1
  port: 30003
  served_model_name: Qwen3-ASR-1.7B
  model_path: /home/zk/Project/LLM-weight/Qwen3-ASR-1.7B
  dtype: bfloat16
  mem_fraction_static: 0.08
  max_running_requests: 1
  trust_remote_code: false
  health_timeout_seconds: 2.0

transcription:
  streaming: false
  auto_send: true
  language: null
  temperature: 0.0
  response_format: json
  request_timeout_seconds: 300

transport:
  websocket_path: /v1/asr/stream
  public_websocket_url: null
  handshake_timeout_seconds: 10
  max_binary_frame_bytes: 65536
  max_buffered_frames: 250
  level_event_interval_ms: 100

runtime:
  config_reload_enabled: false
  fail_on_unknown_config: true
  max_active_sessions: 4
  max_pending_utterances_per_session: 1
  asr_worker_concurrency: 1
  shutdown_timeout_seconds: 15

browser_security:
  https_enabled: false
  require_secure_context_for_voice: true
  ca_certificate_path: data/asr-tls/llm-deployment-ca.crt
  certificate_path: data/asr-tls/llm-deployment-server.crt
  private_key_path: data/asr-tls/llm-deployment-server.key
  subject_alt_names:
    - localhost
    - 127.0.0.1
    - 192.168.1.88
    - 192.168.2.54

observability:
  save_debug_audio: false
  debug_audio_directory: data/debug-audio
  log_vad_probability: false
  log_audio_level: false
  log_state_transitions: true
```

### 5.1 校验规则

| 字段 | 规则 |
|---|---|
| `version` | MVP 必须为 `1` |
| `audio.sample_rate_hz` | MVP 必须为 `16000` |
| `audio.channels` | 必须为 `1` |
| `audio.sample_format` | 必须为 `float32` |
| `audio.capture_frame_ms` | 正整数，换算出的 samples 必须为整数 |
| `audio.input_gain_db` | 有限数，范围 `-24..36` dB |
| `audio.clip_limit` | `0 < value <= 1` |
| `recording.pretrigger_buffer_ms` | `>= 0` |
| `recording.posttrigger_tail_ms` | `>= 0` |
| `recording.end_no_voice_duration_ms` | `> 0` |
| `recording.max_utterance_ms` | 大于 min、candidate timeout、no-voice timeout 和 tail |
| `audio_trigger.start_threshold_dbfs` | `<= 0`；仅门控 BUFFERING→CANDIDATE 开始路径 |
| `audio_trigger.release_threshold_dbfs` | 已移除；出现即按未知字段拒绝 |
| `vad.compute_gating_enabled` | 必填布尔键，schema 严格校验 |
| `vad.compute_wake_threshold_dbfs` | 必填有限数且 `<= 0` |
| `vad.compute_suspend_after_quiet_ms` | `> 0` |
| `vad.compute_warmup_context_ms` | `>= 0` |
| wake/start 交叉校验 | SPL 触发与 compute gating 同启时，`vad.compute_wake_threshold_dbfs` 必须 `<= audio_trigger.start_threshold_dbfs`，违反在加载时拒绝 |
| `vad.version` | 必须为 `6.2` |
| `vad.backend` | 必须为 `onnx` |
| `vad.device` | MVP 必须为 `cpu` |
| `vad.threshold` | `0 < value < 1` |
| `vad.frame_samples` | 16 kHz 下必须为 `512` |
| `sglang.port` | `1..65535`，默认且验收值为 `30003` |
| `sglang.model_path` | 目录可读，配置、tokenizer、index 和全部 shard 存在 |
| `transcription.response_format` | `json`、`text` 或 `verbose_json` |
| `runtime.config_reload_enabled` | MVP 必须为 `false`，单个 WebSocket 生命周期内禁止热换配置 |
| `runtime.fail_on_unknown_config` | 必须为 `true` |
| `runtime.*concurrency` | 正整数；worker concurrency 不大于 SGLang running limit |
| HTTPS 路径 | `https_enabled=true` 时 CA、cert、key 都必须存在可读 |

未知字段在 `fail_on_unknown_config=true` 语义下失败。Schema 可以定义文档默认值，但运行时只使用完成 YAML 解析与校验后的统一配置对象。

### 5.2 配置向前端暴露

`GET /config` 只暴露浏览器所需安全子集：

```json
{
  "asr": {
    "enabled": true,
    "sample_rate_hz": 16000,
    "capture_frame_ms": 20,
    "input_gain_db": 12.0,
    "clip_limit": 0.999,
    "auto_send": true,
    "streaming": false,
    "websocket_path": "/v1/asr/stream",
    "public_websocket_url": null,
    "require_secure_context": true,
    "browser_constraints": {
      "echo_cancellation": false,
      "noise_suppression": false,
      "auto_gain_control": false
    }
  }
}
```

模型绝对路径、私钥路径、调试路径和内部并发限制不返回浏览器。

## 6. SGLang 启动与推理

### 6.1 启动脚本

新增 `scripts/2c_start_asr_sglang.sh`。脚本必须：

1. 定位 project/shared workspace 和 `.sglang`，不创建其他 ASR Python 环境。
2. 读取 `config-overall.yaml` 的 `asr` 路由，再通过统一 ASR config loader 解析配置。
3. 在启动前执行 model preflight，而不是只检查目录名。
4. 检查 `config.json.architectures` 包含 `Qwen3ASRForConditionalGeneration`。
5. 检查 `model.safetensors.index.json` 中每个唯一 shard 均存在且可读。
6. 检查 SGLang model registry 支持该 architecture。
7. 检查端口 `30003` 是否可用；不得无提示杀死未知进程。
8. 将 YAML 的 host、port、model、served name、dtype、memory fraction、max requests 和 trust flag 传给 `sglang.launch_server`。
9. 使用现有 Thor CUDA/Triton 环境约定并保存日志。
10. 启动后探测 `/health` 和 `/v1/models`，确认 served model identity。

模型和推理参数只能从 YAML 到 launcher；shell 不复制第二套默认模型参数。

### 6.2 SGLang client

FastAPI client 将完成 utterance 编码成 mono 16 kHz Float32/PCM WAV，在内存中构造 multipart 请求：

```text
POST http://127.0.0.1:30003/v1/audio/transcriptions
file=<utterance.wav>
model=Qwen3-ASR-1.7B
language=<null or configured language>
response_format=json
temperature=0.0
stream=false
```

当 `streaming=true` 时才传 `stream=true` 并解析 SSE。SGLang `0.5.15` 的 Qwen3-ASR adapter 内部 generation/chunk 参数属于 provider 实现，不修改依赖源码。项目控制并完整传递 transcription endpoint 对外支持的参数。

client 使用一个受控 `httpx.AsyncClient`。取消任务必须关闭 upstream request/response，使 SGLang 感知客户端断开。单 session 取消禁止调用 abort-all。

## 7. 音频捕获与传输

### 7.1 浏览器捕获

Voice 点击处理器必须同步发起 `getUserMedia`，满足 Android Chrome 用户手势要求：

```javascript
navigator.mediaDevices.getUserMedia({
  audio: {
    channelCount: 1,
    sampleRate: 16000,
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: false
  },
  video: false
})
```

浏览器 constraints 是 best effort。随后创建目标采样率为 `16000` 的 AudioContext，并以实际 `audioContext.sampleRate` 验证结果。若不是配置采样率，MVP 返回明确不兼容错误并释放 track，不发送错误采样率 PCM。不得依靠 MediaRecorder 的浏览器特定压缩 codec。

AudioWorklet 将 WebAudio render quantum 聚合为 `capture_frame_ms` 对应的固定帧，向主线程发送 Float32Array。它不运行模型、不编码 WAV、不保存无限音频。

### 7.2 WebSocket protocol

连接建立后客户端首先发送 JSON handshake：

```json
{
  "type": "start",
  "protocol_version": 1,
  "sample_rate_hz": 16000,
  "channels": 1,
  "sample_format": "float32",
  "frame_samples": 320
}
```

后端返回：

```json
{
  "type": "ready",
  "session_id": "...",
  "protocol_version": 1,
  "state": "buffering"
}
```

之后客户端发送 little-endian Float32 binary frame。后端拒绝：

1. handshake 前 binary data；
2. 非 4-byte 对齐 payload；
3. 超过配置最大值的 payload；
4. NaN/Infinity；
5. 错误采样率、声道或格式；
6. 超过 session queue 上限的 frame。

客户端控制消息仅包括 session-local `abort`。后端事件包括：

```text
ready
level
state_changed
transcribing
transcript_delta   only when streaming=true
transcript
utterance_error
aborted
fatal_error
```

`level` 按 YAML 限频，包含 post-gain dBFS 与 clipping ratio。聊天 UI 使用本地轻量估算保持动画流畅，以后端事件校正文本值和状态。

## 8. 增益、声压与数据顺序

### 8.1 软件增益

后端对收到的 raw PCM 执行：

```text
linear_gain = 10 ** (input_gain_db / 20)
amplified = clip(raw * linear_gain, -clip_limit, clip_limit)
clipping_ratio = clipped_sample_count / frame_sample_count
```

默认 `+12 dB` 约等于 3.981 倍振幅。增益只应用一次。post-gain PCM 同时进入 dBFS、ring、Silero 和 ASR，禁止只放大 UI 或重复放大。

### 8.2 RMS dBFS

```text
rms = sqrt(mean(amplified ** 2))
dbfs = 20 * log10(max(rms, epsilon))
```

`epsilon` 是数值安全常量，不是用户调参。完全静音返回负无穷语义或稳定的最小显示值，但触发比较必须视为低于 start threshold，并因此在 BUFFERING 中计入 compute gating 的 quiet 累计（见 10.1.1）。

### 8.3 每帧顺序

```text
1. 验证 raw frame 和连续 sample index
2. 应用 gain 和 clipping
3. 获取 ring 中不含当前帧的 pre-trigger 边界
4. 计算当前 amplified frame dBFS
5. 将 amplified frame 写入 ring
6. 将 amplified samples 聚合为 512-sample Silero chunks
7. 更新最新 VAD probability/is_speech
8. 推进状态机
9. 如果本帧触发候选：snapshot + 当前帧，按 sample index 去重
10. 如果已处 candidate/recording/finalizing：只追加尚未包含的区间
```

## 9. Ring buffer 与相邻 utterance 去重

ring buffer 使用预分配 NumPy Float32 array、write pointer、valid length 和全局 sample index。容量：

```text
capacity_samples = sample_rate_hz * pretrigger_buffer_ms / 1000
```

默认容量为 16000 samples。写满后覆盖最旧数据，空闲监听期间内存不增长。

每次成功提交 ASR 的 utterance 记录 `last_committed_sample_index`。候选 snapshot 起点：

```text
max(current_sample_index - capacity_samples, last_committed_sample_index)
```

追加器根据全局 `[start_sample_index, end_sample_index)` 裁剪重叠区间。它必须保证触发帧只出现一次，且下一 utterance 不重复上一 utterance 已提交尾部。

取消、候选拒绝和推理失败不把未提交区间错误标记为 committed。音频时间线断裂会丢弃当前 utterance、重置 Silero 与 ring，并建立新的 sample timeline boundary。

## 10. VAD 与录音状态机

Silero 以 ONNX CPU 模式运行，按 512 samples 保持 session-local state。`pretrigger_buffer_ms` 与 `speech_pad_ms` 是独立参数。VAD adapter 暴露每 chunk probability、当前 speech state、speech start/end 事件和 reset。

### 10.1 `BUFFERING`

1. 持续接收、放大并覆盖 ring。
2. 持续计算 dBFS；Silero 是否实时推理由 compute gating 决定（见 10.1.1）。
3. 连续达到 `start_threshold_dbfs` 满 `min_trigger_duration_ms` 后进入 `CANDIDATE`。

#### 10.1.1 Silero compute gating

实现位于 `backend/asr/session.py`，仅在 `audio_trigger.enabled=true` 时生效：

1. post-gain RMS dBFS 持续低于 `vad.compute_wake_threshold_dbfs` 的帧累计 quiet 时间；
2. 连续 quiet 达到 `vad.compute_suspend_after_quiet_ms` 后完全挂起 Silero ONNX 推理：adapter 状态重置，最新观测中和为 probability 0 / not-speech，节省长空闲期 CPU；
3. 任一 BUFFERING 帧达到或超过 wake 线立即恢复推理：先重置 adapter，再从 pretrigger ring 取最多 `vad.compute_warmup_context_ms` 的真实 lead-in 音频回灌模型重建 LSTM 状态；warm-up 窗口内产生的 probability 一律丢弃、不用于任何决策，直到 warm-up 样本预算被 live 帧消耗完毕才重新可信；
4. `CANDIDATE` / `RECORDING` / `FINALIZING` 状态永不挂起推理；
5. `audio_trigger.enabled=false` 时门控失效：VAD 成为主触发，必须持续推理。

### 10.2 `CANDIDATE`

1. 保存有序 pre-trigger snapshot 和触发帧，去除重叠。
2. 后续帧持续追加。
3. 若 Silero 当前已在 speech state，或随后确认 speech start，进入 `RECORDING`。
4. 超过 `candidate_confirmation_timeout_ms` 未确认则丢弃并回到 `BUFFERING`，不调用 SGLang。

### 10.3 `RECORDING`

1. 不受 pre-trigger 容量限制，持续追加。
2. 每帧检查 max duration。
3. 每帧检查 VAD-only 无声超时。

> **修订（2026-08-24）：** 原“组合静默”结束判定——`frame_dbfs < release_threshold_dbfs AND latest_vad_probability < vad.threshold` 持续累计 `end_silence_duration_ms`——已废弃，保留于此仅作历史对照。`audio_trigger.release_threshold_dbfs` 字段已整体删除，SPL 不再参与结束判定。

当前实现为纯 VAD 结束：瞬时 Silero speech probability 连续保持在 `vad.threshold` 以下达到 `recording.end_no_voice_duration_ms`（生产默认 1800 ms）时，录音完成并把音频提交 ASR。计时使用即时 probability，不额外等待 Silero 的 delayed speech_end，任一帧概率恢复立即清零，因此 YAML 中的 X ms 对用户具有直接含义。

### 10.4 `FINALIZING`

1. 继续追加最多 `posttrigger_tail_ms`。
2. 验证时间顺序、格式和 duration。
3. 小于 `min_utterance_ms` 则丢弃。
4. 合法 utterance 进入 bounded ASR queue。
5. session 进入录音状态机之外的 `TRANSCRIBING` 生命周期阶段；浏览器暂停 AudioWorklet，后端不接收新 PCM。
6. 推理成功、失败或取消完成清理后，只有仍有效且允许继续监听的 Voice generation 才创建新的 `BUFFERING` 状态机实例。

同 session 最多一个 pending utterance，禁止一边识别上一句一边录制下一句。`TRANSCRIBING` 是 session lifecycle phase，不是 PRD 四状态录音机的第五状态。

达到 `max_utterance_ms` 会强制进入 finalizing，并标记 `max_duration_reached=true`。utterance 元数据 `silero_end_reason` 取值为 `"max_duration"` 或 `"no_voice_timeout"`（旧取值 `"combined_silence"` 已随组合静默机制移除）。

## 11. 生命周期与取消

每次 Voice activation 创建不可复用 generation token。所有 async callback、WebSocket event 和 transcript 在写入 UI 前必须匹配当前 token。

以下操作触发 session-local abort：

1. Voice 切换到 Keyboard；
2. 页面卸载或隐藏到后台；
3. 麦克风 track ended；
4. WebSocket 断线；
5. 用户重新开始一个 generation；
6. 后端 shutdown。

前端 abort 顺序：

```text
invalidate generation token
→ send abort if socket writable
→ stop every MediaStreamTrack
→ disconnect AudioWorklet nodes
→ close AudioContext
→ close WebSocket
→ clear local waveform/pending callbacks
```

后端 abort 顺序：

```text
set session abort event
→ stop accepting frames
→ clear frame/utterance queues
→ reset state machine/VAD/ring
→ cancel session's active SGLang task
→ close upstream HTTP response
→ await worker terminal barrier up to configured shutdown timeout
→ remove session mapping
```

禁止 fallback 到 abort-all。一个手机会话的取消不能影响桌面浏览器、其他手机或主 LLM 请求。

## 12. UI 设计

### 12.1 顶部 Layout A

顶部使用带文本和图标的分段控制：

```text
[ Keyboard ] [ Voice ]
```

桌面与现有标题/状态/操作同一 header；移动端在窄屏换到第二行。触控目标至少 44 CSS px，使用 `aria-pressed`/radio semantics 和可见 focus。

页面刷新后始终从 Keyboard 开始。Voice mode 不写 localStorage，防止加载页面后恢复监听。

### 12.2 Voice panel states

Voice 模式卸载 textarea 和 SlashCommandMenu，以同位置共享面板展示：

| 状态 | 展示 |
|---|---|
| `requesting_permission` | 请求麦克风权限 |
| `listening` | SPL 波形、dBFS、pre-trigger active |
| `candidate` | 检测到声音，确认人声 |
| `recording` | 正在录音 |
| `finalizing` | 收集尾部音频 |
| `transcribing` | SGLang 正在识别 |
| `transcript_ready` | 最终转写文本 |
| `paused_for_response` | 已发送，等待 Chat/Agent |
| `error` | 分类错误和恢复动作 |

Voice 模式中不得渲染普通 textarea。`auto_send=false` 时 transcript 在 voice panel 中只读展示，现有 Send 按钮基于 pending transcript 启用。切换 Keyboard 会把 final transcript 放入 textarea，允许编辑。

### 12.3 发送行为

页面 send 逻辑重构为接受显式文本参数的统一入口，避免 `setInput` 后读取旧 closure：

```text
submitUserMessage(text, currentImages?)
```

Text 页发送 transcript 文本。Vision 页将 transcript 与发送时已选图片一起发送。Chat/Agent API、上下文序列化和消息展示继续使用现有代码路径。

自动发送后立即释放麦克风并显示 `paused_for_response`。当前 Chat/Agent terminal event 到达后，如果 mode 仍为 Voice、页面可见且没有 pending transcript，则通过显式恢复流程重新请求/复用允许的设备流。不得在失败或后台状态无限重连。

## 13. Android Chrome 与 HTTPS

### 13.1 用户手势

`getUserMedia` 只在用户点击 Voice 的事件栈中执行。浏览器权限拒绝时停止创建流程、释放已创建资源、回到 Keyboard 并显示可操作信息。

### 13.2 Secure context

`http://192.168.x.x:3000` 不是 secure context。前端在 Voice 点击前检查：

```text
window.isSecureContext
navigator.mediaDevices?.getUserMedia
window.AudioWorkletNode
AudioContext.prototype.audioWorklet
```

缺失时不尝试绕过浏览器安全策略。

### 13.3 可选本地 HTTPS

新增证书辅助脚本，从 `browser_security.subject_alt_names` 生成本地 CA 与 server certificate。生成物位于配置路径且不纳入 Git。用户需将 CA 安装为 Android 信任凭据。

`https_enabled=true` 时：

1. `scripts/3_start_backend.sh` 以同一 server cert/key 启动 Uvicorn TLS。
2. `scripts/4_start_frontend.sh` 以配置 cert/key 启动 Next HTTPS。
3. Next server-side backend proxy 使用配置 CA 信任后端证书，不使用全局 `NODE_TLS_REJECT_UNAUTHORIZED=0`。
4. 浏览器 WebSocket 使用 `wss://`。显式 `public_websocket_url` 优先，否则以当前 browser hostname、backend port 和 configured path 构造。
5. 证书或 SAN 不匹配时启动脚本快速失败并给出重新生成命令。

HTTP 模式继续支持现有文本功能；当 `require_secure_context_for_voice=true` 时，LAN HTTP 的 Voice 开关只显示 HTTPS 指引。

## 14. 错误处理与恢复

| 错误 | 行为 |
|---|---|
| 不安全 origin | 不请求权限，显示 HTTPS 指引 |
| `NotAllowedError` | 回 Keyboard，说明权限被拒绝 |
| `NotFoundError` | 回 Keyboard，说明无麦克风 |
| `NotReadableError` | 回 Keyboard，说明设备占用/系统错误 |
| AudioWorklet/采样率不支持 | 释放设备，显示浏览器不兼容 |
| WebSocket 断开 | abort 当前 utterance，释放设备，不后台重连 |
| PCM 非法/时间线断裂 | 重置 session 音频状态，返回 utterance error |
| Silero 推理失败 | 丢弃当前 utterance；不静默降级为纯 SPL |
| candidate timeout | 丢弃，不调用 SGLang，回 buffering |
| SGLang 不可用/超时 | 当前 utterance 失败，清理 request，voice session 可继续 |
| queue full | 拒绝新 utterance，保持有界内存，报告 busy |
| max duration | 强制 finalize，提交并记录原因 |

单次 utterance error 不结束主后端。fatal protocol/config error 才关闭对应 WebSocket。

## 15. 健康与可观测性

`GET /health` 增加：

```json
{
  "asr": {
    "status": "healthy|degraded|disabled",
    "vad_ready": true,
    "sglang_ready": true,
    "model_name": "Qwen3-ASR-1.7B",
    "active_sessions": 0,
    "queued_utterances": 0
  }
}
```

SGLang 探测必须验证 `/health` 和 `/v1/models` 中 served model，不只判断端口开放。SGLang 未启动时主 backend 保持可用，ASR health 为 degraded。

每个 utterance 至少记录：

```text
utterance_id
session_id_hash
start_sample_index
end_sample_index
pretrigger_samples
recorded_samples
duration_ms
trigger_dbfs
max_dbfs
clipping_ratio
silero_start_probability
silero_confirmed
silero_end_reason (max_duration | no_voice_timeout)
asr_latency_ms
asr_text_length
max_duration_reached
error_code
```

默认不记录 transcript 正文和 PCM。`save_debug_audio=false` 时不创建音频文件。开启 debug audio 时只保存已确认 utterance，并在启动日志中提示隐私风险。

## 16. Prefix-cache 与 Chat/Agent 合约

ASR 不直接构建主 LLM prompt。final transcript 只能通过 Text/Vision 页现有 send 入口成为当前 turn 的新 user message。

必须保持：

1. 不修改历史 role/content/order/tool traces。
2. 不把 VAD、SPL、utterance ID、WebSocket 状态或诊断写入 LLM prompt。
3. 不创建新的 Agent SSE/timeline 协议。
4. 不把 partial transcript 加入 canonical history。
5. 语音 Stop 只影响 ASR lifecycle，不影响当前或其他 Chat/Agent request，除非用户另外点击现有 Chat/Agent Stop。
6. auto/manual 两种发送模式最终生成等价的普通 user message shape。

因此 ASR 功能不引入新的 prefix boundary。

## 17. 依赖

在 `requirements_for_sglang.txt` 添加并在 `.sglang` 安装：

```text
silero-vad==6.2.0
```

该版本支持 Python 3.12，依赖现有 Torch/TorchAudio/ONNX Runtime。安装必须使用项目 `uv` 环境，禁止调用系统 pip，禁止修改包源码。

前端使用浏览器原生 Web APIs，不引入客户端 VAD、DSP 或 WebSocket proxy 大型依赖。仓库当前 Next CLI 已确认支持 `--experimental-https-key`、`--experimental-https-cert` 和 `--experimental-https-ca`。浏览器 HTTP API 继续经同源 `/api/backend`，浏览器 ASR WebSocket 直接连接启用 TLS 的 FastAPI `8090`；Next server-side proxy 通过配置 CA 信任同一后端证书。实现不得回退到 insecure origin 或全局禁用 TLS 校验。

## 18. 测试策略

实现采用 TDD，先写失败测试，再写最小实现。

### 18.1 后端单元测试

1. config 完整加载、未知字段、类型和交叉校验。
2. `+12 dB` 转换、clipping、NaN 拒绝、静音 dBFS。
3. ring 初始写、覆盖、有序 snapshot、全局 index 裁剪。
4. 触发帧仅一次、相邻 utterance 不重叠。
5. trigger hysteresis 与连续时长。
6. Silero chunk assembler 与 reset。
7. 四状态正常路径、candidate reject、max duration、tail。
8. VAD-only 无声超时在概率恢复时清零；compute gating 的挂起、唤醒、warm-up 丢弃窗口与 `silero_end_reason` 取值。
9. min duration 丢弃。

### 18.2 后端集成测试

1. fake WebSocket PCM -> fake Silero confirm -> fake SGLang transcript。
2. 声压触发但 Silero 拒绝时 SGLang 调用计数为零。
3. disconnect/toggle abort 在推理中关闭 upstream 并等待 worker barrier。
4. 相同/不同 session generation token 隔离。
5. 两个 session 取消互不影响。
6. queue 和 session admission 有界。
7. ASR unavailable 时 `/health` degraded 而主 chat health 可用。
8. streaming false 只发 final；streaming true partial 不进入 send/history。

### 18.3 启动与配置链路测试

1. `config-overall -> config-asr -> launcher` 参数传递。
2. 默认端口严格为 `30003`。
3. model index 每个 shard 检查。
4. `.sglang` Python/SGLang registry 检查。
5. `/health` + `/v1/models` identity probe。
6. HTTPS enabled 时 cert/key/CA 和 SAN preflight。
7. 启动脚本不包含第二套 hard-coded model path 或 infer tuning defaults。

### 18.4 前端测试

1. secure context 和 media capability 分类。
2. ws/wss URL 构造与 explicit URL 优先级。
3. voice state reducer 合法转换。
4. generation token 拒绝旧 transcript。
5. `auto_send=true` 自动调用统一 submit。
6. `auto_send=false` pending transcript 和 Send。
7. Voice -> Keyboard 在 active 阶段 abort，在 transcript-ready 阶段迁移文本。
8. auto send 后 pause，Chat/Agent terminal 后恢复。
9. Vision transcript 与当前图片一起发送。
10. Text/Vision 页面同时包含共享 toggle/panel glue。
11. TypeScript type-check 与 Next build。

### 18.5 硬件验收

1. Jetson 上真实启动 SGLang ASR 并识别短 WAV。
2. Thor 本机 Chromium 点击 Voice 后只弹一次合理权限请求。
3. Android Chrome 通过受信任 HTTPS 访问并授权麦克风。
4. +12 dB 下低输入音量可见，clipping 可观察并可通过 YAML 调整。
5. 低音量开头在 threshold 前 300 ms 出现时仍保留。
6. 无声满 1800 ms（`end_no_voice_duration_ms` 生产默认）才结束；短停顿不结束。
7. 识别期间切 Keyboard 后无晚到 transcript、无自动发送。
8. auto send true/false 两种模式均工作。
9. 连续两句无上一句尾部重复。
10. 长时间静音 ring 内存固定，不产生文件。

自动测试不能替代 Android 系统权限、真实驱动输入和 GPU 模型质量验收。

## 19. PRD 验收标准映射

| PRD AC | 设计覆盖 |
|---|---|
| AC-01/02 | 固定 ring + YAML capacity |
| AC-03 | 单一 strict config sample rate |
| AC-04 | pre-trigger snapshot |
| AC-05 | global sample index append 去重 |
| AC-06 | candidate timeout 不调用 SGLang |
| AC-07 | RECORDING 不受 ring capacity 限制 |
| AC-08 | FINALIZING tail |
| AC-09 | 独立 pretrigger 与 speech_pad 字段 |
| AC-10 | mono 16 kHz continuous WAV preflight |
| AC-11 | strict threshold cross-validation |
| AC-12 | preallocated ring、bounded queues、debug off |

新增验收：

1. AC-13：`input_gain_db=12` 对送入全部后端阶段的 PCM 只应用一次。
2. AC-14：Voice -> Keyboard 中止当前 ASR，旧结果不能发送。
3. AC-15：`streaming=false` 默认不产生 partial transcript。
4. AC-16：`auto_send=false` 等待用户点击 Send。
5. AC-17：Android LAN HTTP 明确阻止 Voice 并指向 HTTPS；受信任 HTTPS 可授权。
6. AC-18：Text 和 Vision 两页行为一致，Vision 保留图片发送语义。

## 20. 实施边界与并行方式

编码阶段最多使用三个不冲突工作流：

1. 后端核心工作流只写 `backend/asr/` 与对应 ASR core tests。
2. 前端工作流只写共享 voice modules/components/worklet 与前端 tests，不直接改两页大型文件。
3. 主工作流负责 config、launch/TLS scripts、FastAPI wiring、Text/Vision glue 和跨层集成。

前端和后端工作流完成后由主工作流整合并运行全套验证。编码完成后启动一个独立 auditor 子代理，只读审查实现是否存在真实越界、硬编码、Prompt/历史污染、取消隔离错误、隐私问题、Android secure-context 缺口或两页不同步；发现的问题由主工作流修复并重新验证。

## 21. 风险与控制

| 风险 | 控制 |
|---|---|
| Android LAN HTTP 无麦克风 | secure-context gate + 可选本地 HTTPS + CA 信任说明 |
| +12 dB clipping | configurable clip、clipping ratio、wave/日志可见 |
| 浏览器忽略 16 kHz | 验证 AudioContext sample rate，不发送错误 PCM |
| SGLang 与主 LLM 争用 GPU | 独立 port、memory fraction、concurrency=1、health probe |
| 长音频或静音内存增长 | max duration、fixed ring、bounded queues |
| 切换后晚到 transcript | generation token + worker barrier |
| 多 session abort 越界 | session-local task/response，无 abort-all |
| 两页实现漂移 | shared controller/components + sync tests |
| 音频隐私 | 默认内存-only、debug audio off、不记录正文 |
| SGLang adapter 内部参数不可配 | 只使用公开 endpoint contract，不修改依赖；记录 provider invariant |

## 22. 完成定义

本功能完成需要同时满足：

1. 规范中的 config、backend、launcher、frontend 与 HTTPS 路径实现。
2. ASR 依赖安装在 `.sglang`，无依赖源码修改。
3. 自动测试通过，type-check/build 通过。
4. SGLang model preflight 和真实短 WAV smoke test 通过，或明确报告因当前 GPU 服务未启动而未执行。
5. Text 与 Vision 页面均通过手动 Voice/Keyboard 切换测试。
6. Android HTTPS/权限步骤有可执行说明，并在可用设备上验收或明确列为未完成硬件验证。
7. 独立 auditor 审查完成，真实问题已修复并复测。
8. 不改动用户现有 OCR 脚本删除/重命名等无关工作区变更。

## 修订记录

### 2026-08-24 —— VAD-only 话轮结束 + Silero compute gating

1. **VAD-only 结束话轮（噪声环境修复）**
   - `RECORDING` 结束判定从“组合静默”（`frame_dbfs < release_threshold_dbfs AND latest_vad_probability < vad.threshold` 持续 `end_silence_duration_ms`）改为纯 Silero VAD：瞬时人声概率连续低于 `vad.threshold` 达 `recording.end_no_voice_duration_ms`（生产默认 1800 ms）即结束并提交 ASR。
   - `recording.end_silence_duration_ms` 更名为 `recording.end_no_voice_duration_ms`；`audio_trigger.release_threshold_dbfs` 字段整体删除，schema 在未知字段拒绝语义下报错。
   - SPL 阈值现只服务开始路径：`start_threshold_dbfs` 门控 BUFFERING→CANDIDATE；`require_vad_confirmation=true` 下 Silero 在 `candidate_confirmation_timeout_ms=900`（原默认 500）内做第二道确认，双层开始设计不变。
   - utterance 元数据 `silero_end_reason` 取值改为 `"max_duration"` / `"no_voice_timeout"`；旧值 `"combined_silence"` 移除。
   - 动机：仓库最初面向安静房间调参；噪声环境中环境 RMS 长期高于 release 阈值，绝对静音结束条件永不满足，导致无限录制环境噪声。改为人声存在性单条件结束，同时保留灵敏的 SPL 开始触发 + VAD 确认作为第二道守门。
2. **Silero VAD compute gating（空闲 CPU 节省）**
   - `vad:` 新增必填键（schema 严格校验）：`compute_gating_enabled: true`、`compute_wake_threshold_dbfs: -35.0`、`compute_suspend_after_quiet_ms: 500`、`compute_warmup_context_ms: 100`。
   - 语义见新增 §10.1.1（实现：`backend/asr/session.py`）：BUFFERING 且 SPL 触发启用时，quiet 低于 wake 线达 suspend 时长即挂起 ONNX 推理；越线帧恢复推理并以 ring lead-in 回灌 warm-up，warm-up 期概率不可信；CANDIDATE/RECORDING/FINALIZING 永不挂起；`audio_trigger.enabled=false` 时门控失效。
   - 新增交叉校验：SPL 触发与 gating 同启时 `vad.compute_wake_threshold_dbfs <= audio_trigger.start_threshold_dbfs`；据此示例配置 `start_threshold_dbfs` 从 -40.0 对齐为生产触发线 -29.0。
   - 动机：语音/听写场景存在长空闲期，此前两段式触发决策并未门控推理本身，Silero 全天候推理浪费 Jetson CPU。默认值依据 2026-08-24 网络调研：500 ms 挂起滞回高于语内停顿（约 80–250 ms）、低于对话间隙（约 1 s）；−35 dBFS wake 线约比 −29 dBFS 触发线低 6 dB，为软起音争取约 30–150 ms 预热提前量；100 ms warm-up ≈ 3×512-sample chunks，符合“adapter reset 后概率需若干 chunks 才重新可信”的实践指引。
