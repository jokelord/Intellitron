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

    connect(canvas_, &TargetCanvas::markerAdded,
        legendPanel, &LegendPanel::addRecentAction);
    auto* clearButton = legendPanel->findChild<QPushButton*>(QStringLiteral("clear-markers-button"));
    connect(clearButton, &QPushButton::clicked, canvas_, &TargetCanvas::clearMarkers);
    connect(clearButton, &QPushButton::clicked, legendPanel, &LegendPanel::clearRecentActions);
}
