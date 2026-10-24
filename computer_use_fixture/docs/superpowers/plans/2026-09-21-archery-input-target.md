# Archery Input Target Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an executable Ubuntu 24.04 archery-target GUI that recognizes six mouse/wheel actions and draws persistent, style-consistent markers at accepted target positions.

**Architecture:** Qt Widgets owns the UI and event loop. `InputGestureRecognizer` converts raw Qt events into semantic actions, `TargetCanvas` stores normalized markers and repaints from data, `TargetGeometry` owns all target geometry, and `LegendStyle` owns the action-to-visual mapping. `LegendPanel` and `TargetCanvas` remain separate widgets.

**Tech Stack:** C++20, Qt 6.4+, Qt Widgets, QPainter, CMake 3.22+, Ninja, Qt Test.

## Global Constraints

- Work only under `computer_use_fixture/`.
- Use C++20 and Qt 6 Widgets; do not use QML, OpenGL, SDL, or a game engine.
- Keep `TargetCanvas` and `LegendPanel` as separate widgets.
- Use `TargetGeometry` as the sole source of target geometry.
- Use `LegendStyle` as the sole source of action text, color, and shape.
- Store markers as normalized coordinates and retain at most 5000 markers.
- Use Qt's native double-click event and `QApplication::doubleClickInterval()` for single-click confirmation.
- Use `angleDelta().y()` with 120 units per wheel action; do not convert `pixelDelta()`.
- Do not commit changes unless the user explicitly requests a commit.

## File Map

- `CMakeLists.txt`: application and test build configuration.
- `src/main.cpp`: application entry point.
- `src/model/InputAction.h`: semantic input enum.
- `src/model/Marker.h`: persistent marker value type.
- `src/model/LegendStyle.h` and `.cpp`: action/style mapping and shape painter.
- `src/geometry/TargetGeometry.h` and `.cpp`: target geometry and normalized coordinates.
- `src/input/InputGestureRecognizer.h` and `.cpp`: button state machines and wheel accumulator.
- `src/ui/TargetCanvas.h` and `.cpp`: event intake, marker storage, and QPainter rendering.
- `src/ui/LegendPanel.h` and `.cpp`: fixed legend and latest-action panel.
- `src/ui/MainWindow.h` and `.cpp`: application composition.
- `tests/CMakeLists.txt`: Qt Test targets.
- `tests/test_target_geometry.cpp`: geometry tests.
- `tests/test_legend_style.cpp`: style tests.
- `tests/test_input_gestures.cpp`: recognizer and wheel tests.
- `tests/test_target_canvas.cpp`: canvas integration tests.
- `tests/test_ui_components.cpp`: legend, main-window, and clear-control tests.
- `docs/superpowers/specs/2026-09-21-archery-input-target-design.md`: approved design record.

---

### Task 1: Build scaffold, model types, and target geometry

**Files:**
- Create: `CMakeLists.txt`
- Create: `src/model/InputAction.h`
- Create: `src/model/Marker.h`
- Create: `src/geometry/TargetGeometry.h`
- Create: `src/geometry/TargetGeometry.cpp`
- Create: `tests/CMakeLists.txt`
- Create: `tests/test_target_geometry.cpp`

**Interfaces:**
- `InputAction` is the semantic enum consumed by all later modules.
- `Marker` stores `InputAction`, `QPointF normalizedPosition`, and `std::uint64_t sequence`.
- `TargetGeometry` exposes `center()`, `radius()`, `contains()`, `toNormalized()`, and `fromNormalized()`.

- [ ] **Step 1: Write the failing geometry test**

Create `tests/test_target_geometry.cpp` with:

```cpp
#include <cstddef>
#include <QtTest>

#include "geometry/TargetGeometry.h"

class TargetGeometryTest : public QObject
{
    Q_OBJECT

private slots:
    void centerAndRadius();
    void containsBoundary();
    void normalizationRoundTrip();

private:
    static constexpr qreal Epsilon = 0.0001;
};

void TargetGeometryTest::centerAndRadius()
{
    const TargetGeometry geometry(QRectF(10, 20, 200, 120));
    QCOMPARE(geometry.center(), QPointF(110, 80));
    QCOMPARE(geometry.radius(), qreal(60));
}

void TargetGeometryTest::containsBoundary()
{
    const TargetGeometry geometry(QRectF(0, 0, 200, 200));
    QVERIFY(geometry.contains(QPointF(100, 100)));
    QVERIFY(geometry.contains(QPointF(200, 100)));
    QVERIFY(!geometry.contains(QPointF(200.1, 100)));
    QVERIFY(!TargetGeometry(QRectF()).contains(QPointF(0, 0)));
}

void TargetGeometryTest::normalizationRoundTrip()
{
    const TargetGeometry geometry(QRectF(0, 0, 200, 200));
    const QPointF normalized(0.25, -0.5);
    const QPointF denormalized = geometry.fromNormalized(normalized);
    const QPointF result = geometry.toNormalized(denormalized);
    QVERIFY(qAbs(result.x() - normalized.x()) < Epsilon);
    QVERIFY(qAbs(result.y() - normalized.y()) < Epsilon);
    QCOMPARE(geometry.toNormalized(QPointF(100, 0)), QPointF(0, -1));
}

QTEST_APPLESS_MAIN(TargetGeometryTest)
#include "test_target_geometry.moc"
```

- [ ] **Step 2: Add the initial CMake scaffold and verify the test fails**

Create `CMakeLists.txt`:

```cmake
cmake_minimum_required(VERSION 3.22)

project(archery_input_target VERSION 1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 6.4 REQUIRED COMPONENTS Widgets Test)

include_directories("${CMAKE_CURRENT_SOURCE_DIR}/src")

file(GLOB_RECURSE APP_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp"
)

if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/src/main.cpp")
    add_executable(archery-input-target ${APP_SOURCES})
    target_link_libraries(archery-input-target PRIVATE Qt6::Widgets)
endif()

enable_testing()
add_subdirectory(tests)
```

Create the initial `tests/CMakeLists.txt`:

```cmake
add_executable(test_target_geometry
    test_target_geometry.cpp
    ../src/geometry/TargetGeometry.cpp
)

target_link_libraries(test_target_geometry PRIVATE Qt6::Widgets Qt6::Test)
add_test(NAME target_geometry COMMAND test_target_geometry)
```

Create empty or returning-zero implementations in `src/geometry/TargetGeometry.h` and `.cpp`, then run:

```bash
cmake -S . -B build -G Ninja
cmake --build build --target test_target_geometry
QT_QPA_PLATFORM=offscreen ctest --test-dir build -R target_geometry --output-on-failure
```

Expected: the geometry assertions fail because the implementation is incomplete.

- [ ] **Step 3: Implement `TargetGeometry` minimally and completely**

`src/model/InputAction.h`:

```cpp
#pragma once

#include <QMetaType>

enum class InputAction
{
    LeftClick,
    LeftDoubleClick,
    RightClick,
    RightDoubleClick,
    WheelUp,
    WheelDown
};

Q_DECLARE_METATYPE(InputAction)
```

`src/model/Marker.h`:

```cpp
#pragma once

#include <cstdint>

#include <QPointF>

#include "model/InputAction.h"

struct Marker
{
    InputAction action;
    QPointF normalizedPosition;
    std::uint64_t sequence;
};
```

`src/geometry/TargetGeometry.h`:

```cpp
#pragma once

#include <QPointF>
#include <QRectF>

class TargetGeometry
{
public:
    explicit TargetGeometry(const QRectF& canvasRect);

    QPointF center() const;
    qreal radius() const;

    bool contains(const QPointF& point) const;

    QPointF toNormalized(const QPointF& point) const;
    QPointF fromNormalized(const QPointF& normalizedPoint) const;

private:
    QRectF canvasRect_;
};
```

`src/geometry/TargetGeometry.cpp`:

```cpp
#include "geometry/TargetGeometry.h"

#include <algorithm>

#include <QLineF>

TargetGeometry::TargetGeometry(const QRectF& canvasRect)
    : canvasRect_(canvasRect)
{
}

QPointF TargetGeometry::center() const
{
    return canvasRect_.center();
}

qreal TargetGeometry::radius() const
{
    return std::max<qreal>(0, std::min(canvasRect_.width(), canvasRect_.height()) / 2);
}

bool TargetGeometry::contains(const QPointF& point) const
{
    if (radius() <= 0) {
        return false;
    }
    return QLineF(center(), point).length() <= radius();
}

QPointF TargetGeometry::toNormalized(const QPointF& point) const
{
    if (radius() <= 0) {
        return {};
    }

    return QPointF(
        (point.x() - center().x()) / radius(),
        (point.y() - center().y()) / radius()
    );
}

QPointF TargetGeometry::fromNormalized(const QPointF& normalizedPoint) const
{
    if (radius() <= 0) {
        return center();
    }

    return QPointF(
        center().x() + normalizedPoint.x() * radius(),
        center().y() + normalizedPoint.y() * radius()
    );
}
```

