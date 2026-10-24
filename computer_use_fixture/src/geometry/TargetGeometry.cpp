#include "geometry/TargetGeometry.h"

#include <algorithm>

#include <QLineF>

TargetGeometry::TargetGeometry(const QRectF& canvasRect)
    : canvasRect_(canvasRect)
{
}

QPointF TargetGeometry::center() const
{
    return canvasRect_.center();
}

qreal TargetGeometry::radius() const
{
    return std::max<qreal>(0, std::min(canvasRect_.width(), canvasRect_.height()) / 2);
}

bool TargetGeometry::contains(const QPointF& point) const
{
    if (radius() <= 0) {
        return false;
    }
    return QLineF(center(), point).length() <= radius();
}

QPointF TargetGeometry::toNormalized(const QPointF& point) const
{
    if (radius() <= 0) {
        return {};
    }

    return QPointF(
        (point.x() - center().x()) / radius(),
        (point.y() - center().y()) / radius()
    );
}

QPointF TargetGeometry::fromNormalized(const QPointF& normalizedPoint) const
{
    if (radius() <= 0) {
        return center();
    }

    return QPointF(
        center().x() + normalizedPoint.x() * radius(),
        center().y() + normalizedPoint.y() * radius()
    );
}
