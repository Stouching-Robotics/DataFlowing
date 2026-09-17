cmake_minimum_required(VERSION 3.16)
project(fayssense_orb_slam_opencv48 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type")
set(KSQ_FAYS_BINARY_SUFFIX "" CACHE STRING
    "Optional output suffix for an isolated Fays bridge candidate")

get_filename_component(KSQ_ROOT "${CMAKE_CURRENT_LIST_DIR}/../.." ABSOLUTE)
set(FAYS_SDK_ROOT "${KSQ_ROOT}/FaysSense_VI_Kit_Release" CACHE PATH
    "FaysSense VI Kit root")

set(_DEFAULT_ORB_ROOT "$ENV{KSQ_ORB_ROOT}")
if(NOT _DEFAULT_ORB_ROOT)
    set(_DEFAULT_ORB_ROOT "${KSQ_ROOT}/ORB-SLAM")
endif()
set(ORB_ROOT "${_DEFAULT_ORB_ROOT}" CACHE PATH
    "ORB-SLAM3 root built against OpenCV 4.8")
set(OpenCV_DIR "/usr/local/lib/cmake/opencv4" CACHE PATH
    "OpenCV 4.8 CMake package")

find_package(OpenCV 4.8 REQUIRED COMPONENTS
    core imgproc imgcodecs highgui calib3d features2d flann)
# 4.8.x 通吃：ABI 由 SONAME libopencv_*.so.408 决定，运行时实际加载的库来自
# core/gripper/native/FaysSense_VI_Kit_Release/thirdparty/orb48_env/lib（见
# core/gripper/paths.py 的注入）。构建机上只有 miniconda 的 4.8.1 开发文件，
# 而现役二进制是 4.8.0 编的，两者可互换，故这里不再钉死最后一位。
if(NOT OpenCV_VERSION VERSION_LESS "4.9")
    message(FATAL_ERROR "This isolated target requires OpenCV 4.8.x, found ${OpenCV_VERSION}")
endif()
if(OpenCV_VERSION VERSION_LESS "4.8")
    message(FATAL_ERROR "This isolated target requires OpenCV 4.8.x, found ${OpenCV_VERSION}")
endif()
find_package(Eigen3 3.1.0 REQUIRED)
find_package(Pangolin REQUIRED)
find_package(Threads REQUIRED)

set(ARCH "${CMAKE_SYSTEM_PROCESSOR}")
set(FAYS_LIB_DIR "${FAYS_SDK_ROOT}/lib/fays_atrak/${ARCH}/Release")
set(FT602_DIR "${FAYS_SDK_ROOT}/thirdparty/ft602-linux-${ARCH}")

find_library(FAYS_VIKIT_LIBRARY NAMES fays_vikit
    PATHS "${FAYS_LIB_DIR}" NO_DEFAULT_PATH REQUIRED)
find_library(FT602_LIBRARY NAMES ft602
    PATHS "${FT602_DIR}" NO_DEFAULT_PATH REQUIRED)
set(KSQ_ORB_LIBRARY_PATH "" CACHE FILEPATH
    "Exact isolated libORB_SLAM3.so to link; empty uses ORB_ROOT/lib")
if(KSQ_ORB_LIBRARY_PATH)
    if(NOT EXISTS "${KSQ_ORB_LIBRARY_PATH}")
        message(FATAL_ERROR
            "KSQ_ORB_LIBRARY_PATH does not exist: ${KSQ_ORB_LIBRARY_PATH}")
    endif()
    set(ORB_SLAM3_LIBRARY "${KSQ_ORB_LIBRARY_PATH}")
else()
    find_library(ORB_SLAM3_LIBRARY NAMES ORB_SLAM3
        PATHS "${ORB_ROOT}/lib" NO_DEFAULT_PATH REQUIRED)
endif()
get_filename_component(ORB_SLAM3_LIBRARY_DIR
    "${ORB_SLAM3_LIBRARY}" DIRECTORY)

set(FAYS_BRIDGE_SOURCE
    "${KSQ_ROOT}/ORB-SLAM/Examples/fays/fayssense_orb_slam.cc"
    CACHE FILEPATH
    "Fays bridge source; override to build from an isolated ORB tree")
if(NOT EXISTS "${FAYS_BRIDGE_SOURCE}")
    message(FATAL_ERROR "Missing Fays bridge source: ${FAYS_BRIDGE_SOURCE}")
endif()

add_executable(fayssense_orb_slam_opencv48 "${FAYS_BRIDGE_SOURCE}")
target_include_directories(fayssense_orb_slam_opencv48 PRIVATE
    "${FAYS_SDK_ROOT}/include"
    "${FT602_DIR}"
    "${ORB_ROOT}"
    "${ORB_ROOT}/include"
    "${ORB_ROOT}/include/CameraModels"
    "${ORB_ROOT}/Thirdparty/Sophus"
    ${OpenCV_INCLUDE_DIRS}
    ${Pangolin_INCLUDE_DIRS}
)
target_link_libraries(fayssense_orb_slam_opencv48 PRIVATE
    "${FAYS_VIKIT_LIBRARY}"
    ${OpenCV_LIBS}
    "${FT602_LIBRARY}"
    "${ORB_SLAM3_LIBRARY}"
    Eigen3::Eigen
    ${Pangolin_LIBRARIES}
    Threads::Threads
)

# Keep this additive binary self-contained with respect to library discovery.
# --disable-new-dtags produces DT_RPATH, which also applies to transitive
# dependencies such as DBoW2 and g2o.
set_target_properties(fayssense_orb_slam_opencv48 PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}/bin"
    BUILD_RPATH "/usr/local/lib;${FAYS_LIB_DIR};${FT602_DIR};${ORB_SLAM3_LIBRARY_DIR};${ORB_ROOT}/Thirdparty/DBoW2/lib;${ORB_ROOT}/Thirdparty/g2o/lib"
    INSTALL_RPATH "/usr/local/lib;${FAYS_LIB_DIR};${FT602_DIR};${ORB_SLAM3_LIBRARY_DIR};${ORB_ROOT}/Thirdparty/DBoW2/lib;${ORB_ROOT}/Thirdparty/g2o/lib"
    LINK_FLAGS "-Wl,--disable-new-dtags"
)

