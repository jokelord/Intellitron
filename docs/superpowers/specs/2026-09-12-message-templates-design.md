# 消息模板功能设计 SPEC

日期：2026-09-12  
状态：设计已确认，待实现  
范围：Web UI text 模式与 vision 模式

## 1. 背景

当前 Web UI 的输入区通过“+”按钮展开附加功能。用户希望增加“消息模板”功能，用于保存和复用多条常用消息，例如：

- `Hello, what is your name?`
- `Tell me the weather forecast for Changsha.`

模板的初始内容由 `config/config-webUI.yaml` 提供；用户在浏览器内的编辑、删除和新增内容应保存在浏览器缓存中，并优先于后端 YAML。YAML 只负责首次没有用户缓存时的初始化来源，不提供浏览器配置写回能力。

本功能必须同时覆盖以下两个独立页面：

- `frontend/src/app/page.tsx`：text 模式；
- `frontend/src/app/vision/page.tsx`：vision 模式。

两页应复用共享的数据 helper 和 UI 组件，只保留页面自身的输入发送 glue code，避免行为漂移。

## 2. 目标

### 2.1 必须实现

1. 在“+”展开的功能栏中增加“消息模板”按钮。
2. 点击“消息模板”后展示当前有效模板列表，每个模板以按钮形式呈现。
3. 点击模板按钮时：
   - 默认将模板全文填入 input bar，允许用户编辑后自行点击发送；
   - 可通过“消息模板”面板中的单一 ToggleSwitch 开启立即发送模式。
4. 每个模板项都提供编辑按钮和删除按钮。
5. 增加“新增”按钮，允许创建新的浏览器模板。
6. 增加“恢复默认”按钮，清除浏览器模板覆盖并恢复使用当前 YAML 中的模板。
7. 长模板按钮显示截断后的单行文本，并通过完整 `aria-label` 向辅助技术提供全文。
8. 浏览器缓存中的模板列表优先于 YAML；编辑、删除、新增只修改浏览器缓存。
9. text 和 vision 页面保持相同的模板数据、缓存、发送模式与编辑语义。

### 2.2 非目标

1. 本功能不新增后端“保存模板”API。
2. 本功能不修改 LLM prompt、system prompt、Agent memory、timeline、context pack 或 Prefix-Cache 相关数据。
3. 本功能不改变现有 Chat、Agent、Vision 的发送协议；模板最终通过既有 input/send 流程进入正常用户消息。
4. 本 SPEC 不要求跨用户、跨设备或账号级模板同步。
5. 本 SPEC 不要求在后端 YAML 中支持用户编辑后的模板回写。

## 3. 设计决策与备选方案

### 3.1 推荐方案：共享组件 + 独立模板缓存

采用一个共享 `MessageTemplateMenu` 组件，以及一组位于 `frontend/src/lib/web-ui-config.ts` 的纯函数，负责：

- 模板配置类型；
- YAML 模板列表的解析结果读取；
- localStorage 的安全读取、校验、写入和恢复；
- 有效模板列表的优先级解析。

页面只负责把当前 input、发送回调和模板列表传给共享组件。vision 页的模板操作仍通过既有 vision send pipeline 发送当前文本和当前待发送图片，不复制一套新的发送协议。

### 3.2 不采用：两页各自实现模板逻辑

虽然改动较快，但会复制缓存解析、编辑、删除和发送模式开关行为，容易造成 text/vision 语义不一致，与项目中“两页同步维护”的约束冲突。

### 3.3 不采用：复用 `llm-chat-settings`

消息模板不是推理参数或 Web UI 设置。将其嵌入 `llm-chat-settings` 会使设置对象、键存在性和模板数组的校验耦合，也会增加现有设置合并逻辑误覆盖模板的风险。因此模板使用独立 localStorage key。

## 4. 配置与数据契约

### 4.1 YAML 配置结构

