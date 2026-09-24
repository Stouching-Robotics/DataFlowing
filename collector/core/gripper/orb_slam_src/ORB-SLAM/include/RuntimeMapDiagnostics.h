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

// 地图「大改动」计数器，取值等同 System::MapChanged() 内部的
// mpAtlas->GetLastBigChangeIdx()。每帧刷新（不受下面诊断快照的 1 秒节流影响）。
//
// 为什么要有它：桥接的 SDK 引擎路径（KSQ_FAYS_SLAM_ENGINE=sdk）走
// slam_sdk::Slam，而 SDK 不提供 MapChanged()。这个计数器让桥接能在
// **同一颗 libORB_SLAM3.so** 上复刻 System::MapChanged() 的轮询语义，
// 从而保住 output.map_changed 这个位姿重锚触发源。
//
// 2026-09-23：SDK 接入已整体回退，回退后的桥接重新直接调
// System::MapChanged()，本计数器暂无调用方（保留：它在现役核心库导出符号里，
// 与 System.h 的只读接口同理 —— 删要重编核心库）。
int GetRuntimeMapChangeIndex();
void UpdateRuntimeTrackingLocalCounts(
    unsigned long long localKf, unsigned long long localMp);
RuntimeMapDiagnostics GetRuntimeMapDiagnostics();

} // namespace ORB_SLAM3

#endif // RUNTIME_MAP_DIAGNOSTICS_H