# Read-only field diagnostic.  It opens only the serial-matched S80M SDK
# configuration and reports stationary raw IMU bias/noise; no ORB map is made.
add_executable(fays_imu_static_bias
    "${KSQ_ROOT}/ORB-SLAM/Examples/fays/fays_imu_static_bias.cc")
target_include_directories(fays_imu_static_bias PRIVATE
    "${FAYS_SDK_ROOT}/include"
    "${FT602_DIR}"
)
target_link_libraries(fays_imu_static_bias PRIVATE
    "${FAYS_VIKIT_LIBRARY}"
    ${OpenCV_LIBS}
    "${FT602_LIBRARY}"
    Threads::Threads
)
set_target_properties(fays_imu_static_bias PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}/bin"
    BUILD_RPATH "${FAYS_LIB_DIR};${FT602_DIR}"
    INSTALL_RPATH "${FAYS_LIB_DIR};${FT602_DIR}"
    LINK_FLAGS "-Wl,--disable-new-dtags"
)

message(STATUS "Fays SDK: ${FAYS_SDK_ROOT}")
message(STATUS "ORB-SLAM3: ${ORB_SLAM3_LIBRARY}")
message(STATUS "OpenCV: ${OpenCV_VERSION} (${OpenCV_DIR})")

# Diagnostic-only build of the SDK's instrumented bridge.  It reports frame
# interval, TrackStereo time and IMU samples per frame without changing the
# vendor source file.
add_executable(fayssense_orb_slam_sdkdiag_opencv48
    "${FAYS_SDK_ROOT}/orb_slam/fayssense_orb_slam.cc")