- [ ] **Step 4: Run the geometry test to verify it passes**

```bash
cmake -S . -B build -G Ninja
cmake --build build --target test_target_geometry
QT_QPA_PLATFORM=offscreen ctest --test-dir build -R target_geometry --output-on-failure
```

Expected: `100% tests passed`.

---

### Task 2: Centralize action styles and shape drawing

**Files:**
- Create: `src/model/LegendStyle.h`
- Create: `src/model/LegendStyle.cpp`
- Create: `tests/test_legend_style.cpp`
- Modify: `tests/CMakeLists.txt`

**Interfaces:**
- `legendStyle(InputAction)` returns a stable `LegendStyle` reference.
- `drawMarkerShape(QPainter&, MarkerShape, const QRectF&)` is reused by the target and legend.
- Later UI modules consume only these APIs.

- [ ] **Step 1: Write the failing style test**

Create `tests/test_legend_style.cpp`:

```cpp
#include <cstddef>
#include <QtTest>

#include "model/LegendStyle.h"

class LegendStyleTest : public QObject
{
    Q_OBJECT

private slots:
    void allActionsHaveStableStyle();
};

void LegendStyleTest::allActionsHaveStableStyle()
{
    const auto leftClick = legendStyle(InputAction::LeftClick);
    const auto leftDouble = legendStyle(InputAction::LeftDoubleClick);
    const auto rightClick = legendStyle(InputAction::RightClick);
    const auto rightDouble = legendStyle(InputAction::RightDoubleClick);
    const auto wheelUp = legendStyle(InputAction::WheelUp);
    const auto wheelDown = legendStyle(InputAction::WheelDown);

    QCOMPARE(leftClick.text, QStringLiteral("左键单击"));
    QCOMPARE(leftClick.color, QColor(QStringLiteral("#2563EB")));
    QCOMPARE(leftClick.shape, MarkerShape::Circle);

    QCOMPARE(leftDouble.text, QStringLiteral("左键双击"));
    QCOMPARE(leftDouble.color, QColor(QStringLiteral("#16A34A")));
    QCOMPARE(leftDouble.shape, MarkerShape::Triangle);

    QCOMPARE(rightClick.text, QStringLiteral("右键单击"));
    QCOMPARE(rightClick.color, QColor(QStringLiteral("#7C3AED")));
    QCOMPARE(rightClick.shape, MarkerShape::Square);

    QCOMPARE(rightDouble.text, QStringLiteral("右键双击"));
    QCOMPARE(rightDouble.color, QColor(QStringLiteral("#EA580C")));
    QCOMPARE(rightDouble.shape, MarkerShape::Diamond);

    QCOMPARE(wheelUp.text, QStringLiteral("滚轮向上"));
    QCOMPARE(wheelUp.color, QColor(QStringLiteral("#0891B2")));
    QCOMPARE(wheelUp.shape, MarkerShape::UpArrow);

    QCOMPARE(wheelDown.text, QStringLiteral("滚轮向下"));
    QCOMPARE(wheelDown.color, QColor(QStringLiteral("#DC2626")));
    QCOMPARE(wheelDown.shape, MarkerShape::DownArrow);
}

QTEST_APPLESS_MAIN(LegendStyleTest)
#include "test_legend_style.moc"
```

- [ ] **Step 2: Add the test target and verify it fails**

Append to `tests/CMakeLists.txt`:

```cmake
add_executable(test_legend_style
    test_legend_style.cpp
    ../src/model/LegendStyle.cpp
)

target_link_libraries(test_legend_style PRIVATE Qt6::Widgets Qt6::Test)
add_test(NAME legend_style COMMAND test_legend_style)
```

Run:

```bash
cmake -S . -B build -G Ninja
cmake --build build --target test_legend_style
QT_QPA_PLATFORM=offscreen ctest --test-dir build -R legend_style --output-on-failure
```

Expected: the target cannot build until `LegendStyle` exists or the assertions fail against a stub.

- [ ] **Step 3: Implement the style mapping and painter**

`src/model/LegendStyle.h`:

```cpp
#pragma once

#include <QColor>
#include <QRectF>
#include <QString>

#include "model/InputAction.h"

class QPainter;

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

const LegendStyle& legendStyle(InputAction action);
void drawMarkerShape(QPainter& painter, MarkerShape shape, const QRectF& rect);
```

`src/model/LegendStyle.cpp`:

```cpp
#include "model/LegendStyle.h"

#include <array>
#include <cstddef>
#include <stdexcept>

#include <QPainter>
#include <QPainterPath>
#include <QPolygonF>

const LegendStyle& legendStyle(InputAction action)
{
    static const std::array<LegendStyle, 6> styles = {
        LegendStyle{QStringLiteral("左键单击"), QColor(QStringLiteral("#2563EB")), MarkerShape::Circle},
        LegendStyle{QStringLiteral("左键双击"), QColor(QStringLiteral("#16A34A")), MarkerShape::Triangle},
        LegendStyle{QStringLiteral("右键单击"), QColor(QStringLiteral("#7C3AED")), MarkerShape::Square},
        LegendStyle{QStringLiteral("右键双击"), QColor(QStringLiteral("#EA580C")), MarkerShape::Diamond},
        LegendStyle{QStringLiteral("滚轮向上"), QColor(QStringLiteral("#0891B2")), MarkerShape::UpArrow},
        LegendStyle{QStringLiteral("滚轮向下"), QColor(QStringLiteral("#DC2626")), MarkerShape::DownArrow}
    };

    const auto index = static_cast<std::size_t>(action);
    if (index >= styles.size()) {
        throw std::out_of_range("Invalid InputAction");
    }
    return styles[index];
}

void drawMarkerShape(QPainter& painter, MarkerShape shape, const QRectF& rect)
{
    const QPointF center = rect.center();
    const qreal left = rect.left();
    const qreal right = rect.right();
    const qreal top = rect.top();
    const qreal bottom = rect.bottom();

    switch (shape) {
    case MarkerShape::Circle:
        painter.drawEllipse(rect);
        break;
    case MarkerShape::Triangle:
        painter.drawPolygon(QPolygonF{
            QPointF(center.x(), top),
            QPointF(right, bottom),
            QPointF(left, bottom)
        });
        break;
    case MarkerShape::Square:
        painter.drawRect(rect);
        break;
    case MarkerShape::Diamond:
        painter.drawPolygon(QPolygonF{
            QPointF(center.x(), top),
            QPointF(right, center.y()),
            QPointF(center.x(), bottom),
            QPointF(left, center.y())
        });
        break;
    case MarkerShape::UpArrow: {
        QPainterPath path;
        path.moveTo(left, center.y());
        path.lineTo(left, top);
        path.lineTo(center.x(), top);
        path.lineTo(center.x(), top - rect.height() * 0.25);
        path.lineTo(right, top);
        path.lineTo(right, center.y());
        path.lineTo(center.x() + rect.width() * 0.25, center.y());
        path.lineTo(center.x() + rect.width() * 0.25, bottom);
        path.lineTo(center.x() - rect.width() * 0.25, bottom);
        path.lineTo(center.x() - rect.width() * 0.25, center.y());
        path.closeSubpath();
        painter.drawPolygon(path.toFillPolygon());
        break;
    }
    case MarkerShape::DownArrow: {
        QPainterPath path;
        path.moveTo(left, center.y());
        path.lineTo(left, bottom);
        path.lineTo(center.x(), bottom);
        path.lineTo(center.x(), bottom + rect.height() * 0.25);
        path.lineTo(right, bottom);
        path.lineTo(right, center.y());
        path.lineTo(center.x() + rect.width() * 0.25, center.y());
        path.lineTo(center.x() + rect.width() * 0.25, top);
        path.lineTo(center.x() - rect.width() * 0.25, top);
        path.lineTo(center.x() - rect.width() * 0.25, center.y());
        path.closeSubpath();
        painter.drawPolygon(path.toFillPolygon());
        break;
    }
    }
}
```

- [ ] **Step 4: Run the style test to verify it passes**

```bash
cmake -S . -B build -G Ninja
cmake --build build --target test_legend_style
QT_QPA_PLATFORM=offscreen ctest --test-dir build -R legend_style --output-on-failure
```

Expected: `100% tests passed`.

---

### Task 3: Implement the input recognizer and wheel accumulator

