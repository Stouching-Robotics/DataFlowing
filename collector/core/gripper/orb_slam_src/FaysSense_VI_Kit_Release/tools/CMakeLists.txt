cmake_minimum_required(VERSION 3.12)
project(tools)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(record record.cpp)
target_link_libraries(record fays_vikit_sdk_deps)

add_executable(dump_calib dump_calib.cpp)
target_link_libraries(dump_calib fays_vikit_sdk_deps)

add_executable(fays_vikit_offline offline_process.cpp)
target_link_libraries(fays_vikit_offline fays_vikit_sdk_deps)