target_include_directories(fayssense_orb_slam_sdkdiag_opencv48 PRIVATE
    "${FAYS_SDK_ROOT}/include"
    "${FT602_DIR}"
    "${ORB_ROOT}"
    "${ORB_ROOT}/include"
    "${ORB_ROOT}/include/CameraModels"
    "${ORB_ROOT}/Thirdparty/Sophus"
    ${OpenCV_INCLUDE_DIRS}
    ${Pangolin_INCLUDE_DIRS}
)
target_link_libraries(fayssense_orb_slam_sdkdiag_opencv48 PRIVATE
    "${FAYS_VIKIT_LIBRARY}"
    ${OpenCV_LIBS}
    "${FT602_LIBRARY}"
    "${ORB_SLAM3_LIBRARY}"
    Eigen3::Eigen
    ${Pangolin_LIBRARIES}
    Threads::Threads
)
set_target_properties(fayssense_orb_slam_sdkdiag_opencv48 PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}/bin"
    BUILD_RPATH "/usr/local/lib;${FAYS_LIB_DIR};${FT602_DIR};${ORB_SLAM3_LIBRARY_DIR};${ORB_ROOT}/Thirdparty/DBoW2/lib;${ORB_ROOT}/Thirdparty/g2o/lib"
    INSTALL_RPATH "/usr/local/lib;${FAYS_LIB_DIR};${FT602_DIR};${ORB_SLAM3_LIBRARY_DIR};${ORB_ROOT}/Thirdparty/DBoW2/lib;${ORB_ROOT}/Thirdparty/g2o/lib"
    LINK_FLAGS "-Wl,--disable-new-dtags"
)

add_executable(dump_calib_opencv48
    "${FAYS_SDK_ROOT}/tools/dump_calib.cpp")
target_include_directories(dump_calib_opencv48 PRIVATE
    "${FAYS_SDK_ROOT}/include"
    ${OpenCV_INCLUDE_DIRS}
)
target_link_libraries(dump_calib_opencv48 PRIVATE
    "${FAYS_VIKIT_LIBRARY}"
    ${OpenCV_LIBS}
    "${FT602_LIBRARY}"
    Threads::Threads
)
set_target_properties(dump_calib_opencv48 PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}/bin"
    BUILD_RPATH "/usr/local/lib;${FAYS_LIB_DIR};${FT602_DIR}"
    INSTALL_RPATH "/usr/local/lib;${FAYS_LIB_DIR};${FT602_DIR}"
    LINK_FLAGS "-Wl,--disable-new-dtags"
)

# Serial-specific build for SN198. SDK 3.5.2 leaves its camera masks at zero;
# the bridge therefore calls the official FAYS_VIK_DumpCalib at every startup
# and combines that live camera dump with the IMU model returned by the API.
add_executable(fayssense_orb_slam_sn198_opencv48 "${FAYS_BRIDGE_SOURCE}")
target_compile_definitions(fayssense_orb_slam_sn198_opencv48 PRIVATE
    KSQ_FAYS_CALIBRATION_SERIAL=\"3500000261980088\")
target_include_directories(fayssense_orb_slam_sn198_opencv48 PRIVATE
    "${FAYS_SDK_ROOT}/include"
    "${FT602_DIR}"
    "${ORB_ROOT}"
    "${ORB_ROOT}/include"
    "${ORB_ROOT}/include/CameraModels"
    "${ORB_ROOT}/Thirdparty/Sophus"
    ${OpenCV_INCLUDE_DIRS}
    ${Pangolin_INCLUDE_DIRS}
)
target_link_libraries(fayssense_orb_slam_sn198_opencv48 PRIVATE
    "${FAYS_VIKIT_LIBRARY}"
    ${OpenCV_LIBS}
    "${FT602_LIBRARY}"
    "${ORB_SLAM3_LIBRARY}"
    Eigen3::Eigen
    ${Pangolin_LIBRARIES}
    Threads::Threads
)
set_target_properties(fayssense_orb_slam_sn198_opencv48 PROPERTIES
    OUTPUT_NAME "fayssense_orb_slam_sn198_opencv48${KSQ_FAYS_BINARY_SUFFIX}"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}/bin"
    BUILD_RPATH "/usr/local/lib;${FAYS_LIB_DIR};${FT602_DIR};${ORB_SLAM3_LIBRARY_DIR};${ORB_ROOT}/Thirdparty/DBoW2/lib;${ORB_ROOT}/Thirdparty/g2o/lib"
    INSTALL_RPATH "/usr/local/lib;${FAYS_LIB_DIR};${FT602_DIR};${ORB_SLAM3_LIBRARY_DIR};${ORB_ROOT}/Thirdparty/DBoW2/lib;${ORB_ROOT}/Thirdparty/g2o/lib"
    LINK_FLAGS "-Wl,--disable-new-dtags"
)

