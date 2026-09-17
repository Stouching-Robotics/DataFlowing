cmake_minimum_required(VERSION 3.12)
project(fayssense_orb_slam)

set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(SDK_ROOT "$ENV{HOME}/FaysSense_VI_Kit_Release")
set(ORB_ROOT "$ENV{HOME}/ORB-SLAM")
set(ARCH "${CMAKE_SYSTEM_PROCESSOR}")

# SDK 头文件
include_directories(${SDK_ROOT}/include)

# SDK OpenCV 4.2.0 (与 libfays_vikit.so 一致)
set(OPENCVDIR "${SDK_ROOT}/thirdparty/opencv-4.2.0-linux-${ARCH}")
set(OpenCV_DIR "${OPENCVDIR}/lib/cmake/opencv4")
find_package(OpenCV REQUIRED)
message(STATUS "OpenCV: ${OpenCV_VERSION} (SDK bundled)")

# FaysSense SDK
set(FAYS_LIB_DIR "${SDK_ROOT}/lib/fays_atrak/${ARCH}/Release")
find_library(FAYS_VIKIT_LIB NAMES fays_vikit PATHS ${FAYS_LIB_DIR} NO_DEFAULT_PATH REQUIRED)

if(CMAKE_HOST_SYSTEM_PROCESSOR MATCHES "x86_64")
    set(FTDPATH "${SDK_ROOT}/thirdparty/ft602-linux-x86_64")
else()
    set(FTDPATH "${SDK_ROOT}/thirdparty/ft602-linux-aarch64")
endif()
find_library(FTD3XX_LIBRARY ft602 PATHS ${FTDPATH} REQUIRED)
include_directories(${FTDPATH})
find_package(Threads REQUIRED)

# ORB-SLAM3
find_library(ORB_SLAM3_LIB NAMES ORB_SLAM3 PATHS ${ORB_ROOT}/lib NO_DEFAULT_PATH REQUIRED)
find_package(Eigen3 3.1.0 REQUIRED)
find_package(Pangolin REQUIRED)
include_directories(${ORB_ROOT} ${ORB_ROOT}/include ${ORB_ROOT}/include/CameraModels
    ${ORB_ROOT}/Thirdparty/Sophus ${EIGEN3_INCLUDE_DIR} ${Pangolin_INCLUDE_DIRS})

# ============================================================================
# 单进程: 回调模式 → SDK 相机 + ORB-SLAM3
# ============================================================================
add_executable(fayssense_orb_slam fayssense_orb_slam.cc)
target_link_libraries(fayssense_orb_slam
    ${FAYS_VIKIT_LIB} ${OpenCV_LIBS} ${FTD3XX_LIBRARY}
    ${ORB_SLAM3_LIB} ${EIGEN3_LIBS} ${Pangolin_LIBRARIES} Threads::Threads)

set_target_properties(fayssense_orb_slam PROPERTIES
    BUILD_RPATH "${OPENCVDIR}/lib;${FAYS_LIB_DIR};${FTDPATH};${SDK_ROOT}/compat_libs;${ORB_ROOT}/lib"
    INSTALL_RPATH "${OPENCVDIR}/lib;${FAYS_LIB_DIR};${FTDPATH};${ORB_ROOT}/lib"
    LINK_FLAGS "-Wl,--disable-new-dtags")

message(STATUS "Target: fayssense_orb_slam (callback mode, single process)")

# ============================================================================
# 单目惯导: 仅左目 + IMU, IMU_MONOCULAR 模式
# ============================================================================
add_executable(fayssense_orb_slam_mono fayssense_orb_slam_mono.cc)
target_link_libraries(fayssense_orb_slam_mono
    ${FAYS_VIKIT_LIB} ${OpenCV_LIBS} ${FTD3XX_LIBRARY}
    ${ORB_SLAM3_LIB} ${EIGEN3_LIBS} ${Pangolin_LIBRARIES} Threads::Threads)

set_target_properties(fayssense_orb_slam_mono PROPERTIES
    BUILD_RPATH "${OPENCVDIR}/lib;${FAYS_LIB_DIR};${FTDPATH};${SDK_ROOT}/compat_libs;${ORB_ROOT}/lib"
    INSTALL_RPATH "${OPENCVDIR}/lib;${FAYS_LIB_DIR};${FTDPATH};${ORB_ROOT}/lib"
    LINK_FLAGS "-Wl,--disable-new-dtags")

message(STATUS "Target: fayssense_orb_slam_mono (mono-inertial, left-only)")

# ============================================================================
# 双目惯导 + AprilTag 重定位修正
# ============================================================================
add_executable(fayssense_orb_slam_april fayssense_orb_slam_april.cc)
target_link_libraries(fayssense_orb_slam_april
    ${FAYS_VIKIT_LIB} ${OpenCV_LIBS} ${FTD3XX_LIBRARY}
    ${ORB_SLAM3_LIB} ${EIGEN3_LIBS} ${Pangolin_LIBRARIES} Threads::Threads)

set_target_properties(fayssense_orb_slam_april PROPERTIES
    BUILD_RPATH "${OPENCVDIR}/lib;${FAYS_LIB_DIR};${FTDPATH};${SDK_ROOT}/compat_libs;${ORB_ROOT}/lib"
    INSTALL_RPATH "${OPENCVDIR}/lib;${FAYS_LIB_DIR};${FTDPATH};${ORB_ROOT}/lib"
    LINK_FLAGS "-Wl,--disable-new-dtags")

message(STATUS "Target: fayssense_orb_slam_april (stereo-inertial + AprilTag correction)")
