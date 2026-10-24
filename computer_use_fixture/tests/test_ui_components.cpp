#include <cstddef>
#include <QtTest>

#include "ui/LegendPanel.h"
#include "ui/MainWindow.h"
#include "ui/TargetCanvas.h"

#include <QLabel>
#include <QListWidget>
#include <QPushButton>
#include <QWheelEvent>

class UiComponentsTest : public QObject
{
    Q_OBJECT

private slots:
    void initTestCase();
    void recentActionsGrowNewestFirst();
    void recentActionsCapAtTen();
    void recentActionRowShowsNameAndCoordinates();
    void canvasEmitsMarkerAddedWithNormalizedPosition();
    void clearEmptiesMarkersAndRecentList();
};

void UiComponentsTest::initTestCase()
{
    qRegisterMetaType<InputAction>("InputAction");
}

void UiComponentsTest::recentActionsGrowNewestFirst()
{
    LegendPanel panel;
    QCOMPARE(panel.recentActions().size(), 0);

    panel.addRecentAction(InputAction::LeftClick, QPointF(0.25, -0.5));
    panel.addRecentAction(InputAction::WheelDown, QPointF(0, 0));

    const QList<RecentAction> actions = panel.recentActions();
    QCOMPARE(actions.size(), 2);
    QCOMPARE(actions.front().action, InputAction::WheelDown);
    QCOMPARE(actions.front().position, QPointF(0, 0));
    QCOMPARE(actions.back().action, InputAction::LeftClick);
    QCOMPARE(actions.back().position, QPointF(0.25, -0.5));
}

void UiComponentsTest::recentActionsCapAtTen()
{
    LegendPanel panel;
    for (int index = 0; index < 11; ++index) {
        panel.addRecentAction(InputAction::LeftClick, QPointF(index * 0.01, 0));
    }

    const QList<RecentAction> actions = panel.recentActions();
    QCOMPARE(actions.size(), 10);
    QCOMPARE(actions.front().position, QPointF(0.10, 0));
    QCOMPARE(actions.back().position, QPointF(0.01, 0));
}

void UiComponentsTest::recentActionRowShowsNameAndCoordinates()
{
    LegendPanel panel;

    auto* title = panel.findChild<QLabel*>("legend-title");
    QVERIFY(title != nullptr);
    QCOMPARE(title->text(), QStringLiteral("Legend"));

    auto* header = panel.findChild<QLabel*>("recent-actions-header");
    QVERIFY(header != nullptr);
    QCOMPARE(header->text(), QStringLiteral("Recent Actions"));

    auto* empty = panel.findChild<QLabel*>("recent-empty-label");
    QVERIFY(empty != nullptr);
    QCOMPARE(empty->text(), QStringLiteral("No actions yet"));
    QVERIFY(empty->isVisibleTo(&panel));

    auto* list = panel.findChild<QListWidget*>("recent-actions-list");
    QVERIFY(list != nullptr);
    QCOMPARE(list->count(), 0);

    panel.addRecentAction(InputAction::LeftClick, QPointF(0.25, -0.5));

    QCOMPARE(list->count(), 1);
    QCOMPARE(list->item(0)->text(), QStringLiteral("Left Click (0.25, -0.50)"));
    QVERIFY(!empty->isVisibleTo(&panel));

    auto* button = panel.findChild<QPushButton*>("clear-markers-button");
    QVERIFY(button != nullptr);
    QCOMPARE(button->text(), QStringLiteral("Clear Markers"));
}

void UiComponentsTest::canvasEmitsMarkerAddedWithNormalizedPosition()
{
    TargetCanvas canvas;
    canvas.resize(400, 400);
    canvas.show();
    QVERIFY(QTest::qWaitForWindowExposed(&canvas));
    QSignalSpy spy(&canvas, &TargetCanvas::markerAdded);

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
    const auto arguments = spy.takeFirst();
    QCOMPARE(qvariant_cast<InputAction>(arguments.at(0)), InputAction::WheelUp);
    const QPointF position = qvariant_cast<QPointF>(arguments.at(1));
    QCOMPARE(position, QPointF(-0.5, -0.5));
    QCOMPARE(canvas.markers().front().normalizedPosition, position);
}

void UiComponentsTest::clearEmptiesMarkersAndRecentList()
{
    MainWindow window;
    window.resize(800, 500);
    window.show();
    QVERIFY(QTest::qWaitForWindowExposed(&window));

    auto* canvas = window.findChild<TargetCanvas*>("target-canvas");
    auto* button = window.findChild<QPushButton*>("clear-markers-button");
    auto* panel = window.findChild<LegendPanel*>("legend-panel");
    QVERIFY(canvas != nullptr);
    QVERIFY(button != nullptr);
    QVERIFY(panel != nullptr);

    QTest::mouseClick(canvas, Qt::LeftButton, Qt::NoModifier, QPoint(100, 100));
    QTest::qWait(QApplication::doubleClickInterval() + 100);
    QCOMPARE(canvas->markers().size(), std::size_t(1));
    QCOMPARE(panel->recentActions().size(), 1);
    QCOMPARE(panel->recentActions().front().action, InputAction::LeftClick);

    QTest::mouseClick(button, Qt::LeftButton);
    QVERIFY(canvas->markers().empty());
    QVERIFY(panel->recentActions().empty());
}

QTEST_MAIN(UiComponentsTest)
#include "test_ui_components.moc"