在 `config/config-webUI.yaml` 的 `webUI.components` 下增加：

```yaml
components:
  message_templates:
    items:
      - "Hello, what is your name?"
      - "Tell me the weather forecast for Changsha."
```

`message_templates` 放在 `components` 下，与 `input_bar`、`action_buttons`、`agent_steps` 等现有 Web UI 组件配置保持一致。

### 4.2 后端配置解析

扩展 `backend/config.py` 的 `WebUIConfig` 和 `_parse_web_ui_config()`：

- 增加 `message_templates: list[str]` 字段，默认值为空数组；
- 从 `webUI.components.message_templates.items` 读取模板；
- 只接受数组中的字符串项；
- 空白模板被忽略，非字符串项被忽略；
- 保留有效模板的原始顺序和文本内容，不对正文做去重或改写；
- 缺失、空对象或非法顶层结构回退为空数组，不影响其他 Web UI 配置加载。

扩展 `backend/main.py` 的 `_serialize_web_ui_components()`，让 `/config` 响应中包含：

```json
{
  "webUI": {
    "components": {
      "message_templates": {
        "items": [
          "Hello, what is your name?"
        ]
      }
    }
  }
}
```

后端只返回 YAML 解析后的初始化列表，不读取或合并浏览器 localStorage。

### 4.3 前端类型与缓存格式

在 `ConfigResponse.webUI.components` 和 `WebUIConfig.components` 中增加：

```ts
message_templates?: {
  items?: string[];
};
```

使用独立缓存 key：

```text
llm-message-templates
```

缓存值为 JSON 数组，例如：

```json
["用户编辑后的模板", "另一条常用消息"]
```

缓存数组中的字符串内容原样保存；只用 `trim()` 判断是否为空，不因校验而改写非空正文。缓存数组允许为空，空数组表示用户明确删除了全部模板，必须覆盖 YAML，而不能被当作“没有缓存”。

## 5. 缓存优先级与生命周期

### 5.1 有效列表解析

共享 helper 应区分“缓存不存在”“缓存有效”“缓存非法”三种状态：

| localStorage 状态 | 有效模板列表 | 是否修改缓存 |
|---|---|---|
| key 不存在 | YAML `items` | 否 |
| JSON 是合法的非空字符串数组 | 缓存数组 | 否 |
| JSON 是合法的空数组 | 空数组 | 否 |
| JSON 解析失败、不是数组或包含非法项 | YAML `items` | 否，保留原缓存便于诊断 |
| localStorage 读取异常 | YAML `items` | 否 |

“合法数组”要求每个元素都是非空字符串；出现非法元素时整份缓存视为非法并回退 YAML，避免静默丢弃用户缓存的一部分。

### 5.2 首次加载

页面收到 `/config` 响应后：

1. 提取后端返回的 YAML 模板列表；
2. 安全读取 `llm-message-templates`；
3. 按 5.1 的规则计算 effective templates；
4. 将 effective templates 放入 React state。

首次加载且没有缓存时只读取 YAML，不自动把 YAML 写入缓存。这样后续 YAML 修改仍可被没有用户自定义过的浏览器读取，同时“恢复默认”可以通过清除缓存重新获得最新 YAML。

### 5.3 用户修改

以下操作都把修改后的完整列表写入 `llm-message-templates`：

- 新增；
- 编辑并保存；
- 删除。

例如用户只编辑了 YAML 列表中的一项，写入缓存的是包含其他未编辑项的完整 effective list；从此该浏览器使用整组缓存列表，后端 YAML 后续变化不会覆盖用户配置。

React state 更新和 localStorage 写入必须由共享 helper/回调完成，避免两个页面出现不同序列化格式。

### 5.4 恢复默认

“恢复默认”按钮执行以下动作：

1. 提示用户确认，因为该动作会清除当前浏览器中的新增、编辑和删除结果；
2. 删除 `llm-message-templates`；
3. 将当前 `/config` 中的 YAML `items` 设为 effective list；
4. 不把 YAML 列表重新写入缓存。

