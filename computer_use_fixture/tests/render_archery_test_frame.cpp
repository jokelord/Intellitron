#include <QApplication>
#include <QBuffer>
#include <QImage>
#include <QPoint>
#include <QtTest>

#include <cstdio>

#include "ui/MainWindow.h"
#include "ui/TargetCanvas.h"

// Test-only offscreen render of the real Qt widget tree. No desktop capture.
int main(int argc, char* argv[])
{
    QApplication application(argc, argv);
    if (argc != 3) {
        return 2;
    }
    const int scale = QString::fromLocal8Bit(argv[1]).toInt();
    const bool marker = QString::fromLocal8Bit(argv[2]) == QStringLiteral("marker");
    if (scale != 1 && scale != 2) {
        return 2;
    }
    MainWindow window;
    window.show();
    application.processEvents();
    if (marker) {
        auto* canvas = window.findChild<TargetCanvas*>(QStringLiteral("target-canvas"));
        if (!canvas) {
            return 3;
        }
        QTest::mouseClick(canvas, Qt::LeftButton, Qt::NoModifier,
                          QPoint(canvas->width() / 2, canvas->height() / 2));
        QTest::qWait(QApplication::doubleClickInterval() + 100);
    }
    QImage image(window.width() * scale, window.height() * scale,
                 QImage::Format_RGB888);
    image.setDevicePixelRatio(scale);
    image.fill(Qt::white);
    window.render(&image);
    QByteArray png;
    QBuffer output(&png);
    if (!output.open(QIODevice::WriteOnly) || !image.save(&output, "PNG")) {
        return 4;
    }
    return std::fwrite(png.constData(), 1, png.size(), stdout) ==
                   static_cast<std::size_t>(png.size()) ? 0 : 5;
}
