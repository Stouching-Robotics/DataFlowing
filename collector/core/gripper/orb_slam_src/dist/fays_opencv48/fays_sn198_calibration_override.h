#pragma once

// Compile-time adapter for the factory calibration dumped from
// FS-VI80-S80M serial 3500000261980088.  The included bridge source contains
// constants from a different camera; this adapter replaces those matrices
// without editing that source file.

#include <cstdint>
#include <opencv2/calib3d.hpp>
#include <opencv2/core.hpp>

#include "fays_atrak/fays_atrak_types.h"
#include "fays_atrak/fays_vikit.h"

namespace cv {
namespace fisheye {

inline void ksqStereoRectifySn198(
    cv::Mat& K1, cv::Mat& D1, cv::Mat& K2, cv::Mat& D2,
    const cv::Size& image_size, cv::Mat& R, cv::Mat& T,
    cv::Mat& R1, cv::Mat& R2, cv::Mat& P1, cv::Mat& P2, cv::Mat& Q,
    int flags) {
    K1 = (cv::Mat_<double>(3, 3) <<
        231.012939453125, 0.0, 296.500885009765625,
        0.0, 231.104904174804688, 206.681167602539062,
        0.0, 0.0, 1.0);
    D1 = (cv::Mat_<double>(1, 4) <<
        0.039406143128872, 0.065453097224236,
        -0.052402395755053, 0.013984472490847);
    K2 = (cv::Mat_<double>(3, 3) <<
        230.908416748046875, 0.0, 316.081298828125,
        0.0, 230.826675415039062, 194.12506103515625,
        0.0, 0.0, 1.0);
    D2 = (cv::Mat_<double>(1, 4) <<
        0.046109389513731, 0.045664116740227,
        -0.034244563430548, 0.008086845278740);
    R = (cv::Mat_<double>(3, 3) <<
        0.999815821647644, 0.007448780350387, 0.017695387825370,
        -0.007383888121694, 0.999965906143188, -0.003729730844498,
        -0.017722563818097, 0.003598378971219, 0.999836444854736);
    T = (cv::Mat_<double>(3, 1) <<
        -0.079930186271667, 0.000282138440525, 0.000258920714259);

    cv::fisheye::stereoRectify(
        K1, D1, K2, D2, image_size, R, T,
        R1, R2, P1, P2, Q, flags);
}

}  // namespace fisheye
}  // namespace cv

inline int ksqRegisterImuCallbackSn198(
    void* handle, FAYS_VIK_ImuCallback callback) {
    // The original bridge subtracts 1.267 ms.  This camera's factory value is
    // 3.753688885 ms, so pre-adjust by the remaining 2.486688885 ms.
    constexpr std::uint64_t kAdditionalShiftNs = 2486689ULL;
    return FAYS_VIK_RegisterImuCallback(
        handle,
        [callback](const AtrakIMU& sample) {
            AtrakIMU adjusted = sample;
            if (adjusted.timestamp > kAdditionalShiftNs) {
                adjusted.timestamp -= kAdditionalShiftNs;
            }
            callback(adjusted);
        });
}

// These macros affect only the subsequently included bridge translation unit.
#define stereoRectify ksqStereoRectifySn198
#define FAYS_VIK_RegisterImuCallback ksqRegisterImuCallbackSn198
