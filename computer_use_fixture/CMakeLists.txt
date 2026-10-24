cmake_minimum_required(VERSION 3.22)

project(archery_input_target VERSION 1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 6.4 REQUIRED COMPONENTS Widgets Test)

include_directories("${CMAKE_CURRENT_SOURCE_DIR}/src")

file(GLOB_RECURSE APP_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp"
)

if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/src/main.cpp")
    add_executable(archery-input-target ${APP_SOURCES})
    target_link_libraries(archery-input-target PRIVATE Qt6::Widgets)
endif()

enable_testing()
add_subdirectory(tests)
