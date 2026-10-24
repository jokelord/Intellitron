#include <cstddef>
#include <QtTest>

#include <QImage>
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
    void blackRingBoundaryHasWhiteSeparator();

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

void TargetCanvasTest::blackRingBoundaryHasWhiteSeparator()
{
    TargetCanvas canvas;
    canvas.resize(400, 400);
    canvas.show();
    QVERIFY(QTest::qWaitForWindowExposed(&canvas));
    QVERIFY(canvas.markers().empty());

    QImage image(canvas.size(), QImage::Format_ARGB32);
    image.fill(Qt::transparent);
    canvas.render(&image);

    const TargetGeometry geometry(QRectF(canvas.rect()));
    const int x = qRound(geometry.center().x() + geometry.radius() * 0.7);
    const int y = qRound(geometry.center().y());
    QVERIFY(qGray(image.pixel(x, y)) > 200);
}

QTEST_MAIN(TargetCanvasTest)
#include "test_target_canvas.moc"
