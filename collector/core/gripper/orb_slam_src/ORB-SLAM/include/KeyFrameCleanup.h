#ifndef KEYFRAME_CLEANUP_H
#define KEYFRAME_CLEANUP_H

namespace ORB_SLAM3
{

struct BadKeyFrameCleanupStats
{
    unsigned long long passes = 0;
    unsigned long long candidates = 0;
    unsigned long long compacted = 0;
    unsigned long long queued = 0;
};

// Release detached payload from bad KeyFrames without deleting their
// raw-pointer object shells or changing the active map.
//
// Counterpart of CompactBadMapPointPayloads().  A bad KeyFrame cannot be
// deleted: Tracking::mpLastKeyFrame is assigned right after a keyframe is
// queued and is dereferenced every frame, with no way to learn the object went
// away, and LocalMapping/LoopClosing keep covisibility snapshots that may still
// name it.  Its feature grid, however, is dead weight the moment the keyframe
// leaves the map -- see KeyFrame::CompactBadPayload().
BadKeyFrameCleanupStats CompactBadKeyFramePayloads(
    double minAgeSeconds, unsigned long long maxCandidates = 0);
unsigned long long GetQueuedBadKeyFramePayloadCount();

} // namespace ORB_SLAM3

#endif // KEYFRAME_CLEANUP_H
