#ifndef MAPPOINT_CLEANUP_H
#define MAPPOINT_CLEANUP_H

namespace ORB_SLAM3
{

struct BadMapPointCleanupStats
{
    unsigned long long passes = 0;
    unsigned long long candidates = 0;
    unsigned long long compacted = 0;
    unsigned long long queued = 0;
};

// Release detached payload from bad MapPoints without deleting their
// raw-pointer object shells or changing the active map.
BadMapPointCleanupStats CompactBadMapPointPayloads(
    double minAgeSeconds, unsigned long long maxCandidates = 0);
unsigned long long GetQueuedBadMapPointPayloadCount();

} // namespace ORB_SLAM3

#endif // MAPPOINT_CLEANUP_H
