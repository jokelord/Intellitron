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

    // A native double-click event replaces the second press, so it can arrive
    // while the first click is still pending (real desktop and QTest sequence).
    // Qt has already applied its double-click time/distance policy.
    if (!geometry.contains(position)) {
        resetState(state);
        return;
    }

    const QPointF confirmedPosition = position;
    cancelWaits(state);
    resetState(state);
    emit actionConfirmed(action, confirmedPosition);
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
