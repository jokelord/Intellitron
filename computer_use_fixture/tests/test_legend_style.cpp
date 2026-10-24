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

    QCOMPARE(leftClick.text, QStringLiteral("Left Click"));
    QCOMPARE(leftClick.color, QColor(QStringLiteral("#2563EB")));
    QCOMPARE(leftClick.shape, MarkerShape::Circle);

    QCOMPARE(leftDouble.text, QStringLiteral("Left Double Click"));
    QCOMPARE(leftDouble.color, QColor(QStringLiteral("#16A34A")));
    QCOMPARE(leftDouble.shape, MarkerShape::Triangle);

    QCOMPARE(rightClick.text, QStringLiteral("Right Click"));
    QCOMPARE(rightClick.color, QColor(QStringLiteral("#7C3AED")));
    QCOMPARE(rightClick.shape, MarkerShape::Square);

    QCOMPARE(rightDouble.text, QStringLiteral("Right Double Click"));
    QCOMPARE(rightDouble.color, QColor(QStringLiteral("#EA580C")));
    QCOMPARE(rightDouble.shape, MarkerShape::Diamond);

    QCOMPARE(wheelUp.text, QStringLiteral("Wheel Up"));
    QCOMPARE(wheelUp.color, QColor(QStringLiteral("#0891B2")));
    QCOMPARE(wheelUp.shape, MarkerShape::UpArrow);

    QCOMPARE(wheelDown.text, QStringLiteral("Wheel Down"));
    QCOMPARE(wheelDown.color, QColor(QStringLiteral("#DC2626")));
    QCOMPARE(wheelDown.shape, MarkerShape::DownArrow);
}

QTEST_APPLESS_MAIN(LegendStyleTest)
#include "test_legend_style.moc"