恢复的是当前加载到的 YAML 默认，而不是首次安装时的静态快照。若删除缓存失败，不应声称恢复成功；保留当前 state，并展示非阻塞错误提示。

## 6. UI 结构与交互

### 6.1 功能栏入口

`+` 按钮仍由两页各自控制功能栏展开状态。功能栏可见时增加“消息模板”按钮：

- text 页：消息模板与上传图片、摄像头拍照占位按钮、编程循环按钮位于同一个 action grid；窄屏允许两列换行，`sm` 及以上为四列等宽；
- vision 页：消息模板与上传图片、手动拍照、视觉自动化、编程循环位于 `ImageUploader` 的同一个 action grid，`sm` 及以上为五列等宽；
- `MessageTemplateMenu` 的触发按钮是 grid cell，展开 panel 使用 `col-span-full` 跨整行；不使用 `display: contents`。

消息模板面板只在功能栏可见且用户点击“消息模板”后显示。关闭 `+` 时应关闭面板；若存在未保存编辑，按取消处理，不写入缓存。

### 6.2 立即发送开关

面板顶部复用 `frontend/src/components/ToggleSwitch.tsx`，只显示一个开关：

```text
[off] 选中模板后立即发送
```

- 缓存缺失或不可用时默认关闭；关闭时选择模板调用 `onInsertTemplate`；
- 开启时选择模板才调用 `onSendTemplate`；
- 关闭开关会恢复插入模式。

发送模式不是后端配置，不进入 YAML，也不改变 API wire format。它由 text/vision 页面级 state 持有，并以独立的 `llm-message-template-immediate-send` localStorage key 保存 JSON boolean；缓存缺失、非法或读取异常时安全回落为关闭。`MessageTemplateMenu` 通过受控 props 读写该状态，因此 Vision 的 `ImageUploader` 子树重挂载、刷新页面和两页面切换均不会让有效缓存复位。

### 6.3 模板列表项

每个模板项以一行呈现，包含三个独立控制：

1. 模板主按钮：
   - 显示模板文本的单行截断标签；
   - `aria-label` 使用全文；
   - 不因显示截断而修改真正模板内容。
2. 编辑按钮：
   - 进入当前行编辑状态；
   - 显示 textarea、保存和取消控制；
   - 编辑控制的点击事件不得冒泡为模板选择。
3. 删除按钮：
   - 删除当前项前要求用户确认；
   - 确认后更新列表并写入缓存；
   - 删除控制的点击事件不得触发模板选择。

模板列表为空时显示空状态，并保留“新增”入口。

### 6.4 新增与编辑

“新增”按钮在列表末尾打开一个空的 textarea 编辑态：

- 保存按钮在内容为空或全是空白时禁用；
- 保存时保留非空正文的原始文本，包括换行；
- 新模板追加到列表末尾并写入缓存；
- 取消不改变列表和缓存；
- 同时只允许一个新增或编辑操作处于打开状态。

编辑保存使用相同的非空校验和原文保存规则。编辑已存在的 YAML 项会把整组列表写入浏览器缓存，因此之后仍不会影响 YAML 文件。

### 6.5 模板选择

#### 默认：填入输入框

点击模板主按钮后：

1. 用模板全文替换当前 input bar 内容；
2. 保留 vision 页当前已选图片，不清除附件；
3. 将 input bar focus 放回文本输入框；
4. 不发起网络请求。

“替换”是有意确定的语义，避免模板与用户已有草稿的拼接结果不明确。

#### 可选：立即发送

点击模板主按钮后：

1. 使用模板全文调用页面已有的发送回调；
2. text 页按当前 Chat/Agent 模式发送；
3. vision 页按现有 vision send pipeline 发送，并保留当前已选图片语义；
4. 不额外先发送一次 input bar 内容，不产生重复请求；
5. 当页面处于 loading、停止收尾、连接错误或其他既有禁用状态时，模板按钮遵循既有发送禁用规则，不绕过生命周期控制。

