# 本地语音 Agent 新增功能 PRD

## 基于 Qwen3-ASR-1.7B、Silero VAD v6.2 与滑动窗口记忆录音的本地 ASR 方案

**文档状态：** Draft
**功能类型：** 新增功能
**目标版本：** MVP v1
**部署方式：** 本地部署
**核心组件：**

* Qwen3-ASR-1.7B
* Silero VAD v6.2
* 本地麦克风采集模块
* 滑动窗口记忆录音模块
* YAML 配置管理模块

---

# 1. 背景

当前本地语音识别系统通常在检测到语音或声压超过阈值后才开始保存音频。

这种方式存在一个常见问题：触发检测本身存在延迟。

延迟来源可能包括：

* 音频采集回调延迟；
* 音频帧聚合延迟；
* 声压峰值晚于实际发音起点；
* VAD 模型需要处理完整音频块后才能输出结果；
* 操作系统或 Python 线程调度延迟；
* 模型首次推理或 GPU 唤醒延迟；
* 用户说话开头音量较低，后续音量才超过触发阈值。

如果系统只从触发时刻开始保存音频，可能丢失用户发音开头，例如：

```text
用户实际说话：  “帮我查询明天的天气”
系统保存结果：    “查询明天的天气”
```

这会降低 Qwen3-ASR-1.7B 的识别准确率，并可能导致 Agent 意图解析失败。

本功能通过引入固定时长的滑动窗口记忆录音机制，持续保留触发前最近一段时间的原始 PCM 音频。当声压触发条件满足并经 Silero VAD 确认为人声后，系统将触发前的历史音频追加到最终录音开头，再将完整音频交给 Qwen3-ASR-1.7B 进行识别。

---

# 2. 功能目标

本功能需要实现以下目标：

1. 麦克风启动后持续采集音频，不因当前未检测到人声而停止采集。
2. 未进入正式录音状态时，只保留最近固定时长的音频。
3. 默认保留最近 1 秒音频，即在 16000Hz 采样率下保留最近 16000 个采样点。
4. 超过滑动窗口容量后，新的采样点覆盖最旧的采样点，而不是停止采集或中止录音。
5. 当声压超过用户配置的触发阈值时，系统冻结触发前的滑动窗口音频。
6. 使用 Silero VAD v6.2 判断触发内容是否为人声。
7. Silero 确认为人声后，解除固定滑动窗口容量限制，进入正式录音状态。
8. 最终录音必须包含：

   * 触发前滑动窗口中的历史 PCM；
   * 触发所在的当前音频帧；
   * 触发后的持续语音；
   * 语音结束后的可配置尾部音频。
9. 完整录音交给 Qwen3-ASR-1.7B 进行本地语音识别。
10. 采样率、滑动窗口时长、声压阈值、Silero 参数、无声结束时长等关键参数必须从 `/config` 目录中的 YAML 文件读取，不允许散落硬编码在业务代码中。

---

# 3. 非目标

MVP v1 暂不包含：

* 说话人分离；
* 多人会议转写；
* 唤醒词检测；
* 声纹识别；
* 远程云端 ASR；
* TTS 回声消除；
* 语义级话轮结束判断；
* Qwen3-ASR streaming partial transcript；
* 自动麦克风声压校准；
* 根据环境噪声自动调整阈值。

MVP v1 默认采用：

```text
完整语句录制完成
→ 将完整音频一次性交给 Qwen3-ASR-1.7B
→ 返回最终识别文本
```

后续版本可扩展为流式 ASR。

---

# 4. 核心概念定义

## 4.1 滑动窗口记忆录音

本方案将该功能统一命名为：

```text
Pre-trigger Audio Buffer
```

对应配置变量统一命名为：

```yaml
pretrigger_buffer_ms
```

不得将该变量命名为：

```text
speech_pad_ms
speech_padding_ms
vad_pad_ms
```

原因是 `speech_pad_ms` 已经是 Silero VAD 自身的边界修正参数，二者功能不同。

### `pretrigger_buffer_ms`

负责真实保存触发前的 PCM 音频。

例如：

```yaml
pretrigger_buffer_ms: 1000
```

在 16000Hz 下，对应：

```text
16000 × 1000 / 1000 = 16000 samples
```

### Silero `speech_pad_ms`

负责调整 Silero 返回的语音起止时间戳。

它本身不负责保存实时场景中过去的 PCM 数据，也不能替代 `pretrigger_buffer_ms`。

---

## 4.2 声压触发

本方案中的“声压”默认指数字音频中的短时音量，而不是经过物理校准后的真实 SPL。

MVP 推荐使用：

```text
RMS dBFS
```

计算逻辑：

```text
RMS = sqrt(mean(sample²))
dBFS = 20 × log10(RMS)
```

配置示例：

```yaml
threshold_dbfs: -40.0
```

当当前音频帧的 RMS dBFS 大于或等于该阈值时，进入候选录音状态。

