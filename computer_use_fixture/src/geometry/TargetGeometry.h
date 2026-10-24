#pragma once

#include <QPointF>
#include <QRectF>

class TargetGeometry
{
public:
    explicit TargetGeometry(const QRectF& canvasRect);

    QPointF center() const;
    qreal radius() const;

    bool contains(const QPointF& point) const;

    QPointF toNormalized(const QPointF& point) const;
    QPointF fromNormalized(const QPointF& normalizedPoint) const;

private:
    QRectF canvasRect_;
};
