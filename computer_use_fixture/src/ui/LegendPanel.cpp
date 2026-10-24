#include "ui/LegendPanel.h"

#include <array>

#include <QFont>
#include <QIcon>
#include <QLabel>
#include <QListWidget>
#include <QPainter>
#include <QPaintEvent>
#include <QPixmap>
#include <QPushButton>
#include <QVBoxLayout>

#include "model/LegendStyle.h"

namespace {
constexpr int MaxRecentActions = 10;
}

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
    setFixedWidth(280);
    auto* layout = new QVBoxLayout(this);
    layout->setContentsMargins(12, 12, 12, 12);
    layout->setSpacing(10);

    auto* title = new QLabel(tr("Legend"), this);
    title->setObjectName(QStringLiteral("legend-title"));
    QFont titleFont = title->font();
    titleFont.setBold(true);
    title->setFont(titleFont);

    legendWidget_ = new LegendWidget(this);

    auto* recentHeader = new QLabel(tr("Recent Actions"), this);
    recentHeader->setObjectName(QStringLiteral("recent-actions-header"));
    QFont headerFont = recentHeader->font();
    headerFont.setBold(true);
    recentHeader->setFont(headerFont);

    recentList_ = new QListWidget(this);
    recentList_->setObjectName(QStringLiteral("recent-actions-list"));

    emptyLabel_ = new QLabel(tr("No actions yet"), this);
    emptyLabel_->setObjectName(QStringLiteral("recent-empty-label"));

    clearButton_ = new QPushButton(tr("Clear Markers"), this);
    clearButton_->setObjectName(QStringLiteral("clear-markers-button"));

    layout->addWidget(title);
    layout->addWidget(legendWidget_);
    layout->addWidget(recentHeader);
    layout->addWidget(recentList_);
    layout->addWidget(emptyLabel_);
    layout->addWidget(clearButton_);
    layout->addStretch(1);

    refreshRecentList();
}

QList<RecentAction> LegendPanel::recentActions() const
{
    return recentActions_;
}

void LegendPanel::addRecentAction(InputAction action, QPointF normalizedPosition)
{
    recentActions_.prepend(RecentAction{action, normalizedPosition});
    while (recentActions_.size() > MaxRecentActions) {
        recentActions_.removeLast();
    }
    refreshRecentList();
}

void LegendPanel::clearRecentActions()
{
    recentActions_.clear();
    refreshRecentList();
}

void LegendPanel::refreshRecentList()
{
    recentList_->clear();
    for (const RecentAction& recent : recentActions_) {
        const LegendStyle& style = legendStyle(recent.action);

        QPixmap swatch(20, 20);
        swatch.fill(Qt::transparent);
        QPainter painter(&swatch);
        painter.setRenderHint(QPainter::Antialiasing, true);
        painter.setPen(QPen(Qt::black, 1));
        painter.setBrush(style.color);
        drawMarkerShape(painter, style.shape, QRectF(2, 2, 16, 16));

        auto* item = new QListWidgetItem(QIcon(swatch), recentRowText(recent));
        recentList_->addItem(item);
    }
    emptyLabel_->setVisible(recentActions_.isEmpty());
}

QString LegendPanel::recentRowText(const RecentAction& recent)
{
    const LegendStyle& style = legendStyle(recent.action);
    return QStringLiteral("%1 (%2, %3)").arg(
        style.text,
        QString::number(recent.position.x(), 'f', 2),
        QString::number(recent.position.y(), 'f', 2));
}