声压触发用于快速发现可能的语音开始，但不能单独判断当前声音是否为人声。敲击、关门、键盘和音乐也可能超过声压阈值，因此需要 Silero VAD 进一步确认。

---

## 4.3 Silero VAD

Silero VAD v6.2 用于判断当前音频中是否存在人声。

Silero 的输出阈值与声压阈值是两个不同参数：

```yaml
audio_trigger.threshold_dbfs
vad.threshold
```

其中：

* `audio_trigger.threshold_dbfs` 表示声音是否足够响；
* `vad.threshold` 表示 Silero 判断为人声的概率阈值。

---

# 5. 用户场景

## 5.1 正常语音触发

用户开始说：

```text
“帮我查询明天上海的天气”
```

系统行为：

1. 麦克风此前已持续采集音频；
2. 环形缓冲区保存最近 1 秒 PCM；
3. 用户开始说话；
4. 声压超过配置阈值；
5. 系统冻结触发前的滑动窗口快照；
6. Silero 确认为人声；
7. 系统进入正式录音状态；
8. 用户说完后，Silero 检测到持续无人声（瞬时概率连续低于阈值）；
9. 系统添加尾部音频；
10. 将完整录音发送给 Qwen3-ASR-1.7B；
11. 返回完整文本。

预期识别：

```text
帮我查询明天上海的天气
```

而不是：

```text
查询明天上海的天气
```

---

## 5.2 非人声误触发

用户敲击桌面，声音超过声压阈值。

系统行为：

1. 声压模块触发；
2. 系统暂时冻结滑动窗口并进入候选状态；
3. Silero 未在配置时间内确认存在人声；
4. 候选录音被丢弃；
5. 系统返回滑动窗口等待状态；
6. 不调用 Qwen3-ASR-1.7B。

---

## 5.3 低音量开头

用户先小声说“帮我”，随后正常音量说“查一下天气”。

即使系统在“查一下”附近才达到声压触发阈值，滑动窗口仍然保存了此前的“帮我”，最终完整音频可以交给 ASR。

---

## 5.4 长时间静音

麦克风持续开启，但环境中没有足够大的声音。

系统行为：

* 持续采集；
* 持续覆盖环形缓冲区；
* 内存占用保持固定；
* 不生成无限增长的录音文件；
* 不调用 ASR。

---

# 6. 总体架构

```text
Microphone
    │
    ▼
Audio Capture
    │
    ├──► RMS / dBFS Detector
    │
    ├──► Pre-trigger Ring Buffer
    │
    └──► Silero Chunk Assembler
               │
               ▼
        Silero VAD v6.2
               │
               ▼
       Recording State Machine
               │
               ▼
       Completed PCM Utterance
               │
               ▼
       Qwen3-ASR-1.7B
               │
               ▼
        Final Transcript
               │
               ▼
         Agent Orchestrator
```

---

# 7. 模块设计

## 7.1 Audio Capture

职责：

* 打开指定麦克风设备；
* 按配置采样率采集单声道 PCM；
* 为每个音频帧分配连续递增的全局采样位置；
* 将音频发送给滑动窗口、声压检测器和 Silero；
* 不在未触发状态下停止麦克风。

输出数据结构示例：

```python
AudioFrame(
    samples: np.ndarray,
    start_sample_index: int,
    end_sample_index: int,
    timestamp_ms: int,
)
```

---

## 7.2 Pre-trigger Ring Buffer

职责：

* 始终保存最近 `pretrigger_buffer_ms` 的原始 PCM；
* 容量固定；
* 写满后覆盖最旧数据；
* 支持按时间顺序生成快照；
* 支持依据全局采样位置裁剪；
* 避免相邻语句间重复音频。

缓冲区容量计算：

```text
capacity_samples =
    sample_rate_hz × pretrigger_buffer_ms / 1000
```

默认配置：

```text
16000 × 1000 / 1000 = 16000 samples
```

### 覆盖行为

```text
收到第 1～16000 个样本：
缓冲区逐渐填满

收到第 16001 个样本：
覆盖第 1 个样本

收到第 16002 个样本：
覆盖第 2 个样本
```

实现必须使用环形写指针，不应在每次收到新样本时整体移动数组。

---

## 7.3 Audio Trigger Detector

职责：

* 计算每个音频帧的 RMS dBFS；
* 根据用户配置阈值判断是否出现候选触发；
* 使用连续帧数量和滞回阈值降低抖动；
* 在首次越过阈值时通知录音状态机。

仅支持一个阈值：

```yaml
start_threshold_dbfs: -29.0
```

* 开始阈值只用于触发候选录音（BUFFERING→CANDIDATE 开始路径）。

> **修订（2026-08-24）：** 历史方案中的第二阈值 `release_threshold_dbfs`（低于开始阈值、原计划用于结束判定的滞回释放阈值）已整体删除：schema 在未知字段拒绝语义下直接报错。声压不再参与录音结束判定，也不存在基于释放阈值的结束滞回；结束完全由 Silero VAD 负责（见 7.5 `RECORDING`）。

