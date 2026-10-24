# Archery Input Target Design

## Goal

Build an Ubuntu 24.04 desktop GUI that displays an archery target, recognizes six mouse/wheel input actions, and draws a persistent colored geometric marker at each accepted input position.

## Decisions

- Use C++20, Qt 6 Widgets, QPainter, CMake, and Qt Test.
- Keep `TargetCanvas` and `LegendPanel` as separate widgets.
- Use `TargetGeometry` as the sole source of target geometry.
- Store markers using normalized coordinates so resize preserves their relative position.
- Use `LegendStyle` as the sole source of action text, color, and shape.
- Use Qt's native `MouseButtonDblClick` event for double-click timing and distance policy.
- Store markers with normalized coordinates `nx = (x - center.x) / radius`, `ny = (y - center.y) / radius`, so the top edge of the target is `(0, -1)` in Qt's downward-growing y coordinates.
- Define the target as the largest circle that fits inside the current `TargetCanvas` rectangle.

## Architecture

- `MainWindow` owns a horizontal layout containing `TargetCanvas` and `LegendPanel`, plus the clear-markers control.
- `TargetCanvas` receives Qt input events, owns the marker collection, owns `InputGestureRecognizer`, and repaints from data.
- `InputGestureRecognizer` owns independent left/right button state machines and wheel accumulation state. It emits `actionConfirmed(InputAction, QPointF)`.
- `TargetGeometry` computes center, radius, containment, normalization, and denormalization from one `QRectF`.
- `LegendPanel` renders the fixed six-item legend and the latest confirmed action.
- `LegendStyle` provides a single mapping from `InputAction` to text, color, and `MarkerShape`.

## Input Semantics

- A click is accepted only when press and release are both inside the target and the press-to-release movement does not exceed `QApplication::startDragDistance()`.
- A valid first release enters a pending-single state and waits for `QApplication::doubleClickInterval()`.
- A native double-click event cancels the pending single and produces one double-click marker at the second release position.
- A triple click produces one double-click operation followed by one single-click operation.
- Left and right button state is independent.
- Wheel input uses signed `angleDelta().y()` accumulation with 120 units per action.
- Wheel events outside the target produce no marker and do not carry their delta forward.
- Leaving the canvas clears an unfinished wheel remainder.
- Right-click context menus are disabled.

## Rendering

- Draw ten concentric target rings, grouped from outside to inside as white, black, blue, red, and yellow.
- Draw each marker as a fixed-size logical-pixel shape with a white halo, dark stroke, and action color.
- Shapes are Circle, Triangle, Square, Diamond, UpArrow, and DownArrow, drawn with QPainter paths or polygons rather than Unicode glyphs.
- Draw markers in sequence order so the newest marker is on top.
- Keep a maximum of 5000 markers.
- Do not draw text markers on the target.

## Lifecycle and Failure Behavior

- Recognizer timers and the recognizer itself are QObject children of `TargetCanvas`.
- Reset pending input state when the canvas is hidden, disabled, or explicitly reset.
- Do not paint from event handlers; event handlers update data and call `update()`.
- Ignore zero-sized geometry and all out-of-target actions safely.
- Do not persist markers across process restarts.

## Tests and Acceptance

- Test geometry center, boundary inclusion, outside exclusion, normalization round trips, and resize stability.
- Test single, double, and triple clicks; left/right isolation; invalid release positions; drag cancellation; and pending timer behavior.
- Test wheel accumulation, multiple units per event, high-resolution partial units, out-of-target events, and leave-event clearing.
- Test all six action/style mappings, latest-action updates, marker clearing, marker ordering, and resize redraw behavior.
- Build with CMake/Ninja, run Qt Test through CTest, and launch `archery-input-target` on Ubuntu 24.04.
