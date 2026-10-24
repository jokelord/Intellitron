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
