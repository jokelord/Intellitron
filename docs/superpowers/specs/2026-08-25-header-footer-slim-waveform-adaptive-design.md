# Header/Footer 瘦身 + 声波图宽度自适应 设计

日期: 2026-08-25
状态: 已获用户批准（对话内确认）

## 目标

1. text 与 vision 两个页面的页眉 (header) 和页脚 (input bar/footer) 高度压到约现状的 80%。
2. 页眉/页脚内的元素（按钮、图标、输入框等）尺寸保持不变，只通过缩减 padding/间距实现瘦身。
3. 听写模式与语音模式的声波图 (sound level waveform) 宽度自适应可用空间，并保留一个合理的最小宽度（≈当前显示宽度）。

## 变更点

### 1. 页眉（`frontend/src/app/page.tsx` 与 `frontend/src/app/vision/page.tsx`）

- `<header>` 的 `py-3` → `py-1.5`：上下 padding 各 12px → 6px。
- 内容高度不变：页眉内容最高元素为 VoiceModeToggle 按钮 `min-h-[44px]`，故总高度 ≈68px → ≈56px（约 82%，受按钮最小高度约束无法精确到 80%）。
- 不改任何按钮/标题/图标尺寸与横向布局。

### 2. 页脚（同上两文件 + `frontend/src/app/globals.css`）

- footer 元素 className 的 `pt-4` → `pt-2`：顶部 padding 16px → 8px。
- `.chat-input-footer` 底部 padding：
  `max(1rem, calc(1rem + env(safe-area-inset-bottom)))` → `max(0.5rem, calc(0.5rem + env(safe-area-inset-bottom)))`
  （桌面基线 16px → 8px；safe-area 保护逻辑保留）。
- 内部容器 `space-y-3` → `space-y-2`：展开输入操作/图片预览等堆叠区块间距 12px → 8px。
- 基线（键盘模式、无展开区块）：80px → 64px = 恰好 80%。
- textarea（`--webui-input-min-height: 48px`）与 action button（48px CSS var 驱动）尺寸均不动。

### 3. 声波图宽度自适应（`frontend/src/components/VoiceListeningIndicator.tsx`）

该组件同时被听写模式（footer 内直接渲染）和语音模式（`VoiceInputPanel` 内渲染）、text/vision 两页共用，改一处全覆盖。

- 外层图表容器 div：`shrink-0` → `min-w-[130px] flex-1`。130px ≈ 当前容器整体宽度（svg 112px + px-2 + border），作为最小宽度；`flex-1` 使其占据剩余空间自适应拉伸；父行 `flex flex-wrap gap-3` 保证窄屏时换行且不小于最小宽。
- SVG：`h-10 w-28` → `h-10 w-full`。`viewBox="0 0 256 64"`、折线数学、`preserveAspectRatio="none"` 全部保持不变。
- 不设最大宽度上限（用户确认）。

### 4. 测试同步

- `frontend/src/lib/voice-input-pages.test.mts:131` 锁定 `className="h-10 w-28"`，更新为新类名断言（`h-10 w-full`），并可补一条锁定容器 `min-w-[130px] flex-1` 的断言，防止回归为固定宽。
- `chat-input-alignment.test.mjs` 锁定的字符串（`flex items-end space-x-3`、textarea 类前缀、action-button 无 px/py 工具类）均不受本次改动影响。

## 明确不做

- 不把页眉/页脚 padding 配置化进 yaml/web 设置（现有代码即为硬编码 Tailwind 类，遵循现状；未来如需可另立 spec）。
- 不改 header/footer 的横向 padding (`px-4`)、内部水平 gap、任何元素尺寸。
- 不改 GlassJellySurface / skin 相关逻辑。

## 验证

- `cd frontend && npx tsc --noEmit`（或项目既有 typecheck/lint 脚本）。
- 运行前端既有 node:test 套件中与 voice/pages/alignment 相关的测试。
- 手动核对两页 header/footer 视觉高度与声波图在窄屏/宽屏下的表现。