立即发送模式下模板仍是普通用户消息，不增加特殊 metadata，不绕过 stop、Answer Now、Agent session 或 Vision Automation 的现有约束。

### 6.6 长文本可访问名称

模板主按钮的可视标签使用单行截断，并以 `aria-label={template}` 保留完整可访问名称。模板 item 不渲染自定义 `role="tooltip"`，不使用 `aria-describedby`，也不设置原生全文 `title`。

不设一个会截断真实模板内容的业务字符上限；截断只发生在按钮视觉标签层。

## 7. 组件与文件边界

### 7.1 后端

- `backend/config.py`
  - 增加 WebUI 模板配置字段；
  - 解析并规范化 YAML 模板列表。
- `backend/main.py`
  - 在 `/config` 的 `webUI.components` 序列化结果中返回 `message_templates.items`。
- `config/config-webUI.yaml`
  - 增加默认模板示例与注释。

后端不接收浏览器模板保存请求，也不读取前端 localStorage。

### 7.2 前端共享层

- `frontend/src/lib/api.ts`
  - 扩展 `ConfigResponse.webUI` 的 TypeScript 类型。
- `frontend/src/lib/web-ui-config.ts`
  - 扩展 `WebUIConfig` 类型；
  - 增加 `llm-message-templates` key 常量；
  - 增加独立的立即发送 boolean key 及安全读写 helper；
  - 增加缓存读取、合法性判断、列表解析、写入和恢复 helper；
  - 所有 storage 操作捕获异常。
- `frontend/src/components/MessageTemplateMenu.tsx`
  - 提供消息模板入口按钮、模板面板、立即发送 ToggleSwitch、列表项、编辑/删除、新增和恢复默认 UI；
  - 通过 props 接收 effective templates、YAML defaults、`immediateSend`/`onImmediateSendChange`、input setter、focus 和发送回调；
  - 不直接依赖 text/vision 页面内部状态。

如单项编辑逻辑足够复杂，可以在该组件内拆分私有子组件，但不另建与页面模式绑定的实现。

### 7.3 页面 glue code

- `frontend/src/app/page.tsx`
  - 加载 YAML defaults 和 effective templates；
  - 持有并持久化受控的立即发送状态；
  - 在 text 功能栏挂载共享组件；
  - 提供 `setInput`、输入框 focus 和现有 `handleSend` 回调。
- `frontend/src/app/vision/page.tsx`
  - 使用同一缓存和共享组件；
  - 在 `ImageUploader` 重挂载边界之外持有并持久化立即发送状态；
  - 在 vision 功能栏挂载共享组件；
  - 提供 vision 现有发送回调，保持图片附件和 Agent/Chat 模式语义。

两页的状态命名、缓存读取顺序、立即发送禁用条件和恢复默认行为必须保持一致。

## 8. 错误处理与边界

1. `/config` 缺少 `webUI` 或 `message_templates` 时，使用空 YAML 默认列表，页面仍可使用新增功能。
2. YAML 模板部分非法时，后端过滤非法项；合法项仍按原顺序返回。
3. localStorage JSON 非法、类型错误或读取异常时，使用 YAML 列表，并保留原始缓存，不让页面崩溃。
4. localStorage 写入失败时，页面可更新当前 state，但必须展示或记录明确的“无法保存到浏览器缓存”错误；不能伪装为持久化成功。
5. 删除、编辑、恢复默认与发送操作必须互相隔离，编辑按钮和删除按钮不能触发模板主按钮。
6. 恢复默认必须清除缓存，而不是只把 React state 临时设置成 YAML 列表。
7. 立即发送不得绕过现有 `isLoading`、连接状态、请求生命周期和 Vision Automation admission/attachment 规则。
8. 模板正文可以包含换行和长文本；UI 标签可以截断，但完整 `aria-label`、编辑 textarea、input bar 和实际发送内容必须保留全文。
9. 模板功能没有任何内容写入 LLM 历史，只有用户实际选择并发送后才按普通用户消息进入会话历史。

