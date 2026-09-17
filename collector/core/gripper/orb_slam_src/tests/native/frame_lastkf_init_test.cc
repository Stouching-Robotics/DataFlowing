// Hardware-free regression: every Frame constructor must leave
// mpLastKeyFrame at NULL or a live keyframe. No USB, no Fays SDK, no images on
// disk.
//
// Regression for the 2026-09-17 SIGSEGV. GrabImageStereo() builds the frame by
// *copy-assigning a temporary* (Tracking.cc:1506) and the image constructors
// never initialised the member -- Frame.h:264 has no initialiser and only the
// copy constructor ever mentions it -- so the temporary's indeterminate value
// was copied into mCurrentFrame. Tracking::PreintegrateIMU() normally
// overwrites it (Tracking.cc:1808, eight lines after the IMU integration
// succeeds), but both of its early returns leave before that line:
//
//   "Insufficient IMU measurements"  (Tracking.cc:1726, n <= 0)
//   missing keyframe accumulator     (Tracking.cc:1748, IMU_RECOVERY)
//
// and UpdateLocalKeyFrames() then walks the temporal keyframe chain starting
// from that value (Tracking.cc:3777) with a null check that cannot help,
// because the leftover value is not null:
//
//   [FATAL_SIGNAL] signo=11 code=1 fault_addr=0x283
//   UpdateLocalKeyFrames()+0x424   cmp %rax,0x30(%rbx)   rbx=0x253
//
// 0x283 = 0x253 + offsetof(KeyFrame, mnTrackReferenceForFrame). That frame was
// the only one in the session where the IMU guard fired (frame=2364), 2 ms
// before the fault. The same skip path has been benign for months: it only
// crashes when the leftover word happens to be non-null *and* the local map is
// small enough for the loop to run (mvpLocalKeyFrames.size() < 80), which is
// why this session -- whose 30 s map-forgetting cleanup keeps local_kf ~46 --
// is the first to hit it.
//
// Detection: the constructors are run on memory poisoned with 0xAB, so a member
// a constructor forgets keeps 0xABABABABABABABAB (non-null garbage, exactly the
// production failure mode) instead of NULL. Poisoned objects are never
// destroyed -- a poisoned std::string member would be released through a
// 0xABAB... pointer -- the process exits right after the checks.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include "Frame.h"
#include "ORBextractor.h"
#include "CameraModels/KannalaBrandt8.h"
#include "CameraModels/Pinhole.h"
#include <cassert>
#include <cstring>
#include <iostream>
#include <new>
#include <string>

namespace {

int gFailures = 0;

void check(bool ok, const std::string& what) {
    std::cout << (ok ? "PASS: " : "FAIL: ") << what << std::endl;
    if (!ok) ++gFailures;
}

constexpr std::size_t kFrameSize = sizeof(ORB_SLAM3::Frame);

// One buffer, reused: every constructor sees the same poisoned bytes. Static
// storage is aligned for Frame and never destructed.
alignas(ORB_SLAM3::Frame) unsigned char gStorage[kFrameSize];

void poison_storage() { std::memset(gStorage, 0xAB, kFrameSize); }

const void* kPoisonWord = reinterpret_cast<const void*>(
    static_cast<std::uintptr_t>(0xABABABABABABABABULL));

// Constructs into the poisoned buffer and reports what the constructor left in
// mpLastKeyFrame. Never destructs: see the header comment.
void check_constructor_leaves_null(const std::string& name,
                                   ORB_SLAM3::Frame* frame) {
    const void* last = static_cast<const void*>(frame->mpLastKeyFrame);
    if (last == kPoisonWord) {
        check(false, name + " left mpLastKeyFrame poisoned (0xabab...), i.e."
                           " uninitialised -- UpdateLocalKeyFrames() would"
                           " dereference it as the temporal chain head");
        return;
    }
    check(frame->mpLastKeyFrame == nullptr,
          name + " leaves mpLastKeyFrame == NULL");
    if (frame->mpLastKeyFrame == nullptr) {
        // Only safe to touch once the pointer proves the constructor ran.
        check(frame->mNameFile.empty(),
              name + " leaves mNameFile empty (same init-list defect, copied"
                     " from the temporary every frame)");
    }
}

}  // namespace

