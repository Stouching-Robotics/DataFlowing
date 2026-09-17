// Hardware-free regression: blank Fays frames and default Frame objects must
// always have a live IMU completion lock. No USB or Fays SDK is opened.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include "Frame.h"
#include "ORBextractor.h"
#include "CameraModels/KannalaBrandt8.h"
#include <cassert>
#include <iostream>
#include <thread>
#include <vector>

int main() {
    ORB_SLAM3::Frame initial;
    assert(!initial.imuIsPreintegrated());
    initial.setIntegrated();
    assert(initial.imuIsPreintegrated());
    std::vector<std::thread> threads;
    for (int j = 0; j < 4; ++j) {
        threads.emplace_back([]() {
            for (int i = 0; i < 10000; ++i) {
                ORB_SLAM3::Frame frame;
                assert(!frame.imuIsPreintegrated());
                frame.setIntegrated();
                assert(frame.imuIsPreintegrated());
            }
        });
    }
    for (auto& thread : threads) thread.join();
    cv::Mat black(480, 640, CV_8U, cv::Scalar(0));
    cv::Mat K = (cv::Mat_<float>(3,3) << 300,0,320, 0,300,240, 0,0,1);
    cv::Mat distortion = cv::Mat::zeros(4,1,CV_32F);
    ORB_SLAM3::ORBextractor left(500, 1.2, 8, 20, 7);
    ORB_SLAM3::ORBextractor right(500, 1.2, 8, 20, 7);
    ORB_SLAM3::KannalaBrandt8 camera({300,300,320,240,0,0,0,0});
    ORB_SLAM3::KannalaBrandt8 camera2({300,300,320,240,0,0,0,0});
    camera.mvLappingArea = {0,640};
    camera2.mvLappingArea = {0,640};
    Sophus::SE3f extrinsics;
    ORB_SLAM3::Frame blank(black, black, 1.0, &left, &right, nullptr,
                          K, distortion, 24.0, 40.0, &camera, &camera2, extrinsics);
    assert(blank.N == 0);
    assert(!blank.imuIsPreintegrated());
    blank.setIntegrated();
    assert(blank.imuIsPreintegrated());
    std::cout << "PASS: default frames, 40000 concurrent completions, zero-feature Fays stereo frame\n";
}
