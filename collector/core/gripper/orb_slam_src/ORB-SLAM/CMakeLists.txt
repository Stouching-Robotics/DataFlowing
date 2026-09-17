cmake_minimum_required(VERSION 2.8)
project(ORB_SLAM3)

IF(NOT CMAKE_BUILD_TYPE)
  SET(CMAKE_BUILD_TYPE Release)
ENDIF()

MESSAGE("Build type: " ${CMAKE_BUILD_TYPE})

set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS}  -Wall   -O3")
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -Wall   -O3")
set(CMAKE_C_FLAGS_RELEASE "${CMAKE_C_FLAGS_RELEASE} -march=native")
set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} -march=native")

# Check C++14 or C++11 support
include(CheckCXXCompilerFlag)
CHECK_CXX_COMPILER_FLAG("-std=c++14" COMPILER_SUPPORTS_CXX14)
CHECK_CXX_COMPILER_FLAG("-std=c++11" COMPILER_SUPPORTS_CXX11)
CHECK_CXX_COMPILER_FLAG("-std=c++0x" COMPILER_SUPPORTS_CXX0X)
if(COMPILER_SUPPORTS_CXX14)
   set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -std=c++14")
   add_definitions(-DCOMPILEDWITHC14)
   message(STATUS "Using flag -std=c++14.")
elseif(COMPILER_SUPPORTS_CXX11)
   set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -std=c++11")
   add_definitions(-DCOMPILEDWITHC11)
   message(STATUS "Using flag -std=c++11.")
elseif(COMPILER_SUPPORTS_CXX0X)
   set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -std=c++0x")
   add_definitions(-DCOMPILEDWITHC0X)
   message(STATUS "Using flag -std=c++0x.")
else()
   message(FATAL_ERROR "The compiler ${CMAKE_CXX_COMPILER} has no C++11 support. Please use a different C++ compiler.")
endif()

LIST(APPEND CMAKE_MODULE_PATH ${PROJECT_SOURCE_DIR}/cmake_modules)

find_package(OpenCV 4.4)
   if(NOT OpenCV_FOUND)
      message(FATAL_ERROR "OpenCV > 4.4 not found.")
   endif()

MESSAGE("OPENCV VERSION:")
MESSAGE(${OpenCV_VERSION})

find_package(Eigen3 3.1.0 REQUIRED)
find_package(Pangolin REQUIRED)
find_package(Boost 1.74 REQUIRED COMPONENTS serialization)
find_library(KSQ_CRYPTO_LIBRARY NAMES crypto libcrypto.so.3)
if(NOT KSQ_CRYPTO_LIBRARY)
  message(FATAL_ERROR "OpenSSL libcrypto was not found.")
endif()

include_directories(
${PROJECT_SOURCE_DIR}
${PROJECT_SOURCE_DIR}/include
${PROJECT_SOURCE_DIR}/include/CameraModels
${PROJECT_SOURCE_DIR}/Thirdparty/Sophus
${EIGEN3_INCLUDE_DIR}
${Pangolin_INCLUDE_DIRS}
)

set(KSQ_ORB_LIBRARY_OUTPUT_DIRECTORY "${PROJECT_SOURCE_DIR}/lib" CACHE PATH
    "Output directory for an isolated ORB-SLAM3 core build")
set(KSQ_ORB_THIRDPARTY_LIBRARY_ROOT "${PROJECT_SOURCE_DIR}" CACHE PATH
    "ORB-SLAM3 root that provides prebuilt DBoW2 and g2o libraries")
option(KSQ_LOW_MEMORY_OPTIMIZER_BUILD
       "Compile only Optimizer.cc at -O2 to reduce the compiler memory peak" OFF)
if(KSQ_LOW_MEMORY_OPTIMIZER_BUILD)
  set_source_files_properties(src/Optimizer.cc PROPERTIES COMPILE_OPTIONS "-O2")
endif()
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${KSQ_ORB_LIBRARY_OUTPUT_DIRECTORY})

add_library(${PROJECT_NAME} SHARED
src/System.cc
src/Tracking.cc
src/LocalMapping.cc
src/LoopClosing.cc
src/ORBextractor.cc
src/ORBmatcher.cc
src/FrameDrawer.cc
src/Converter.cc
src/MapPoint.cc
src/KeyFrame.cc
src/Atlas.cc
src/Map.cc
src/MapDrawer.cc
src/Optimizer.cc
src/Frame.cc
src/KeyFrameDatabase.cc
src/Sim3Solver.cc
src/Viewer.cc
src/ImuTypes.cc
src/G2oTypes.cc
src/CameraModels/Pinhole.cpp
src/CameraModels/KannalaBrandt8.cpp
src/OptimizableTypes.cpp
src/MLPnPsolver.cpp
src/GeometricTools.cc
src/TwoViewReconstruction.cc
src/Config.cc
src/Settings.cc
include/System.h
include/FaysThreadAffinity.h
include/Tracking.h
include/LocalMapping.h
include/LoopClosing.h
include/ORBextractor.h
include/ORBmatcher.h
include/FrameDrawer.h
include/Converter.h
include/MapPoint.h
include/KeyFrame.h
include/Atlas.h
include/Map.h
include/MapDrawer.h
include/Optimizer.h
include/Frame.h
include/KeyFrameDatabase.h
include/Sim3Solver.h
include/Viewer.h
include/ImuTypes.h
include/G2oTypes.h
include/CameraModels/GeometricCamera.h
include/CameraModels/Pinhole.h
include/CameraModels/KannalaBrandt8.h
include/OptimizableTypes.h
include/MLPnPsolver.h
include/GeometricTools.h
include/TwoViewReconstruction.h
include/SerializationUtils.h
include/Config.h
include/Settings.h)

