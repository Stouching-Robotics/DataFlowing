"""夹爪 RGB 采集链路的静默丢帧可见化 —— 纯类，无 Qt / 无 IO。

2026-09-18 的 episode-099 在录到第 45 帧时**丢了 4.68 秒**（两个时钟同时跳），
而当时所有计数器都是 0：`_rgb_run` 的 `read()` 失败分支只睡 5ms 接着来，
latest-wins 帧槽的覆盖更是连一行日志都没有。事后翻遍日志也无法判断是
「相机侧没帧可读」还是「帧来了被 emit/GUI 侧吞掉」。

这里的两个类就是那两处的计数器。它们只记账、只产出日志行，不做 IO、不发信号
——调用方（bridge）把日志行交给 ``_log``（内部走 _events 队列，任意线程安全）。
"""

import threading


class RgbReadWatch:
    """采集侧看门狗：``read()`` 失败连击 + latest-wins 覆盖计数。

    去抖契约照抄 ``core/gripper/slam/process_controller.py`` 的 FaysRateAlarm：
    健康态静默；进入停摆打一行（带重连次数与最近错误）；持续停摆每
    ``repeat_ns`` 重复一行；恢复再打一行收尾。这样一次抽风只留两行，长时间
    故障也不刷屏，而恢复行让「抽了一下还是一直坏」一眼可辨。

    ``note_fail`` / ``note_ok`` 返回**要写进日志的行列表**（多数时候为空）。
    线程契约：RGB 线程写、主线程读快照，类内自锁。
    """

    def __init__(self, *, alert_ns: int, repeat_ns: int, tag: str = "夹爪-RGB"):
        self._alert_ns = int(alert_ns)
        self._repeat_ns = int(repeat_ns)
        self._tag = tag
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self._fail_since_ns = None   # 本次停摆起点（None = 正常）
            self._fail_ns = 0            # 停摆累计时长
            self._fail_max_ns = 0
            self._fail_stalls = 0
            self._alerted = False        # 已经在日志里报过停摆（尚未恢复）
            self._last_alert_ns = None
            self._overwrites = 0

    # ── 采集侧 ──────────────────────────────────────────────
    def note_fail(self, mono_ns: int, *, reconnects: int = 0,
                  error: str = "") -> list:
        """read() 返回 not ok。停摆未达门槛时静默。"""
        with self._lock:
            if self._fail_since_ns is None:
                self._fail_since_ns = int(mono_ns)
            elapsed = int(mono_ns) - self._fail_since_ns
            if elapsed < self._alert_ns:
                return []
            first = not self._alerted
            if not first and (self._last_alert_ns is not None
                              and int(mono_ns) - self._last_alert_ns < self._repeat_ns):
                return []
            self._alerted = True
            self._last_alert_ns = int(mono_ns)
            secs = elapsed / 1e9
            if first:
                return [f"[{self._tag}告警] RGB 采集停摆 {secs:.1f}s——read() "
                        f"连续失败，相机侧已无帧可读"
                        f"（重连 {int(reconnects)} 次{self._err(error)}）"]
            return [f"[{self._tag}告警] RGB 采集仍未恢复（已停摆 {secs:.1f}s，"
                    f"重连 {int(reconnects)} 次）"]

    def note_ok(self, mono_ns: int, *, reconnects: int = 0,
                error: str = "") -> list:
        """read() 拿到帧。健康帧不会产生任何日志行。"""
        with self._lock:
            if self._fail_since_ns is None:
                return []
            stall = int(mono_ns) - self._fail_since_ns
            self._fail_since_ns = None
            self._fail_ns += stall
            self._fail_stalls += 1
            if stall > self._fail_max_ns:
                self._fail_max_ns = stall
            if not self._alerted:
                return []
            self._alerted = False
            return [f"[{self._tag}] RGB 采集已恢复（停摆 {stall / 1e9:.1f}s，"
                    f"重连 {int(reconnects)} 次{self._err(error)}）"
                    f"——本段视频该处会有一段静止"]

    def note_overwrite(self, n: int = 1):
        """latest-wins 帧槽被更新的帧顶掉（帧确实采到了，只是没人及时取走）。"""
        with self._lock:
            self._overwrites += int(n)

    @staticmethod
    def _err(error: str) -> str:
        """最近一次的传输错误（空则整段省略；截断免得刷屏）。"""
        if not error:
            return ""
        return f"；最近错误: {str(error)[:60]}"

    # ── 读数 ────────────────────────────────────────────────
    def snapshot(self, now_ns: int = None) -> dict:
        """now_ns 传入时，尚未恢复的停摆也计入（否则进行中的停摆会少报）。"""
        with self._lock:
            fail_ns = self._fail_ns
            fail_max_ns = self._fail_max_ns
            stalls = self._fail_stalls
            if self._fail_since_ns is not None and now_ns is not None:
                ongoing = max(0, int(now_ns) - self._fail_since_ns)
                fail_ns += ongoing
                fail_max_ns = max(fail_max_ns, ongoing)
                stalls += 1
            return {
                "readfail_ns": fail_ns,
                "readfail_max_ns": fail_max_ns,
                "readfail_stall_count": stalls,
                "overwrite_count": self._overwrites,
            }


