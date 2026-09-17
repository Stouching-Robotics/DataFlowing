// Hardware-free regression: FullInertialBA() must skip an observation whose
// keyframe never got a vertex in the optimizer, instead of dereferencing NULL.
//
// Field crash this guards (2026-09-11 11:14:18, in *production* -- fays-local-map
// is the bridge's LocalMapping thread, not a test binary):
//   fays-local-map[133328]: segfault at 54 ip ...libORB_SLAM3.so[2386f2,...+290000]
// `addr2line -e libORB_SLAM3.so 0x2386f2` lands in Optimizer::FullInertialBA.
// 0x54 is the offset of g2o::OptimizableGraph::Vertex::_fixed, and the faulting
// instruction is the `movzbl 0x54(%rax)` that reads it, so `VP->fixed()` was
// handed NULL. It was reached only 43 s before the bridge-side stale-frame guard
// went live, i.e. it used to be blocked by its upstream, not by anything here.
//
// Why the vertex is missing: the setup loop registers one vertex per keyframe in
// pMap->GetAllKeyFrames(), then the map-point loop looks up a vertex for every
// keyframe that observes the point. After Atlas switches maps, a map point can
// still carry observations recorded in a map that was since abandoned, and those
// keyframes are not in GetAllKeyFrames() -- so optimizer.vertex() returns NULL.
// It used to be dereferenced immediately, in all three observation branches
// (monocular / stereo / right-monocular). Note the `mnId > maxKFid` test just
// above it does *not* filter these out: an abandoned keyframe is older than the
// current one, so its id is smaller and it passes straight through to the lookup.
//
// Scope: this is a guard-presence detector, not a reproduction of the field
// crash. Driving a real Atlas hand-off needs a running System with two live maps
// and real image data; what is pinned here is the shape of the broken lookup --
// a map point whose observation set names a keyframe that is not in the map being
// optimized. Remove the guard and every case below segfaults at 0x54 instead, so
// the test fails loudly rather than silently losing its coverage.
//
// No USB, Fays SDK, vocabulary, image or IMU data is opened.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include "CameraModels/Pinhole.h"
#include "Frame.h"
#include "KeyFrame.h"
#include "Map.h"
#include "MapPoint.h"
#include "Optimizer.h"
#include <cassert>
#include <iostream>
#include <sstream>
#include <string>

using namespace ORB_SLAM3;

// A real camera object, because a vertex's estimate is an ImuCamPose, whose
// pCamera[0] is copied straight from the keyframe -- and EdgeMono::computeError
// projects through it (`pCamera[cam_idx]->project(Xc)`) once the optimizer runs.
// Leaving mpCamera null crashes inside ImuCamPose::Project with a perfectly
// intact guard, which would make this test lie. Never deleted: Frame has no
// destructor and nothing here takes ownership.
static GeometricCamera* makeCamera() {
    return new Pinhole(std::vector<float>{100.f, 100.f, 0.f, 0.f});
}

// KeyFrame's constructor copies a fixed set of vectors straight out of the Frame,
// and FullInertialBA then reads three of them (mvKeysUn, mvuRight,
// mvInvLevelSigma2) plus the feature grid. Frame's default constructor leaves its
// scalar members indeterminate, so every field that either the keyframe
// constructor or the observation loop actually *dereferences or indexes* is set
// here. Plain copies of garbage (fx, mK, the grid cells, ...) are harmless and
// deliberately left alone.
static void initBlankFrame(Frame& f) {
    f.N = 1;
    f.mnId = 0;
    f.mTimeStamp = 0.0;
    // Mono left keyframe. Nleft == -1 also keeps the keyframe constructor from
    // resizing mGridRight, and later makes UpdateNormalAndDepth read mvKeysUn
    // rather than one of the stereo vectors.
    f.Nleft = -1;
    f.Nright = 0;
    // A single left camera keeps the right-monocular branch (gated on
    // pKFi->mpCamera2) out of the observation loop -- but note
    // MapPoint::AddObservation still indexes mvuRight when mpCamera2 is null
    // (`!pKF->mpCamera2 && pKF->mvuRight[idx]`), which is why mvuRight has to be
    // sized even for a monocular keyframe.
    f.mpCamera = makeCamera();
    f.mpCamera2 = nullptr;
    // mvInvLevelSigma2 is indexed by keypoint octave; mvScaleFactors is indexed
    // by the reference keyframe's scale level and by mnScaleLevels-1.
    f.mnScaleLevels = 8;
    f.mvScaleFactors.assign(f.mnScaleLevels, 1.0f);
    f.mvLevelSigma2.assign(f.mnScaleLevels, 1.0f);
    f.mvInvLevelSigma2.assign(f.mnScaleLevels, 1.0f);
    f.mvKeysUn.assign(f.N, cv::KeyPoint(0.f, 0.f, 1.f));
    f.mvuRight.assign(f.N, -1.0f);
}