**Files:**
- Create: `src/input/InputGestureRecognizer.h`
- Create: `src/input/InputGestureRecognizer.cpp`
- Create: `tests/test_input_gestures.cpp`
- Modify: `tests/CMakeLists.txt`

**Interfaces:**
- `processMousePress(QMouseEvent*, const TargetGeometry&)`
- `processMouseRelease(QMouseEvent*, const TargetGeometry&)`
- `processMouseDoubleClick(QMouseEvent*, const TargetGeometry&)`
- `processWheel(QWheelEvent*, const TargetGeometry&)`
- `clearWheelRemainder()`
- `reset()`
- Emits `actionConfirmed(InputAction, QPointF)`.

- [ ] **Step 1: Write the failing recognizer tests**

Create `tests/test_input_gestures.cpp` with direct recognizer tests:

```cpp
#include <cstddef>
#include <QtTest>

#include <QMouseEvent>
#include <QWheelEvent>

#include "input/InputGestureRecognizer.h"
#include "geometry/TargetGeometry.h"

namespace {
QMouseEvent makeMouse(QEvent::Type type, const QPointF& position, Qt::MouseButton button)
{
    return QMouseEvent(type, position, button, Qt::NoButton, Qt::NoModifier);
}

QWheelEvent makeWheel(const QPointF& position, int deltaY)
{
    return QWheelEvent(
        position,
        position,
        QPoint(),
        QPoint(0, deltaY),
        Qt::NoButton,
        Qt::NoModifier,
        Qt::NoScrollPhase,
        false
    );
}

InputAction firstAction(QSignalSpy& spy)
{
    const auto arguments = spy.takeFirst();
    return qvariant_cast<InputAction>(arguments.at(0));
}
}

class InputGesturesTest : public QObject
{
    Q_OBJECT

private slots:
    void initTestCase();
    void singleClickWaitsForDoubleInterval();
    void doubleClickProducesOneMarker();
    void tripleClickProducesDoubleThenSingle();
    void leftAndRightStatesAreIndependent();
    void invalidReleaseAndDragDoNotConfirm();
    void resetCancelsPendingSingle();
    void wheelAccumulatesUnits();
    void outsideWheelDoesNotCarryDelta();

private:
    static constexpr qreal Epsilon = 0.0001;
};

void InputGesturesTest::initTestCase()
{
    qRegisterMetaType<InputAction>("InputAction");
}

void InputGesturesTest::singleClickWaitsForDoubleInterval()
{
    InputGestureRecognizer recognizer;
    QSignalSpy spy(&recognizer, &InputGestureRecognizer::actionConfirmed);
    const TargetGeometry geometry(QRectF(0, 0, 200, 200));
    const QPointF center(100, 100);

    auto press = makeMouse(QEvent::MouseButtonPress, center, Qt::LeftButton);
    auto release = makeMouse(QEvent::MouseButtonRelease, center, Qt::LeftButton);
    recognizer.processMousePress(&press, geometry);
    recognizer.processMouseRelease(&release, geometry);
    QCOMPARE(spy.count(), 0);

    QTest::qWait(QApplication::doubleClickInterval() + 100);
    QCOMPARE(spy.count(), 1);
    const auto arguments = spy.takeFirst();
    QCOMPARE(qvariant_cast<InputAction>(arguments.at(0)), InputAction::LeftClick);
    QCOMPARE(qvariant_cast<QPointF>(arguments.at(1)), center);
}

void InputGesturesTest::doubleClickProducesOneMarker()
{
    InputGestureRecognizer recognizer;
    QSignalSpy spy(&recognizer, &InputGestureRecognizer::actionConfirmed);
    const TargetGeometry geometry(QRectF(0, 0, 200, 200));
    const QPointF first(80, 80);
    const QPointF second(120, 120);

    auto p1 = makeMouse(QEvent::MouseButtonPress, first, Qt::LeftButton);
    auto r1 = makeMouse(QEvent::MouseButtonRelease, first, Qt::LeftButton);
    recognizer.processMousePress(&p1, geometry);
    recognizer.processMouseRelease(&r1, geometry);

    auto p2 = makeMouse(QEvent::MouseButtonPress, second, Qt::LeftButton);
    auto r2 = makeMouse(QEvent::MouseButtonRelease, second, Qt::LeftButton);
    recognizer.processMousePress(&p2, geometry);
    recognizer.processMouseRelease(&r2, geometry);

    auto dbl = makeMouse(QEvent::MouseButtonDblClick, second, Qt::LeftButton);
    recognizer.processMouseDoubleClick(&dbl, geometry);

    QCOMPARE(spy.count(), 1);
    QCOMPARE(firstAction(spy), InputAction::LeftDoubleClick);
}

void InputGesturesTest::tripleClickProducesDoubleThenSingle()
{
    InputGestureRecognizer recognizer;
    QSignalSpy spy(&recognizer, &InputGestureRecognizer::actionConfirmed);
    const TargetGeometry geometry(QRectF(0, 0, 200, 200));
    const QPointF first(70, 70);
    const QPointF second(80, 80);
    const QPointF third(90, 90);

    auto p1 = makeMouse(QEvent::MouseButtonPress, first, Qt::LeftButton);
    auto r1 = makeMouse(QEvent::MouseButtonRelease, first, Qt::LeftButton);
    recognizer.processMousePress(&p1, geometry);
    recognizer.processMouseRelease(&r1, geometry);
    auto p2 = makeMouse(QEvent::MouseButtonPress, second, Qt::LeftButton);
    auto r2 = makeMouse(QEvent::MouseButtonRelease, second, Qt::LeftButton);
    recognizer.processMousePress(&p2, geometry);
    recognizer.processMouseRelease(&r2, geometry);
    auto dbl = makeMouse(QEvent::MouseButtonDblClick, second, Qt::LeftButton);
    recognizer.processMouseDoubleClick(&dbl, geometry);

    auto p3 = makeMouse(QEvent::MouseButtonPress, third, Qt::LeftButton);
    auto r3 = makeMouse(QEvent::MouseButtonRelease, third, Qt::LeftButton);
    recognizer.processMousePress(&p3, geometry);
    recognizer.processMouseRelease(&r3, geometry);
    QTest::qWait(QApplication::doubleClickInterval() + 100);

    QCOMPARE(spy.count(), 2);
    QCOMPARE(firstAction(spy), InputAction::LeftDoubleClick);
    QCOMPARE(firstAction(spy), InputAction::LeftClick);
}

void InputGesturesTest::leftAndRightStatesAreIndependent()
{
    InputGestureRecognizer recognizer;
    QSignalSpy spy(&recognizer, &InputGestureRecognizer::actionConfirmed);
    const TargetGeometry geometry(QRectF(0, 0, 200, 200));
    const QPointF left(60, 60);
    const QPointF right(140, 140);

    auto lp = makeMouse(QEvent::MouseButtonPress, left, Qt::LeftButton);
    auto lr = makeMouse(QEvent::MouseButtonRelease, left, Qt::LeftButton);
    recognizer.processMousePress(&lp, geometry);
    recognizer.processMouseRelease(&lr, geometry);

    auto rp1 = makeMouse(QEvent::MouseButtonPress, right, Qt::RightButton);
    auto rr1 = makeMouse(QEvent::MouseButtonRelease, right, Qt::RightButton);
    recognizer.processMousePress(&rp1, geometry);
    recognizer.processMouseRelease(&rr1, geometry);
    auto rp2 = makeMouse(QEvent::MouseButtonPress, right, Qt::RightButton);
    auto rr2 = makeMouse(QEvent::MouseButtonRelease, right, Qt::RightButton);
    recognizer.processMousePress(&rp2, geometry);
    recognizer.processMouseRelease(&rr2, geometry);
    auto rdbl = makeMouse(QEvent::MouseButtonDblClick, right, Qt::RightButton);
    recognizer.processMouseDoubleClick(&rdbl, geometry);

    QTest::qWait(QApplication::doubleClickInterval() + 100);
    QCOMPARE(spy.count(), 2);
    QCOMPARE(firstAction(spy), InputAction::RightDoubleClick);
    QCOMPARE(firstAction(spy), InputAction::LeftClick);
}

void InputGesturesTest::invalidReleaseAndDragDoNotConfirm()
{
    InputGestureRecognizer recognizer;
    QSignalSpy spy(&recognizer, &InputGestureRecognizer::actionConfirmed);
    const TargetGeometry geometry(QRectF(0, 0, 200, 200));

    auto press = makeMouse(QEvent::MouseButtonPress, QPointF(100, 100), Qt::LeftButton);
    auto outside = makeMouse(QEvent::MouseButtonRelease, QPointF(250, 100), Qt::LeftButton);
    recognizer.processMousePress(&press, geometry);
    recognizer.processMouseRelease(&outside, geometry);

    auto dragPress = makeMouse(QEvent::MouseButtonPress, QPointF(100, 100), Qt::LeftButton);
    auto dragRelease = makeMouse(QEvent::MouseButtonRelease, QPointF(140, 100), Qt::LeftButton);
    recognizer.processMousePress(&dragPress, geometry);
    recognizer.processMouseRelease(&dragRelease, geometry);
    QTest::qWait(QApplication::doubleClickInterval() + 100);

    QCOMPARE(spy.count(), 0);
}

void InputGesturesTest::resetCancelsPendingSingle()
{
    InputGestureRecognizer recognizer;
    QSignalSpy spy(&recognizer, &InputGestureRecognizer::actionConfirmed);
    const TargetGeometry geometry(QRectF(0, 0, 200, 200));
    auto press = makeMouse(QEvent::MouseButtonPress, QPointF(100, 100), Qt::LeftButton);
    auto release = makeMouse(QEvent::MouseButtonRelease, QPointF(100, 100), Qt::LeftButton);
    recognizer.processMousePress(&press, geometry);
    recognizer.processMouseRelease(&release, geometry);
    recognizer.reset();
    QTest::qWait(QApplication::doubleClickInterval() + 100);
    QCOMPARE(spy.count(), 0);
}

void InputGesturesTest::wheelAccumulatesUnits()
{
    InputGestureRecognizer recognizer;
    QSignalSpy spy(&recognizer, &InputGestureRecognizer::actionConfirmed);
    const TargetGeometry geometry(QRectF(0, 0, 200, 200));
    const QPointF center(100, 100);

    auto up = makeWheel(center, 120);
    recognizer.processWheel(&up, geometry);
    auto down = makeWheel(center, -240);
    recognizer.processWheel(&down, geometry);

    QCOMPARE(spy.count(), 3);
    QCOMPARE(firstAction(spy), InputAction::WheelUp);
    QCOMPARE(firstAction(spy), InputAction::WheelDown);
    QCOMPARE(firstAction(spy), InputAction::WheelDown);

    for (int i = 0; i < 3; ++i) {
        auto partial = makeWheel(center, 30);
        recognizer.processWheel(&partial, geometry);
        QCOMPARE(spy.count(), 0);
    }
    auto finalPartial = makeWheel(center, 30);
    recognizer.processWheel(&finalPartial, geometry);
    QCOMPARE(spy.count(), 1);
    QCOMPARE(firstAction(spy), InputAction::WheelUp);
}

void InputGesturesTest::outsideWheelDoesNotCarryDelta()
{
    InputGestureRecognizer recognizer;
    QSignalSpy spy(&recognizer, &InputGestureRecognizer::actionConfirmed);
    const TargetGeometry geometry(QRectF(0, 0, 200, 200));

    auto outside = makeWheel(QPointF(250, 100), 120);
    recognizer.processWheel(&outside, geometry);
    auto partial = makeWheel(QPointF(100, 100), 30);
    recognizer.processWheel(&partial, geometry);
    QCOMPARE(spy.count(), 0);

    recognizer.clearWheelRemainder();
    auto inside = makeWheel(QPointF(100, 100), 120);
    recognizer.processWheel(&inside, geometry);
    QCOMPARE(spy.count(), 1);
}

QTEST_MAIN(InputGesturesTest)
#include "test_input_gestures.moc"
```