# Production build for the S80M currently paired with this upper computer.
# Runtime rectification and ORB calibration come from the serial-matched SDK
# factory data; the matching ORB YAML is only the non-calibration template.
add_executable(fayssense_orb_slam_sn187_opencv48 "${FAYS_BRIDGE_SOURCE}")
target_compile_definitions(fayssense_orb_slam_sn187_opencv48 PRIVATE
    KSQ_FAYS_CALIBRATION_SERIAL=\"3500000261870088\")
target_include_directories(fayssense_orb_slam_sn187_opencv48 PRIVATE
    "${FAYS_SDK_ROOT}/include"
    "${FT602_DIR}"
    "${ORB_ROOT}"
    "${ORB_ROOT}/include"
    "${ORB_ROOT}/include/CameraModels"
    "${ORB_ROOT}/Thirdparty/Sophus"
    ${OpenCV_INCLUDE_DIRS}
    ${Pangolin_INCLUDE_DIRS}
)
target_link_libraries(fayssense_orb_slam_sn187_opencv48 PRIVATE
    "${FAYS_VIKIT_LIBRARY}"
    ${OpenCV_LIBS}
    "${FT602_LIBRARY}"
    "${ORB_SLAM3_LIBRARY}"
    Eigen3::Eigen
    ${Pangolin_LIBRARIES}
    Threads::Threads
)
set_target_properties(fayssense_orb_slam_sn187_opencv48 PROPERTIES
    OUTPUT_NAME "fayssense_orb_slam_sn187_opencv48${KSQ_FAYS_BINARY_SUFFIX}"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}/bin"
    BUILD_RPATH "/usr/local/lib;${FAYS_LIB_DIR};${FT602_DIR};${ORB_SLAM3_LIBRARY_DIR};${ORB_ROOT}/Thirdparty/DBoW2/lib;${ORB_ROOT}/Thirdparty/g2o/lib"
    INSTALL_RPATH "/usr/local/lib;${FAYS_LIB_DIR};${FT602_DIR};${ORB_SLAM3_LIBRARY_DIR};${ORB_ROOT}/Thirdparty/DBoW2/lib;${ORB_ROOT}/Thirdparty/g2o/lib"
    LINK_FLAGS "-Wl,--disable-new-dtags"
)

# TEMPORARY FIELD-TEST TARGET for S80C SN219. This is not a production
# supported camera and must be removed after the current test session.
add_executable(fayssense_orb_slam_sn219_opencv48 "${FAYS_BRIDGE_SOURCE}")
target_compile_definitions(fayssense_orb_slam_sn219_opencv48 PRIVATE
    KSQ_FAYS_CALIBRATION_SERIAL=\"3500000262190088\")
target_include_directories(fayssense_orb_slam_sn219_opencv48 PRIVATE
    "${FAYS_SDK_ROOT}/include"
    "${FT602_DIR}"
    "${ORB_ROOT}"
    "${ORB_ROOT}/include"
    "${ORB_ROOT}/include/CameraModels"
    "${ORB_ROOT}/Thirdparty/Sophus"
    ${OpenCV_INCLUDE_DIRS}
    ${Pangolin_INCLUDE_DIRS}
)
target_link_libraries(fayssense_orb_slam_sn219_opencv48 PRIVATE
    "${FAYS_VIKIT_LIBRARY}"
    ${OpenCV_LIBS}
    "${FT602_LIBRARY}"
    "${ORB_SLAM3_LIBRARY}"
    Eigen3::Eigen
    ${Pangolin_LIBRARIES}
    Threads::Threads
)
set_target_properties(fayssense_orb_slam_sn219_opencv48 PROPERTIES
    OUTPUT_NAME "fayssense_orb_slam_sn219_opencv48${KSQ_FAYS_BINARY_SUFFIX}"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}/bin"
    BUILD_RPATH "/usr/local/lib;${FAYS_LIB_DIR};${FT602_DIR};${ORB_SLAM3_LIBRARY_DIR};${ORB_ROOT}/Thirdparty/DBoW2/lib;${ORB_ROOT}/Thirdparty/g2o/lib"
    INSTALL_RPATH "/usr/local/lib;${FAYS_LIB_DIR};${FT602_DIR};${ORB_SLAM3_LIBRARY_DIR};${ORB_ROOT}/Thirdparty/DBoW2/lib;${ORB_ROOT}/Thirdparty/g2o/lib"
    LINK_FLAGS "-Wl,--disable-new-dtags"
)

