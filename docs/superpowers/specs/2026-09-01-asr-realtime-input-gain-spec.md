# ASR 实时输入增益滑块规格

**状态：** 已批准，可直接实施  
**日期：** 2026-09-01  
**范围：** Web UI 听写模式与全自动语音模式、浏览器 ASR 控制协议、单个 ASR WebSocket 会话  
**不涉及：** LLM 提示词、SGLang/vLLM 推理请求、浏览器自动增益控制、服务器 YAML 在线回写

## 1. 背景

当前浏览器通过 `AudioWorklet` 发送单声道 Float32 PCM。后端 `AsrSession` 对每个音频帧使用固定的 `config.audio.input_gain_db` 施加一次软件增益，随后依次完成：

1. post-gain RMS dBFS 计算；
2. SPL 启动触发与 VAD compute gating；
3. Silero VAD；
4. pre-trigger/recording 音频缓冲；
5. ASR 转写。

前端 `VoiceListeningIndicator` 目前只展示 `gain x.x dB`，不能调节。增益在 ASR WebSocket 会话创建时被配置对象固定，因此 SPL 已触发或进入 `RECORDING` 后无法改变。

本规格增加一个紧邻 `gain x.x dB` 的滑块。滑块值来自浏览器缓存或 YAML 默认值，并通过既有 ASR WebSocket 在单个会话内实时更新后端增益。

本文中的“SPL”沿用项目既有命名，实际指标是数字音频 post-gain RMS dBFS，不是经过声学标定的物理声压级。

## 2. 目标

1. Text 页面与 Vision 页面在听写或语音模式下显示同一套输入增益滑块。
2. 滑块最小值、最大值和步长全部来自 `config/config-asr.yaml`，前端和后端不复制隐藏范围常量。
3. 浏览器缓存值优先；只有缓存不存在或无效时才采用 YAML 的 `audio.input_gain_db`。
4. 拖动时立即更新标签、浏览器缓存和当前 ASR WebSocket 会话。
5. 增益在 `BUFFERING`、`CANDIDATE` 和 `RECORDING` 阶段均可改变，不因 SPL 触发或录音开始而冻结。
6. 增益更新严格作用于控制消息之后接收的 PCM 帧；已经接收或缓冲的历史 PCM 不重新放大。
7. 增益仍只在后端应用一次，浏览器不得对 PCM 另做软件放大。
8. 一个浏览器会话的变化不修改 YAML，也不改变其他已连接的 ASR 会话。

## 3. 非目标

- 不校准真实物理 SPL。
- 不启用或修改 `getUserMedia` 的 `autoGainControl`。
- 不把增益写入 `llm-chat-settings`；该键仍只管理聊天设置。
- 不增加 REST 写配置端点，不在线修改 `config/config-asr.yaml`。
- 不在多个已打开标签页之间实时广播滑块变化；相同 origin 的新页面会读取同一缓存。
- 不重新处理 pre-trigger ring buffer、已录制音频或已经提交给 ASR 的 WAV。
- 不改变听写插入文本、语音自动发送、Voice replay、页面可见性恢复、Stop 或 Agent/Chat 路由语义。

## 4. 配置契约

`config/config-asr.yaml` 的 `audio` 段新增三个必填字段：

```yaml
audio:
  input_gain_db: 0
  input_gain_min_db: -24
  input_gain_max_db: 36
  input_gain_step_db: 0.5
```

字段语义：

| 字段 | 语义 |
|---|---|
| `input_gain_db` | 没有有效浏览器缓存时使用的默认增益；也是旧客户端的会话默认值 |
| `input_gain_min_db` | Web 滑块与 WebSocket 控制允许的最小值，含边界 |
| `input_gain_max_db` | Web 滑块与 WebSocket 控制允许的最大值，含边界 |
| `input_gain_step_db` | 滑块步长与允许的离散增益间隔 |

### 4.1 校验

