// Hardware-free regression: the inertial optimizers must fall back to visual
// optimization when the inertial prior they need was never built.
//
// Field crash this guards (4x on 09-10/09-11, same address every time):
//   fays-track[N]: segfault at 40 ip ...libORB_SLAM3.so[312e1b] error 4
// 0x312e1b = EdgePriorPoseImu::EdgePriorPoseImu(ConstraintPoseImu*), and 0x40
// is the offset of `bg` in that object, so the constructor was handed NULL.
// Its only caller is PoseInertialOptimizationLastFrame(), which used to reach
// `new EdgePriorPoseImu(pFrame->mpPrevFrame->mpcpi)` with mpcpi == NULL and no
// check in between. A frame that took the visual-only path (see
// Tracking::PreintegrateIMU) never gets an mpcpi, and Tracking::mLastFrame is
// a shallow copy that propagates the NULL into the next frame's mpPrevFrame.
//
// These cases reach the guard's early-return. Remove the guard and every one
// of them dereferences a NULL pointer instead, so the test fails loudly rather
// than silently losing its coverage.
//
// Scope: this is a guard-presence detector, not a reproduction of the field
// crash. Reproducing that exactly needs a frame with a real Preintegrated, a
// real map point cloud, and a populated ConstraintPoseImu whose *neighbours*
// are valid -- far more scaffolding than the regression is worth. What is
// covered here is the state transition that made the pointer NULL.
//
// No USB, Fays SDK, vocabulary or map is opened.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include "Frame.h"
#include "Optimizer.h"
#include <cassert>
#include <iostream>

using namespace ORB_SLAM3;

// Frame::Frame() leaves N indeterminate -- only the image-backed constructors
// set it. Every case here must force N = 0, otherwise PoseOptimization()'s
// `for (int i = 0; i < pFrame->N; i++)` walks an empty mvpMapPoints/vmvKeysUn
// with a garbage bound and the test itself becomes the crash.
static void initBlankFrame(Frame& f) {
    f.N = 0;
    f.mnId = 0;
    f.mTimeStamp = 0.0;
}

int main() {
    // Case 1 -- the field precondition: a previous frame exists, but it took
    // the visual-only path and so carries no inertial prior.
    {
        Frame previous;                      // mpcpi == NULL
        initBlankFrame(previous);
        Frame current;
        initBlankFrame(current);
        current.mpPrevFrame = &previous;
        assert(current.mpPrevFrame->mpcpi == NULL);
        assert(Optimizer::PoseInertialOptimizationLastFrame(&current) == 0);
    }

    // Case 2 -- first frame of a run: no previous frame at all.
    {
        Frame current;
        initBlankFrame(current);
        current.mpPrevFrame = NULL;
        assert(Optimizer::PoseInertialOptimizationLastFrame(&current) == 0);
    }

    // Case 3 -- the keyframe path, whose prior is the last keyframe instead.
    {
        Frame current;
        initBlankFrame(current);
        current.mpLastKeyFrame = NULL;
        assert(Optimizer::PoseInertialOptimizationLastKeyFrame(&current) == 0);
    }

    std::cout << "PASS: a missing inertial prior degrades to visual optimization on "
                 "the previous-frame, first-frame and last-keyframe paths "
                 "(3 [IMU_RECOVERY] lines on stderr are the guard firing)\n";
}