- [ ] **Step 2: Add the test target and verify it fails**

Append to `tests/CMakeLists.txt`:

```cmake
add_executable(test_input_gestures
    test_input_gestures.cpp
    ../src/input/InputGestureRecognizer.cpp
    ../src/geometry/TargetGeometry.cpp
)

target_link_libraries(test_input_gestures PRIVATE Qt6::Widgets Qt6::Test)
add_test(NAME input_gestures COMMAND test_input_gestures)
```

Run the target after adding temporary stubs for the recognizer. Expected: compile failure or failing signal assertions.

- [ ] **Step 3: Implement `InputGestureRecognizer`**

`src/input/InputGestureRecognizer.h`:

```cpp
#pragma once

#include <QObject>
#include <QPointF>
#include <Qt>

#include "model/InputAction.h"

class QMouseEvent;
class QTimer;
class QWheelEvent;
class TargetGeometry;

class InputGestureRecognizer : public QObject
{
    Q_OBJECT

public:
    explicit InputGestureRecognizer(QObject* parent = nullptr);

    void reset();
    void clearWheelRemainder();

    void processMousePress(QMouseEvent* event, const TargetGeometry& geometry);
    void processMouseRelease(QMouseEvent* event, const TargetGeometry& geometry);
    void processMouseDoubleClick(QMouseEvent* event, const TargetGeometry& geometry);
    void processWheel(QWheelEvent* event, const TargetGeometry& geometry);

signals:
    void actionConfirmed(InputAction action, QPointF widgetPosition);

private:
    enum class ButtonPhase
    {
        Idle,
        PressedInside,
        PendingSingle,
        DoubleCandidate,
        AwaitingDoubleClickEvent
    };

    struct ButtonState
    {
        ButtonPhase phase = ButtonPhase::Idle;
        QPointF pressPosition;
        QPointF pendingPosition;
        QTimer* singleTimer = nullptr;
        QTimer* doubleTimer = nullptr;
    };

    ButtonState& stateFor(Qt::MouseButton button);
    void handlePress(ButtonState& state, const QPointF& position, const TargetGeometry& geometry);
    void handleRelease(ButtonState& state, const QPointF& position, const TargetGeometry& geometry);
    void handleDoubleClick(ButtonState& state, InputAction action, const QPointF& position, const TargetGeometry& geometry);
    void startSingleWait(ButtonState& state);
    void startDoubleWait(ButtonState& state);
    void cancelWaits(ButtonState& state);
    void resetState(ButtonState& state);
    void confirmPendingSingle(InputAction action);
    void abandonDoubleCandidate(InputAction action);

    ButtonState leftState_;
    ButtonState rightState_;
    int wheelRemainderY_ = 0;
};
```

`src/input/InputGestureRecognizer.cpp`:

```cpp
#include "input/InputGestureRecognizer.h"

#include <QApplication>
#include <QLineF>
#include <QMouseEvent>
#include <QTimer>
#include <QWheelEvent>

#include "geometry/TargetGeometry.h"

InputGestureRecognizer::InputGestureRecognizer(QObject* parent)
    : QObject(parent)
{
    leftState_.singleTimer = new QTimer(this);
    leftState_.doubleTimer = new QTimer(this);
    rightState_.singleTimer = new QTimer(this);
    rightState_.doubleTimer = new QTimer(this);

    leftState_.singleTimer->setSingleShot(true);
    leftState_.doubleTimer->setSingleShot(true);
    rightState_.singleTimer->setSingleShot(true);
    rightState_.doubleTimer->setSingleShot(true);

    connect(leftState_.singleTimer, &QTimer::timeout, this, [this] {
        confirmPendingSingle(InputAction::LeftClick);
    });
    connect(rightState_.singleTimer, &QTimer::timeout, this, [this] {
        confirmPendingSingle(InputAction::RightClick);
    });
    connect(leftState_.doubleTimer, &QTimer::timeout, this, [this] {
        abandonDoubleCandidate(InputAction::LeftClick);
    });
    connect(rightState_.doubleTimer, &QTimer::timeout, this, [this] {
        abandonDoubleCandidate(InputAction::RightClick);
    });
}

InputGestureRecognizer::ButtonState& InputGestureRecognizer::stateFor(Qt::MouseButton button)
{
    return button == Qt::LeftButton ? leftState_ : rightState_;
}

void InputGestureRecognizer::reset()
{
    resetState(leftState_);
    resetState(rightState_);
    wheelRemainderY_ = 0;
}

void InputGestureRecognizer::clearWheelRemainder()
{
    wheelRemainderY_ = 0;
}

void InputGestureRecognizer::processMousePress(QMouseEvent* event, const TargetGeometry& geometry)
{
    if (event->button() != Qt::LeftButton && event->button() != Qt::RightButton) {
        return;
    }

    handlePress(stateFor(event->button()), event->position(), geometry);
    event->accept();
}

void InputGestureRecognizer::processMouseRelease(QMouseEvent* event, const TargetGeometry& geometry)
{
    if (event->button() != Qt::LeftButton && event->button() != Qt::RightButton) {
        return;
    }

    handleRelease(stateFor(event->button()), event->position(), geometry);
    event->accept();
}

void InputGestureRecognizer::processMouseDoubleClick(QMouseEvent* event, const TargetGeometry& geometry)
{
    if (event->button() != Qt::LeftButton && event->button() != Qt::RightButton) {
        return;
    }

    const auto action = event->button() == Qt::LeftButton
        ? InputAction::LeftDoubleClick
        : InputAction::RightDoubleClick;
    handleDoubleClick(stateFor(event->button()), action, event->position(), geometry);
    event->accept();
}

void InputGestureRecognizer::processWheel(QWheelEvent* event, const TargetGeometry& geometry)
{
    if (!geometry.contains(event->position())) {
        wheelRemainderY_ = 0;
        event->accept();
        return;
    }

    wheelRemainderY_ += event->angleDelta().y();
    while (wheelRemainderY_ >= 120) {
        wheelRemainderY_ -= 120;
        emit actionConfirmed(InputAction::WheelUp, event->position());
    }
    while (wheelRemainderY_ <= -120) {
        wheelRemainderY_ += 120;
        emit actionConfirmed(InputAction::WheelDown, event->position());
    }
    event->accept();
}

void InputGestureRecognizer::handlePress(ButtonState& state, const QPointF& position, const TargetGeometry& geometry)
{
    if (!geometry.contains(position)) {
        if (state.phase == ButtonPhase::PressedInside
            || state.phase == ButtonPhase::DoubleCandidate
            || state.phase == ButtonPhase::AwaitingDoubleClickEvent) {
            resetState(state);
        }
        return;
    }

    if (state.phase == ButtonPhase::PendingSingle) {
        cancelWaits(state);
        state.phase = ButtonPhase::DoubleCandidate;
        state.pressPosition = position;
        return;
    }

    resetState(state);
    state.phase = ButtonPhase::PressedInside;
    state.pressPosition = position;
}

void InputGestureRecognizer::handleRelease(ButtonState& state, const QPointF& position, const TargetGeometry& geometry)
{
    if (!geometry.contains(position)) {
        if (state.phase == ButtonPhase::PressedInside
            || state.phase == ButtonPhase::DoubleCandidate
            || state.phase == ButtonPhase::AwaitingDoubleClickEvent) {
            resetState(state);
        }
        return;
    }

    if (state.phase == ButtonPhase::PressedInside) {
        if (QLineF(state.pressPosition, position).length() > QApplication::startDragDistance()) {
            resetState(state);
            return;
        }
        state.pendingPosition = position;
        state.phase = ButtonPhase::PendingSingle;
        startSingleWait(state);
        return;
    }

    if (state.phase == ButtonPhase::DoubleCandidate) {
        if (QLineF(state.pressPosition, position).length() > QApplication::startDragDistance()) {
            resetState(state);
            return;
        }
        state.pendingPosition = position;
        state.phase = ButtonPhase::AwaitingDoubleClickEvent;
        startDoubleWait(state);
        return;
    }

    if (state.phase == ButtonPhase::AwaitingDoubleClickEvent) {
        resetState(state);
    }
}

void InputGestureRecognizer::handleDoubleClick(ButtonState& state, InputAction action, const QPointF& position, const TargetGeometry& geometry)
{
    if (state.phase == ButtonPhase::AwaitingDoubleClickEvent) {
        if (!geometry.contains(state.pendingPosition)) {
            resetState(state);
            return;
        }
        const QPointF confirmedPosition = state.pendingPosition;
        cancelWaits(state);
        resetState(state);
        emit actionConfirmed(action, confirmedPosition);
        return;
    }

    if (!geometry.contains(position)) {
        resetState(state);
        return;
    }
    cancelWaits(state);
    resetState(state);
    emit actionConfirmed(action, position);
}

void InputGestureRecognizer::cancelWaits(ButtonState& state)
{
    state.singleTimer->stop();
    state.doubleTimer->stop();
}

void InputGestureRecognizer::resetState(ButtonState& state)
{
    cancelWaits(state);
    state.phase = ButtonPhase::Idle;
    state.pressPosition = {};
    state.pendingPosition = {};
}

void InputGestureRecognizer::confirmPendingSingle(InputAction action)
{
    ButtonState& state = stateFor(action == InputAction::LeftClick ? Qt::LeftButton : Qt::RightButton);
    if (state.phase != ButtonPhase::PendingSingle) {
        return;
    }

    const QPointF position = state.pendingPosition;
    resetState(state);
    emit actionConfirmed(action, position);
}

void InputGestureRecognizer::abandonDoubleCandidate(InputAction action)
{
    ButtonState& state = stateFor(action == InputAction::LeftClick ? Qt::LeftButton : Qt::RightButton);
    if (state.phase == ButtonPhase::DoubleCandidate || state.phase == ButtonPhase::AwaitingDoubleClickEvent) {
        resetState(state);
    }
}

void InputGestureRecognizer::startSingleWait(ButtonState& state)
{
    cancelWaits(state);
    state.singleTimer->start(QApplication::doubleClickInterval());
}

void InputGestureRecognizer::startDoubleWait(ButtonState& state)
{
    cancelWaits(state);
    state.doubleTimer->start(QApplication::doubleClickInterval());
}
```
- [ ] **Step 4: Run recognizer tests to verify they pass**

```bash
cmake -S . -B build -G Ninja
cmake --build build --target test_input_gestures
QT_QPA_PLATFORM=offscreen ctest --test-dir build -R input_gestures --output-on-failure
```

Expected: all single, double, triple, isolation, invalid-release, reset, and wheel assertions pass.

---

### Task 4: Implement `TargetCanvas` data flow and QPainter rendering

**Files:**
- Create: `src/ui/TargetCanvas.h`
- Create: `src/ui/TargetCanvas.cpp`
- Create: `tests/test_target_canvas.cpp`
- Modify: `tests/CMakeLists.txt`

**Interfaces:**
- `TargetCanvas::markers()` returns the current marker deque.
- `TargetCanvas::clearMarkers()` clears all markers.
- `TargetCanvas::resetInput()` resets recognizer state.
- `TargetCanvas` emits `actionConfirmed(InputAction, QPointF)` and `latestActionConfirmed(InputAction)`.

- [ ] **Step 1: Write failing canvas integration tests**

Create `tests/test_target_canvas.cpp`:

