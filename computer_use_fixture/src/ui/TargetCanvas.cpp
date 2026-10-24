#include "ui/TargetCanvas.h"

#include <array>
#include <algorithm>

#include <QEvent>
#include <QMouseEvent>
#include <QPainter>
#include <QPaintEvent>
#include <QWheelEvent>

#include "geometry/TargetGeometry.h"
#include "input/InputGestureRecognizer.h"
#include "model/LegendStyle.h"

namespace {
constexpr int MarkerDiameter = 16;
constexpr int MaxMarkers = 5000;
}

TargetCanvas::TargetCanvas(QWidget* parent)
    : QWidget(parent)
{
    setContextMenuPolicy(Qt::NoContextMenu);
    setMinimumSize(320, 320);
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);

    recognizer_ = new InputGestureRecognizer(this);
    connect(recognizer_, &InputGestureRecognizer::actionConfirmed, this, [this](InputAction action, QPointF position) {
        addMarker(action, position);
        emit actionConfirmed(action, position);
    });
}

const std::deque<Marker>& TargetCanvas::markers() const
{
    return markers_;
}

void TargetCanvas::clearMarkers()
{
    markers_.clear();
    nextSequence_ = 1;
    update();
}

void TargetCanvas::resetInput()
{
    recognizer_->reset();
}

TargetGeometry TargetCanvas::currentGeometry() const
{
    return TargetGeometry(QRectF(rect()));
}

void TargetCanvas::addMarker(InputAction action, const QPointF& widgetPosition)
{
    const TargetGeometry geometry = currentGeometry();
    if (!geometry.contains(widgetPosition)) {
        return;
    }

    const QPointF normalized = geometry.toNormalized(widgetPosition);
    markers_.push_back(Marker{action, normalized, nextSequence_++});
    while (markers_.size() > MaxMarkers) {
        markers_.pop_front();
    }

    emit markerAdded(action, normalized);
    update();
}

void TargetCanvas::paintEvent(QPaintEvent* event)
{
    Q_UNUSED(event)
    QPainter painter(this);
    painter.setRenderHint(QPainter::Antialiasing, true);

    const TargetGeometry geometry = currentGeometry();
    drawTarget(painter, geometry);
    for (const Marker& marker : markers_) {
        drawMarker(painter, geometry, marker);
    }
}

void TargetCanvas::mousePressEvent(QMouseEvent* event)
{
    recognizer_->processMousePress(event, currentGeometry());
}

void TargetCanvas::mouseReleaseEvent(QMouseEvent* event)
{
    recognizer_->processMouseRelease(event, currentGeometry());
}

void TargetCanvas::mouseDoubleClickEvent(QMouseEvent* event)
{
    recognizer_->processMouseDoubleClick(event, currentGeometry());
}

void TargetCanvas::wheelEvent(QWheelEvent* event)
{
    recognizer_->processWheel(event, currentGeometry());
}

void TargetCanvas::leaveEvent(QEvent* event)
{
    recognizer_->clearWheelRemainder();
    QWidget::leaveEvent(event);
}

void TargetCanvas::hideEvent(QHideEvent* event)
{
    recognizer_->reset();
    QWidget::hideEvent(event);
}

void TargetCanvas::changeEvent(QEvent* event)
{
    if (event->type() == QEvent::EnabledChange && !isEnabled()) {
        recognizer_->reset();
    }
    QWidget::changeEvent(event);
}

void TargetCanvas::drawTarget(QPainter& painter, const TargetGeometry& geometry) const
{
    painter.fillRect(rect(), Qt::white);
    if (geometry.radius() <= 0) {
        return;
    }

    static const std::array<QColor, 10> ringColors = {
        QColor(Qt::white), QColor(Qt::white),
        QColor(Qt::black), QColor(Qt::black),
        QColor(QStringLiteral("#2563EB")), QColor(QStringLiteral("#2563EB")),
        QColor(QStringLiteral("#DC2626")), QColor(QStringLiteral("#DC2626")),
        QColor(QStringLiteral("#FACC15")), QColor(QStringLiteral("#FACC15"))
    };

    for (int index = 0; index < 10; ++index) {
        const qreal radius = geometry.radius() * (10 - index) / 10;
        painter.setPen(QPen(Qt::black, 1));
        painter.setBrush(ringColors.at(index));
        painter.drawEllipse(geometry.center(), radius, radius);
    }

    painter.setPen(QPen(Qt::white, 2));
    painter.setBrush(Qt::NoBrush);
    painter.drawEllipse(geometry.center(), geometry.radius() * 0.7, geometry.radius() * 0.7);
}

void TargetCanvas::drawMarker(QPainter& painter, const TargetGeometry& geometry, const Marker& marker) const
{
    const QPointF position = geometry.fromNormalized(marker.normalizedPosition);
    const QRectF rect(
        position.x() - MarkerDiameter / 2,
        position.y() - MarkerDiameter / 2,
        MarkerDiameter,
        MarkerDiameter
    );
    const LegendStyle& style = legendStyle(marker.action);

    painter.save();
    painter.setPen(QPen(Qt::white, 4, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    painter.setBrush(style.color);
    drawMarkerShape(painter, style.shape, rect);
    painter.setPen(QPen(Qt::black, 1));
    painter.setBrush(style.color);
    drawMarkerShape(painter, style.shape, rect);
    painter.restore();
}
