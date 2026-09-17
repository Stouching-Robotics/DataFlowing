/**
 * @file camera_server.cc
 * @brief S80M 相机采集进程 — 写入 /dev/shm 供 ORB-SLAM3 进程读取
 *        只链接 SDK OpenCV 4.2.0，与 ORB-SLAM3 进程完全隔离
 */
#include <signal.h>
#include <unistd.h>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <fstream>
#include <sys/stat.h>
#include <sys/prctl.h>

#include <thread>
#include <chrono>
#include <iomanip>

#include <opencv2/core.hpp>
#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>
#include <opencv2/calib3d.hpp>

#include "fays_atrak/fays_atrak_types.h"
#include "fays_atrak/fays_vikit.h"
#include "common/print_helpers.h"

static volatile bool g_running = true;
static void* g_handle = nullptr;

void sigint_handler(int) {
    const char msg[] = "\n[CAM] Shutting down...\n";
    write(STDERR_FILENO, msg, sizeof(msg)-1);
    g_running = false;  // 只设标志, 主循环做清理
}

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "Usage: " << argv[0] << " <camera_config.yaml> <output_dir>\n"
                  << "  output_dir: /dev/shm/fayssense/ (in-memory, fast)\n";
        return 1;
    }
    std::string cam_config = argv[1];
    std::string out_dir    = argv[2];

    // 父进程死自己也死 (run_orb_slam.sh 被杀时自动清理)
    prctl(PR_SET_PDEATHSIG, SIGTERM);

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = sigint_handler;
    sigaction(SIGINT, &sa, nullptr);
    sigaction(SIGTERM, &sa, nullptr);

    mkdir(out_dir.c_str(), 0755);
    std::string stereo_dir = out_dir + "/stereo";
    std::string imu_path   = out_dir + "/imu.csv";
    std::string sig_path   = out_dir + "/SIGNAL";  // 每帧写完 touch 一下
    mkdir(stereo_dir.c_str(), 0755);

    void* handle = nullptr;
    if (FAYS_VIK_CreateHandleWithConfig(&handle, cam_config.c_str()) != EXIT_SUCCESS) {
        std::cerr << "[CAM] ERROR: Cannot open camera" << std::endl;
        return 1;
    }
    g_handle = handle;
    PrintDeviceInfo(handle);

    // 预计算矫正映射 (KB4 → Rectified, 与 ORB-SLAM3 "Rectified" 类型对齐)
    cv::Mat K1 = (cv::Mat_<double>(3,3) << 231.513824, 0, 325.251587, 0, 231.521408, 193.431366, 0, 0, 1);
    cv::Mat D1 = (cv::Mat_<double>(1,4) << 0.054344, 0.020572, -0.002152, -0.004231);
    cv::Mat K2 = (cv::Mat_<double>(3,3) << 230.590, 0, 319.743, 0, 230.699, 188.880, 0, 0, 1);
    cv::Mat D2 = (cv::Mat_<double>(1,4) << 0.0381711, 0.0786609, -0.0846951, 0.0343996);
    cv::Mat R_stereo = (cv::Mat_<double>(3,3) << 0.999881, 0.001432, 0.015391, -0.001406, 0.999998, -0.001685, -0.015393, 0.001663, 0.999880);
    cv::Mat T_stereo = (cv::Mat_<double>(3,1) << -0.080087, 0.000009, 0.000599);
    cv::Size img_sz(640, 400);

    cv::Mat R1, R2, P1, P2, Q;
    cv::fisheye::stereoRectify(K1, D1, K2, D2, img_sz, R_stereo, T_stereo, R1, R2, P1, P2, Q,
                               cv::CALIB_ZERO_DISPARITY);
    cv::Mat map1x, map1y, map2x, map2y;
    cv::fisheye::initUndistortRectifyMap(K1, D1, R1, P1, img_sz, CV_32FC1, map1x, map1y);
    cv::fisheye::initUndistortRectifyMap(K2, D2, R2, P2, img_sz, CV_32FC1, map2x, map2y);
    std::cout << "[CAM] Rectification ready. fx=" << P1.at<double>(0,0) << std::endl;

    std::ofstream imu_file(imu_path);
    imu_file << "#t_ns,ax,ay,az,gx,gy,gz\n";

    AtrakImage img_data;
    img_data.data = new uchar[FAYS_ATRAK_MONO_MAX_BYTES * 3];

    int frame_cnt = 0;
    auto t0 = std::chrono::steady_clock::now();
    auto last_frame_time = t0;

    std::cout << "[CAM] Streaming to " << out_dir << " ... Ctrl+C to stop." << std::endl;

    while (g_running) {
        // 看门狗: 5 秒没新帧就退出
        auto wd_now = std::chrono::steady_clock::now();
        if (std::chrono::duration<double>(wd_now - last_frame_time).count() > 5.0) {
            std::cerr << "[CAM] Watchdog: no frames for 5s, exiting." << std::endl;
            break;
        }
        // 图像 — S80M 上下堆叠: 640×800 = 上半左目(640×400) + 下半右目(640×400)
        if (FAYS_VIK_GetStereoFrames(handle, &img_data) == EXIT_SUCCESS) {
            int half_h = img_data.height / 2;
            cv::Mat stitched(img_data.height, img_data.width, CV_8UC1, img_data.data);
            cv::Mat left  = stitched(cv::Rect(0, 0, img_data.width, half_h));
            cv::Mat right = stitched(cv::Rect(0, half_h, img_data.width, half_h));

            // 矫正 (KB4 → Rectified)
            cv::Mat Lr, Rr;
            cv::remap(left,  Lr, map1x, map1y, cv::INTER_LINEAR);
            cv::remap(right, Rr, map2x, map2y, cv::INTER_LINEAR);
            // 左右拼接保存
            cv::Mat combined;
            cv::hconcat(Lr, Rr, combined);
            char fname[256], tmpname[256];
            snprintf(fname, sizeof(fname), "%s/%lu.png", stereo_dir.c_str(), img_data.timestamp);
            snprintf(tmpname, sizeof(tmpname), "%s/.tmp_%lu.png", stereo_dir.c_str(), img_data.timestamp);
            cv::imwrite(tmpname, combined);
            rename(tmpname, fname);  // 原子操作, 读进程不会看到半截文件

            std::ofstream(sig_path, std::ios::trunc).close();

            last_frame_time = std::chrono::steady_clock::now();
            frame_cnt++;
        }

        // IMU — 批量读取
        for (int i = 0; i < 100; i++) {
            AtrakIMU imu;
            if (FAYS_VIK_GetImuData(handle, &imu) == EXIT_SUCCESS) {
                imu_file << imu.timestamp << ","
                         << imu.acc[0] << "," << imu.acc[1] << "," << imu.acc[2] << ","
                         << imu.gyro[0] << "," << imu.gyro[1] << "," << imu.gyro[2] << "\n";
            } else break;
        }
        imu_file.flush();

        // 每秒打印状态
        static auto last = t0;
        auto now = std::chrono::steady_clock::now();
        if (std::chrono::duration<double>(now - last).count() > 1.0) {
            std::cout << "[CAM] frames=" << frame_cnt << std::endl;
            last = now;
        }

        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }

    delete[] img_data.data;
    imu_file.close();
    if (handle) FAYS_VIK_DestroyHandle(handle);
    std::cout << "[CAM] Done. " << frame_cnt << " frames. Camera released." << std::endl;
    return 0;
}