```cpp
#include <cstddef>
#include <QtTest>

#include <QWheelEvent>

#include "ui/TargetCanvas.h"
#include "geometry/TargetGeometry.h"

class TargetCanvasTest : public QObject
{
    Q_OBJECT

private slots:
    void initTestCase();
    void clickCreatesNormalizedMarker();
    void doubleClickCreatesOneMarker();
    void wheelCreatesMarker();
    void clearRemovesMarkers();
    void resizePreservesNormalizedPosition();
    void outsideInputDoesNotCreateMarker();
    void leaveClearsWheelRemainder();
    void markerLimitDropsOldest();

private:
    static constexpr qreal Epsilon = 0.0001;
};

void TargetCanvasTest::initTestCase()
{
    qRegisterMetaType<InputAction>("InputAction");
}

void TargetCanvasTest::clickCreatesNormalizedMarker()
{
    TargetCanvas canvas;
    canvas.resize(400, 400);
    canvas.show();
    QVERIFY(QTest::qWaitForWindowExposed(&canvas));
    QSignalSpy spy(&canvas, &TargetCanvas::actionConfirmed);

    QTest::mouseClick(&canvas, Qt::LeftButton, Qt::NoModifier, QPoint(200, 200));
    QTest::qWait(QApplication::doubleClickInterval() + 100);

    QCOMPARE(spy.count(), 1);
    QCOMPARE(canvas.markers().size(), std::size_t(1));
    const Marker marker = canvas.markers().front();
    QCOMPARE(marker.action, InputAction::LeftClick);
    QVERIFY(qAbs(marker.normalizedPosition.x()) < Epsilon);
    QVERIFY(qAbs(marker.normalizedPosition.y()) < Epsilon);
}

void TargetCanvasTest::doubleClickCreatesOneMarker()
{
    TargetCanvas canvas;
    canvas.resize(400, 400);
    canvas.show();
    QVERIFY(QTest::qWaitForWindowExposed(&canvas));
    QSignalSpy spy(&canvas, &TargetCanvas::actionConfirmed);

    QTest::mouseDClick(&canvas, Qt::LeftButton, Qt::NoModifier, QPoint(120, 120));
    QTest::qWait(50);

    QCOMPARE(spy.count(), 1);
    QCOMPARE(canvas.markers().size(), std::size_t(1));
    QCOMPARE(canvas.markers().front().action, InputAction::LeftDoubleClick);
}

void TargetCanvasTest::wheelCreatesMarker()
{
    TargetCanvas canvas;
    canvas.resize(400, 400);
    canvas.show();
    QVERIFY(QTest::qWaitForWindowExposed(&canvas));
    QSignalSpy spy(&canvas, &TargetCanvas::actionConfirmed);

    QWheelEvent event(
        QPointF(100, 100),
        QPointF(100, 100),
        QPoint(),
        QPoint(0, 120),
        Qt::NoButton,
        Qt::NoModifier,
        Qt::NoScrollPhase,
        false
    );
    QApplication::sendEvent(&canvas, &event);

    QCOMPARE(spy.count(), 1);
    QCOMPARE(canvas.markers().size(), std::size_t(1));
    QCOMPARE(canvas.markers().front().action, InputAction::WheelUp);
}

void TargetCanvasTest::clearRemovesMarkers()
{
    TargetCanvas canvas;
    canvas.resize(400, 400);
    canvas.show();
    QVERIFY(QTest::qWaitForWindowExposed(&canvas));

    QTest::mouseClick(&canvas, Qt::LeftButton, Qt::NoModifier, QPoint(100, 100));
    QTest::qWait(QApplication::doubleClickInterval() + 100);
    QCOMPARE(canvas.markers().size(), std::size_t(1));

    canvas.clearMarkers();
    QVERIFY(canvas.markers().empty());
}

void TargetCanvasTest::resizePreservesNormalizedPosition()
{
    TargetCanvas canvas;
    canvas.resize(400, 400);
    canvas.show();
    QVERIFY(QTest::qWaitForWindowExposed(&canvas));

    QTest::mouseClick(&canvas, Qt::LeftButton, Qt::NoModifier, QPoint(150, 150));
    QTest::qWait(QApplication::doubleClickInterval() + 100);
    const QPointF normalized = canvas.markers().front().normalizedPosition;

    canvas.resize(600, 300);
    QCOMPARE(canvas.markers().front().normalizedPosition, normalized);
}

void TargetCanvasTest::outsideInputDoesNotCreateMarker()
{
    TargetCanvas canvas;
    canvas.resize(600, 400);
    canvas.show();
    QVERIFY(QTest::qWaitForWindowExposed(&canvas));
    QSignalSpy spy(&canvas, &TargetCanvas::actionConfirmed);

    QTest::mouseClick(&canvas, Qt::LeftButton, Qt::NoModifier, QPoint(550, 200));
    QWheelEvent event(
        QPointF(550, 200),
        QPointF(550, 200),
        QPoint(),
        QPoint(0, 120),
        Qt::NoButton,
        Qt::NoModifier,
        Qt::NoScrollPhase,
        false
    );
    QApplication::sendEvent(&canvas, &event);
    QTest::qWait(QApplication::doubleClickInterval() + 100);

    QCOMPARE(spy.count(), 0);
    QVERIFY(canvas.markers().empty());
}

void TargetCanvasTest::leaveClearsWheelRemainder()
{
    TargetCanvas canvas;
    canvas.resize(400, 400);
    canvas.show();
    QVERIFY(QTest::qWaitForWindowExposed(&canvas));

    QWheelEvent first(
        QPointF(100, 100),
        QPointF(100, 100),
        QPoint(),
        QPoint(0, 90),
        Qt::NoButton,
        Qt::NoModifier,
        Qt::NoScrollPhase,
        false
    );
    QApplication::sendEvent(&canvas, &first);
    QEvent leave(QEvent::Leave);
    QApplication::sendEvent(&canvas, &leave);
    QWheelEvent second(
        QPointF(100, 100),
        QPointF(100, 100),
        QPoint(),
        QPoint(0, 30),
        Qt::NoButton,
        Qt::NoModifier,
        Qt::NoScrollPhase,
        false
    );
    QApplication::sendEvent(&canvas, &second);
    QVERIFY(canvas.markers().empty());

    QWheelEvent third(
        QPointF(100, 100),
        QPointF(100, 100),
        QPoint(),
        QPoint(0, 120),
        Qt::NoButton,
        Qt::NoModifier,
        Qt::NoScrollPhase,
        false
    );
    QApplication::sendEvent(&canvas, &third);
    QCOMPARE(canvas.markers().size(), std::size_t(1));
}

void TargetCanvasTest::markerLimitDropsOldest()
{
    TargetCanvas canvas;
    canvas.resize(400, 400);
    canvas.show();
    QVERIFY(QTest::qWaitForWindowExposed(&canvas));

    for (int index = 0; index < 5001; ++index) {
        QWheelEvent event(
            QPointF(100, 100),
            QPointF(100, 100),
            QPoint(),
            QPoint(0, 120),
            Qt::NoButton,
            Qt::NoModifier,
            Qt::NoScrollPhase,
            false
        );
        QApplication::sendEvent(&canvas, &event);
    }

    QCOMPARE(canvas.markers().size(), std::size_t(5000));
    QCOMPARE(canvas.markers().front().sequence, std::uint64_t(2));
    QCOMPARE(canvas.markers().back().sequence, std::uint64_t(5001));
}

QTEST_MAIN(TargetCanvasTest)
#include "test_target_canvas.moc"
```

- [ ] **Step 2: Add the test target and verify it fails**

Append to `tests/CMakeLists.txt`:

```cmake
add_executable(test_target_canvas
    test_target_canvas.cpp
    ../src/ui/TargetCanvas.cpp
    ../src/input/InputGestureRecognizer.cpp
    ../src/geometry/TargetGeometry.cpp
    ../src/model/LegendStyle.cpp
)

target_link_libraries(test_target_canvas PRIVATE Qt6::Widgets Qt6::Test)
add_test(NAME target_canvas COMMAND test_target_canvas)
```

Run the target with temporary canvas stubs. Expected: compile failure or marker assertions fail.

- [ ] **Step 3: Implement `TargetCanvas`**

`src/ui/TargetCanvas.h`:

```cpp
#pragma once

#include <deque>

#include <QEvent>
#include <QPainter>
#include <QWidget>

#include "model/Marker.h"

class InputGestureRecognizer;
class QHideEvent;
class QMouseEvent;
class QPaintEvent;
class QWheelEvent;
class TargetGeometry;

class TargetCanvas : public QWidget
{
    Q_OBJECT

public:
    explicit TargetCanvas(QWidget* parent = nullptr);

    const std::deque<Marker>& markers() const;
    void clearMarkers();
    void resetInput();

signals:
    void actionConfirmed(InputAction action, QPointF widgetPosition);
    void latestActionConfirmed(InputAction action);

protected:
    void paintEvent(QPaintEvent* event) override;
    void mousePressEvent(QMouseEvent* event) override;
    void mouseReleaseEvent(QMouseEvent* event) override;
    void mouseDoubleClickEvent(QMouseEvent* event) override;
    void wheelEvent(QWheelEvent* event) override;
    void leaveEvent(QEvent* event) override;
    void hideEvent(QHideEvent* event) override;
    void changeEvent(QEvent* event) override;

private:
    TargetGeometry currentGeometry() const;
    void addMarker(InputAction action, const QPointF& widgetPosition);
    void drawTarget(QPainter& painter, const TargetGeometry& geometry) const;
    void drawMarker(QPainter& painter, const TargetGeometry& geometry, const Marker& marker) const;

    std::deque<Marker> markers_;
    std::uint64_t nextSequence_ = 1;
    InputGestureRecognizer* recognizer_ = nullptr;
};
```

`src/ui/TargetCanvas.cpp`:

