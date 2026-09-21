"""帧间隔空洞看门狗 —— 纯类，无 Qt / 无 IO / 无第三方依赖。

录制侧（core/pipeline.py 的写入线程）与离线审计脚本（tools/audit_frame_gaps.py）
共用同一个类：**空洞的定义只此一份**，否则两边口径迟早漂移。

术语
----
空洞 = 相邻两帧的时间差**超出标称帧间隔**的部分。只报「断了多久」，不判
「丢了几帧」——帧率本就不恒定，反推帧数会给出看似精确实则错的数字。

时间基座（本类唯一容易踩的坑）
------------------------------
note() 每段录制只喂一个时间源。**首帧决定基座**，之后 source 与基座不符的帧
一律跳过空洞计算（只累加 frames）。混源（设备钟 vs 宿主单调钟）会把两个时钟
的基准差当成一次天文数字级的空洞——2026-09 排查 4.68s 空洞时差点因此误判。
"""


class GapWatch:
    """相邻帧间隔的空洞累计 / 峰值 / 次数。

    参数
    ----
    nominal_ns : 标称帧间隔（30fps → 33_333_333）。只用于算「超出部分」。
    min_gap_ns : **间隔**下限（不是超出量）：相邻帧时间差 ≥ 该值才记为空洞。
                 默认 100ms ≈ 3 帧，实测正常帧距 29.9~37.5ms，有 3 倍余量。
    """

    def __init__(self, nominal_ns: int, min_gap_ns: int):
        self._nominal_ns = int(nominal_ns)
        self._min_gap_ns = int(min_gap_ns)
        self.reset()

    # ── 记账 ────────────────────────────────────────────────
    def reset(self):
        self._source = None        # 本段基座（首次 note 锁定）
        self._last_ns = None       # 上一帧时刻
        self._first_ns = 0         # 本段首帧时刻（相对时间的原点）
        self._frames = 0
        self._span_ns = 0          # 相邻帧时间差之和（连续跨度）
        self._gap_ns = 0           # 空洞累计（已扣除标称间隔）
        self._gap_max_ns = 0
        self._gap_count = 0
        self._gap_max_at_ns = 0    # 最大空洞的**起点**（洞前最后一帧的时刻）
        self._resyncs = 0          # 时间倒退（时钟跳变）次数，不计空洞

    def note(self, ts_ns: int, source: str = "hw") -> int:
        """记录一帧；返回本次空洞 ns（0 = 正常/跳过）。

        source : "hw"   帧自带的采集时刻（parquet 的 hardware_ns 同源）
                 "mono" 取出队瞬间取宿主单调钟（hw_ns=0 的无时间戳槽位，
                        如 lite 路径恒传 0）
        """
        ts_ns = int(ts_ns)
        # 0 是「无时间戳」哨兵，不是时刻 0。**负值是合法时刻**：早期段的设备钟
        # 是有符号 32 位纳秒计数器、值可为负（见 tools/audit_frame_gaps.py 的
        # trunc32），按 <= 0 跳过会把整段负值区当成「无戳」并凭空造出假空洞。
        if ts_ns == 0:
            return 0
        if self._source is None:
            self._source = source
        elif source != self._source:
            # 基座不符：本帧不参与空洞计算（混源只会造出假空洞）
            self._frames += 1
            return 0

        if self._last_ns is None:
            self._last_ns = ts_ns
            self._first_ns = ts_ns
            self._frames += 1
            return 0

        delta = ts_ns - self._last_ns
        if delta < 0:             # 时间倒退：重新锚定，不报空洞
            self._last_ns = ts_ns
            self._resyncs += 1
            self._frames += 1
            return 0

        start_ns = self._last_ns
        self._last_ns = ts_ns
        self._frames += 1
        self._span_ns += delta

        if delta < self._min_gap_ns:
            return 0
        gap = delta - self._nominal_ns
        if gap <= 0:
            return 0
        self._gap_ns += gap
        self._gap_count += 1
        if gap > self._gap_max_ns:
            self._gap_max_ns = gap
            self._gap_max_at_ns = start_ns
        return gap

    # ── 读数 ────────────────────────────────────────────────
    def snapshot(self) -> dict:
        return {
            "source": self._source or "",
            "first_ns": self._first_ns,
            "frames": self._frames,
            "span_ns": self._span_ns,
            "gap_ns": self._gap_ns,
            "gap_max_ns": self._gap_max_ns,
            "gap_count": self._gap_count,
            "gap_max_at_ns": self._gap_max_at_ns,
            "resyncs": self._resyncs,
        }
