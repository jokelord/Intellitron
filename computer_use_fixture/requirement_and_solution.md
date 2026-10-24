# 用户需求
开发一个 Ubuntu24 的 GUI 程序。其为一个射箭靶，当鼠标停留在靶子上进行左键单击、左键双击、右键单击、右键双击、滚轮向上滚1单位、滚轮向下滚1单位时，要显示以上文本信息（含不同颜色及形状的 legend），并且还要在靶子的相对应位置（鼠标操作时 其停留的位置）上显示对应的 legend 。

# 技术方案
# Ubuntu 24.04 射箭靶 GUI 技术方案 v0.2

## 1. 技术栈

采用 **C++20 + Qt 6 Widgets + QPainter + CMake + Qt Test**。

推荐环境：

* OS：Ubuntu 24.04 LTS
* 编译器：GCC，启用 C++20
* Qt：Qt 6.4+
* GUI：`Qt6::Widgets`
* 测试：`Qt6::Test`
* 构建：CMake 3.22+
* 推荐生成器：Ninja

Ubuntu 基础依赖：

```bash
sudo apt install build-essential cmake ninja-build qt6-base-dev qt6-base-dev-tools
```

CMake 核心配置：

```cmake
find_package(Qt6 6.4 REQUIRED COMPONENTS Widgets Test)

add_executable(archery-input-target ...)
target_compile_features(archery-input-target PRIVATE cxx_std_20)
target_link_libraries(archery-input-target PRIVATE Qt6::Widgets)
```

不使用 QML、OpenGL、SDL 或游戏引擎。该需求属于典型的 QWidget 二维矢量绘制和桌面鼠标事件处理，Qt Widgets 已足够。

---

## 2. UI 布局

靶面与固定 Legend **必须分成两个 Widget**，避免 Legend 改变靶面实际几何尺寸。

```text
┌────────────────────────────────────────────────────────────┐
│ Archery Input Target                                       │
├─────────────────────────────────────┬──────────────────────┤
│                                     │ 操作说明             │
│                                     │                      │
│                                     │ ● 左键单击           │
│              TargetCanvas           │ ▲ 左键双击           │
│                                     │ ■ 右键单击           │
│                ◎                    │ ◆ 右键双击           │
│                                     │ ↑ 滚轮向上           │
│                                     │ ↓ 滚轮向下           │
│                                     │                      │
│                                     │ 最近操作：左键单击   │
│                                     │ [ 清空标记 ]         │
└─────────────────────────────────────┴──────────────────────┘
```

推荐：

```text
MainWindow
└── CentralWidget
    └── QHBoxLayout
        ├── TargetCanvas
        └── LegendPanel
```

`TargetCanvas` 只负责靶子，不包含固定 Legend。

---

## 3. 六种操作的视觉定义

每种输入使用独立的颜色和几何形状：

| 操作        | 形状        | 颜色     |
| --------- | --------- | ------ |
| 左键单击      | Circle    | Blue   |
| 左键双击      | Triangle  | Green  |
| 右键单击      | Square    | Purple |
| 右键双击      | Diamond   | Orange |
| 滚轮向上 1 单位 | UpArrow   | Cyan   |
| 滚轮向下 1 单位 | DownArrow | Red    |

所有形状使用 `QPainterPath` / Qt 几何 API 绘制。

**不依赖 `● ▲ ◆ ↑ ↓` Unicode 字符本身作为图形。**

这些字符可以出现在文档说明里，但实际 GUI 图形直接绘制，以避免 Ubuntu 字体差异。

---

## 4. 固定 Legend 与靶面 Marker 的定义

两者职责明确区分。

### 固定 Legend

右侧 `LegendPanel` 永久显示：

```text
图形 + 颜色 + 操作名称
```

例如：

```text
●  左键单击
```

同时显示：

```text
最近操作：左键单击
```

只有真正确认完成的操作才更新“最近操作”。