---

## 7.4 Silero VAD Adapter

职责：

* 加载 Silero VAD v6.2；
* 按配置帧长持续处理音频；
* 保持模型流式状态；
* 输出语音开始和语音结束事件；
* 在新音频流开始、设备切换或时间线中断时重置状态；
* 不负责保存 pre-trigger PCM。

Silero 参数必须从 YAML 中读取，包括：

* 模型版本或路径；
* ONNX 模型路径；
* 推理设备；
* `threshold`；
* `min_silence_duration_ms`；
* `speech_pad_ms`；
* VAD 输入帧样本数。

`vad.speech_pad_ms` 与 `recording.pretrigger_buffer_ms` 必须作为两个独立参数存在。

---

## 7.5 Recording State Machine

状态机包含四个状态。

### `BUFFERING`

麦克风持续采集，环形缓冲区持续覆盖。

行为：

* 写入 pre-trigger ring buffer；
* 计算声压；
* 向 Silero 输入音频（是否实时推理受 compute gating 门控，见下）；
* 不生成正式录音。

#### Silero compute gating（仅当 `audio_trigger.enabled=true`）

长时间空闲监听期间全天候运行 Silero ONNX 推理会浪费 CPU。实现于 `backend/asr/session.py`：

* post-gain dBFS 持续低于 `vad.compute_wake_threshold_dbfs` 的帧累计 quiet 时间；
* 连续 quiet 达到 `vad.compute_suspend_after_quiet_ms`（默认 500 ms）后完全挂起 Silero 推理：adapter 状态重置，最新观测中和为 probability 0 / not-speech；
* 任一 BUFFERING 帧达到或超过 wake 线立即恢复推理：重置 adapter 后，从 pretrigger ring 取最多 `vad.compute_warmup_context_ms`（默认 100 ms）的真实 lead-in 音频回灌模型重建 LSTM 状态；warm-up 窗口内的 probability 丢弃、不用于决策，直至 warm-up 样本预算被 live 帧消耗完毕；
* `CANDIDATE` / `RECORDING` / `FINALIZING` 永不挂起推理；
* `audio_trigger.enabled=false` 时门控失效：VAD 成为主触发，必须持续推理。

转换条件：

```text
声压达到开始阈值
→ CANDIDATE
```

---

### `CANDIDATE`

检测到可能的语音开始，但尚未得到 Silero 确认。

进入该状态时：

1. 对当前 pre-trigger ring buffer 做一次有序快照；
2. 保存触发所在的当前音频帧；
3. 创建候选录音；
4. 后续音频继续追加到候选录音；
5. 等待 Silero 确认。

转换条件：

```text
Silero 确认存在人声
→ RECORDING

超过 candidate_confirmation_timeout_ms
且 Silero 未确认人声
→ 丢弃候选录音
→ BUFFERING
```

---

### `RECORDING`

Silero 已确认当前音频为人声。

行为：

* 解除固定 1 秒容量限制；
* 持续将后续音频追加到当前录音；
* 继续运行 Silero；
* 每帧检查 VAD-only 无声超时；
* 检查最大录音时长。

转换条件：

```text
瞬时人声概率连续低于 vad.threshold 达 end_no_voice_duration_ms
→ FINALIZING

录音超过 max_utterance_ms
→ FINALIZING
```

> **修订（2026-08-24）：** 旧文本“Silero 报告 speech_end”以及任何基于声压释放阈值（`release_threshold_dbfs`）的组合静默结束判定均已废弃。当前实现只看 Silero 瞬时概率：连续低于 `vad.threshold` 满 `recording.end_no_voice_duration_ms`（生产默认 1800 ms）即结束录音并提交 ASR，任一帧概率恢复立即清零。原因：噪声环境中环境声 RMS 长期高于释放阈值，旧的绝对静音结束条件永不满足，会造成无限录制环境噪声。

---

### `FINALIZING`

行为：

* 继续收集 `posttrigger_tail_ms` 的尾部音频；
* 完成 PCM 拼接；
* 检查重复样本和时间顺序；
* 将音频发送给 Qwen3-ASR-1.7B；
* 保存必要的调试元数据；
* 返回 `BUFFERING`。

---

# 8. 关键数据顺序

每个音频帧到达时，推荐执行顺序如下：

```text
1. 读取当前 ring buffer 的触发前快照引用
2. 计算当前帧 RMS / dBFS
3. 将当前帧写入 ring buffer
4. 将当前帧发送给 Silero
5. 更新录音状态机
6. 如果本帧触发候选录音：
   候选录音 = 触发前快照 + 当前帧
7. 如果已经处于候选或正式录音：
   继续追加后续帧
```

必须避免：

```text
ring buffer 已经包含当前触发帧
+
当前触发帧再次单独追加
```

否则会产生重复音频。

推荐使用全局采样位置进行去重，而不是仅依赖代码调用顺序。

---

## 8.1 实时输入增益控制

