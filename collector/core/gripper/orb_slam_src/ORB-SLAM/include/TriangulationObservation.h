#ifndef ORB_SLAM3_TRIANGULATION_OBSERVATION_H
#define ORB_SLAM3_TRIANGULATION_OBSERVATION_H

#include <cstddef>
#include <opencv2/core/types.hpp>

namespace ORB_SLAM3 {

// A fisheye pair uses joint left/right feature indices, but mvuRight only
// contains NLeft entries. Never read rectified stereo coordinates on that
// path, even for a valid right-camera feature index.
struct TriangulationObservation {
    const cv::KeyPoint* keypoint = nullptr;
    float rightCoordinate = -1.0f;
    bool rectifiedStereo = false;
    bool rightCamera = false;
};

template<class KeyFrameLike>
inline bool GetTriangulationObservation(const KeyFrameLike& keyframe,
                                        std::size_t index,
                                        TriangulationObservation& out) {
    out = TriangulationObservation{};
    if (keyframe.NLeft == -1) {
        if (index >= keyframe.mvKeysUn.size()) return false;
        out.keypoint = &keyframe.mvKeysUn[index];
    } else {
        if (keyframe.NLeft < 0) return false;
        const auto leftCount = static_cast<std::size_t>(keyframe.NLeft);
        out.rightCamera = index >= leftCount;
        if (out.rightCamera) {
            if (index - leftCount >= keyframe.mvKeysRight.size()) return false;
            out.keypoint = &keyframe.mvKeysRight[index - leftCount];
        } else {
            if (index >= keyframe.mvKeys.size()) return false;
            out.keypoint = &keyframe.mvKeys[index];
        }
    }
    if (!keyframe.mpCamera2) {
        if (index >= keyframe.mvuRight.size()) return false;
        out.rightCoordinate = keyframe.mvuRight[index];
        out.rectifiedStereo = out.rightCoordinate >= 0;
    }
    const int octave = out.keypoint->octave;
    return octave >= 0 &&
           static_cast<std::size_t>(octave) < keyframe.mvLevelSigma2.size() &&
           static_cast<std::size_t>(octave) < keyframe.mvScaleFactors.size();
}

}  // namespace ORB_SLAM3
#endif