### Target Marker

靶子上的 Marker **只显示颜色 + 几何形状，不显示文字**。

原因是操作文字直接画在靶子上会遮挡靶环，并且大量操作后无法阅读。

Marker 默认永久保留，直到：

```text
用户点击“清空标记”
```

或程序退出。

---

## 5. 核心数据模型

统一定义输入类型：

```cpp
enum class InputAction
{
    LeftClick,
    LeftDoubleClick,
    RightClick,
    RightDoubleClick,
    WheelUp,
    WheelDown
};
```

Marker：

```cpp
struct Marker
{
    InputAction action;
    QPointF normalizedPosition;
    std::uint64_t sequence;
};
```

这里不保存 Widget 像素坐标，而保存靶面归一化坐标。

定义：

```text
nx = (x - center.x) / radius
ny = (y - center.y) / radius
```

因此：

```text
(0, 0)   = 靶心
(1, 0)   = 靶子最右边界
(0, -1)  = 靶子最上边界
```

窗口 resize 后 Marker 仍然位于正确的靶面相对位置。

---

## 6. TargetGeometry：整个设计中的唯一几何真源

新增核心类：

```cpp
class TargetGeometry
{
public:
    explicit TargetGeometry(const QRectF& canvasRect);

    QPointF center() const;
    qreal radius() const;

    bool contains(const QPointF& p) const;

    QPointF toNormalized(const QPointF& p) const;
    QPointF fromNormalized(const QPointF& p) const;
};
```

它必须成为以下功能的**唯一几何来源**：

```text
靶子绘制
命中检测
坐标归一化
Marker 反向定位
```

禁止在：

```cpp
paintEvent()
mousePressEvent()
wheelEvent()
```

等不同地方各写一套：

```cpp
center = ...
radius = ...
```

否则 resize 后很容易产生绘制与命中区域不一致。

---

## 7. 靶子命中规则

明确采用以下规则。

外圆定义：

```text
distance(mouse, targetCenter) <= targetRadius
```

因此：

**靶子最外圈边界算命中。**

鼠标点击必须满足：

```text
Press 在靶内
+
Release 在靶内
+
Press→Release 移动距离没有超过 QApplication::startDragDistance()
```

才认为这是一次合法 click。

因此：

```text
靶内按下 → 靶外释放    不产生 Marker
靶外按下 → 靶内释放    不产生 Marker
靶内按下 → 大幅拖动    不产生 Marker
```

这与“鼠标停留在靶子上进行点击”的需求一致。

滚轮则使用：

```cpp
TargetGeometry::contains(event->position())
```

只根据滚轮事件发生瞬间的位置判断。

靶外滚轮不产生 Marker。

---

## 8. 单击 / 双击识别策略

这里不自行重新实现完整的桌面双击算法。

**双击时间和两次点击之间的距离判定交给 Qt/桌面系统。**

即使用：

```cpp
mouseDoubleClickEvent(QMouseEvent*)
```

对应的 `QEvent::MouseButtonDblClick`。

这样自动遵循 Ubuntu 桌面环境中配置的：

```text
double-click interval
double-click distance
```

不会只通过时间把两个距离很远的点击错误识别成双击。

---

## 9. 为什么单击仍然需要 Timer

第一次合法点击结束时，暂时不能立即发出：

```text
LeftClick
```

因为它可能是双击的第一次点击。

因此：

```text
第一次合法 Release
        │
        ▼
进入 PendingSingle
        │
        ├── QApplication::doubleClickInterval() 到期
        │        ↓
        │     SingleClick
        │
        └── Qt MouseButtonDblClick 到达
                 ↓
           取消 PendingSingle
                 ↓
           等待第二次 Release
                 ↓
            DoubleClick
```

**单击确认前不绘制临时 Marker。**

这样不会出现：

```text
先出现单击 Marker
随后又变成双击 Marker
```

