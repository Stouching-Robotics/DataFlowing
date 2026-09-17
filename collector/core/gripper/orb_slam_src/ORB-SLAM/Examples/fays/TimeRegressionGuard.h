#ifndef KSQ_TIME_REGRESSION_GUARD_H
#define KSQ_TIME_REGRESSION_GUARD_H

// 帧时间戳单调性判定。
//
// 现场问题：帧时间戳直接取 SDK 原始 AtrakImage.timestamp（64 位纳秒），全程无
// 校验。偶发的单帧回退（全日志 113 次，其中 107 次是孤立单帧，最长连 4 帧）会
// 命中 ORB 核心 Tracking.cc:1932-1938 —— 清空 IMU 队列 + CreateMapInAtlas()，
// 后者把 mState 打回 NO_IMAGES_YET，应用侧随即停发位姿并需要重做双目+IMU 初始化
// （移动中 0.2~1.34 s，静止时因 not enough acceleration 闸门更久）。同一个陈旧
// 时间戳还会让应用侧 processPoseOutput 的 interval <= 0，触发一次位姿重锚。
//
// 判定只看「连续回退了几帧」，不看倒退幅度：幅度阈值没有实测支撑（现场不打
// delta），而连续计数同时兜得住两种形态 —— 单帧脏读被丢弃，时钟真的跳变则在连丢
// max_consecutive_drops 帧（约 1 s）后回落到今天的行为（接受并换基准），不会把
// 位姿流永久卡死。
//
// 纯头文件、无依赖，便于离线回归（online/tests/native/time_regression_test.cc）。

namespace ksq {

enum class FrameTimeVerdict {
    Accept,     // current 严格大于基准，正常前进
    DropStale,  // current <= 基准且连续次数未到上限，丢弃该帧
    Rebase,     // 连续回退已达上限，判定为时钟跳变：接受该帧并换基准
};

inline FrameTimeVerdict ClassifyFrameTime(double previous, double current,
                                          unsigned consecutive_drops,
                                          unsigned max_consecutive_drops = 30) {
    if (current > previous) return FrameTimeVerdict::Accept;
    if (consecutive_drops < max_consecutive_drops) {
        return FrameTimeVerdict::DropStale;
    }
    return FrameTimeVerdict::Rebase;
}

}  // namespace ksq
#endif