后端配置模型必须严格校验：

- 四个值都是有限数字；
- `input_gain_min_db < input_gain_max_db`；
- `input_gain_db` 位于闭区间 `[min, max]`；
- `input_gain_step_db > 0` 且不大于区间宽度；
- 默认值和最大值都能按步长从最小值到达，浮点比较使用小容差；
- 最小值、最大值和默认值转换为线性增益时必须得到有限、可由 Float32 音频路径处理的值。

旧的 `Field(ge=-24.0, le=36.0)` 必须移除；合法范围的来源只能是 YAML 新字段。

ASR 配置的 `version` 保持 `1`。因为项目使用严格必填配置并在启动时失败，部署者必须同步更新 YAML；不提供代码内隐藏默认值。

### 4.2 `/config` 浏览器安全契约

`GET /config` 的 `asr` 对象新增：

```json
{
  "input_gain_db": 0,
  "input_gain_min_db": -24,
  "input_gain_max_db": 36,
  "input_gain_step_db": 0.5
}
```

不得暴露模型路径、证书路径等服务器私有字段。YAML 增益范围由 ASR 服务启动时加载；修改 YAML 后需要重启后端，浏览器再重新读取 `/config`。

## 5. 浏览器缓存契约

使用独立的 localStorage key：

```text
llm-asr-input-gain
```

值采用带版本对象：

```json
{
  "version": 1,
  "inputGainDb": 6.5
}
```

### 5.1 读取顺序

1. `/config` 尚未到达时不推断范围，也不写缓存。
2. 配置到达后读取 `llm-asr-input-gain`。
3. 若对象版本正确且 `inputGainDb` 是有限数字，则按当前 YAML 范围钳制并按 YAML 步长归一化。
4. 若钳制或步长归一化改变了缓存值，立即回写规范值。
5. 若缓存缺失、JSON 损坏、版本错误、字段缺失或数值非有限，使用 YAML `input_gain_db`；损坏缓存应删除。
6. 用户每次改变滑块后立即写入缓存。

缓存值存在时优先于 YAML 默认值，包括 YAML 默认值后来发生变化的情况；只有当前 YAML 范围仍具有最终约束权。

缓存读写必须放在浏览器副作用或事件处理路径，不得在 React render 阶段写 localStorage，也不得影响 SSR/Node 测试导入。

## 6. WebSocket 协议

### 6.1 客户端控制消息

在成功完成现有 `start` handshake 后，客户端可以发送：

```json
{
  "type": "set_input_gain",
  "input_gain_db": 6.5
}
```

该消息只影响当前 WebSocket 对应的 `AsrSession`。旧客户端不发送该消息时继续使用 YAML `input_gain_db`。

### 6.2 成功确认

```json
{
  "type": "input_gain_changed",
  "input_gain_db": 6.5
}
```

服务端只确认已经通过范围与步长校验、并已经成为后续 PCM 入队快照值的增益。

### 6.3 非致命控制错误

```json
{
  "type": "control_error",
  "code": "invalid_input_gain",
  "message": "Input gain is outside the configured range.",
  "input_gain_db": 0
}
```

`input_gain_db` 是服务端仍在使用的最后一个有效值。错误输入包括：

- 缺失字段；
- 布尔值、字符串、对象或其他错误类型；
- `NaN`/无穷值；
- 越界值；
- 不符合配置步长的值；
- 在 handshake 完成前发送增益控制。

无效增益不得关闭 WebSocket、重置 VAD、清除 ring buffer 或终止正在录制的 utterance。前端收到错误后回退到服务端返回值、更新缓存，并通过既有 `onError` 路径报告可恢复错误。

未知控制消息仍沿用当前严格协议错误处理。`abort` 的终止语义保持不变。

### 6.4 协议版本

`protocol_version` 继续为 `1`。新增的是 handshake 后的可选控制消息和服务端事件，不改变 PCM 编码、握手必填字段或旧客户端行为。

