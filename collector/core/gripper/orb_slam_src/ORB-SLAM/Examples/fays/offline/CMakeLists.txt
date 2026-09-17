cmake_minimum_required(VERSION 3.16)
project(ksq_slam_offline LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
get_filename_component(KSQ_PROJECT_ROOT "${CMAKE_CURRENT_LIST_DIR}/../../../.." ABSOLUTE)
set(KSQ_ORB_ROOT "${KSQ_PROJECT_ROOT}/ORB-SLAM")
set(KSQ_ORB_LIBRARY "${KSQ_PROJECT_ROOT}/dist/orb_mark_only/lib/libORB_SLAM3.so" CACHE FILEPATH "Existing production ORB library to test")
find_package(OpenCV 4.8 REQUIRED COMPONENTS core imgproc imgcodecs features2d)
find_package(Eigen3 REQUIRED)
find_package(Threads REQUIRED)
find_package(Pangolin REQUIRED)
# Headers must match the existing core's Boost ABI. Nothing here rebuilds it.
find_package(Boost REQUIRED COMPONENTS serialization)
add_executable(ksq-slam-offline replay.cc)
target_compile_options(ksq-slam-offline PRIVATE -O2 -march=native -Wall -Wextra)
target_include_directories(ksq-slam-offline PRIVATE
    "${KSQ_ORB_ROOT}" "${KSQ_ORB_ROOT}/include" "${KSQ_ORB_ROOT}/include/CameraModels"
    "${KSQ_ORB_ROOT}/Thirdparty/Sophus" ${OpenCV_INCLUDE_DIRS} ${Boost_INCLUDE_DIRS} ${Pangolin_INCLUDE_DIRS})
target_link_libraries(ksq-slam-offline PRIVATE "${KSQ_ORB_LIBRARY}" ${OpenCV_LIBS} ${Pangolin_LIBRARIES} Eigen3::Eigen Threads::Threads)
get_filename_component(KSQ_ORB_LIB_DIR "${KSQ_ORB_LIBRARY}" DIRECTORY)
set_target_properties(ksq-slam-offline PROPERTIES BUILD_RPATH
    "${CMAKE_CXX_IMPLICIT_LINK_DIRECTORIES};/usr/local/lib;${KSQ_ORB_LIB_DIR};${KSQ_ORB_ROOT}/Thirdparty/DBoW2/lib;${KSQ_ORB_ROOT}/Thirdparty/g2o/lib")
add_executable(ksq-shutdown-probe shutdown_probe.cc)
target_compile_options(ksq-shutdown-probe PRIVATE -O2 -march=native -Wall -Wextra)
target_include_directories(ksq-shutdown-probe PRIVATE
    "${KSQ_ORB_ROOT}" "${KSQ_ORB_ROOT}/include" "${KSQ_ORB_ROOT}/include/CameraModels"
    "${KSQ_ORB_ROOT}/Thirdparty/Sophus" ${OpenCV_INCLUDE_DIRS} ${Boost_INCLUDE_DIRS} ${Pangolin_INCLUDE_DIRS})
target_link_libraries(ksq-shutdown-probe PRIVATE "${KSQ_ORB_LIBRARY}" ${OpenCV_LIBS} ${Pangolin_LIBRARIES} Eigen3::Eigen Threads::Threads)
set_target_properties(ksq-shutdown-probe PROPERTIES BUILD_RPATH
    "${CMAKE_CXX_IMPLICIT_LINK_DIRECTORIES};/usr/local/lib;${KSQ_ORB_LIB_DIR};${KSQ_ORB_ROOT}/Thirdparty/DBoW2/lib;${KSQ_ORB_ROOT}/Thirdparty/g2o/lib")
add_executable(ksq-settings-probe settings_probe.cc)
target_compile_options(ksq-settings-probe PRIVATE -O2 -march=native -Wall -Wextra)
target_include_directories(ksq-settings-probe PRIVATE
    "${KSQ_ORB_ROOT}" "${KSQ_ORB_ROOT}/include" "${KSQ_ORB_ROOT}/include/CameraModels"
    "${KSQ_ORB_ROOT}/Thirdparty/Sophus" ${OpenCV_INCLUDE_DIRS} ${Boost_INCLUDE_DIRS} ${Pangolin_INCLUDE_DIRS})
target_link_libraries(ksq-settings-probe PRIVATE "${KSQ_ORB_LIBRARY}" ${OpenCV_LIBS} ${Pangolin_LIBRARIES} Eigen3::Eigen Threads::Threads)
set_target_properties(ksq-settings-probe PROPERTIES BUILD_RPATH
    "${CMAKE_CXX_IMPLICIT_LINK_DIRECTORIES};/usr/local/lib;${KSQ_ORB_LIB_DIR};${KSQ_ORB_ROOT}/Thirdparty/DBoW2/lib;${KSQ_ORB_ROOT}/Thirdparty/g2o/lib")

# Reuse the same include/ABI/link configuration as the hardware-free replay.
add_executable(ksq-imu-recovery-probe imu_recovery_probe.cc)
foreach(prop INCLUDE_DIRECTORIES COMPILE_OPTIONS BUILD_RPATH)
  get_target_property(replay_${prop} ksq-slam-offline ${prop})
  set_target_properties(ksq-imu-recovery-probe PROPERTIES ${prop} "${replay_${prop}}")
endforeach()
get_target_property(replay_libraries ksq-slam-offline LINK_LIBRARIES)
target_link_libraries(ksq-imu-recovery-probe PRIVATE ${replay_libraries})
# This probe owns Frame instances and therefore directly destroys DBoW2 fields.
target_link_libraries(ksq-imu-recovery-probe PRIVATE
    "${KSQ_ORB_ROOT}/Thirdparty/DBoW2/lib/libDBoW2.so")