// The keyframe constructor copies its fields, so the Frame does not have to
// outlive this call. pMap only has to be non-null (it is read for the IMU flag
// and the origin map id).
static KeyFrame* makeKeyFrame(Map* pMap) {
    Frame F;
    initBlankFrame(F);
    return new KeyFrame(F, pMap, nullptr);
}

// FullInertialBA returns void, so the guard's own summary line is the only
// observable: [FULL_BA_RECOVERY] reaches stderr only when at least one
// observation was skipped. Its wording avoids core/gripper/slam/protocol.py's
// _ERROR_RE on purpose, so that a recovery does not surface as an error in the UI.
static std::string runAndCapture(Map* pMap, bool bInit) {
    std::stringstream log;
    std::streambuf* previous = std::cerr.rdbuf(log.rdbuf());
    Optimizer::FullInertialBA(pMap, 5, /*bFixLocal=*/false, /*nLoopId=*/0,
                              /*pbStopFlag=*/NULL, bInit);
    std::cerr.rdbuf(previous);
    return log.str();
}

static MapPoint* makePointObservedBy(KeyFrame* pRefKF, Map* pMap,
                                     KeyFrame* pOther) {
    MapPoint* pMP = new MapPoint(Eigen::Vector3f::Zero(), pRefKF, pMap);
    pMP->AddObservation(pRefKF, 0);
    pMP->AddObservation(pOther, 0);
    return pMP;
}

// Everything is intentionally leaked: these are process-lifetime fixtures, and
// tearing an ORB-SLAM3 map down exercises destructors that have nothing to do
// with what is under test here.
int main() {
    // Case 1 -- the field shape. pKFStale was created first, in a map that is
    // then abandoned, so its id is *below* maxKFid and the loop reaches the
    // vertex lookup instead of skipping the observation on the id test.
    {
        Map* pMap = new Map();
        Map* pAbandoned = new Map();

        KeyFrame* pKFStale = makeKeyFrame(pAbandoned);
        KeyFrame* pKF = makeKeyFrame(pMap);

        MapPoint* pMP = makePointObservedBy(pKF, pMap, pKFStale);
        assert(pKFStale->mnId < pKF->mnId);

        pMap->AddKeyFrame(pKF);
        pMap->AddMapPoint(pMP);
        assert(pMap->GetAllKeyFrames().size() == 1);

        const std::string log = runAndCapture(pMap, /*bInit=*/false);
        assert(log.find("[FULL_BA_RECOVERY]") != std::string::npos);
        assert(log.find("skipped_obs=1") != std::string::npos);
    }

    // Case 2 -- same graph, but every observed keyframe is in the map. The guard
    // must stay silent: it is a presence test, not an unconditional skip. This
    // case also runs the optimizer and the recovery loop to completion, which is
    // what proves the fixture above is complete enough to be worth trusting.
    {
        Map* pMap = new Map();

        KeyFrame* pKFOther = makeKeyFrame(pMap);
        KeyFrame* pKF = makeKeyFrame(pMap);

        MapPoint* pMP = makePointObservedBy(pKF, pMap, pKFOther);

        pMap->AddKeyFrame(pKF);
        pMap->AddKeyFrame(pKFOther);
        pMap->AddMapPoint(pMP);
        assert(pMap->GetAllKeyFrames().size() == 2);

        const std::string log = runAndCapture(pMap, /*bInit=*/false);
        assert(log.find("[FULL_BA_RECOVERY]") == std::string::npos);
    }

    // Case 3 -- the bInit shape, which is what LocalMapping actually calls with
    // when it is not the incremental-BA entry point.
    {
        Map* pMap = new Map();
        Map* pAbandoned = new Map();

        KeyFrame* pKFStale = makeKeyFrame(pAbandoned);
        KeyFrame* pKF = makeKeyFrame(pMap);

        MapPoint* pMP = makePointObservedBy(pKF, pMap, pKFStale);
        pMap->AddKeyFrame(pKF);
        pMap->AddMapPoint(pMP);

        const std::string log = runAndCapture(pMap, /*bInit=*/true);
        assert(log.find("[FULL_BA_RECOVERY]") != std::string::npos);
        assert(log.find("b_init=1") != std::string::npos);
    }

    std::cout << "PASS: FullInertialBA skips observations whose keyframe has no "
                 "vertex (one [FULL_BA_RECOVERY] line per affected call) instead "
                 "of dereferencing NULL at Vertex::_fixed, and stays silent when "
                 "every observed keyframe is present\n";
}