## 9. 测试策略

### 9.1 后端单元测试

扩展 `tests/test_web_ui_config.py`：

1. 缺省 WebUI 配置得到空模板列表；
2. 正确解析 `message_templates.items` 并保持顺序和正文；
3. 忽略空白字符串和非字符串项；
4. 非法顶层模板配置回退为空列表；
5. `/config` 返回 `webUI.components.message_templates.items`。

### 9.2 前端 helper 测试

扩展 `frontend/src/lib/web-ui-config.test.mts` 或新增针对模板 helper 的测试：

1. key 不存在时使用 YAML defaults；
2. 合法非空缓存覆盖 YAML；
3. 合法空数组覆盖 YAML；
4. 非法 JSON、对象、混合非法数组回退 YAML；
5. localStorage 读写异常不会抛出未处理异常；
6. 新增、编辑、删除序列化为完整列表；
7. 恢复默认清除 key 并回到 YAML defaults；
8. 正文中的换行、长文本和空格按约定保留。

### 9.3 组件/页面验收

至少覆盖以下 UI 场景：

1. text 页点击 `+` 后可见“消息模板”；
2. vision 页点击 `+` 后可见“消息模板”；
3. “选中模板后立即发送”开关默认关闭；
4. 默认模式选择模板只替换 input，不请求后端；
5. 立即发送模式只产生一次既有发送请求；
6. 编辑、删除、新增均只改变浏览器缓存；
7. 删除/编辑按钮不会触发模板选择；
8. 长模板视觉文本保持截断，模板按钮 `aria-label` 保留全文，且不存在自定义或原生全文 tooltip；
9. 关闭 `+` 后面板关闭且未保存编辑不落盘；
10. vision 页已有图片时，默认选择不清除图片，立即发送遵循现有附件发送语义；
11. 恢复默认后当前 YAML 模板重新出现，浏览器自定义列表消失。
12. 立即发送缓存覆盖 true、false、缺失、非法和读写异常；写入使用独立 key，且 Vision 子树重挂载后仍由页面状态控制。

### 9.4 构建验证

实现完成后至少运行：

- 后端相关 pytest；
- 前端类型检查；
- 前端既有测试；
- 前端 production build 或项目规定的等价构建验证。

## 10. 验收标准

功能满足以下条件才视为完成：

1. `/` 和 `/vision` 的“+”功能栏均可打开消息模板；
2. 默认选择模式为“填入输入框”，模板全文可编辑后手动发送；
3. 单一 ToggleSwitch 可以切换立即发送，且立即发送不重复、不绕过既有发送生命周期；
4. 每个条目均有编辑和删除控制，并且新增、编辑、删除只落在浏览器缓存；
5. “恢复默认”清除缓存并恢复当前 YAML 配置；
6. 没有缓存时使用 YAML，有合法缓存时（包括空数组）完整覆盖 YAML；
7. 非法缓存不会破坏页面，并回退到 YAML；
8. 长文本按钮可 focus，视觉文本保持截断，辅助技术可从完整 `aria-label` 获取全文；
9. text/vision 两页共享同一缓存和行为；
10. 模板内容不改变任何 LLM prompt 或 Prefix-Cache 输入形状，只有正常发送后才作为普通用户消息出现。

## 11. 后续可扩展项

以下内容不属于本次实现：

- 模板拖拽排序；
- 模板标题与正文分离；
- 按 Agent/Chat/Vision 模式分组；
- 跨设备同步；
- 在设置面板中编辑模板；

这些扩展应在不破坏当前 `items` 列表兼容性和缓存优先级的前提下单独设计。
