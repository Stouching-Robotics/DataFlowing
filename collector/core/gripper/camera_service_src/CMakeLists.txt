cmake_minimum_required(VERSION 3.16)
project(ksq_camera_service C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

set(BUILD_EXAMPLE OFF CACHE BOOL "" FORCE)
set(BUILD_TEST OFF CACHE BOOL "" FORCE)
set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)

add_subdirectory(third_party/libuvc)

find_package(Threads REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(LIBUSB REQUIRED IMPORTED_TARGET libusb-1.0)

add_executable(ksq-camera-service
    src/camera_service.c
)
target_include_directories(ksq-camera-service PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/third_party/libuvc/include
)
target_compile_definitions(ksq-camera-service PRIVATE
    _GNU_SOURCE
)
target_compile_options(ksq-camera-service PRIVATE
    -Wall -Wextra -Wpedantic
)
target_link_libraries(ksq-camera-service PRIVATE
    LibUVC::UVC
    PkgConfig::LIBUSB
    Threads::Threads
)

add_executable(sonix-flash-probe
    tools/sonix_flash_probe.c
)
target_compile_options(sonix-flash-probe PRIVATE
    -Wall -Wextra -Wpedantic
)
target_link_libraries(sonix-flash-probe PRIVATE
    PkgConfig::LIBUSB
)

add_executable(discover-uvc-config
    tools/discover_uvc_config.c
)
target_include_directories(discover-uvc-config PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/third_party/libuvc/include
)
target_compile_definitions(discover-uvc-config PRIVATE
    _GNU_SOURCE
)
target_compile_options(discover-uvc-config PRIVATE
    -Wall -Wextra -Wpedantic
)
target_link_libraries(discover-uvc-config PRIVATE
    LibUVC::UVC
    PkgConfig::LIBUSB
)

install(TARGETS ksq-camera-service RUNTIME DESTINATION bin)

# Hardware-free integrity regression tests for the production callback.
enable_testing()
add_executable(test-frame-integrity tests/test_frame_integrity.c)
target_compile_definitions(test-frame-integrity PRIVATE _GNU_SOURCE)
target_compile_options(test-frame-integrity PRIVATE -Wall -Wextra -Wpedantic)
target_link_libraries(test-frame-integrity PRIVATE
    LibUVC::UVC PkgConfig::LIBUSB Threads::Threads)
add_test(NAME frame-integrity COMMAND test-frame-integrity)

# The altsetting ladder decides which isochronous setting each camera is opened
# with, and its state-file key has to be derived identically in Python. Neither
# is observable without hardware, so pin both here.
add_executable(test-altsetting-ladder tests/test_altsetting_ladder.c)
target_compile_definitions(test-altsetting-ladder PRIVATE _GNU_SOURCE)
target_compile_options(test-altsetting-ladder PRIVATE -Wall -Wextra -Wpedantic)
target_link_libraries(test-altsetting-ladder PRIVATE
    LibUVC::UVC PkgConfig::LIBUSB Threads::Threads)
add_test(NAME altsetting-ladder COMMAND test-altsetting-ladder)

# Checkout deployment keeps the bundled libuvc next to these executables.
# Avoid CMake embedding the build machine's absolute checkout directory.
#
# CMake appends an empty element to this list for ksq-camera-service (it counts
# Threads::Threads as an empty link directory), which would leave the RUNPATH as
# "$ORIGIN/third_party/libuvc:" -- and an empty element means "also search the
# current working directory". build.sh strips that element after the link and
# fails the build if it survives; do not "fix" it here by hand, the per-target
# SKIP_BUILD_RPATH and global CMAKE_SKIP_RPATH routes both make it worse
# (CMake then emits the entire empty list).
foreach(_target ksq-camera-service discover-uvc-config test-frame-integrity
        test-altsetting-ladder)
    set_target_properties(${_target} PROPERTIES
        BUILD_RPATH "$ORIGIN/third_party/libuvc"
        INSTALL_RPATH "$ORIGIN/../lib"
        BUILD_RPATH_USE_ORIGIN TRUE)
endforeach()
