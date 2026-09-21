"""收尾路径的分阶段计时 —— 纯 stdlib，无锁、无 IO、不在这里做重活。

**为什么有这个东西**（2026-09-18 查实的「SLAM 崩溃」）：用户报的轨迹冻结 +
日志刷屏，根因是点「停止录制」之后那段收尾窗口里，我们自己的进程既持了
GIL、又抢了 SLAM 分区的 SMT 兄弟核：

* ``_write_data_parquet`` 的两条 ``list<int16>`` 力矩阵列走 pyarrow 逐值
  装箱慢路径，实测**各持 GIL 1.6s**（本机实测，同表其余列全 <50ms）⇒
  客户端 raw 接收线程连 ``recv()`` 都进不去 ⇒ 服务端 16 包队列（≈0.53s）
  被填满 ⇒ ``g_raw_stereo_dropped != 0`` ⇒ 服务端**主动断链**（当天
  43 次完成录制里 37 次断链；6 次中止不做 parquet，0 次断链）。
* 同一窗口 x265 flush 不限线程，吃满我们的掩码，而掩码里含 SLAM 分区
  {4,5,7,8,9} 的 SMT 兄弟 {16..21} ⇒ 同秒 ``image=32.3 imu=203.1``。

修法（numpy 扁平缓冲 / 线程上限 / 掩码收窄）都依赖这段窗口的**实测**时长，
所以先落地这个仪表、先留档。口径：``monotonic_ns`` 计时（不受墙钟跳变
影响）；单位毫秒、取整；调用点都在停止通路上，必须 O(1)。

用法::

    sw = Stopwatch()
    self._close_ffmpeg();   sw.lap("flush")
    self._write_data_parquet(); sw.lap("parquet")
    ...
    self._log(f"[录制] 收尾 {sw.format()}")
"""
from __future__ import annotations

import time
from typing import Dict, Iterable, Optional


class Stopwatch:
    """顺序分阶段计时：``lap()`` 记的是「距上一次 lap」的耗时。

    同名 lap 累加（收尾里同一段可能被调用多次，如多路 ffmpeg），
    所以 ``snapshot()`` 里一个阶段一个数、语义是「该阶段总耗时」。
    """

    __slots__ = ("_last_ns", "_stages")

    def __init__(self) -> None:
        self._last_ns = time.monotonic_ns()
        self._stages: Dict[str, int] = {}

    def lap(self, name: str) -> int:
        """记一段（自上一次 lap 或构造起算），返回本次毫秒。"""
        now = time.monotonic_ns()
        ms = int((now - self._last_ns) // 1_000_000)
        self._last_ns = now
        self._stages[name] = self._stages.get(name, 0) + ms
        return ms

    def add(self, name: str, ms: int) -> None:
        """并入一段在别处测得的耗时（writer 自己算的那几段走这里）。"""
        self._stages[name] = self._stages.get(name, 0) + int(ms)

    def snapshot(self) -> Dict[str, int]:
        """阶段 → 毫秒（插入序；副本）。"""
        return dict(self._stages)

    def total_ms(self) -> int:
        return sum(self._stages.values())

    def format(self, order: Optional[Iterable[str]] = None) -> str:
        """``flush=12ms parquet=34ms``；``order`` 可钉死显示顺序。

        未命中的阶段显示 ``0ms``（不是省略）——「这段没跑」与「没测到」
        要能一眼分开。
        """
        keys = list(order) if order is not None else list(self._stages)
        return " ".join(f"{k}={self._stages.get(k, 0)}ms" for k in keys)
