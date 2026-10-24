---
name: playwright-browser-skill
description: 通过真实浏览器打开网页、读取页面、点击输入、管理标签页或保存截图；需要浏览器交互或登录态时使用。
---

# Playwright 浏览器技能

## 适用场景

当任务需要真实浏览器页面或登录态时，通过 `exec_command` 调用本技能入口。

## 执行入口

```bash
{{SKILL_DIR}}/scripts/playwright-browser --session-id {{SESSION_ID}} open "https://example.com"
```

除下文的 `--probe-runtime` 诊断外，所有调用都必须带当前会话的 `{{SESSION_ID}}`；不要复制或猜测其他 session ID。

同一会话的后续调用会继续使用当前标签页和 Cookie。任务完成后调用 `close-all` 释放该会话的浏览器上下文。

## 推荐工作流

每次页面发生变化后都先观察返回的快照，再决定下一步；一次模型回复中不要并发多个浏览器操作：

```bash
{{SKILL_DIR}}/scripts/playwright-browser --session-id {{SESSION_ID}} open "https://example.com"
{{SKILL_DIR}}/scripts/playwright-browser --session-id {{SESSION_ID}} click e3
{{SKILL_DIR}}/scripts/playwright-browser --session-id {{SESSION_ID}} fill e4 "搜索文字"
{{SKILL_DIR}}/scripts/playwright-browser --session-id {{SESSION_ID}} press Enter
```

`press` 可能触发表单提交或页面导航。使用成功返回的新快照决定下一步；遇到导航错误时按下文恢复，不要直接重复按键。

快照会返回 `Page URL`、标题、标签页和元素引用（例如 `e3`）。元素行会标注 `type`、`visible`、`enabled`、`editable` 和允许的 `actions`：

```text
- e4: textbox type=search visible=true enabled=true editable=true actions=fill,type label='Search'
- e5: button type=submit visible=true enabled=true editable=false actions=click label='Search'
```

`fill`/`type` 只能使用 `editable=true` 的 textbox，提交按钮必须使用 `click`；不要把 `input[type=submit]` 当作文本框。隐藏的普通控件不会进入快照，隐藏的 `input[type=file]` 会保留并标记为 `actions=upload`。点击、输入、导航或重新加载后旧引用可能失效；使用动作返回的新快照，没有可用新快照时再运行 `snapshot`，不要盲猜 selector。

## 支持的命令

- 页面：`open URL`、`goto URL`、`snapshot`、`reload`、`go-back`、`go-forward`
- 元素：`click REF`、`dblclick REF`、`fill REF TEXT`、`type REF TEXT`、`hover REF`、`select REF VALUE`、`check REF`、`uncheck REF`、`upload REF FILE`
- 键盘：`press KEY`、`press REF KEY`、`keydown KEY`、`keyup KEY`
- 标签页：`tab-list`、`tab-new [URL]`、`tab-select INDEX`、`tab-close`
- 输出：`screenshot [--filename=PATH]`、`pdf [--filename=PATH]`
- 页面检查：`eval JAVASCRIPT`（只用于读取或操作当前网页 DOM，不执行操作系统命令）
- 状态：`state-save PATH`、`cookie-list`、`cookie-get NAME`
- 释放：`close`（当前标签页）、`close-all`（当前 session 的浏览器上下文）

不在上述列表中的命令不要自行猜测；报告当前入口不支持该能力。

`open`/`goto` 只接受 `http(s)` URL 或 `about:blank`。默认使用有界面的 Chromium；若不需要显示窗口，在启动该会话浏览器时加入 `--headless`（放在 `--session-id` 后、动作前）。

只检查运行环境而不启动浏览器 context 时，可直接运行
`{{SKILL_DIR}}/scripts/playwright-browser --probe-runtime`（不需要 session ID）；检查其中的运行环境信息和 `chromium=... exists=...` 结果。

## 标签页和登录态

- 如果点击后快照显示 `Open tabs` 中出现新标签页而当前页仍是原页面，先 `tab-list`，再 `tab-select INDEX`；不要重复点击。
- 同一会话会复用 Cookie 和 localStorage。不要把 `state-save` 文件或 profile 路径当成另一个会话的输入。
- `upload`、`screenshot`、`pdf` 和 `state-save` 的文件必须位于 Agent 工作目录或 `/tmp` 下；需要分析用户本地图片时优先使用 `load-local-images` 技能。

## 安全和故障处理

- 不要套用 `bash -c`、重定向、管道、命令替换或后台 shell；直接调用入口及其参数。
- `fill`、`click` 等动作会在执行前检查目标的可见性、启用状态和元素类型；`element_not_editable`、`element_not_visible`、`element_missing_or_stale` 等错误都表示当前引用或动作不适用。不要为规避失败而反复重试同一个操作；先重新 `snapshot`，修正 URL、动作、引用或标签页索引后再尝试。
- 优先依据失败输出中的 `[code]`、`details` 和恢复提示判断下一步，不要只依据退出码猜测页面状态。
- 如果 `press` 最终返回 `browser_navigation_failed`，先等待页面稳定后执行 `snapshot` 确认状态；不要直接重复原来的 `press`，因为按键可能已经提交成功。
- 如果提示运行环境或图形会话不可用，保留错误信息并报告，不要安装全局包或猜测 `DISPLAY`/D-Bus 环境变量。