## 7. 顺序与并发不变量

### 7.1 核心不变量

增益不得在 frame worker 消费 PCM 时读取一个共享的“当前值”。必须在 WebSocket receive loop 接收每个二进制 PCM 消息时，将当时的会话增益随 PCM 一起快照到队列项：

```text
receive PCM A       -> queue(A, 0 dB)
receive PCM B       -> queue(B, 0 dB)
receive gain +6 dB  -> current session gain = +6 dB
receive PCM C       -> queue(C, +6 dB)
receive PCM D       -> queue(D, +6 dB)
```

即使 worker 稍后才消费 A/B/C/D，结果仍必须分别使用 0/0/+6/+6 dB。

### 7.2 后端数据结构

`AsrSession` 新增会话字段：

```python
self._input_gain_db = config.audio.input_gain_db
```

frame queue item 从：

```python
(generation, payload, start_sample_index)
```

扩展为：

```python
(generation, payload, start_sample_index, input_gain_db)
```

`_enqueue_binary()` 在 receive loop 中快照 `_input_gain_db`；`_frame_worker()` 把快照传给 `_process_binary()`；`apply_gain()` 使用快照，不再直接读取 `self._config.audio.input_gain_db`。

控制消息与二进制消息在同一个 WebSocket receive loop 中顺序接收，因此不需要让控制项占用有界 PCM 队列，也不会由高频拖动挤占音频帧容量。

### 7.3 对录音状态的影响

| 状态 | 改变增益后的行为 |
|---|---|
| `BUFFERING` | 后续帧的新 post-gain dBFS 立即影响 compute wake 和 SPL trigger |
| `CANDIDATE` | 后续帧以新增益进入 VAD 与候选确认；已有帧不重算 |
| `RECORDING` | 后续帧以新增益写入 utterance；已有 pre-trigger/recording 帧保持原值 |
| `FINALIZING` | 若仍接收 PCM，则按正常消息顺序快照；已固定的 utterance 不变 |
| `TRANSCRIBING` | 当前实现忽略新 PCM；UI/缓存仍更新，下一次捕获使用最新值 |
| `PAUSED/下一轮` | 新会话建立后、首个 PCM 发送前应用缓存值 |

录音中改变增益可能改变 clipping、SPL 与 VAD 输入，这是功能的预期效果，不做平滑或冻结。

## 8. 前端控制器

`VoiceInputConfig` 新增 `inputGainMinDb`、`inputGainMaxDb` 和 `inputGainStepDb`。

`VoiceInputController` 暴露一个明确的增益控制对象：

```ts
export interface VoiceInputGainControl {
  valueDb: number;
  minDb: number;
  maxDb: number;
  stepDb: number;
  setValueDb: (valueDb: number) => void;
}
```

控制器负责：

- 配置到达后解析缓存或 YAML 默认值；
- 保存当前值和最后一次服务端确认值；
- 将 UI 输入规范化并持久化；
- 当前 socket 已打开且 handshake 完成时发送控制消息；
- socket 尚未 ready、正在清理或没有活动采集时只更新缓存；
- 新 socket 收到 `ready` 后先发送当前增益控制，再将 `socketReady` 设为 `true`，确保后续 `socket.send(frame)` 排在控制消息之后；
- 解析 `input_gain_changed` 与 `control_error`；
- `control_error` 时回退并持久化服务端有效值，不销毁麦克风资源。

不得通过修改 `config` 对象或把 `inputGainDb` 放进 `activateVoice` 的重建依赖来实现拖动，否则会导致 callback 身份变化或错误重启采集。

## 9. UI

共享组件 `VoiceListeningIndicator` 负责实际滑块，`VoiceInputPanel` 只转发控制对象。Text/Vision 两页只把 `voiceController.inputGain` 传给现有两条渲染路径：

