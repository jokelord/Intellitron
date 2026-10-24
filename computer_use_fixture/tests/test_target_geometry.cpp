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