```cpp
#include "ui/TargetCanvas.h"

#include <array>
#include <algorithm>

#include <QEvent>
#include <QMouseEvent>
#include <QPainter>
#include <QPaintEvent>
#include <QWheelEvent>

#include "geometry/TargetGeometry.h"
#include "input/InputGestureRecognizer.h"
#include "model/LegendStyle.h"

namespace {
constexpr int MarkerDiameter = 16;
constexpr int MaxMarkers = 5000;
}

TargetCanvas::TargetCanvas(QWidget* parent)
    : QWidget(parent)
{
    setContextMenuPolicy(Qt::NoContextMenu);
    setMinimumSize(320, 320);
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);

    recognizer_ = new InputGestureRecognizer(this);
    connect(recognizer_, &InputGestureRecognizer::actionConfirmed, this, [this](InputAction action, QPointF position) {
        addMarker(action, position);
        emit actionConfirmed(action, position);
    });
}

const std::deque<Marker>& TargetCanvas::markers() const
{
    return markers_;
}

void TargetCanvas::clearMarkers()
{
    markers_.clear();
    nextSequence_ = 1;
    update();
}

void TargetCanvas::resetInput()
{
    recognizer_->reset();
}

TargetGeometry TargetCanvas::currentGeometry() const
{
    return TargetGeometry(QRectF(rect()));
}

void TargetCanvas::addMarker(InputAction action, const QPointF& widgetPosition)
{
    const TargetGeometry geometry = currentGeometry();
    if (!geometry.contains(widgetPosition)) {
        return;
    }

    markers_.push_back(Marker{action, geometry.toNormalized(widgetPosition), nextSequence_++});
    while (markers_.size() > MaxMarkers) {
        markers_.pop_front();
    }

    emit latestActionConfirmed(action);
    update();
}

void TargetCanvas::paintEvent(QPaintEvent* event)
{
    Q_UNUSED(event)
    QPainter painter(this);
    painter.setRenderHint(QPainter::Antialiasing, true);

    const TargetGeometry geometry = currentGeometry();
    drawTarget(painter, geometry);
    for (const Marker& marker : markers_) {
        drawMarker(painter, geometry, marker);
    }
}

void TargetCanvas::mousePressEvent(QMouseEvent* event)
{
    recognizer_->processMousePress(event, currentGeometry());
}

void TargetCanvas::mouseReleaseEvent(QMouseEvent* event)
{
    recognizer_->processMouseRelease(event, currentGeometry());
}

void TargetCanvas::mouseDoubleClickEvent(QMouseEvent* event)
{
    recognizer_->processMouseDoubleClick(event, currentGeometry());
}

void TargetCanvas::wheelEvent(QWheelEvent* event)
{
    recognizer_->processWheel(event, currentGeometry());
}

void TargetCanvas::leaveEvent(QEvent* event)
{
    recognizer_->clearWheelRemainder();
    QWidget::leaveEvent(event);
}

void TargetCanvas::hideEvent(QHideEvent* event)
{
    recognizer_->reset();
    QWidget::hideEvent(event);
}

void TargetCanvas::changeEvent(QEvent* event)
{
    if (event->type() == QEvent::EnabledChange && !isEnabled()) {
        recognizer_->reset();
    }
    QWidget::changeEvent(event);
}

void TargetCanvas::drawTarget(QPainter& painter, const TargetGeometry& geometry) const
{
    painter.fillRect(rect(), Qt::white);
    if (geometry.radius() <= 0) {
        return;
    }

    static const std::array<QColor, 10> ringColors = {
        QColor(Qt::white), QColor(Qt::white),
        QColor(Qt::black), QColor(Qt::black),
        QColor(QStringLiteral("#2563EB")), QColor(QStringLiteral("#2563EB")),
        QColor(QStringLiteral("#DC2626")), QColor(QStringLiteral("#DC2626")),
        QColor(QStringLiteral("#FACC15")), QColor(QStringLiteral("#FACC15"))
    };

    for (int index = 0; index < 10; ++index) {
        const qreal radius = geometry.radius() * (10 - index) / 10;
        painter.setPen(QPen(Qt::black, 1));
        painter.setBrush(ringColors.at(index));
        painter.drawEllipse(geometry.center(), radius, radius);
    }
}

void TargetCanvas::drawMarker(QPainter& painter, const TargetGeometry& geometry, const Marker& marker) const
{
    const QPointF position = geometry.fromNormalized(marker.normalizedPosition);
    const QRectF rect(
        position.x() - MarkerDiameter / 2,
        position.y() - MarkerDiameter / 2,
        MarkerDiameter,
        MarkerDiameter
    );
    const LegendStyle& style = legendStyle(marker.action);

    painter.save();
    painter.setPen(QPen(Qt::white, 4, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    painter.setBrush(style.color);
    drawMarkerShape(painter, style.shape, rect);
    painter.setPen(QPen(Qt::black, 1));
    painter.setBrush(style.color);
    drawMarkerShape(painter, style.shape, rect);
    painter.restore();
}
```

Add the missing includes to `TargetCanvas.h`:

```cpp
#include <QPainter>
```

and to `TargetCanvas.cpp`:

```cpp
#include <array>
```

- [ ] **Step 4: Run canvas tests to verify they pass**

```bash
cmake -S . -B build -G Ninja
cmake --build build --target test_target_canvas
QT_QPA_PLATFORM=offscreen ctest --test-dir build -R target_canvas --output-on-failure
```

Expected: click, double-click, wheel, clear, resize, outside-input, leave-clear, and marker-limit assertions pass.

---

### Task 5: Compose the legend panel, main window, and executable

**Files:**
- Create: `src/ui/LegendPanel.h`
- Create: `src/ui/LegendPanel.cpp`
- Create: `src/ui/MainWindow.h`
- Create: `src/ui/MainWindow.cpp`
- Create: `src/main.cpp`
- Create: `tests/test_ui_components.cpp`
- Modify: `tests/CMakeLists.txt`

**Interfaces:**
- `LegendPanel::setLatestAction(InputAction)` updates the latest-action label.
- `MainWindow` connects `TargetCanvas::latestActionConfirmed` to `LegendPanel::setLatestAction`.
- The clear button calls `TargetCanvas::clearMarkers`.

- [ ] **Step 1: Write failing UI composition tests**

Create `tests/test_ui_components.cpp`:

```cpp
#include <cstddef>
#include <QtTest>

#include <QLabel>
#include <QPushButton>

#include "ui/LegendPanel.h"
#include "ui/MainWindow.h"
#include "ui/TargetCanvas.h"

class UiComponentsTest : public QObject
{
    Q_OBJECT

private slots:
    void latestActionLabelUpdates();
    void mainWindowWiresClearButton();
};

void UiComponentsTest::latestActionLabelUpdates()
{
    LegendPanel panel;
    panel.setLatestAction(InputAction::LeftDoubleClick);
    auto* label = panel.findChild<QLabel*>("latest-action-label");
    QVERIFY(label != nullptr);
    QCOMPARE(label->text(), QStringLiteral("最近操作：左键双击"));
}

void UiComponentsTest::mainWindowWiresClearButton()
{
    MainWindow window;
    window.resize(800, 500);
    window.show();
    QVERIFY(QTest::qWaitForWindowExposed(&window));

    auto* canvas = window.findChild<TargetCanvas*>("target-canvas");
    auto* button = window.findChild<QPushButton*>("clear-markers-button");
    QVERIFY(canvas != nullptr);
    QVERIFY(button != nullptr);

    QTest::mouseClick(canvas, Qt::LeftButton, Qt::NoModifier, QPoint(100, 100));
    QTest::qWait(QApplication::doubleClickInterval() + 100);
    QCOMPARE(canvas->markers().size(), std::size_t(1));

    QTest::mouseClick(button, Qt::LeftButton);
    QVERIFY(canvas->markers().empty());
}

QTEST_MAIN(UiComponentsTest)
#include "test_ui_components.moc"
```

- [ ] **Step 2: Add the UI test target and verify it fails**

Append to `tests/CMakeLists.txt`:

```cmake
add_executable(test_ui_components
    test_ui_components.cpp
    ../src/ui/LegendPanel.cpp
    ../src/ui/MainWindow.cpp
    ../src/ui/TargetCanvas.cpp
    ../src/input/InputGestureRecognizer.cpp
    ../src/geometry/TargetGeometry.cpp
    ../src/model/LegendStyle.cpp
)

target_link_libraries(test_ui_components PRIVATE Qt6::Widgets Qt6::Test)
add_test(NAME ui_components COMMAND test_ui_components)
```

Run the target with temporary UI stubs. Expected: compile failure or label/wiring assertions fail.

- [ ] **Step 3: Implement the legend panel**

`src/ui/LegendPanel.h`:

```cpp
#pragma once

#include <QWidget>

#include "model/InputAction.h"

class QLabel;
class QPushButton;

class LegendPanel : public QWidget
{
    Q_OBJECT

public:
    explicit LegendPanel(QWidget* parent = nullptr);

public slots:
    void setLatestAction(InputAction action);

private:
    class LegendWidget;

    LegendWidget* legendWidget_ = nullptr;
    QLabel* latestLabel_ = nullptr;
    QPushButton* clearButton_ = nullptr;
};
```

`src/ui/LegendPanel.cpp`:

```cpp
#include "ui/LegendPanel.h"

#include <array>

#include <QFont>
#include <QLabel>
#include <QPainter>
#include <QPaintEvent>
#include <QPushButton>
#include <QVBoxLayout>

#include "model/LegendStyle.h"

class LegendPanel::LegendWidget : public QWidget
{
public:
    using QWidget::QWidget;

protected:
    void paintEvent(QPaintEvent* event) override
    {
        Q_UNUSED(event)
        QPainter painter(this);
        painter.setRenderHint(QPainter::Antialiasing, true);

        constexpr int RowHeight = 34;
        constexpr int ShapeSize = 20;
        const std::array<InputAction, 6> actions = {
            InputAction::LeftClick,
            InputAction::LeftDoubleClick,
            InputAction::RightClick,
            InputAction::RightDoubleClick,
            InputAction::WheelUp,
            InputAction::WheelDown
        };

        for (std::size_t index = 0; index < actions.size(); ++index) {
            const LegendStyle& style = legendStyle(actions[index]);
            const QRectF shapeRect(8, index * RowHeight + 7, ShapeSize, ShapeSize);
            painter.setPen(QPen(Qt::black, 1));
            painter.setBrush(style.color);
            drawMarkerShape(painter, style.shape, shapeRect);
            painter.setPen(Qt::black);
            painter.drawText(QRectF(38, index * RowHeight, width() - 46, RowHeight),
                Qt::AlignVCenter | Qt::AlignLeft, style.text);
        }
    }

    QSize sizeHint() const override
    {
        return QSize(220, 6 * 34 + 14);
    }
};

LegendPanel::LegendPanel(QWidget* parent)
    : QWidget(parent)
{
    setFixedWidth(240);
    auto* layout = new QVBoxLayout(this);
    layout->setContentsMargins(12, 12, 12, 12);
    layout->setSpacing(10);

    auto* title = new QLabel(tr("操作说明"), this);
    QFont titleFont = title->font();
    titleFont.setBold(true);
    title->setFont(titleFont);

    legendWidget_ = new LegendWidget(this);
    latestLabel_ = new QLabel(tr("最近操作：无"), this);
    latestLabel_->setObjectName(QStringLiteral("latest-action-label"));
    clearButton_ = new QPushButton(tr("清空标记"), this);
    clearButton_->setObjectName(QStringLiteral("clear-markers-button"));

    layout->addWidget(title);
    layout->addWidget(legendWidget_);
    layout->addWidget(latestLabel_);
    layout->addWidget(clearButton_);
    layout->addStretch(1);
}

void LegendPanel::setLatestAction(InputAction action)
{
    latestLabel_->setText(tr("最近操作：%1").arg(legendStyle(action).text));
}
```

- [ ] **Step 4: Implement the main window and executable**

`src/ui/MainWindow.h`:

```cpp
#pragma once

#include <QWidget>

class TargetCanvas;

class MainWindow : public QWidget
{
    Q_OBJECT

public:
    explicit MainWindow(QWidget* parent = nullptr);

private:
    TargetCanvas* canvas_ = nullptr;
};
```

`src/ui/MainWindow.cpp`:

```cpp
#include "ui/MainWindow.h"

#include <QHBoxLayout>
#include <QPushButton>

#include "ui/LegendPanel.h"
#include "ui/TargetCanvas.h"

MainWindow::MainWindow(QWidget* parent)
    : QWidget(parent)
{
    setWindowTitle(tr("Archery Input Target"));
    resize(900, 600);

    auto* layout = new QHBoxLayout(this);
    layout->setContentsMargins(0, 0, 0, 0);
    layout->setSpacing(0);

    canvas_ = new TargetCanvas(this);
    canvas_->setObjectName(QStringLiteral("target-canvas"));
    auto* legendPanel = new LegendPanel(this);
    legendPanel->setObjectName(QStringLiteral("legend-panel"));

    layout->addWidget(canvas_, 1);
    layout->addWidget(legendPanel, 0);

    connect(canvas_, &TargetCanvas::latestActionConfirmed,
        legendPanel, &LegendPanel::setLatestAction);
    connect(legendPanel->findChild<QPushButton*>(QStringLiteral("clear-markers-button")),
        &QPushButton::clicked, canvas_, &TargetCanvas::clearMarkers);
}
```

`src/main.cpp`:

```cpp
#include <QApplication>

#include "ui/MainWindow.h"

int main(int argc, char* argv[])
{
    QApplication application(argc, argv);
    MainWindow window;
    window.show();
    return application.exec();
}
```

- [ ] **Step 5: Run UI tests to verify they pass**

```bash
cmake -S . -B build -G Ninja
cmake --build build --target test_ui_components
QT_QPA_PLATFORM=offscreen ctest --test-dir build -R ui_components --output-on-failure
```

Expected: latest-action text and clear-button wiring pass.

---

### Task 6: Run full verification and launch acceptance

**Files:**
- Modify: `tests/CMakeLists.txt` only if a target is missing.

**Interfaces:**
- All public interfaces are now defined by Tasks 1-5.

- [ ] **Step 1: Build every target**

```bash
cmake -S . -B build -G Ninja
cmake --build build
```

Expected: `archery-input-target`, all five test executables, and no compiler or linker errors.

- [ ] **Step 2: Run the complete Qt Test suite**

```bash
QT_QPA_PLATFORM=offscreen ctest --test-dir build --output-on-failure
```

Expected: all geometry, style, recognizer, canvas, and UI tests pass.

- [ ] **Step 3: Launch the executable**

```bash
QT_QPA_PLATFORM=offscreen timeout 3s ./build/archery-input-target
```

Expected: the process starts without an immediate crash and exits only because of the timeout. In a real Ubuntu desktop session, run:

```bash
./build/archery-input-target
```

and verify that the window opens with a target on the left and the fixed legend on the right.

- [ ] **Step 4: Perform the acceptance matrix manually**

Verify:

- Six actions produce six distinct colors and shapes.
- Target markers contain no text.
- Outside clicks and wheel events create no marker.
- Boundary clicks are accepted and points just outside are rejected.
- Single click waits for the double-click interval.
- Double click produces one marker at the second release.
- Triple click produces double then single.
- Left and right states do not interfere.
- Drag and press-inside/release-outside produce no marker.
- Right-click context menu does not appear.
- `+120`, `+240`, `-120`, and `-240` wheel deltas produce the required number of markers.
- Four `+30` wheel events produce one marker.
- Resize preserves marker relative positions.
- Repaint, minimize, restore, and hide/show do not lose markers.
- The newest overlapping marker is drawn last.
- Clear removes all markers.

- [ ] **Step 5: Stop condition**

Do not commit or push. Report the build/test commands and any environment limitation, especially if no graphical display is available for manual launch.

---

## Amendments (2026-09-22, implemented, all suites green)

### A1 — All GUI text in English

- `legendStyle()` texts are exactly: `"Left Click"`, `"Left Double Click"`, `"Right Click"`, `"Right Double Click"`, `"Wheel Up"`, `"Wheel Down"`.
- `LegendPanel` title is `"Legend"`; the clear button is `"Clear Markers"`.
- `tests/test_legend_style.cpp` expects the English strings. No Chinese remains in `src/` (verified by grep).

### A2 — White separator between the two black rings

- `TargetCanvas::drawTarget`, after drawing the ten rings, strokes one white circle (width 2 logical px, `Qt::NoBrush`) at radius `geometry.radius() * 0.7`, the shared boundary of the two black rings. All other ring outlines are unchanged.
- `tests/test_target_canvas.cpp::blackRingBoundaryHasWhiteSeparator` renders the marker-free widget to a `QImage` and asserts `qGray(pixel at (center.x + 0.7*radius, center.y)) > 200`.

### A3 — Recent-actions list (latest 10) in LegendPanel

Changed public interfaces (exact):

```cpp
// TargetCanvas: latestActionConfirmed(InputAction) is REMOVED, replaced by:
void markerAdded(InputAction action, QPointF normalizedPosition);
// emitted from addMarker() with the stored normalized coordinates (origin = target center).

// LegendPanel.h:
struct RecentAction
{
    InputAction action;
    QPointF position;
};
// QList<RecentAction> recentActions() const;   // newest first
// public slots:
//     void addRecentAction(InputAction action, QPointF normalizedPosition);
//     void clearRecentActions();                // newest-first list of at most the last 10
```

- Panel layout from top: `"Legend"` title, six fixed legend rows (unchanged), `"Recent Actions"` header, the recent list (`QListWidget`, objectName `recent-actions-list`; each row is a `drawMarkerShape` swatch icon plus text like `Left Click (0.25, -0.50)` with normalized nx/ny at 2 decimals), empty state label `"No actions yet"` (objectName `recent-empty-label`, visible only when the list is empty), then the `"Clear Markers"` button. Panel fixed width is 280 (was 240).
- `MainWindow` connects `TargetCanvas::markerAdded` to `LegendPanel::addRecentAction`; the Clear button calls both `TargetCanvas::clearMarkers()` and `LegendPanel::clearRecentActions()`. Canvas marker storage itself is unchanged (persists until cleared, 5000 cap).
- `tests/test_ui_components.cpp` no longer asserts on the latest-action label; it asserts newest-first growth, the 10-item cap (11th drops the oldest), name+coordinates row text, `markerAdded` emission with normalized position equal to the stored marker position, and that Clear empties both canvas markers and the recent list.