- `inputMode === 'voice'`：`VoiceInputPanel`；
- `inputMode === 'dictation'` 且现有语音控制器处于语音生命周期：`VoiceListeningIndicator`。

布局要求：

- `Post-gain estimate ... / gain +x.x dB` 保留；
- `<input type="range">` 紧邻 gain 文本右侧；
- 使用 `minDb`、`maxDb`、`stepDb`，不得写死范围；
- 使用 React range `onChange`，拖动时连续更新；
- 不因 `listening`、SPL candidate、`recording` 或 `finalizing` 禁用；
- 窄屏允许增益文字和滑块作为一个 flex-wrap 单元换行；
- 保留现有 waveform 的弹性宽度；
- 提供 `aria-label="Input gain"`、`aria-valuemin`、`aria-valuemax`、`aria-valuenow` 和可辨识的 focus ring；
- 标签显示一位小数和显式正号，例如 `gain +6.5 dB`。

键盘模式不显示滑块，但缓存值不被清除。

## 10. 模式兼容矩阵

| 页面 | Chat/Agent | 输入模式 | 预期 |
|---|---|---|---|
| Text `/` | Chat | Dictation | 滑块可用；转写插入光标，不发送 |
| Text `/` | Agent | Dictation | 同上；不改变 Agent 路由 |
| Text `/` | Chat/Agent | Voice | 滑块可用；保留自动发送与响应后恢复 |
| Vision `/vision` | Chat | Dictation | 滑块可用；保留图片列表 |
| Vision `/vision` | Agent | Dictation | 同上；不改变 Vision/Agent context |
| Vision `/vision` | Chat/Agent | Voice | 滑块可用；保留 Vision acceptance 与可见性恢复门槛 |
| 任一页面 | 任意 | Keyboard | 不显示滑块，不清除缓存 |
| 任一页面 | Cycle running | Voice/Dictation | 沿用现有 activation gating；已经显示的增益控件不引入新音频生命周期 |

## 11. 错误处理与恢复

- localStorage 不可用或抛异常：继续使用内存值与 YAML 默认值，不阻止 ASR。
- 缓存损坏：删除损坏键，使用 YAML 默认值。
- 服务端拒绝增益：回退到 `control_error.input_gain_db`，缓存回退值，报告可恢复错误；WebSocket 保持连接。
- 控制消息发送异常：沿用当前 WebSocket recoverable error 路径；缓存保留用户最后选择，便于下一次连接重试。
- 页面卸载：沿用现有麦克风、worklet、socket 和 replay URL 清理，不增加全局 listener。
- 标签页 hidden：不得停止采集；滑块功能不改变现有 visibility 语义。
- YAML 范围变化：重启后端并刷新页面；旧缓存按新范围钳制和步长归一化。

## 12. 文件边界

### 后端与配置

- `config/config-asr.yaml`：声明默认值、最小值、最大值、步长。
- `backend/asr/config.py`：严格 schema 与跨字段校验。
- `backend/asr/session.py`：控制协议、会话当前值、PCM 入队快照和确认事件。
- `backend/main.py`：向 `/config.asr` 暴露浏览器安全范围。
- `tests/test_asr_config.py`：配置合法/非法组合。
- `tests/test_asr_websocket.py`：协议、会话隔离、录音中更新和顺序不变量。

### 前端

- `frontend/src/lib/input-gain-storage.ts`：纯函数缓存解析、钳制、步长归一化与写入。
- `frontend/src/lib/input-gain-storage.test.mts`：缓存优先级与边界测试。
- `frontend/src/lib/api.ts`：`ConfigResponse.asr` 新字段。
- `frontend/src/lib/voice-input.ts`：配置映射、控制器状态、控制消息、服务端事件和新会话初始化顺序。
- `frontend/src/components/VoiceListeningIndicator.tsx`：共享滑块与无障碍 UI。
- `frontend/src/components/VoiceInputPanel.tsx`：转发增益控制对象。
- `frontend/src/app/page.tsx`：Text 页两条语音渲染路径接线。
- `frontend/src/app/vision/page.tsx`：Vision 页同步接线。
- `frontend/src/lib/voice-input-state.test.mts`：配置映射、协议事件和控制器源契约。
- `frontend/src/lib/voice-input-pages.test.mts`：共享组件、两页同步与模式可用性回归。

