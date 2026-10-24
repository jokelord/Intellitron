#pragma once

#include <QColor>
#include <QRectF>
#include <QString>

#include "model/InputAction.h"

class QPainter;

enum class MarkerShape
{
    Circle,
    Triangle,
    Square,
    Diamond,
    UpArrow,
    DownArrow
};

struct LegendStyle
{
    QString text;
    QColor color;
    MarkerShape shape;
};

const LegendStyle& legendStyle(InputAction action);
void drawMarkerShape(QPainter& painter, MarkerShape shape, const QRectF& rect);