# VI Kit identity/calibration probe shared by discovery and ORB setup. This
# uses the camera SDK only; no separate localization client/service is needed.
add_executable(fays_vikit_calibration_probe
    "${KSQ_ROOT}/ORB-SLAM/Examples/fays/fays_vikit_calibration_probe.cpp")
target_include_directories(fays_vikit_calibration_probe PRIVATE
    "${FAYS_SDK_ROOT}/include" "${FT602_DIR}")
# Match the vendor probe's existing OpenCV 4.2 ABI, independently of the
# OpenCV 4.8 ORB bridge built by the other targets in this file.
set(FAYS_PROBE_OPENCV_DIR "${FAYS_SDK_ROOT}/thirdparty/opencv-4.2.0-linux-${ARCH}/lib")
find_library(FAYS_PROBE_OPENCV_CORE NAMES opencv_core
    PATHS "${FAYS_PROBE_OPENCV_DIR}" NO_DEFAULT_PATH REQUIRED)
find_library(FAYS_PROBE_OPENCV_IMGPROC NAMES opencv_imgproc
    PATHS "${FAYS_PROBE_OPENCV_DIR}" NO_DEFAULT_PATH REQUIRED)
target_link_libraries(fays_vikit_calibration_probe PRIVATE
    "${FAYS_VIKIT_LIBRARY}" "-Wl,--no-as-needed"
    "${FAYS_PROBE_OPENCV_IMGPROC}" "${FAYS_PROBE_OPENCV_CORE}"
    "-Wl,--as-needed" "${FT602_LIBRARY}" Threads::Threads)
set_target_properties(fays_vikit_calibration_probe PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_LIST_DIR}/bin"
    BUILD_RPATH "${FAYS_LIB_DIR};${FT602_DIR};${FAYS_SDK_ROOT}/thirdparty/opencv-4.2.0-linux-${ARCH}/lib"
    INSTALL_RPATH "${FAYS_LIB_DIR};${FT602_DIR};${FAYS_SDK_ROOT}/thirdparty/opencv-4.2.0-linux-${ARCH}/lib"
    LINK_FLAGS "-Wl,--disable-new-dtags")

# Keep checkout-owned library paths relocatable for all production binaries.
# Explicit external SDK/core overrides remain absolute build dependencies.
get_property(_ksq_targets DIRECTORY PROPERTY BUILDSYSTEM_TARGETS)
foreach(_target IN LISTS _ksq_targets)
    get_target_property(_type "${_target}" TYPE)
    if(NOT _type STREQUAL "EXECUTABLE")
        continue()
    endif()
    get_target_property(_paths "${_target}" BUILD_RPATH)
    if(NOT _paths)
        continue()
    endif()
    set(_portable_paths "")
    foreach(_path IN LISTS _paths)
        file(RELATIVE_PATH _from_root "${KSQ_ROOT}" "${_path}")
        if(NOT _from_root MATCHES "^\\.\\./" AND NOT IS_ABSOLUTE "${_from_root}")
            file(RELATIVE_PATH _relative "${CMAKE_CURRENT_LIST_DIR}/bin" "${_path}")
            list(APPEND _portable_paths "$ORIGIN/${_relative}")
        else()
            list(APPEND _portable_paths "${_path}")
        endif()
    endforeach()
    set_target_properties("${_target}" PROPERTIES
        BUILD_RPATH "${_portable_paths}"
        INSTALL_RPATH "${_portable_paths}"
        BUILD_WITH_INSTALL_RPATH TRUE)
endforeach()
