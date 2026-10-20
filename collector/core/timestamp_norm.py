"""设备时间戳归一：把「补帧造成的重复戳」重建成均匀 + 严格递增。

为什么需要它
------------
`rs2::pipeline.wait_for_frames()` 的 syncer 在多流模式下会给「本拍没出新帧」的
那一路**补上一帧旧帧**（`frame_counter` 不前进），本拍到两帧时又会跳号。
于是写进 parquet 的 `hardware_ns` 会出现：

* **Δ = 0**：相邻两行同一个时间戳（补帧）；
* **Δ = 2T**：紧接着的双倍间隔，或孤立的跳号。

真机实测（D435 213622075531，30fps）：7134 个相邻间隔只有 5 种取值，
Δ=0 共 18 次、Δ=2T 共 25 次。而同一批数据里宿主侧到达节奏全程正常
（Δwall ≈ 33ms）、**行数 == 视频帧数 == 容器 nb_frames** —— 一帧都没丢，
坏的只是时间戳数值。

本类的职责就一条：**一帧不丢，把输出变成 `锚点 + 序号 × T`**。

与离线工具的关系
----------------
`tools/fix_hardware_ns_uniform.py` 对已录好的数据做同一件事（默认 uniform 口径），
本类是它在**录制期**的等价物，口径一致：同一个 T（干净间隔的中位/众数）、
同一个锚点（该路首帧的原始戳）。

注意保留诊断
------------
归一后 `hardware_ns` 不再有 Δ=0，**"这一帧是补帧"这个信息不会从时间戳上看出来**。
所以调用方必须继续写 `rgb_dup` / `rgb_gap` / `depth_dup` / `depth_gap`
（`core/d435_camera._stamp_frame` 已经在写），否则这次修复就变成"把问题藏起来"。
"""
from typing import List, Optional

# 估计 T 时只收「像一拍」的间隔，避免被 0 / 2T 伪影带偏
_CLEAN_LO = 0.5
_CLEAN_HI = 1.5


class UniformTsClock:
    """单路设备时间戳 → 均匀严格递增 ns。

    锁定行为：T 未锁定时按 `nominal_ns` 走，收满 `lock_frames` 个干净间隔后
    锁定为其实测**均值**并从此不变。锁定瞬间有一个**追赶步**（D435 实测
    60 帧约 0.63 ms），因为前 59 帧是按标称 33.333ms 排的、而设备实际是
    33.344ms。

    ⚠️ 这个拐点落在**每段录制的第 60 帧附近**：归一时钟只在真正写行时前进
    （= 只在录制期间），所以预览期学不到 T。第二段起 `begin_segment()` 会
    保留已锁定的 T，只有**开机后第一段**才有这个拐点。拐点 < 1ms，比它要修的
    33~133ms 伪影小两个数量级。

    Args:
        nominal_ns: 标称帧周期（ns），如 30fps → 33_333_333。T 未锁定时先用它。
        lock_frames: 收满这么多**干净**间隔后锁定 T（**均值**）。锁定后不再变，
                     避免录制中途斜率跳变。
    """

    def __init__(self, nominal_ns: int, lock_frames: int = 60):
        self._nominal = int(nominal_ns)
        self._lock_frames = int(lock_frames)
        self.reset()

    def reset(self):
        """开流/重连时调用：计数器与设备钟都从新管道的首帧重新锚定。"""
        self._anchor: Optional[int] = None
        self._last_raw: Optional[int] = None
        self._k = 0
        self._T = self._nominal
        self._clean: List[int] = []
        self._locked = False
        self.frames = 0

    def begin_segment(self):
        """新的一段录制：重新锚定到本段首帧，**保留已锁定的 T**。

        为什么要分段重锚：整条录制链路是"每录一行前进一拍"，若跨 episode 也
        连续累加，两段之间的空闲时间就会被抹掉，episode 之间不再可比。
        重锚后每段从自己的首帧真实设备戳起步，空档保留。
        """
        self._anchor = None
        self._last_raw = None
        self._k = 0
        self.frames = 0

    # ── 只读诊断 ────────────────────────────────────────
    @property
    def period_ns(self) -> int:
        return self._T

    @property
    def locked(self) -> bool:
        return self._locked

    def to_ns(self, raw_ns: Optional[int]) -> int:
        """把本帧的原始设备戳换成均匀递增的 ns。

        0 / None 表示「本帧没有时间戳」（仓库既有约定：0=未知，不是时刻 0），
        原样返回 0，且**不占用序号** —— 否则后续帧会被平白推进一拍。
        """
        if not raw_ns:
            return 0
        raw = int(raw_ns)

        if self._anchor is None:            # 该路首帧：用真实值做锚点
            self._anchor = raw
            self._last_raw = raw
            self._k = 0
            self.frames = 1
            return raw

        d = raw - self._last_raw
        self._last_raw = raw
        if not self._locked and d > 0:
            if _CLEAN_LO * self._nominal <= d <= _CLEAN_HI * self._nominal:
                self._clean.append(d)
                if len(self._clean) >= self._lock_frames:
                    # 用**均值**而不是中位：均匀网格要求 n×T ≈ 真实跨度，
                    # 只有均值满足这一点。宿主钟源的到达间隔是偏斜双峰
                    # （UMI 实测 30~33ms 与 36~38ms 两簇，均值 33.337ms、
                    # 中位 32.1ms）—— 中位会把速率算低 3.7%，整段漂掉几百 ms。
                    # 设备钟源（D435）间隔锁死在 33343/33344 µs，两者只差 2ppm。
                    self._T = int(round(sum(self._clean) / len(self._clean)))
                    self._locked = True

        self._k += 1
        self.frames += 1
        return self._anchor + self._k * self._T

    def to_ns_pair(self, raw_ns: Optional[int]):
        """返回 (归一后 ns, 是否发生修正)：修正 = 原始值与上一帧的间隔不是一拍。"""
        if not raw_ns:
            return 0, False
        raw = int(raw_ns)
        prev = self._last_raw
        out = self.to_ns(raw)
        fixed = False
        if prev is not None:
            d = raw - prev
            fixed = not (_CLEAN_LO * self._T <= d <= _CLEAN_HI * self._T)
        return out, fixed