浏览器原样发送 Float32 PCM；软件增益只在后端的 ASR PCM 路径应用一次。`input_gain_db` 的浏览器缓存键为 `llm-asr-input-gain`，缓存存在且有效时优先于 YAML 默认值；YAML 的 `input_gain_min_db`、`input_gain_max_db` 和 `input_gain_step_db` 始终约束最终值。范围编辑需要重启后端并让页面重新读取 `/config`；schema 会严格拒绝无效配置，不提供隐藏范围兜底。

完成 `start` 握手后，客户端可在单个 WebSocket 会话内发送：

```json
{"type":"set_input_gain","input_gain_db":6.5}
```

服务端接受后确认：

```json
{"type":"input_gain_changed","input_gain_db":6.5}
```

无效值（缺失、非数值、非有限、越界或不符合步长）不会关闭连接，而是返回当前仍有效的值：

```json
{
  "type":"control_error",
  "code":"invalid_input_gain",
  "message":"Input gain is outside the configured range or step",
  "input_gain_db":0
}
```

控制仅影响当前 WebSocket session，在 `BUFFERING`、`CANDIDATE` 和 `RECORDING` 都可变更。每个 PCM 队列项在 WebSocket receive loop 接收时快照当时的增益；worker 使用该快照，绝不回读可变的当前值。因此控制消息前的历史、pre-trigger 与已录制帧不会被重新放大，消息后的新帧才使用新值。文中“SPL”仍指增益后 RMS dBFS，不是已校准的物理声压级。

---

# 9. 相邻语句去重

滑动窗口持续运行可能导致下一段语音的 pre-trigger 数据包含上一段录音的尾部。

系统必须记录：

```text
last_committed_sample_index
```

新录音生成 pre-trigger 快照时，实际起点应为：

```text
max(
    当前样本位置 - pretrigger_buffer_samples,
    last_committed_sample_index
)
```

目标：

* 不丢失新语句开头；
* 不重复发送上一语句已提交的 PCM；
* 保持全局样本时间线单调递增。

---

# 10. Qwen3-ASR-1.7B 集成

## 10.1 MVP 推理模式

MVP 采用 utterance/offline 模式：

```text
VAD-only 无声超时（或 max duration）结束录音
→ 得到完整 PCM
→ 转换为 Qwen3-ASR 所需格式
→ 执行一次完整识别
```

Qwen3-ASR 输入要求由适配层统一处理：

* 采样率：16000Hz；
* 声道：单声道；
* 音频格式：根据推理 SDK 转换；
* 输入音频不得删除句子内部的自然停顿；
* 只允许裁剪句首、句尾的冗余静音。

## 10.2 ASR Adapter 职责

* 加载 Qwen3-ASR-1.7B；
* 管理本地设备与精度类型；
* 接收完整 utterance；
* 验证采样率；
* 必要时执行重采样；
* 输出最终文本；
* 返回推理耗时与错误状态。

接口示例：

```python
class AsrResult:
    text: str
    language: str | None
    duration_ms: int
    inference_latency_ms: int
    error: str | None
```

---

# 11. YAML 配置设计

建议配置文件路径：

```text
/config/asr.yaml
```

示例：

```yaml
version: 1

audio:
  input_device: default
  sample_rate_hz: 16000
  channels: 1
  sample_format: float32
  capture_frame_ms: 20
  # 浏览器缓存缺失时的默认值，也是旧客户端的会话默认值。
  input_gain_db: 0
  # Web 滑块和 WebSocket 实时控制的唯一范围与步长来源。
  input_gain_min_db: -24
  input_gain_max_db: 36
  input_gain_step_db: 0.5

recording:
  # 滑动窗口记忆时长。
  # 不得与 Silero speech_pad_ms 混用。
  pretrigger_buffer_ms: 1000

  # Silero 判定结束后继续保存的尾部音频。
  posttrigger_tail_ms: 250

  # 单次语音允许的最大时长。
  max_utterance_ms: 30000

  # 最短有效录音时长。
  min_utterance_ms: 200

  # 候选触发等待 Silero 确认的最长时间。
  candidate_confirmation_timeout_ms: 900

  # VAD-only 结束：瞬时人声概率连续低于 vad.threshold 达到该时长即结束录音。
  end_no_voice_duration_ms: 1800

  # 是否避免相邻 utterance 之间重复样本。
  prevent_segment_overlap: true

# release_threshold_dbfs 已删除：录音结束不再依赖声压释放阈值，
# 该字段出现即按未知字段拒绝。
audio_trigger:
  enabled: true
  metric: rms_dbfs

  # 声压候选触发阈值（仅用于开始路径）。
  start_threshold_dbfs: -29.0

  # 连续多少毫秒超过阈值后才触发。
  min_trigger_duration_ms: 40

  # 声压触发后是否必须由 Silero 确认。
  require_vad_confirmation: true

vad:
  engine: silero
  version: "6.2"
  backend: onnx
  model_path: /models/silero-vad/silero_vad.onnx
  device: cpu

  # Silero 人声概率阈值。
  threshold: 0.50

  # Silero 退出人声状态前要求的连续静音时长。
  min_silence_duration_ms: 500

  # Silero 自身的时间戳边界修正参数。
  # 该参数不承担滑动窗口 PCM 保存职责。
  speech_pad_ms: 30

  # 16kHz 下的 Silero 输入块大小。
  frame_samples: 512

  # Silero 推理计算门控（空闲省 CPU，语义见 7.5 BUFFERING）。
  compute_gating_enabled: true
  compute_wake_threshold_dbfs: -35.0
  compute_suspend_after_quiet_ms: 500
  compute_warmup_context_ms: 100

asr:
  engine: qwen3_asr
  model_name: Qwen3-ASR-1.7B
  model_path: /models/qwen3-asr-1.7b
  device: cuda
  dtype: bfloat16
  inference_mode: offline
  language: auto

  # 输入 ASR 前是否强制检查采样率。
  validate_sample_rate: true

  # 仅在输入采样率不一致时启用重采样。
  allow_resample: true

runtime:
  config_reload_enabled: false
  fail_on_unknown_config: true
  log_level: INFO

observability:
  save_debug_audio: false
  debug_audio_directory: /data/debug-audio
  log_vad_probability: false
  log_audio_level: false
  log_state_transitions: true
```

