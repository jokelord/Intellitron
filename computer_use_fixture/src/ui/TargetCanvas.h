#pragma once

#include <deque>

#include <QEvent>
#include <QPainter>
#include <QWidget>

#include "model/Marker.h"

class InputGestureRecognizer;
class QHideEvent;
class QMouseEvent;
class QPaintEvent;
class QWheelEvent;
class TargetGeometry;

class TargetCanvas : public QWidget
{
    Q_OBJECT

public:
    explicit TargetCanvas(QWidget* parent = nullptr);

    const std::deque<Marker>& markers() const;
    void clearMarkers();
    void resetInput();

signals:
    void actionConfirmed(InputAction action, QPointF widgetPosition);
    void markerAdded(InputAction action, QPointF normalizedPosition);

protected:
    void paintEvent(QPaintEvent* event) override;
    void mousePressEvent(QMouseEvent* event) override;
    void mouseReleaseEvent(QMouseEvent* event) override;
    void mouseDoubleClickEvent(QMouseEvent* event) override;
    void wheelEvent(QWheelEvent* event) override;
    void leaveEvent(QEvent* event) override;
    void hideEvent(QHideEvent* event) override;
    void changeEvent(QEvent* event) override;

private:
    TargetGeometry currentGeometry() const;
    void addMarker(InputAction action, const QPointF& widgetPosition);
    void drawTarget(QPainter& painter, const TargetGeometry& geometry) const;
    void drawMarker(QPainter& painter, const TargetGeometry& geometry, const Marker& marker) const;

    std::deque<Marker> markers_;
    std::uint64_t nextSequence_ = 1;
    InputGestureRecognizer* recognizer_ = nullptr;
};