造成闪烁或数据回滚。

---

## 10. 双击的严格语义

一次双击必须满足：

```text
第一次点击合法
+
Qt 判定第二次点击属于 DoubleClick
+
第二次 Press 在靶内
+
第二次 Release 在靶内
+
第二次点击没有发生拖动
```

Marker 放置位置：

```text
第二次 Release 的位置
```

因此双击对应的是完成双击时鼠标所在位置。

---

## 11. 左键、右键状态完全独立

Recognizer 不使用一个共享状态。

内部至少存在：

```cpp
ButtonState leftState;
ButtonState rightState;
```

每个状态各自管理：

```text
Press 状态
PendingSingle
DoubleCandidate
Timer
Position
```

因此类似：

```text
左键单击等待期间 → 用户右键双击
```

不会发生状态串扰。

---

## 12. Triple Click 规则

明确规定：

```text
三连击 = 一次 DoubleClick + 一次 SingleClick
```

也就是前两个点击构成 Double，第三个点击重新开启一个新的单击周期。

这样行为可预测，也容易自动化测试。

---

## 13. InputGestureRecognizer 生命周期

定义：

```cpp
class InputGestureRecognizer : public QObject
```

并让：

```cpp
TargetCanvas
```

成为它的 QObject parent：

```cpp
recognizer_ = new InputGestureRecognizer(this);
```

所有 `QTimer` 同样有 QObject parent。

因此 `TargetCanvas` 销毁时：

```text
Recognizer
Timer
Pending callback
```

都会随 QObject 生命周期安全销毁。

另外提供：

```cpp
void reset();
```

在以下场景调用：

```text
TargetCanvas hide
窗口禁用
输入状态被主动取消
```

`reset()` 清除左右键 pending timer 和输入状态。

---

## 14. 滚轮“一单位”的正式定义

定义：

```text
1 wheel unit = angleDelta().y() 的 120
```

维护一个带符号累积值：

```cpp
int wheelRemainderY = 0;
```

收到：

```cpp
event->angleDelta().y()
```

后累积。

例如：

```text
+120 → 一个 WheelUp
+240 → 两个 WheelUp
-240 → 两个 WheelDown
+30 +30 +30 +30 → 一个 WheelUp
```

不足 120 的部分继续保留：

```text
+150
→ 产生一个 WheelUp
→ remainder = +30
```

每产生一个单位，就生成一个 Marker。

如果一次事件为：

```text
+240
```

则逻辑上产生两个 Marker，位置均为当前鼠标位置。

---

## 15. pixelDelta 的处理边界

本程序定义的是**鼠标滚轮单位**，因此正式输入源采用：

```cpp
angleDelta()
```

高精度鼠标即使单次报告：

```text
15
30
60
```

也通过累积解决。

对于只产生：

```cpp
pixelDelta()
```

而完全没有 `angleDelta()` 的纯触控板像素滚动，v1.0 不转换成“滚轮 1 单位”。

原因是 pixelDelta 没有跨设备统一的“120 等价距离”，强行换算会导致不同触控板行为不一致。

---

## 16. Wheel 累积状态规则

只有鼠标位于靶内的滚轮输入参与累积。

鼠标滚轮发生在靶外时：

```text
不产生 Marker
不携带该 delta 进入下一次靶内滚动
```

鼠标离开 `TargetCanvas` 时清空未完成的 wheel remainder。

因此不会出现：

```text
靶外滚了 90
进入靶子再滚 30
突然产生 Marker
```

这种不符合用户直觉的情况。

---

## 17. 右键行为

`TargetCanvas` 的右键完全用于本程序输入。

禁用默认 Context Menu：

```cpp
setContextMenuPolicy(Qt::NoContextMenu);
```

并对已经处理的：

```text
mouse
double click
wheel
```

事件执行：

```cpp
event->accept();
```

防止事件继续向父控件传播。

---

