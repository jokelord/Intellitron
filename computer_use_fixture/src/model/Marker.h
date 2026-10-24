#pragma once

#include <cstdint>

#include <QPointF>

#include "model/InputAction.h"

struct Marker
{
    InputAction action;
    QPointF normalizedPosition;
    std::uint64_t sequence;
};
