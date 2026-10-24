#pragma once

#include <QMetaType>

enum class InputAction
{
    LeftClick,
    LeftDoubleClick,
    RightClick,
    RightDoubleClick,
    WheelUp,
    WheelDown
};

Q_DECLARE_METATYPE(InputAction)