### 文档

- `docs/ASR-部署-PRD.md`：补充 YAML 字段、WebSocket 控制消息、缓存优先级与实时顺序语义。

## 13. 验收标准

### 配置与缓存

- AC-1：修改 YAML min/max/step 后，`/config` 与滑块属性随之变化，前端无重复范围常量。
- AC-2：缓存存在时覆盖 YAML 默认值；缓存不存在或损坏时使用 YAML 默认值。
- AC-3：缓存越过新范围时被钳制、按步长归一化并回写。

### 实时行为

- AC-4：首个 PCM 帧之前，当前缓存/default 增益控制已先在 WebSocket 发送。
- AC-5：`BUFFERING` 拖动后，下一个 PCM 帧使用新增益。
- AC-6：SPL 已进入 `CANDIDATE` 后仍可改变增益。
- AC-7：`RECORDING` 中仍可改变增益，控制前后的帧分别使用旧值和新值。
- AC-8：worker 队列积压不会让新值追溯影响已入队旧帧。
- AC-9：一个会话改变增益不影响另一个会话。
- AC-10：增益只应用一次，浏览器发送的 PCM 保持原始采样值。

### UI 与模式

- AC-11：Text/Vision 的 Dictation/Voice 四条渲染路径显示同一共享滑块。
- AC-12：滑块紧邻 `gain x.x dB`，录音中不 disabled，键盘和触摸拖动连续更新。
- AC-13：Keyboard 模式隐藏滑块但保留缓存。
- AC-14：听写、自动语音恢复、Vision 图片、Agent/Chat、Cycle gating 和 replay 行为无回归。
- AC-15：滑块满足原生 range 与 ARIA 无障碍要求。

### 错误与兼容性

- AC-16：非法控制返回非致命 `control_error`，不关闭 socket、不清空录音。
- AC-17：旧客户端只做 start + PCM 时仍使用 YAML 默认值。
- AC-18：前端收到控制拒绝时回退值并保持麦克风会话。
- AC-19：Python ASR 测试、前端 voice 测试、TypeScript type-check 和独立 Next build 均通过。

## 14. 验证命令

后端：

```bash
.sglang/bin/python -m pytest tests/test_asr_config.py tests/test_asr_websocket.py tests/test_asr_audio.py tests/test_asr_state_machine.py -q
```

前端：

```bash
cd frontend
node --test --experimental-strip-types \
  src/lib/input-gain-storage.test.mts \
  src/lib/voice-input-state.test.mts \
  src/lib/voice-input-pages.test.mts
npm run type-check
npm run build:verify
```

## 15. 风险与控制

- **录音中突变产生幅度台阶：** 这是实时调节的明确语义；不做平滑，因为平滑会模糊控制消息对应的帧边界。
- **高增益 clipping：** 继续由 `clip_limit` 限制，并由现有 clipping ratio/波形反馈给用户。
- **频繁拖动消息：** 控制消息不进入 PCM 有界队列；每次浏览器事件只更新一个标量，不复制音频。
- **Text/Vision 漂移：** 滑块实现在共享组件和控制器中；页面只做相同的一行 props 接线，并用双页源契约测试锁定。
- **缓存与服务端范围不一致：** 前端先按 `/config` 归一化，服务端再次严格验证，并用 `control_error` 给出当前有效值。
- **热改 YAML 造成配置分歧：** ASR `config_reload_enabled` 仍为 false；范围变更明确要求后端重启和页面刷新。