add_subdirectory(Thirdparty/g2o)

target_link_libraries(${PROJECT_NAME}
${OpenCV_LIBS}
${EIGEN3_LIBS}
${Pangolin_LIBRARIES}
${KSQ_ORB_THIRDPARTY_LIBRARY_ROOT}/Thirdparty/DBoW2/lib/libDBoW2.so
${KSQ_ORB_THIRDPARTY_LIBRARY_ROOT}/Thirdparty/g2o/lib/libg2o.so
Boost::serialization
${KSQ_CRYPTO_LIBRARY}
)

# 本仓库只在这里构建 ORB-SLAM3 核心库。应用层目标严格限定为 FaysSense，
# 请使用 ORB-SLAM/Examples/fays/CMakeLists.txt（SDK OpenCV 4.2 诊断）或
# dist/fays_opencv48/CMakeLists.txt（生产 OpenCV 4.8）构建。
message(STATUS "Application targets are FaysSense-only; generic examples are disabled")

# Opt-in, hardware-free regression tests. Keep application entrypoints in fays/.
option(KSQ_BUILD_RUNTIME_TESTS "Build native Frame/IMU safety regression" OFF)
if(KSQ_BUILD_RUNTIME_TESTS)
  enable_testing()
  # BUILD_WITH_INSTALL_RPATH is ON for the shared library (no patchelf/chrpath
  # here, so the runtime path has to be fixed at link time), and that makes
  # CMake ignore BUILD_RPATH for executables too. The test binaries therefore
  # have to carry their search path as INSTALL_RPATH, or they cannot find the
  # build-tree core library, g2o/DBoW2, or the OpenCV/Pangolin it links
  # against. Compiler runtime first: keep Conda's libstdc++ from shadowing it.
  get_filename_component(KSQ_OPENCV_LIB_DIR "${OpenCV_DIR}/../.." ABSOLUTE)
  # The shipped gripper payload (vendor SDK, orb48_env, ORBvoc.txt) is 785 MB
  # and stays out of git, so it is not part of this source tree and has to be
  # located separately. This used to read ${PROJECT_SOURCE_DIR}/../../core/...,
  # which only worked while the sources lived in online/ORB-SLAM -- two levels
  # below the repo root. core/gripper/orb_slam_src/ORB-SLAM is four, so that
  # expression silently produced a non-existent path (CMake does not validate
  # INSTALL_RPATH entries and nothing reads this by default). Keep it relative
  # to the payload root, not to a fixed depth.
  set(KSQ_GRIPPER_NATIVE_ROOT "$ENV{KSQ_GRIPPER_NATIVE_ROOT}" CACHE PATH
      "core/gripper/native: holds the untracked vendor SDK and orb48_env")
  if(NOT KSQ_GRIPPER_NATIVE_ROOT)
    get_filename_component(KSQ_GRIPPER_NATIVE_ROOT
      "${PROJECT_SOURCE_DIR}/../../native" ABSOLUTE)
  endif()
  set(KSQ_TEST_RUNTIME_RPATH
    "${CMAKE_CXX_IMPLICIT_LINK_DIRECTORIES}"
    "${KSQ_ORB_LIBRARY_OUTPUT_DIRECTORY}"
    "${PROJECT_SOURCE_DIR}/Thirdparty/DBoW2/lib"
    "${PROJECT_SOURCE_DIR}/Thirdparty/g2o/lib"
    "${KSQ_OPENCV_LIB_DIR}"
    "${KSQ_GRIPPER_NATIVE_ROOT}/FaysSense_VI_Kit_Release/thirdparty/orb48_env/lib")
  add_executable(ksq-triangulation-observation-test ../tests/native/triangulation_observation_test.cc)
  target_include_directories(ksq-triangulation-observation-test PRIVATE ${OpenCV_INCLUDE_DIRS})
  add_test(NAME triangulation-observation-safety COMMAND ksq-triangulation-observation-test)
  # Header-only and library-free: it pins the stale-frame decision used by the
  # Fays bridge's preprocessWorker, which lives outside this build tree.
  add_executable(ksq-time-regression-test ../tests/native/time_regression_test.cc)
  target_include_directories(ksq-time-regression-test PRIVATE
    ${PROJECT_SOURCE_DIR}/Examples/fays)
  add_test(NAME time-regression-safety COMMAND ksq-time-regression-test)
  add_executable(ksq-frame-imu-test ../tests/native/frame_imu_test.cc)
  target_link_libraries(ksq-frame-imu-test ${PROJECT_NAME})
  set_target_properties(ksq-frame-imu-test PROPERTIES
    INSTALL_RPATH "${KSQ_TEST_RUNTIME_RPATH}")
  # DT_RUNPATH is only consulted for the object's own dependencies, so the test
  # binary would still fail to locate libg2o/libDBoW2 -- those are needed by
  # libORB_SLAM3.so, which carries the production RPATH and cannot be repathed
  # here. Emit the older DT_RPATH, which *is* inherited by transitive lookups.
  target_link_options(ksq-frame-imu-test PRIVATE -Wl,--disable-new-dtags)
  add_test(NAME frame-imu-safety COMMAND ksq-frame-imu-test)
  # Fails with a SIGSEGV rather than a clean assertion if the IMU_RECOVERY
  # fallback in Optimizer.cc is ever dropped.
  add_executable(ksq-prior-recovery-test ../tests/native/prior_recovery_test.cc)
  target_link_libraries(ksq-prior-recovery-test ${PROJECT_NAME})
  set_target_properties(ksq-prior-recovery-test PROPERTIES
    INSTALL_RPATH "${KSQ_TEST_RUNTIME_RPATH}")
  target_link_options(ksq-prior-recovery-test PRIVATE -Wl,--disable-new-dtags)
  add_test(NAME prior-recovery-safety COMMAND ksq-prior-recovery-test)
  # Fails with a SIGSEGV at Vertex::_fixed (offset 0x54) rather than a clean
  # assertion if the keyframe-vertex presence guard in Optimizer::FullInertialBA
  # is ever dropped. Same field crash family as the test above, different site.
  add_executable(ksq-full-inertial-ba-guard-test ../tests/native/full_inertial_ba_guard_test.cc)
  target_link_libraries(ksq-full-inertial-ba-guard-test ${PROJECT_NAME})
  set_target_properties(ksq-full-inertial-ba-guard-test PROPERTIES
    INSTALL_RPATH "${KSQ_TEST_RUNTIME_RPATH}")
  target_link_options(ksq-full-inertial-ba-guard-test PRIVATE -Wl,--disable-new-dtags)
  add_test(NAME full-inertial-ba-guard-safety COMMAND ksq-full-inertial-ba-guard-test)
  # Fails with a clean assertion if KeyFrame payload reclamation is dropped, and
  # runs off the end of a cleared grid if the empty check in
  # KeyFrame::GetFeaturesInArea is removed.
  add_executable(ksq-keyframe-payload-cleanup-test ../tests/native/keyframe_payload_cleanup_test.cc)
  target_link_libraries(ksq-keyframe-payload-cleanup-test ${PROJECT_NAME})
  set_target_properties(ksq-keyframe-payload-cleanup-test PROPERTIES
    INSTALL_RPATH "${KSQ_TEST_RUNTIME_RPATH}")
  target_link_options(ksq-keyframe-payload-cleanup-test PRIVATE -Wl,--disable-new-dtags)
  add_test(NAME keyframe-payload-cleanup-safety COMMAND ksq-keyframe-payload-cleanup-test)
  # Fails with a clean assertion (not a SIGSEGV) if any Frame constructor stops
  # initialising mpLastKeyFrame. Poisoned-buffer test: the production crash is
  # this member carrying indeterminate garbage into the temporal-chain walk in
  # Tracking::UpdateLocalKeyFrames.
  add_executable(ksq-frame-lastkf-init-test ../tests/native/frame_lastkf_init_test.cc)
  target_link_libraries(ksq-frame-lastkf-init-test ${PROJECT_NAME})
  set_target_properties(ksq-frame-lastkf-init-test PROPERTIES
    INSTALL_RPATH "${KSQ_TEST_RUNTIME_RPATH}")
  target_link_options(ksq-frame-lastkf-init-test PRIVATE -Wl,--disable-new-dtags)
  add_test(NAME frame-lastkf-init-safety COMMAND ksq-frame-lastkf-init-test)
  # libg2o/libDBoW2 are needed by libORB_SLAM3.so, not by the test binary, and
  # the loader resolves a transitive dependency with the *calling* object's
  # RUNPATH -- which for the build-tree library is the production one and does
  # not resolve under /tmp. DT_RPATH on the executable does not help (verified
  # with LD_DEBUG=libs). LD_LIBRARY_PATH is the only lever that reaches the
  # transitive lookup, so ctest supplies it. Running the binaries by hand needs
  # the same variable.
  string(REPLACE ";" ":" KSQ_TEST_LD_LIBRARY_PATH "${KSQ_TEST_RUNTIME_RPATH}")
  set_tests_properties(frame-imu-safety prior-recovery-safety
                       full-inertial-ba-guard-safety
                       keyframe-payload-cleanup-safety
                       frame-lastkf-init-safety PROPERTIES
    ENVIRONMENT "LD_LIBRARY_PATH=${KSQ_TEST_LD_LIBRARY_PATH};KSQ_FAYS_ORB_LEFT_CPU=6;KSQ_FAYS_ORB_RIGHT_CPU=7")
endif()