---

# 12. 配置管理要求

所有关键参数必须通过统一配置对象访问。

禁止在业务代码中出现以下形式：

```python
sample_rate = 16000
pretrigger_ms = 1000
threshold = -40.0
speech_pad_ms = 30
```

正确形式：

```python
sample_rate = config.audio.sample_rate_hz
pretrigger_ms = config.recording.pretrigger_buffer_ms
threshold = config.audio_trigger.start_threshold_dbfs
speech_pad_ms = config.vad.speech_pad_ms
```

应用启动时必须：

1. 加载 `/config/asr.yaml`；
2. 校验字段类型；
3. 校验参数范围；
4. 校验字段之间的兼容性；
5. 对非法配置立即报错并终止启动；
6. 不允许静默回退到代码中的隐藏默认值。

代码可以在配置 Schema 中提供默认值，但最终运行参数必须统一进入配置对象，并支持被 YAML 覆盖。

---

# 13. 配置校验规则

至少包含以下规则：

| 配置项                                  | 校验规则                          |
| ------------------------------------ | ----------------------------- |
| `audio.sample_rate_hz`               | 必须大于 0；MVP 推荐且默认仅支持 16000     |
| `audio.channels`                     | MVP 必须为 1                     |
| `audio.input_gain_db`                | 有限数，位于最小/最大闭区间且按步长对齐；线性系数必须可由 Float32 路径表示 |
| `audio.input_gain_min_db`            | 有限数，严格小于最大值；线性系数必须可由 Float32 路径表示 |
| `audio.input_gain_max_db`            | 有限数，按从最小值开始的步长网格对齐；线性系数必须可由 Float32 路径表示 |
| `audio.input_gain_step_db`           | 有限正数，不得超过区间宽度 |
| `recording.pretrigger_buffer_ms`     | 必须大于等于 0                      |
| `recording.posttrigger_tail_ms`      | 必须大于等于 0                      |
| `recording.max_utterance_ms`         | 必须大于 `min_utterance_ms`       |
| `audio_trigger.start_threshold_dbfs` | 必须 `<= 0`；仅门控 BUFFERING→CANDIDATE 开始路径 |
| `audio_trigger.release_threshold_dbfs` | 已移除；出现即被未知字段拒绝 |
| `recording.end_no_voice_duration_ms`  | 必须大于 0 |
| `vad.compute_gating_enabled`          | 必填布尔键，schema 严格校验 |
| `vad.compute_wake_threshold_dbfs`     | 必填有限数；SPL 触发与 gating 同启时必须 `<= start_threshold_dbfs`，违反即启动失败 |
| `vad.compute_suspend_after_quiet_ms`  | 必须大于 0 |
| `vad.compute_warmup_context_ms`       | 必须大于等于 0 |
| `vad.threshold`                      | 必须在 0 到 1 之间                  |
| `vad.frame_samples`                  | 16kHz 默认必须为 512，除非对应模型明确支持其他值 |
| `vad.speech_pad_ms`                  | 必须大于等于 0                      |
| `asr.model_path`                     | 必须存在且可读                       |
| `vad.model_path`                     | 必须存在且可读                       |

还必须校验：

```text
pretrigger_buffer_samples =
    sample_rate_hz × pretrigger_buffer_ms / 1000
```

计算结果必须为正整数或经过明确的取整策略处理。

---

# 14. 错误处理

## 14.1 麦克风断开

系统应：

* 停止当前录音；
* 标记当前 utterance 为失败；
* 清空 Silero 状态；
* 清空或重建 ring buffer；
* 记录错误日志；
* 根据上层策略尝试重新打开设备。

## 14.2 音频时间线中断

