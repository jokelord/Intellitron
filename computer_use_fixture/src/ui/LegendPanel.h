#pragma once

#include <QList>
#include <QPointF>
#include <QWidget>

#include "model/InputAction.h"

class QLabel;
class QListWidget;
class QPushButton;

struct RecentAction
{
    InputAction action;
    QPointF position;
};

class LegendPanel : public QWidget
{
    Q_OBJECT

public:
    explicit LegendPanel(QWidget* parent = nullptr);

    QList<RecentAction> recentActions() const;

public slots:
    void addRecentAction(InputAction action, QPointF normalizedPosition);
    void clearRecentActions();

private:
    class LegendWidget;

    void refreshRecentList();
    static QString recentRowText(const RecentAction& recent);

    LegendWidget* legendWidget_ = nullptr;
    QListWidget* recentList_ = nullptr;
    QLabel* emptyLabel_ = nullptr;
    QPushButton* clearButton_ = nullptr;
    QList<RecentAction> recentActions_;
};
