add_executable(test_target_geometry
    test_target_geometry.cpp
    ../src/geometry/TargetGeometry.cpp
)

target_link_libraries(test_target_geometry PRIVATE Qt6::Widgets Qt6::Test)
add_test(NAME target_geometry COMMAND test_target_geometry)

add_executable(test_legend_style
    test_legend_style.cpp
    ../src/model/LegendStyle.cpp
)

target_link_libraries(test_legend_style PRIVATE Qt6::Widgets Qt6::Test)
add_test(NAME legend_style COMMAND test_legend_style)

add_executable(test_input_gestures
    test_input_gestures.cpp
    ../src/input/InputGestureRecognizer.cpp
    ../src/geometry/TargetGeometry.cpp
)

target_link_libraries(test_input_gestures PRIVATE Qt6::Widgets Qt6::Test)
add_test(NAME input_gestures COMMAND test_input_gestures)

add_executable(test_target_canvas
    test_target_canvas.cpp
    ../src/ui/TargetCanvas.cpp
    ../src/input/InputGestureRecognizer.cpp
    ../src/geometry/TargetGeometry.cpp
    ../src/model/LegendStyle.cpp
)

target_link_libraries(test_target_canvas PRIVATE Qt6::Widgets Qt6::Test)
add_test(NAME target_canvas COMMAND test_target_canvas)

add_executable(test_ui_components
    test_ui_components.cpp
    ../src/ui/LegendPanel.cpp
    ../src/ui/MainWindow.cpp
    ../src/ui/TargetCanvas.cpp
    ../src/input/InputGestureRecognizer.cpp
    ../src/geometry/TargetGeometry.cpp
    ../src/model/LegendStyle.cpp
)

target_link_libraries(test_ui_components PRIVATE Qt6::Widgets Qt6::Test)
add_test(NAME ui_components COMMAND test_ui_components)

add_executable(render_archery_test_frame
    render_archery_test_frame.cpp
    ../src/ui/LegendPanel.cpp
    ../src/ui/MainWindow.cpp
    ../src/ui/TargetCanvas.cpp
    ../src/input/InputGestureRecognizer.cpp
    ../src/geometry/TargetGeometry.cpp
    ../src/model/LegendStyle.cpp
)
target_link_libraries(render_archery_test_frame PRIVATE Qt6::Widgets Qt6::Test)