## 18. Marker 绘制规则

Marker 使用固定的 Qt logical pixel 尺寸，例如：

```text
主图形：16 logical px
外部 halo：2 logical px
内部 stroke：1 logical px
```

为了保证它在：

```text
白色
黑色
蓝色
红色
黄色
```

靶环上都清晰可见，推荐绘制：

```text
外层白色 halo
+
深色细描边
+
操作对应填充色
```

颜色负责第一层区分，形状负责第二层区分，因此即使存在色觉差异仍可识别。

Qt 在 High-DPI 环境下使用 logical coordinate，因此不手工乘：

```cpp
devicePixelRatio()
```

避免重复缩放。

---

## 19. LegendStyle

所有视觉规则统一集中管理：

```cpp
enum class MarkerShape
{
    Circle,
    Triangle,
    Square,
    Diamond,
    UpArrow,
    DownArrow
};

struct LegendStyle
{
    QString text;
    QColor color;
    MarkerShape shape;
};
```

提供：

```cpp
const LegendStyle& legendStyle(InputAction action);
```

以下模块全部使用同一份配置：

```text
LegendPanel
Target Marker
最近操作显示
```

禁止分别 hard-code。

---

## 20. Marker 生命周期

采用：

```cpp
std::deque<Marker>
```

默认最大：

```text
5000 个 Marker
```

超过以后自动删除最旧 Marker，防止程序长期运行造成无界增长。

绘制顺序：

```text
旧 Marker → 新 Marker
```

因此最新 Marker 永远位于最上层。

提供：

```text
清空标记
```

按钮。

Resize、窗口遮挡、最小化再恢复时 Marker 数据均保持。

程序退出后不持久化，下一次启动为空。

---

## 21. paintEvent 原则

所有显示都通过数据重绘：

```cpp
void TargetCanvas::paintEvent(QPaintEvent*)
{
    QPainter painter(this);
    painter.setRenderHint(QPainter::Antialiasing);

    const TargetGeometry geometry(rect());

    drawTarget(painter, geometry);
    drawMarkers(painter, geometry);
}
```

禁止在：

```cpp
mouseReleaseEvent()
wheelEvent()
```

中直接使用 QPainter 留下图形。

事件处理流程只能：

```text
更新数据
→ update()
→ paintEvent()
```

---

## 22. 类职责

推荐最终结构：

```text
src/
├── main.cpp
│
├── ui/
│   ├── MainWindow.h
│   ├── MainWindow.cpp
│   ├── TargetCanvas.h
│   ├── TargetCanvas.cpp
│   ├── LegendPanel.h
│   └── LegendPanel.cpp
│
├── input/
│   ├── InputAction.h
│   ├── InputGestureRecognizer.h
│   └── InputGestureRecognizer.cpp
│
├── model/
│   ├── Marker.h
│   ├── LegendStyle.h
│   └── LegendStyle.cpp
│
└── geometry/
    ├── TargetGeometry.h
    └── TargetGeometry.cpp
```

关系：

```text
MainWindow
│
├── TargetCanvas
│   ├── TargetGeometry
│   ├── InputGestureRecognizer
│   └── Marker collection
│
└── LegendPanel
        │
        └── LegendStyle

InputGestureRecognizer
        │
        └── emits actionConfirmed(action, position)

TargetCanvas
        │
        ├── position → normalized position
        ├── append Marker
        └── update()
```

---

## 23. 推荐 Qt Signal

Recognizer 对外只暴露一个核心信号：

```cpp
signals:
    void actionConfirmed(
        InputAction action,
        QPointF widgetPosition);
```

Recognizer 不负责绘制、不保存 Marker、不知道靶子的颜色。

这样输入识别逻辑可以单独测试。

---

## 24. TargetCanvas 的职责

它负责：

```text
接收 Qt 输入事件
把事件交给 Recognizer
提供 TargetGeometry 命中信息
接收 actionConfirmed
转换成 normalized position
保存 Marker
触发 repaint
```

