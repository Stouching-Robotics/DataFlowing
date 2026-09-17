// No SDK/camera. Exercise both inertial optimizers' missing-state guards.
#include "System.h"
#include "Optimizer.h"
#include <iostream>
int main() {
    ORB_SLAM3::Frame current, previous;
    current.N = 0;
    current.mnId = 1;
    current.mTimeStamp = 1.;
    current.mpLastKeyFrame = nullptr;
    current.SetPose(Sophus::SE3f());
    if (ORB_SLAM3::Optimizer::PoseInertialOptimizationLastFrame(&current) != 0)
        return 1;
    current.mpPrevFrame = &previous; // previous.mpcpi is null by construction
    if (ORB_SLAM3::Optimizer::PoseInertialOptimizationLastFrame(&current) != 0)
        return 2;
    if (ORB_SLAM3::Optimizer::PoseInertialOptimizationLastKeyFrame(&current) != 0)
        return 3;
    if (!current.GetPose().matrix().allFinite() || current.mpcpi)
        return 4;
    std::cout << "PASS missing previous frame/prior/keyframe: visual fallback, no fabricated prior" << std::endl;
    return 0;
}
