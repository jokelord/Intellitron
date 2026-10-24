#include "model/LegendStyle.h"

#include <array>
#include <cstddef>
#include <stdexcept>

#include <QPainter>
#include <QPainterPath>
#include <QPolygonF>

const LegendStyle& legendStyle(InputAction action)
{
    static const std::array<LegendStyle, 6> styles = {
        LegendStyle{QStringLiteral("Left Click"), QColor(QStringLiteral("#2563EB")), MarkerShape::Circle},
        LegendStyle{QStringLiteral("Left Double Click"), QColor(QStringLiteral("#16A34A")), MarkerShape::Triangle},
        LegendStyle{QStringLiteral("Right Click"), QColor(QStringLiteral("#7C3AED")), MarkerShape::Square},
        LegendStyle{QStringLiteral("Right Double Click"), QColor(QStringLiteral("#EA580C")), MarkerShape::Diamond},
        LegendStyle{QStringLiteral("Wheel Up"), QColor(QStringLiteral("#0891B2")), MarkerShape::UpArrow},
        LegendStyle{QStringLiteral("Wheel Down"), QColor(QStringLiteral("#DC2626")), MarkerShape::DownArrow}
    };

    const auto index = static_cast<std::size_t>(action);
    if (index >= styles.size()) {
        throw std::out_of_range("Invalid InputAction");
    }
    return styles[index];
}

void drawMarkerShape(QPainter& painter, MarkerShape shape, const QRectF& rect)
{
    const QPointF center = rect.center();
    const qreal left = rect.left();
    const qreal right = rect.right();
    const qreal top = rect.top();
    const qreal bottom = rect.bottom();

    switch (shape) {
    case MarkerShape::Circle:
        painter.drawEllipse(rect);
        break;
    case MarkerShape::Triangle:
        painter.drawPolygon(QPolygonF{
            QPointF(center.x(), top),
            QPointF(right, bottom),
            QPointF(left, bottom)
        });
        break;
    case MarkerShape::Square:
        painter.drawRect(rect);
        break;
    case MarkerShape::Diamond:
        painter.drawPolygon(QPolygonF{
            QPointF(center.x(), top),
            QPointF(right, center.y()),
            QPointF(center.x(), bottom),
            QPointF(left, center.y())
        });
        break;
    case MarkerShape::UpArrow: {
        QPainterPath path;
        path.moveTo(left, center.y());
        path.lineTo(left, top);
        path.lineTo(center.x(), top);
        path.lineTo(center.x(), top - rect.height() * 0.25);
        path.lineTo(right, top);
        path.lineTo(right, center.y());
        path.lineTo(center.x() + rect.width() * 0.25, center.y());
        path.lineTo(center.x() + rect.width() * 0.25, bottom);
        path.lineTo(center.x() - rect.width() * 0.25, bottom);
        path.lineTo(center.x() - rect.width() * 0.25, center.y());
        path.closeSubpath();
        painter.drawPolygon(path.toFillPolygon());
        break;
    }
    case MarkerShape::DownArrow: {
        QPainterPath path;
        path.moveTo(left, center.y());
        path.lineTo(left, bottom);
        path.lineTo(center.x(), bottom);
        path.lineTo(center.x(), bottom + rect.height() * 0.25);
        path.lineTo(right, bottom);
        path.lineTo(right, center.y());
        path.lineTo(center.x() + rect.width() * 0.25, center.y());
        path.lineTo(center.x() + rect.width() * 0.25, top);
        path.lineTo(center.x() - rect.width() * 0.25, top);
        path.lineTo(center.x() - rect.width() * 0.25, center.y());
        path.closeSubpath();
        painter.drawPolygon(path.toFillPolygon());
        break;
    }
    }
}
