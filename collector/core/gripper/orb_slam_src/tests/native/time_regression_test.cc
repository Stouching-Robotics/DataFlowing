// Hardware-free regression for the stale-frame guard in the Fays bridge.
//
// Field symptom this guards: the frame timestamp is the raw SDK
// AtrakImage.timestamp (64-bit nanoseconds) used unchecked, so an occasional
// single-frame regression reaches ORB core Tracking.cc:1932-1938, which clears
// the IMU queue and calls CreateMapInAtlas() -- dropping mState back to
// NO_IMAGES_YET. The application then stops publishing poses ([POSE_HOLD]
// tracking_state=0) and has to redo stereo+IMU initialization. Same stale stamp
// also drives processPoseOutput's interval <= 0, re-anchoring the pose.
// Observed 113x in one log, 107 of them isolated single frames, longest run 4.
//
// preprocessWorker drops those frames via ClassifyFrameTime(). The cases below
// pin the three verdicts and, critically, that the run-length cap makes the
// guard self-limiting: a real clock jump is forwarded after ~1 s instead of
// stalling the pose stream forever.
//
// Scope: this is the decision function only, not the worker. It covers the
// branch that decides a frame's fate; it does not exercise the IMU buffer
// contract around the drop (the stale frame's IMU samples must stay in
// g_imu_buf for the next accepted frame to preintegrate).
//
// No USB, Fays SDK, vocabulary or map is opened.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include "TimeRegressionGuard.h"
#include <cassert>
#include <iostream>

using ksq::ClassifyFrameTime;
using ksq::FrameTimeVerdict;

int main() {
    // Case 1 -- the normal path: a strictly advancing timestamp is accepted.
    assert(ClassifyFrameTime(100.0, 100.04, 0) == FrameTimeVerdict::Accept);
    assert(ClassifyFrameTime(-1.0, 0.0, 0) == FrameTimeVerdict::Accept);

    // Case 2 -- a repeated timestamp is stale too. `<=` not `<` is what makes
    // a duplicated delivery (same stamp, new frame) land here as well.
    assert(ClassifyFrameTime(100.0, 100.0, 0) == FrameTimeVerdict::DropStale);

    // Case 3 -- the field case: one frame goes backwards, next one recovers.
    assert(ClassifyFrameTime(100.0, 99.96, 0) == FrameTimeVerdict::DropStale);
    assert(ClassifyFrameTime(100.0, 99.96, 4) == FrameTimeVerdict::DropStale);

    // Case 4 -- magnitude does not participate. A full 32-bit nanosecond
    // wraparound (4.295 s back) is still just one stale frame, because a
    // wraparound would arrive every 4.295 s while the observed rate was one
    // per 60-270 s and always isolated. Counting run length instead of
    // comparing against a threshold is what avoids inventing a bound with no
    // measurement behind it.
    assert(ClassifyFrameTime(100.0, 95.705, 0) == FrameTimeVerdict::DropStale);

    // Case 5 -- the cap. One below the limit still drops; at the limit the
    // frame is forwarded and the baseline moves to it. This is the escape
    // hatch that keeps a genuinely re-based clock from stalling poses forever.
    assert(ClassifyFrameTime(100.0, 99.0, 29) == FrameTimeVerdict::DropStale);
    assert(ClassifyFrameTime(100.0, 99.0, 30) == FrameTimeVerdict::Rebase);

    // Case 6 -- Rebase resumes normal operation: the accepted timestamp
    // becomes the new baseline, so the next advancing frame is accepted even
    // though it is still below the original one.
    assert(ClassifyFrameTime(99.0, 99.04, 0) == FrameTimeVerdict::Accept);

    std::cout << "PASS: stale frame timestamps are dropped, and a persistent "
                 "regression is forwarded after the run-length cap instead of "
                 "stalling poses\n";
}