输入层和绘图层不直接耦合。

---

## 25. 自动化测试

直接使用 Qt 官方：

```text
Qt Test
QSignalSpy
QTest
```

避免自己造 GUI 测试框架。

重点给：

```text
TargetGeometry
InputGestureRecognizer
wheel accumulation
```

做自动化测试。

几何逻辑属于纯逻辑类，测试：

```text
中心点命中
边界命中
边界外 0.1px 不命中
normalize / denormalize round-trip
resize 后坐标保持
```

Recognizer 测试：

```text
SingleClick
DoubleClick
TripleClick
左/右状态隔离
Press inside / release outside
拖动后释放
pending timer reset
```

---

## 26. 最终验收矩阵

### A. 六种基本操作

六种操作分别产生：

```text
不同文字
不同颜色
不同几何形状
```

靶面 Marker 与固定 Legend 完全一致。

### B. 靶外输入

点击或滚轮发生在靶外：

```text
不增加 Marker
不更新最近操作
```

### C. 靶边界

鼠标中心位于最外圆边界：

```text
算命中
```

边界之外：

```text
不命中
```

### D. 单击

一次左/右单击最终只产生：

```text
一个 SingleClick Marker
```

允许存在约一个系统 double-click interval 的确认延迟。

### E. 双击

一次双击最终只产生：

```text
一个 DoubleClick Marker
```

不得额外留下第一次单击 Marker。

### F. Triple Click

三连击最终得到：

```text
DoubleClick
+
SingleClick
```

共两个操作。

### G. Click 与 Drag

```text
Press inside
Release outside
```

或者移动距离超过：

```cpp
QApplication::startDragDistance()
```

不认为是 click。

### H. 右键

右键单击和右键双击正常识别：

```text
不得弹出 Context Menu
```

### I. Wheel 单位

```text
+120 → 1 × WheelUp
+240 → 2 × WheelUp
-120 → 1 × WheelDown
-240 → 2 × WheelDown
```

### J. 高精度滚轮

例如：

```text
+30 +30 +30 +30
```

最终产生：

```text
1 × WheelUp
```

### K. Resize

改变窗口尺寸后：

```text
靶子同比例变化
Marker 保持相对于靶子的位置
Legend 不遮挡靶面
```

### L. 窗口重绘

经过：

```text
遮挡
最小化
恢复
resize
```

Marker 不丢失、不偏移。

### M. High DPI

125%、150%、200% 缩放下：

```text
靶子
命中位置
Marker
鼠标输入坐标
```

保持一致。

### N. Marker 顺序

重叠 Marker：

```text
最新操作显示在最上层
```

### O. Ubuntu 构建

在干净 Ubuntu 24.04 环境执行：

```bash
cmake -S . -B build -G Ninja
cmake --build build
./build/archery-input-target
```

可以正常启动。

---

## 27. 最终架构

最终建议收敛成：

```text
                   Qt Mouse / Wheel Events
                            │
                            ▼
                 InputGestureRecognizer
                 │          │
          Button FSM     Wheel accumulator
                 │          │
                 └────┬─────┘
                      ▼
              actionConfirmed()
                      │
                      ▼
                 TargetCanvas
                 │          │
          TargetGeometry   Marker[]
                 │          │
                 └────┬─────┘
                      ▼
                   QPainter

LegendPanel ───────────► LegendStyle ◄────────── TargetCanvas
```

其中有三个关键原则：

1. `TargetGeometry` 是所有靶面几何计算的唯一真源。
2. `InputGestureRecognizer` 只判断“发生了什么输入”，不参与绘图。
3. `LegendStyle` 是文字、颜色、形状对应关系的唯一真源。

这三点可以避免这个项目后续最容易出现的三类问题：**坐标漂移、单双击状态混乱、Legend 显示不一致。**

