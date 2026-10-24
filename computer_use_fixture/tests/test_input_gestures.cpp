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