int main() {
    cv::Mat black(480, 640, CV_8U, cv::Scalar(0));
    cv::Mat depth(480, 640, CV_32F, cv::Scalar(0));
    cv::Mat K = (cv::Mat_<float>(3, 3) << 300, 0, 320, 0, 300, 240, 0, 0, 1);
    cv::Mat distortion = cv::Mat::zeros(4, 1, CV_32F);
    ORB_SLAM3::ORBextractor left(500, 1.2, 8, 20, 7);
    ORB_SLAM3::ORBextractor right(500, 1.2, 8, 20, 7);
    ORB_SLAM3::Pinhole pinhole({300, 300, 320, 240});
    ORB_SLAM3::KannalaBrandt8 camera({300, 300, 320, 240, 0, 0, 0, 0});
    ORB_SLAM3::KannalaBrandt8 camera2({300, 300, 320, 240, 0, 0, 0, 0});
    camera.mvLappingArea = {0, 640};
    camera2.mvLappingArea = {0, 640};
    Sophus::SE3f extrinsics;
    ORB_SLAM3::IMU::Calib calib;
    ORB_SLAM3::Frame previous;  // properly initialised, only stored as pPrevF

    poison_storage();
    check_constructor_leaves_null(
        "Frame()", new (gStorage) ORB_SLAM3::Frame());

    poison_storage();
    check_constructor_leaves_null(
        // No K parameter: the mono constructor takes it from pCamera->toK(),
        // which is also why pCamera has to be a Pinhole here.
        "Frame(imGray, ...) mono",
        new (gStorage) ORB_SLAM3::Frame(black, 1.0, &left, nullptr, &pinhole,
                                        distortion, 24.0, 40.0));

    // The sensor path that crashed: IMU_STEREO with one camera pair builds the
    // frame through this constructor (Tracking.cc:1508).
    poison_storage();
    check_constructor_leaves_null(
        "Frame(imLeft, imRight, ..., pCamera, pPrevF, ImuCalib) IMU_STEREO",
        new (gStorage) ORB_SLAM3::Frame(black, black, 1.0, &left, &right,
                                        nullptr, K, distortion, 24.0, 40.0,
                                        &camera, &previous, calib));

    poison_storage();
    check_constructor_leaves_null(
        "Frame(imLeft, imRight, ..., pCamera, pCamera2, Tlr) stereo two-camera",
        new (gStorage) ORB_SLAM3::Frame(black, black, 1.0, &left, &right,
                                        nullptr, K, distortion, 24.0, 40.0,
                                        &camera, &camera2, extrinsics));

    poison_storage();
    check_constructor_leaves_null(
        "Frame(imGray, imDepth, ...) RGB-D",
        new (gStorage) ORB_SLAM3::Frame(black, depth, 1.0, &left, nullptr, K,
                                        distortion, 24.0, 40.0, &camera));

    // The copy constructor must keep copying the chain head: the value means
    // "last keyframe as of this frame", and mLastFrame inherits it.
    ORB_SLAM3::Frame source;
    // A default-constructed Frame has an empty mK and the copy constructor
    // feeds it to Converter::toMatrix3f, which indexes it -- so hand it a real
    // calibration. (Upstream behaviour, unrelated to mpLastKeyFrame; the
    // production copies are always of image-built frames.)
    source.mK = K.clone();
    source.mpLastKeyFrame = reinterpret_cast<ORB_SLAM3::KeyFrame*>(0x1234);
    ORB_SLAM3::Frame copy(source);
    check(copy.mpLastKeyFrame == source.mpLastKeyFrame,
          "copy constructor preserves a non-null mpLastKeyFrame");

    if (gFailures != 0) {
        std::cout << "FAILED: " << gFailures << " check(s)" << std::endl;
        return 1;
    }
    std::cout << "PASS: all Frame constructors initialise mpLastKeyFrame"
              << std::endl;
    return 0;
}
