#ifndef RUNTIME_MAP_DIAGNOSTICS_H
#define RUNTIME_MAP_DIAGNOSTICS_H

namespace ORB_SLAM3
{

struct RuntimeMapDiagnostics
{
    int state = -1;
    unsigned long long inliers = 0;
    unsigned long long maps = 0;
    unsigned long long activeKf = 0;
    unsigned long long activeMp = 0;
    unsigned long long localKf = 0;
    unsigned long long localMp = 0;
    unsigned long long lmQueue = 0;
    unsigned long long kfCreated = 0;
    unsigned long long mpCreated = 0;
};

// Read-only counters used by the Fays runtime diagnostics. They do not own,
// retire or delete any SLAM object.
unsigned long long GetCreatedKeyFrameCount();
unsigned long long GetCreatedMapPointCount();
void UpdateRuntimeTrackingLocalCounts(
    unsigned long long localKf, unsigned long long localMp);
RuntimeMapDiagnostics GetRuntimeMapDiagnostics();

} // namespace ORB_SLAM3

#endif // RUNTIME_MAP_DIAGNOSTICS_H
