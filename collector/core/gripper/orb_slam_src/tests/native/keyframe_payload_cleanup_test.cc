// Hardware-free regression for KeyFrame payload reclamation.
//
// Field context (2026-09-11): a 30-minute session grew RSS from 542 MB to
// 2998 MB while active_kf never left ~60 and kf_created reached 4333.  No
// KeyFrame is ever freed.  It cannot be deleted -- raw KeyFrame* is shared
// across Tracking, LocalMapping and LoopClosing, and Tracking::mpLastKeyFrame
// is assigned in CreateNewKeyFrame and dereferenced every frame with no way to
// learn the object went away -- so every keyframe retired by KeyFrameCulling()
// or PeriodicTimeWindowCleanup() keeps its whole payload for the life of the
// process.  The feature grid is the largest of those allocations, and it is
// read from exactly one place, so it is the one piece
// KeyFrame::CompactBadPayload() releases.
//
// Four things are pinned here:
//   1. a live keyframe is never a candidate, however long the queue runs;
//   2. the grace period really holds the payload back;
//   3. a released keyframe answers GetFeaturesInArea with nothing rather than
//      indexing a cleared grid -- the grid is empty but mnGridCols/mnGridRows
//      are const members still holding FRAME_GRID_COLS/FRAME_GRID_ROWS, so the
//      cell loops would run off the end without the empty check;
//   4. SetBadFlag() never destroys the object, which is what lets LocalMapping
//      stop calling delete on keyframes Tracking still names.
//
// No USB, Fays SDK, vocabulary, image or IMU data is opened.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include "Frame.h"
#include "KeyFrame.h"
#include "KeyFrameCleanup.h"
#include "Map.h"
#include <cassert>
#include <iostream>

using namespace ORB_SLAM3;

// 640x480 over FRAME_GRID_COLS x FRAME_GRID_ROWS cells gives a 0.1 inverse cell
// size in both axes, so the single feature at (10,10) lands in cell (1,1) and
// GetFeaturesInArea(10,10,r=2) scans cells [0..2][0..2].
static constexpr float kFeatureX = 10.f;
static constexpr float kFeatureY = 10.f;
static constexpr int kFeatureCol = 1;
static constexpr int kFeatureRow = 1;

// Frame's default constructor leaves scalars indeterminate; everything the
// KeyFrame constructor copies or GetFeaturesInArea indexes is set here.
static void initGriddedFrame(Frame& f) {
    f.N = 1;
    f.mnId = 0;
    f.mTimeStamp = 0.0;
    // Monocular left keyframe: the keyframe constructor then skips resizing
    // mGridRight entirely, and GetFeaturesInArea reads mvKeysUn.
    f.Nleft = -1;
    f.Nright = 0;
    f.mpCamera = nullptr;
    f.mpCamera2 = nullptr;
    f.mnScaleLevels = 8;
    f.mvScaleFactors.assign(f.mnScaleLevels, 1.0f);
    f.mvLevelSigma2.assign(f.mnScaleLevels, 1.0f);
    f.mvInvLevelSigma2.assign(f.mnScaleLevels, 1.0f);
    f.mvKeysUn.assign(f.N, cv::KeyPoint(kFeatureX, kFeatureY, 1.f));
    f.mvuRight.assign(f.N, -1.0f);
    f.mvDepth.assign(f.N, 1.0f);
    // SetBadFlag() walks mvpMapPoints; nulls keep it a no-op there.
    f.mvpMapPoints.assign(f.N, static_cast<MapPoint*>(NULL));

    f.mnMinX = 0.f;
    f.mnMinY = 0.f;
    f.mnMaxX = 640.f;
    f.mnMaxY = 480.f;
    f.mfGridElementWidthInv = static_cast<float>(FRAME_GRID_COLS) / 640.f;
    f.mfGridElementHeightInv = static_cast<float>(FRAME_GRID_ROWS) / 480.f;
    // Frame::mGrid is a plain array of vectors, so this cell starts empty and
    // the keyframe constructor copies it cell by cell.
    f.mGrid[kFeatureCol][kFeatureRow].push_back(0);
}

static KeyFrame* makeKeyFrame(Map* pMap) {
    Frame F;
    initGriddedFrame(F);
    // Null KeyFrameDatabase: SetBadFlag() guards the erase with `if(mpKeyFrameDB)`.
    return new KeyFrame(F, pMap, nullptr);
}

static size_t featuresVisibleFrom(KeyFrame* pKF) {
    return pKF->GetFeaturesInArea(kFeatureX, kFeatureY, 2.f, false).size();
}

int main() {
    // Everything is intentionally leaked: these are process-lifetime fixtures,
    // and tearing an ORB-SLAM3 map down exercises destructors that have nothing
    // to do with what is under test here.
    Map* pMap = new Map();

    // Case 1 -- a live keyframe is never queued, so no pass can reach it.
    KeyFrame* pKFLive = makeKeyFrame(pMap);
    assert(!pKFLive->isBad());
    assert(featuresVisibleFrom(pKFLive) == 1);

    const unsigned long long compactedAfterFirstPass =
        CompactBadKeyFramePayloads(0.0, 0).compacted;
    assert(compactedAfterFirstPass == 0);
    assert(featuresVisibleFrom(pKFLive) == 1);

    // Case 2 -- SetBadFlag queues the keyframe, but the grace period holds the
    // payload back so a reader that was already in flight still finds it.
    KeyFrame* pKFBad = makeKeyFrame(pMap);
    pKFBad->SetBadFlag();
    assert(pKFBad->isBad());

    const unsigned long long compactedBeforeGrace =
        CompactBadKeyFramePayloads(3600.0, 0).compacted;
    assert(compactedBeforeGrace == compactedAfterFirstPass);
    assert(featuresVisibleFrom(pKFBad) == 1);

    // Case 3 -- once the grace expires the grid is released, and reading it
    // afterwards returns nothing instead of running off the end of a cleared
    // vector.  Exactly one object is compacted: the live keyframe above is not
    // in the queue.
    const BadKeyFrameCleanupStats afterRelease =
        CompactBadKeyFramePayloads(0.0, 0);
    assert(afterRelease.compacted == compactedBeforeGrace + 1);
    assert(featuresVisibleFrom(pKFBad) == 0);
    assert(afterRelease.queued == 0);

    // Case 4 -- the object itself survives both SetBadFlag() and the payload
    // release.  GetPose() takes mMutexPose, so this is exactly the lock that
    // would fault if the keyframe had been deleted instead of reclaimed.
    // LocalMapping no longer deletes keyframes out of mlNewKeyFrames precisely
    // because Tracking::mpLastKeyFrame keeps naming them.
    const Sophus::SE3f pose = pKFBad->GetPose();
    (void)pose;
    assert(pKFBad->mTimeStamp == 0.0);

    // Case 5 -- a released identity is never queued or compacted a second time.
    const BadKeyFrameCleanupStats repeat = CompactBadKeyFramePayloads(0.0, 0);
    assert(repeat.compacted == afterRelease.compacted);
    assert(repeat.candidates == afterRelease.candidates);

    std::cout << "PASS: CompactBadKeyFramePayloads releases the feature grid of a "
                 "bad keyframe after the grace period, leaves live and "
                 "still-in-grace keyframes untouched, makes a released keyframe "
                 "report no features instead of indexing a cleared grid, and "
                 "never destroys the object itself\n";
}