如果发现采样位置不连续：

* 不得直接把不连续音频拼接为同一录音；
* 应结束或丢弃当前 utterance；
* 重置 Silero 状态；
* 重置滑动窗口。

## 14.3 Silero 推理失败

根据配置决定：

* 丢弃当前候选录音；
* 或降级为纯声压触发模式。

MVP 默认不建议静默降级，推荐记录错误并返回 `BUFFERING`。

## 14.4 Qwen3-ASR 推理失败

系统应保留：

* utterance ID；
* 音频时长；
* 错误原因；
* 推理耗时；
* 可选的调试音频。

不得因单次 ASR 失败导致麦克风采集线程停止。

## 14.5 超长录音

当达到 `max_utterance_ms`：

* 强制结束当前录音；
* 添加允许范围内的尾部音频；
* 提交 ASR；
* 记录 `max_duration_reached=true`。

---

# 15. 并发与实时性要求

麦克风回调线程不得执行：

* Qwen3-ASR 推理；
* 文件写入；
* 大规模数组拼接；
* 阻塞式模型加载；
* 长时间日志输出。

推荐线程模型：

```text
Audio Callback Thread
    ↓ lock-free queue
Audio Processing Thread
    ↓
VAD / Trigger / Ring Buffer
    ↓ completed utterance queue
ASR Worker
    ↓
Qwen3-ASR-1.7B
```

Ring buffer 应使用预分配数组和写指针。

不得在每个采样点到达时移动整个数组。

---

# 16. 数据完整性要求

最终交给 Qwen3-ASR 的 PCM 必须满足：

1. 样本按时间顺序排列；
2. 不缺失触发所在帧；
3. 不重复触发所在帧；
4. 不重复上一 utterance 已提交的样本；
5. 保留触发前最多 `pretrigger_buffer_ms` 的数据；
6. 保留短时自然停顿；
7. 不将所有 speech chunk 无间隔拼接；
8. 采样率与配置一致；
9. 声道数与配置一致；
10. 不因 Silero `speech_pad_ms` 再次重复添加 pre-trigger PCM。

---

# 17. 日志与可观测性

每次 utterance 至少记录：

```text
utterance_id
start_sample_index
end_sample_index
pretrigger_samples
recorded_samples
duration_ms
trigger_dbfs
max_dbfs
silero_start_probability
silero_confirmed
silero_end_reason
asr_latency_ms
asr_text_length
max_duration_reached
error_code
```

`silero_end_reason` 取值为 `max_duration` 或 `no_voice_timeout`（旧值 `combined_silence` 已随组合静默机制移除）。

调试模式下可额外保存：

* 完整录音；
* 仅 pre-trigger 音频；
* 声压时间序列；
* Silero 概率时间序列；
* 状态机转换时间线。

调试音频默认关闭，避免隐私与磁盘占用问题。

---

# 18. 安全与隐私

由于麦克风处于持续采集状态，必须明确：

* 未触发时，音频只存在于固定容量的内存环形缓冲区；
* 环形缓冲区中的旧数据会自动覆盖；
* 未确认的人声候选录音应及时释放；
* 默认不将未触发音频写入磁盘；
* 默认不上传任何音频；
* 调试音频保存必须由配置显式开启；
* 应在产品界面显示麦克风持续监听状态；
* 停止监听后必须释放音频设备并清除内存缓冲区。

---

# 19. 验收标准

## AC-01：滑动窗口容量

配置：

```yaml
sample_rate_hz: 16000
pretrigger_buffer_ms: 1000
```

预期：

```text
ring buffer 最大容量为 16000 samples
```

写入第 16001 个样本时，最早的第 1 个样本被覆盖，系统不停止采集。

---

## AC-02：参数非硬编码

修改：

```yaml
pretrigger_buffer_ms: 1500
```

重启后，滑动窗口容量应变为：

```text
16000 × 1500 / 1000 = 24000 samples
```

不需要修改业务代码。

---

## AC-03：采样率配置生效

修改：

```yaml
sample_rate_hz: 16000
```

采集模块、ring buffer、Silero Adapter 和 ASR Adapter 必须使用同一个配置值。

系统中不得存在第二套独立硬编码采样率。

---

## AC-04：保留语音开头

测试音频在声压触发前 300ms 已包含低音量语音。

最终提交给 ASR 的音频必须包含该 300ms 低音量语音。

---

## AC-05：触发帧不重复

触发所在音频帧在最终 PCM 中只能出现一次。

通过样本序列编号验证不得出现重复区间。

---

## AC-06：Silero 未确认时不调用 ASR

声音超过声压阈值，但 Silero 在确认超时时间内未检测到人声。

预期：

* 丢弃候选录音；
* 不调用 Qwen3-ASR；
* 状态恢复为 `BUFFERING`。

---

## AC-07：Silero 确认后解除容量限制

Silero 确认为人声后，即使语音持续超过 1 秒，系统也必须继续录制。

