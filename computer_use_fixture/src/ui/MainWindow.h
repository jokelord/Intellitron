#pragma once

#include <QWidget>

class TargetCanvas;

class MainWindow : public QWidget
{
    Q_OBJECT

public:
    explicit MainWindow(QWidget* parent = nullptr);

private:
    TargetCanvas* canvas_ = nullptr;
};