class MaxLagWatch:
    """滞后峰值 + 样本数（emit 侧滞后 / GUI 派发滞后共用）。

    只记峰值：这两个量的价值在「最坏卡了多久」，平均值会被 30Hz 的正常
    小抖动稀释到看不出问题。线程安全（可能由采集线程写、主线程读）。
    """

    def __init__(self, tag: str = ""):
        self._tag = tag
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self._lag_max_ns = 0
            self._samples = 0

    def note(self, lag_ns: int):
        lag_ns = int(lag_ns)
        if lag_ns < 0:
            return
        with self._lock:
            self._samples += 1
            if lag_ns > self._lag_max_ns:
                self._lag_max_ns = lag_ns

    def snapshot(self) -> dict:
        with self._lock:
            return {"lag_max_ns": self._lag_max_ns, "lag_samples": self._samples}


class RawStallWatch:
    """原始流**接收侧**停滞：相邻两个完整包的间隔峰值 + 超门槛次数。

    L0-2（v1.3.11）。SLAM 进程是原始流的**服务端**，本进程的接收线程是
    Python 线程：它一旦因为 GIL 被别人占住而进不去 ``recv()``，服务端
    16 包队列（≈0.53s）填满就判 ``stereo loss`` 并**主动 close**。实测
    2026-09-18：43 次完成录制里 37 次在收尾窗口断链，而 6 次中止（不做
    parquet 落盘）一次都没断——怀疑是 ``_write_data_parquet`` 建两条
    ``list<int16>`` 力矩阵列时各持 GIL 1.6s。这类读数此前完全没有：
    接收线程卡住就是卡住，没人记账。

    与 ``RgbReadWatch`` 的差别：那边抓的是「``read()`` 失败」这种显式
    事件，这边只能靠**间隔**——所以用间隔峰值 + 超门槛计数表达。
    跨断链的间隔也算（那正是重连黑洞的真实时长）。

    线程契约：接收线程写、主线程读快照，类内自锁。``note_recv`` 返回
    **要写进日志的行列表**（多数时候为空），本类自己不碰 IO。
    """

    def __init__(self, *, stall_ns: int, alert_ns: int, repeat_ns: int,
                 tag: str = "Gripper-Raw"):
        self._stall_ns = int(stall_ns)
        self._alert_ns = int(alert_ns)
        self._repeat_ns = int(repeat_ns)
        self._tag = tag
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self._last_ns = None
            self._packets = 0
            self._gap_max_ns = 0
            self._stall_ns_total = 0
            self._stalls = 0
            self._alerted = False        # 已在日志里报过（尚未恢复）
            self._last_alert_ns = None

    def note_recv(self, mono_ns: int) -> list:
        """收到一个完整包。返回要打的行（健康时恒为空列表）。"""
        with self._lock:
            now = int(mono_ns)
            last = self._last_ns
            self._last_ns = now
            self._packets += 1
            if last is None:
                return []
            gap = now - last
            if gap < 0:                  # 单调钟倒退（不该发生，防御）
                return []
            if gap > self._gap_max_ns:
                self._gap_max_ns = gap
            if gap < self._stall_ns:
                self._alerted = False    # 流已恢复，下次抽风重新报
                return []
            self._stalls += 1
            self._stall_ns_total += gap
            if gap < self._alert_ns:
                return []
            first = not self._alerted
            if not first and (self._last_alert_ns is not None
                              and now - self._last_alert_ns < self._repeat_ns):
                return []
            self._alerted = True
            self._last_alert_ns = now
            ms = gap / 1e6
            if first:
                return [f"[{self._tag}] 原始流接收停滞 {ms:.0f}ms"
                        f"——服务端队列 ≈0.53s 满即主动断开"
                        f"（本段已停顿 {self._stalls} 次）"]
            return [f"[{self._tag}] 原始流接收仍未恢复（本次停顿 {ms:.0f}ms，"
                    f"累计 {self._stalls} 次）"]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "gap_max_ns": self._gap_max_ns,
                "stall_ns": self._stall_ns_total,
                "stall_count": self._stalls,
                "packets": self._packets,
            }