1 秒只限制未触发状态下的历史记忆窗口，不限制正式录音时长。

---

## AC-08：尾部音频

配置：

```yaml
posttrigger_tail_ms: 250
```

Silero 报告结束后，最终录音必须额外保留最多约 250ms 的尾部 PCM。

> 修订（2026-08-24）：“Silero 报告结束”现为 VAD-only 无声超时（或 max duration）；尾部语义不变。

---

## AC-09：Silero padding 与滑动窗口独立

分别修改：

```yaml
recording:
  pretrigger_buffer_ms: 1000

vad:
  speech_pad_ms: 30
```

两者必须独立生效。

修改 `speech_pad_ms` 不得改变 ring buffer 容量。

修改 `pretrigger_buffer_ms` 不得改变 Silero 的边界参数。

---

## AC-10：Qwen3-ASR 输入格式

发送给 Qwen3-ASR-1.7B 的音频必须满足：

```text
16000Hz
mono
连续 PCM
无重复帧
包含 pre-trigger 音频
```

---

## AC-11：配置错误快速失败

若 SPL 触发与 compute gating 同时启用且配置为：

```yaml
audio_trigger:
  start_threshold_dbfs: -40.0

vad:
  compute_gating_enabled: true
  compute_wake_threshold_dbfs: -35.0
```

由于 wake 线（-35）高于开始阈值（-40），违反 `vad.compute_wake_threshold_dbfs <= audio_trigger.start_threshold_dbfs` 的交叉校验，系统必须在启动阶段报配置错误，不得静默运行。

> **修订（2026-08-24）：** 旧示例 `start_threshold_dbfs: -50 / release_threshold_dbfs: -40` 随 `release_threshold_dbfs` 字段删除而失效，替换为上述 wake/start 交叉校验示例。

---

## AC-12：持续监听内存稳定

系统在无语音环境中持续运行至少 8 小时：

* ring buffer 内存占用保持固定；
* 不产生持续增长的音频数组；
* 不生成未配置的临时音频文件；
* 不持续累积候选录音对象。

---

# 20. 测试方案

## 20.1 单元测试

覆盖：

* ring buffer 写入；
* ring buffer 覆盖；
* ring buffer 有序快照；
* 样本位置裁剪；
* 触发帧去重；
* 相邻 utterance 去重；
* dBFS 计算；
* 配置加载；
* 配置校验；
* 状态机转换。

## 20.2 集成测试

覆盖：

* 麦克风模拟流；
* 声压触发；
* Silero 确认；
* Silero 拒绝；
* 完整录音生成；
* Qwen3-ASR 调用；
* 超时和异常恢复。

## 20.3 音频场景测试

至少准备以下测试集：

| 场景      | 预期              |
| ------- | --------------- |
| 正常近讲语音  | 正确触发并识别         |
| 低音量语音开头 | 开头不丢失           |
| 突然大声说话  | 不削掉首音素          |
| 键盘声     | 不调用 ASR         |
| 敲桌声     | Silero 拒绝或无有效文本 |
| 电视背景声   | 控制误触发率          |
| 长停顿语句   | 不过早丢失录音         |
| 连续两句话   | 不重复上一段尾部        |
| 超过最大时长  | 自动结束            |
| 麦克风断开   | 可恢复且不崩溃         |

---

# 21. 性能指标

MVP 建议目标：

| 指标                |                   目标 |
| ----------------- | -------------------: |
| 未触发状态内存           |          固定，不随运行时间增长 |
| 默认 pre-trigger 内存 |      float32 下约 64KB |
| Ring buffer 写入复杂度 |                 O(1) |
| Silero 运行设备       |                  CPU |
| Qwen3-ASR 运行设备    |               优先 GPU |
| 丢失语音开头率           | 显著低于无 pre-trigger 基线 |
| 非人声事件调用 ASR 比例    |     通过 Silero 确认明显降低 |
| 单次录音最大时长          |          默认 30 秒，可配置 |

默认 1 秒、16000Hz、float32、单声道的内存大小：

```text
16000 samples × 4 bytes = 64000 bytes
```

不包含对象元数据时约为 64KB。

---

# 22. 推荐目录结构

```text
project/
├── config/
│   └── asr.yaml
├── models/
│   ├── silero-vad/
│   └── qwen3-asr-1.7b/
├── src/
│   ├── audio/
│   │   ├── capture.py
│   │   ├── ring_buffer.py
│   │   ├── level_detector.py
│   │   └── resampler.py
│   ├── vad/
│   │   └── silero_adapter.py
│   ├── recording/
│   │   ├── state_machine.py
│   │   └── utterance.py
│   ├── asr/
│   │   └── qwen_adapter.py
│   ├── config/
│   │   ├── loader.py
│   │   └── schema.py
│   └── main.py
└── tests/
    ├── test_ring_buffer.py
    ├── test_state_machine.py
    ├── test_config.py
    └── test_asr_pipeline.py
```

---

# 23. 实施阶段

## Phase 1：配置和音频基础设施

