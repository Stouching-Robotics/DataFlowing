"""夹爪 CPU 预留（P5 补丁）：主进程掩码收窄 + raw 流接收线程专用核。

P5 只绑了 native SLAM 进程与触觉进程的线程；主进程（GUI、力矩阵泵、
x265 编码子进程、各采集泵）的线程仍可漂到 SLAM 分区核上抢占
fays_input 抓帧线程 → raw 流突发空桶、显示 <15fps、SLAM 取帧 <30fps。
（后续探针实验定论：损耗主因不是主进程争用，而是 rig1 任务集含毒核
6——见 bridge.py 分区表注释；本模块掩码收窄仍保留，防止主进程线程落
SLAM 分区核或毒核。）

本模块职责：
  - reserve(rig_index)：主线程掩码 = 当前掩码 − 该 rig SLAM 分区 −
    raw 专用核。之后创建的线程与录制开始时 fork 的 ffmpeg 子进程
    （start_episode 在录制开始时由继承掩码的线程调用）继承受限掩码，
    不再与 SLAM 分区抢核。
  - release(rig_index)：退还该 rig 预留；全部 rig 关闭后恢复完整掩码。
  - raw_cpu(rig_index)：raw 流接收线程专用核（SMT 兄弟核，与轻载
    触觉核共享物理核；接收线程负载极轻，只求不被重线程抢占）。
  - pin_raw_thread(cpu)：raw 接收线程启动时自绑（线程级掩码独立于
    继承掩码，可扩回专用核）。

机器 <24 逻辑 CPU 或掩码不含所需核 → 不预留，回退 P5 原状（
bridge._select_cpu_partition 同款回退口径）。
"""

import os

# SLAM 分区（与 bridge.py 常量一致；独立重复一份避免 import 环）。
# rig1 任务集已改为 {4,5,7,8,9}：核 6 因 SMT 兄弟 18 挂 xhci 摄像头
# 中断风暴（流式期间 3000+/s）被整体移出任务集——主进程线程也不得落
# 该核（风暴会拖慢任何实时性敏感的线程），故预留集合仍含 6。
_SLAM_RIG1 = frozenset({4, 5, 6, 7, 8, 9})
_SLAM_RIG2 = frozenset({10, 11, 13, 15, 22, 23})
# raw 专用核：物理核 2（rig1 触觉右）/ 物理核 3（rig2 触觉右）的
# SMT 兄弟（12+2=14、12+3=15）——触觉线程约 40% 单核，raw 接收
# 线程负载极轻，共享物理核无碍
_RAW_CPU_RIG1 = 14
_RAW_CPU_RIG2 = 15

_state = {
    "baseline": None,   # 首次预留前的主线程完整掩码（恢复基准）
    "reserved": set(),  # 当前已预留的 CPU 集合（多 rig 并集）
}


def _all_cpus():
    try:
        return {int(cpu) for cpu in os.sched_getaffinity(0)}
    except (OSError, AttributeError):
        return None


def supported() -> bool:
    """本机是否支持预留（>=24 逻辑 CPU 且完整掩码含全部所需核）。

    必须按 baseline（首次预留前的完整掩码）判断：首台 rig 预留后
    主线程当前掩码已收窄，若按当前掩码判断，第二台 rig 的预留会
    因专用核已不在当前掩码里而被误判为不支持。"""
    cpus = _state["baseline"] if _state["baseline"] is not None else _all_cpus()
    if cpus is None or len(cpus) < 24:
        return False
    return (_SLAM_RIG1 <= cpus and _SLAM_RIG2 <= cpus
            and _RAW_CPU_RIG1 in cpus and _RAW_CPU_RIG2 in cpus)


def raw_cpu(rig_index: int) -> int:
    """rig 序号（1 起）→ raw 流接收线程专用核。"""
    return _RAW_CPU_RIG2 if rig_index >= 2 else _RAW_CPU_RIG1


def pin_raw_thread(cpu: int) -> bool:
    """raw 流接收线程内自绑专用核；失败返回 False（调用方记日志）。"""
    try:
        os.sched_setaffinity(0, {int(cpu)})
        return True
    except (OSError, AttributeError):
        return False


def _apply_mask(logger):
    if _state["baseline"] is None or logger is None:
        return
    mask = _state["baseline"] - _state["reserved"]
    try:
        os.sched_setaffinity(0, mask)
    except (OSError, AttributeError):
        return
    logger(f"[CPU] 主进程掩码收窄至 {sorted(mask)}"
           f"（预留 {sorted(_state['reserved'])}）")


def reserve(rig_index: int, logger=None):
    """打开 rig 时预留其 SLAM 分区 + raw 专用核（主线程调用）。"""
    if _state["baseline"] is None:
        _state["baseline"] = _all_cpus()
    if not supported():
        return None
    rig_set = _SLAM_RIG2 if rig_index >= 2 else _SLAM_RIG1
    _state["reserved"] |= rig_set | {raw_cpu(rig_index)}
    _apply_mask(logger)
    return sorted(_state["reserved"])


def release(rig_index: int, logger=None):
    """关闭 rig 时退还预留；全部关闭后恢复完整掩码（主线程调用）。"""
    if _state["baseline"] is None:
        return
    rig_set = _SLAM_RIG2 if rig_index >= 2 else _SLAM_RIG1
    _state["reserved"] -= rig_set | {raw_cpu(rig_index)}
    if _state["reserved"]:
        _apply_mask(logger)
        return
    try:
        os.sched_setaffinity(0, _state["baseline"])
    except (OSError, AttributeError):
        pass
    _state["baseline"] = None
    if logger:
        logger("[CPU] 夹爪预留全部释放，主进程掩码恢复")