实现：

* YAML 配置加载；
* Schema 校验；
* 麦克风采集；
* 全局样本计数；
* ring buffer；
* dBFS 检测。

## Phase 2：Silero 集成

实现：

* Silero v6.2 ONNX 加载；
* 512-sample chunk 聚合；
* VAD 状态维护；
* speech start/end 事件；
* 配置化阈值。

## Phase 3：录音状态机

实现：

* `BUFFERING`；
* `CANDIDATE`；
* `RECORDING`；
* `FINALIZING`；
* pre-trigger 快照；
* 触发帧去重；
* post-trigger tail；
* 最大录音时长。

## Phase 4：Qwen3-ASR 集成

实现：

* 本地模型加载；
* PCM 格式转换；
* utterance 识别；
* 错误处理；
* 识别结果输出。

## Phase 5：测试与调优

完成：

* 真实麦克风测试；
* 噪声环境测试；
* 参数 A/B 测试；
* 声压阈值调优；
* Silero 阈值调优；
* pre-trigger 时长调优；
* 性能和内存测试。

---

# 24. 最终方案摘要

本功能采用两阶段触发：

```text
声压超过用户配置阈值
→ 快速建立候选录音
→ Silero VAD v6.2 确认是否为人声
→ 确认后进入正式录音
→ 瞬时人声概率连续低于阈值达 end_no_voice_duration_ms（VAD-only 无声超时）
→ 拼接尾部音频
→ Qwen3-ASR-1.7B 完整识别
```

滑动窗口机制采用独立配置：

```yaml
recording:
  pretrigger_buffer_ms: 1000
```

Silero 边界修正采用独立配置：

```yaml
vad:
  speech_pad_ms: 30
```

二者不得混用。

所有关键参数，包括：

* 采样率；
* 声道数；
* 音频帧时长；
* 滑动窗口时长；
* 声压阈值；
* Silero 阈值；
* Silero speech padding；
* 无声结束时长（`recording.end_no_voice_duration_ms`）；
* 录音尾部时长；
* 最大录音时长；
* 模型路径；
* 推理设备；

均必须从 `/config/asr.yaml` 中统一读取，不允许在业务逻辑中硬编码。

---

# 修订记录（2026-08-24）

1. **VAD-only 话轮结束（噪声环境修复）**
   - 录音结束判定改为纯 Silero VAD：瞬时人声概率连续低于 `vad.threshold` 达 `recording.end_no_voice_duration_ms`（生产默认 1800 ms）即结束并提交 ASR；不再存在基于声压释放阈值的组合静默结束。
   - `audio_trigger.release_threshold_dbfs` 字段整体删除（schema 未知字段拒绝）；`candidate_confirmation_timeout_ms` 默认对齐为 900 ms；`recording.end_silence_duration_ms` 由 `recording.end_no_voice_duration_ms` 取代。
   - 声压阈值只服务开始路径（BUFFERING→CANDIDATE + Silero 第二道确认）；`silero_end_reason` 取值为 `max_duration` / `no_voice_timeout`，旧值 `combined_silence` 移除。涉及 §5.1、§7.3、§7.5、§10.1、§11、§13、§17、AC-08、AC-11、§24。
   - 动机：仓库最初面向安静房间调参；噪声环境中环境 RMS 长期高于释放阈值，绝对静音结束条件永不满足，导致无限录制环境噪声。改为人声存在性单条件结束，同时保留灵敏的 SPL 开始触发 + VAD 确认作为第二道守门。
2. **Silero VAD compute gating（空闲 CPU 节省）**
   - `vad:` 新增必填键 `compute_gating_enabled: true`、`compute_wake_threshold_dbfs: -35.0`、`compute_suspend_after_quiet_ms: 500`、`compute_warmup_context_ms: 100`（schema 严格校验）；语义见 §7.5 BUFFERING 小节（实现：`backend/asr/session.py`）。
   - 新增交叉校验：SPL 触发与 gating 同启时 `vad.compute_wake_threshold_dbfs <= audio_trigger.start_threshold_dbfs`；示例配置 `start_threshold_dbfs` 由 -40.0 对齐为生产触发线 -29.0 以满足该校验（涉及 §7.3、§11、§13、AC-11）。
   - 动机：语音/听写场景空闲期长，此前两段式触发决策并未门控推理本身，Silero 全天候推理浪费 Jetson CPU。默认值依据 2026-08-24 网络调研：500 ms 挂起滞回高于语内停顿（约 80–250 ms）、低于对话间隙（约 1 s）；−35 dBFS wake 线约比 −29 dBFS 触发线低 6 dB，为软起音争取约 30–150 ms 预热提前量；100 ms warm-up ≈ 3×512-sample chunks，符合“adapter reset 后概率需若干 chunks 才重新可信”的实践指引。
3. 以上修订将本文档相应小节中与之直接矛盾的旧表述改写或标注为“修订（2026-08-24）……已废弃”；历史设计意图保留作对照，未整体删除。
