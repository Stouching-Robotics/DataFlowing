#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""陈旧帧取证：下陷帧到底是「新图像配了旧戳」还是「旧图像被重投递」。

背景：桥接的单调守卫（`fayssense_orb_slam.cc` 的 `ClassifyFrameTime`）会丢掉
时间戳没前进的帧，日志形如

    [TIME_DROP] ts=354.486 previous=354.546 delta=-0.060 seq=16504 prev_seq=16503

现场 508 个事件里 seq **全部严格前进**，据此我曾断言「帧是新的、只有戳错」。
但那一步是循环论证：`seq` 若是 SDK 在宿主侧按投递次数自增的计数器（SDK 头
`fays_atrak_types.h` 对它没有任何语义说明），「同一帧被重投递」会给出**一模
一样**的日志。两条假设在 ts 和 seq 上处处等价 ——

    H1 只戳错     帧号计数器滞后 L 帧（戳 = 计数 × 20ms），**图像是新的**
    H2 重投递     往前第 L 帧的载荷连同它的戳被再交一次，**图像是旧的**

—— 差别只在像素。所以判据只能是像素。**但裸 mad（平均绝对像素差）不够**：
09-17 真机抓到的 4 个下陷帧里，3 个的像素差与正常帧**没有区别**（同一瞬间被
重新渲染、整幅电平变了），裸 mad 会把它们判成 H1。真正分得开的是**结构**：
每帧取 |Δx|+|Δy| 得边缘图，整幅电平变化不改边缘图。对每个下陷帧：

  1. 与往前 1..8 帧各求一次边缘差 e(k)，看最小值落在哪个 lag；
  2. 拿**同一个窗里的正常帧**做对照，得出正常帧的 e(L)/e(1) 是多少
     （现场实测 1.03~1.29 —— 运动越快越大，所以必须同时段的对照，不能拍脑袋定阈值）。

    下陷帧的 e(L)/e(1) 明显低于对照 且 最小 lag 正是 L  →  H2，今天丢帧是对的
    下陷帧最像 i-1、且 e(L)/e(1) 与对照同量级              →  H1，该把戳修回来
    每个下陷帧都逐字节等于往前第 L 帧                       →  H2_EXACT（最强）

取证（现役二进制已带，**不用重编**，但它只在**连接夹爪**时开录）：

    venv/bin/python tools/diag_frame_trace.py preflight --dir /tmp/ksq-frame-trace
    # 照它打印的三行 export，然后 venv/bin/python main.py，照常连夹爪；
    # **这几分钟里让夹爪一直动** —— 静止时相邻帧本来就几乎一样，判别力归零。

分析：

    venv/bin/python tools/diag_frame_trace.py analyze /tmp/ksq-frame-trace/xxx \\
        --native-log logs/slam_native/<...>_slam_stdout.log

输入是 `KSQ_FAYS_DEBUG_DIR` 下的 `samples-NNNN.bin`（每 512MB 一片），格式与
录制链路同源：80 字节 `RawStreamHeader` + 载荷，常量和
`core/gripper/recording/fays_raw_client.py` 逐字对齐（那里是已被真机验证过的
读法）。取证点在回调**最前面**，早于 3-取-5 抽帧和单调守卫，所以它比
`[TIME_DROP]` 日志全。
"""
import argparse
import collections
import datetime
import glob
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 与 core/gripper/recording/fays_raw_client.py 逐字对齐（不得单独改）
HEADER = struct.Struct("<6I3Q4i2hi2I")
HEADER_SIZE = HEADER.size
RAW_STREAM_MAGIC = 0x53544F55
RAW_STREAM_VERSION = 1
KIND_STEREO = 1
KIND_IMU = 2
IMU_PAYLOAD_SIZE = 48

GRID_NS = 20_000_000          # 设备戳是合成格子：帧计数器 × 20ms
DEFAULT_LAGS = (1, 2, 3, 4, 5, 6, 7, 8)
STRIDE = 4                    # mad 抽样步长（下陷与基线用同一估计量）
EDGE_STRIDE = 1               # 边缘图抽点（1=全分辨率；调大换速度）
H2_ZERO_RATIO = 0.2           # e(i,i-L) ≤ 0.2·e(i,i-1) → 结构就是那一幅
H2_EDGE_FACTOR = 0.75         # e(L)/e(1) 低于同窗对照的这么多倍 → 载荷是 L 帧前那一瞬
H1_EDGE_BAND = (0.8, 1.3)     # 与对照曲线同量级 → 载荷是新的
EDGE_FLAT_RATIO = 1.15        # 各 lag 的边缘差挤在 1.15 倍内 ⇒ 太静，判不了
STATIC_PAIRS = 2              # 窗内逐字节相同的正常帧对 ≥ 此数 ⇒ 精确相同没信息量
EDGE_BASE_EVERY = 100         # 全场边缘差基线每多少帧采一次（贵，只做旁证）
MIN_FREE_BYTES = 2 * 1024**3  # C++ 侧硬要求：严格大于 2 GiB
STEREO_FPS = 50.0
FRAME_BYTES = 640 * 800 * 1   # 仅用于预估体积，不必精确


# ── 取证 ────────────────────────────────────────────────────────────────

def preflight(directory, seconds, cpus):
    """开录前的预检：C++ 侧 `start()` 失败会让**桥接直接退出**（夹爪连不上），
    所以把这些条件在开录前挡掉。返回本次要用的取证目录。"""
    problems = []

    stale = [k for k in ("KSQ_FAYS_DEBUG_DIR", "KSQ_FAYS_DEBUG_SECONDS",
                         "KSQ_FAYS_DEBUG_CPUS") if os.environ.get(k)]
    if stale:
        problems.append(f"当前 shell 已有 {', '.join(stale)} —— 会被下面几行覆盖，"
                        f"但别忘了它们对别的终端也生效")

    if not (0 < seconds <= 300):
        problems.append(f"--seconds={seconds} 越界（C++ 只接受 0 < s ≤ 300）")
    cpu_list = []
    for token in str(cpus).split(","):
        token = token.strip()
        if not token.isdigit():
            problems.append(f"--cpus={cpus} 里有非数字项 {token!r}")
            continue
        cpu_list.append(int(token))
    if not cpu_list:
        problems.append("--cpus 为空")
    else:
        usable = os.sched_getaffinity(0)
        outside = [c for c in cpu_list if c not in usable]
        if outside:
            problems.append(f"--cpus 里 {outside} 不在本进程可用掩码内（{sorted(usable)}）")

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    target = os.path.join(directory, f"capture-{stamp}")
    if os.path.isdir(directory) and glob.glob(os.path.join(directory, "samples-*.bin")):
        # 分片名从 samples-0000 开始，C++ 会**直接覆盖**同名文件
        problems.append(f"{directory} 下已有 samples-*.bin —— C++ 分片从 "
                        f"samples-0000.bin 起编，会覆盖旧取证；换个 --dir")
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        problems.append(f"建目录 {target} 失败：{exc}")

    estimate = int(seconds * STEREO_FPS * FRAME_BYTES * 1.02)
    if os.path.isdir(target):
        free = shutil.disk_usage(target).free
        if free <= MIN_FREE_BYTES:
            problems.append(f"{target} 所在盘空闲 {free / 2**30:.2f} GiB "
                            f"≤ 2 GiB（C++ 硬要求）")
        elif free < estimate * 1.2:
            problems.append(f"空闲 {free / 2**30:.2f} GiB 对预计体积 "
                            f"{estimate / 2**30:.2f} GiB 太紧（留 20% 余量）")

    running = subprocess.run(["pgrep", "-af", "main.py"], capture_output=True,
                             text=True).stdout.strip()
    if running:
        problems.append(f"已经有 main.py 在跑：\n    {running}")

    print(f"取证目录（本次专用，不复用）：{target}")
    print(f"预计体积：{seconds:.0f}s × {STEREO_FPS:.0f}Hz × "
          f"{FRAME_BYTES / 1024:.0f}KiB ≈ {estimate / 2**30:.2f} GiB"
          f"（+IMU 约 {seconds * 1000 * 56 / 2**20:.0f} MiB）")
    print("\n开录前先设好这三行（新开一个终端跑）：")
    print(f"    export KSQ_FAYS_DEBUG_DIR={target}")
    print(f"    export KSQ_FAYS_DEBUG_SECONDS={seconds:.0f}")
    print(f"    export KSQ_FAYS_DEBUG_CPUS={cpus}")
    print(f"    cd {ROOT} && venv/bin/python main.py")
    print("\n然后照常**连接夹爪**（不用开始录制）—— 桥接进程启动时自己开录。")
    print("★ 这几分钟里让夹爪**持续运动**：静止时相邻帧几乎相同，H1/H2 都看不出来。")
    print("  到点自动停（或断开夹爪即停），原生日志里会有 "
          "[Connect-Debug] started / finished 两行。")
    print("⚠ 一个目录只连一次：**断开后再连=从头覆盖分片**（分片号恒从 samples-0000 "
          "起）。要再抓就先重跑 preflight 换个目录、把三行 export 重新设一遍。")

    if problems:
        print("\n预检不通过：")
        for item in problems:
            print(f"  ✗ {item}")
        return None
    print("\n预检通过。")
    return target


# ── 读分片 ──────────────────────────────────────────────────────────────

def resolve_directory(directory):
    """preflight 每次建一个 capture-<时间戳> 子目录，所以 analyze 常被指到父目录。"""
    if glob.glob(os.path.join(directory, "samples-*.bin")):
        return directory
    found = sorted({os.path.dirname(path) for path in
                    glob.glob(os.path.join(directory, "**", "samples-*.bin"),
                              recursive=True)})
    if not found:
        raise SystemExit(f"{directory} 下没有 samples-*.bin（取证目录给错了？）")
    raise SystemExit(f"{directory} 下没有直接的 samples-*.bin，候选取证目录：\n  "
                     + "\n  ".join(found))


def iter_samples(directory):
    """按分片顺序流式产出 (kind, ts_ns, mono_ns, seq, dims, payload)。"""
    paths = sorted(glob.glob(os.path.join(directory, "samples-*.bin")))
    if not paths:
        raise SystemExit(f"{directory} 下没有 samples-*.bin")
    for path in paths:
        with open(path, "rb") as stream:
            while True:
                raw = stream.read(HEADER_SIZE)
                if not raw:
                    break
                if len(raw) < HEADER_SIZE:
                    # 进程被 SIGKILL（没走 stop() 的 flush）会留下残尾，
                    # 前面的帧仍然可用，不要因此丢掉整次取证
                    print(f"  [warn] {os.path.basename(path)} 尾部残包头 "
                          f"{len(raw)} 字节，停在上一帧", file=sys.stderr)
                    break
                (magic, version, kind, header_size, payload_size, _r0,
                 ts_ns, mono_ns, _real_ns, seq, width, height, channels,
                 _enc, _r1, _step, _r2, _r3) = HEADER.unpack(raw)
                if magic != RAW_STREAM_MAGIC or version != RAW_STREAM_VERSION:
                    raise SystemExit(f"{path}: 帧不齐 magic={magic:#x} "
                                     f"version={version}")
                if header_size != HEADER_SIZE:
                    raise SystemExit(f"{path}: header_size={header_size} "
                                     f"≠ {HEADER_SIZE}")
                payload = stream.read(payload_size)
                if len(payload) != payload_size:
                    print(f"  [warn] {os.path.basename(path)} 载荷截断 "
                          f"{len(payload)}/{payload_size}，停在上一帧",
                          file=sys.stderr)
                    break
                yield (kind, ts_ns, mono_ns, seq,
                       (width, height, channels, _step, payload_size), payload)


class _Frame:
    __slots__ = ("index", "seq", "ts_ns", "mono_ns", "dims", "payload",
                 "_digest", "is_dip", "lag")

    def __init__(self, index, seq, ts_ns, mono_ns, dims, payload):
        self.index, self.seq, self.ts_ns = index, seq, ts_ns
        self.mono_ns, self.dims, self.payload = mono_ns, dims, payload
        self._digest = None
        self.is_dip, self.lag = False, None

    def digest(self):
        if self._digest is None:
            self._digest = hashlib.sha256(self.payload).digest()
        return self._digest


def mad(a, b, stride=STRIDE):
    """平均绝对像素差（抽样估计；下陷与基线用同一估计量才可比）。"""
    if len(a) != len(b):
        return None
    x = np.frombuffer(a, dtype=np.uint8)[::stride]
    y = np.frombuffer(b, dtype=np.uint8)[::stride]
    return float(np.abs(x.astype(np.int16) - y.astype(np.int16)).mean())


def edge_mad(a, b, dims):
    """边缘图之差：每帧先求 |Δx|+|Δy|（结构），再取两幅边缘图的平均绝对差。

    为什么要多这一把尺子：真机 09-17 抓到的 4 个下陷帧里，有 3 个的**像素差
    与正常帧无异**（同一瞬间被重新渲染、整幅电平变了），裸 mad 会把它们判成
    「只戳错（H1）」；但它们的**结构**（边缘图）与往前第 L 帧几乎重合 ——
    整幅电平变化不改边缘图，所以这把尺子分得出「内容旧了」和「值变了」。
    单通道才成立（多通道载荷返回 None，调用方据此跳过判读）。
    """
    if dims is None:
        return None
    width, height, channels = dims[0], dims[1], dims[2]
    if channels != 1 or width < 2 or height < 2:
        return None
    if len(a) != width * height or len(b) != width * height:
        return None
    ga = np.frombuffer(a, dtype=np.uint8).reshape(height, width)[::EDGE_STRIDE,
                                                                ::EDGE_STRIDE]
    gb = np.frombuffer(b, dtype=np.uint8).reshape(height, width)[::EDGE_STRIDE,
                                                                ::EDGE_STRIDE]
    ga = ga.astype(np.int16)
    gb = gb.astype(np.int16)
    # 两个方向的差分形状不同（少一行/一列），都裁到 (h-1, w-1) 才能相加
    edges = []
    for gray in (ga, gb):
        rows, cols = gray.shape
        dx = np.abs(np.diff(gray, axis=1))[:rows - 1, :]     # (h, w-1) → (h-1, w-1)
        dy = np.abs(np.diff(gray, axis=0))[:, :cols - 1]     # (h-1, w) → (h-1, w-1)
        edges.append(dx + dy)
    return float(np.abs(edges[0] - edges[1]).mean())


def _median(values):
    return float(np.median(values)) if values else None


def analyze(directory, native_log=None):
    directory = resolve_directory(directory)
    frames = []
    window = []                     # 最近 max(lags)+1 帧（含当前）
    dips = []
    base = collections.defaultdict(list)
    edge_base = collections.defaultdict(list)
    imu_steps, imu_prev, imu_count = [], None, 0
    stereo_count, lost, grid_off, seq_back = 0, 0, 0, 0
    dims_seen, shapes_mixed, size_mismatch = None, 0, 0
    first_ts, ts_phases = None, collections.Counter()
    chunk_files = sorted(glob.glob(os.path.join(directory, "samples-*.bin")))

    for kind, ts_ns, mono_ns, seq, dims, payload in iter_samples(directory):
        if kind == KIND_IMU:
            imu_count += 1
            if imu_prev is not None:
                imu_steps.append(ts_ns - imu_prev)
            imu_prev = ts_ns
            continue
        if kind != KIND_STEREO:
            raise SystemExit(f"未知包类型 {kind}（既不是双目也不是 IMU）")

        stereo_count += 1
        # 格子性质说的是**差值**：现场设备戳 = 常数起点 + 20ms 计数，所以绝对值
        # 本来就不在格点上（轨迹文件里是 ts−origin_ts 才看着对齐）。这里两样都记：
        # 相位偏移是常数（1 种取值）就说明差值全在格点上
        ts_phases[ts_ns % GRID_NS] += 1
        if first_ts is None:
            first_ts = ts_ns
        elif (ts_ns - first_ts) % GRID_NS:
            grid_off += 1
        if dims_seen is None:
            dims_seen = dims
        elif dims != dims_seen:
            shapes_mixed += 1
        # 载荷长度必须等于 step×height：这是对**我这份头部解析**的自校验，
        # 字段错位会在这里立刻暴露，而不是给出一个看着像样的假结论
        if dims[3] > 0 and dims[4] != dims[3] * dims[1]:
            size_mismatch += 1

        frame = _Frame(len(frames), seq, ts_ns, mono_ns, dims, payload)
        prev = frames[-1] if frames else None
        if prev is not None:
            d_ts, d_seq = ts_ns - prev.ts_ns, seq - prev.seq
            if d_seq < 0:
                seq_back += 1
            if ts_ns <= prev.ts_ns:
                frame.is_dip = True
                frame.lag = d_seq - d_ts / GRID_NS   # 戳是往前第几帧的
            elif d_seq > 1:
                lost += 1
        frames.append(frame)
        window.append(frame)
        if len(window) > max(DEFAULT_LAGS) + 1:
            window.pop(0)

        # 基线：整段「正常帧对」的差，按 lag 分开（跨度里不许有下陷帧）
        for lag in DEFAULT_LAGS:
            if len(window) < lag + 1:
                continue
            span = window[-(lag + 1):]
            if any(item.is_dip for item in span):
                continue
            if span[-1].seq - span[0].seq != lag:
                continue
            if span[-1].ts_ns - span[0].ts_ns != lag * GRID_NS:
                continue
            value = mad(span[-1].payload, span[0].payload)
            if value is not None:
                base[lag].append(value)
                if stereo_count % EDGE_BASE_EVERY == 0:   # 抽采，别把分析拖成分钟级
                    edge = edge_mad(span[-1].payload, span[0].payload, span[-1].dims)
                    if edge is not None:
                        edge_base[lag].append(edge)

        if frame.is_dip and len(window) >= 2:
            dips.append(_describe_dip(window, frame))

    report = {"directory": os.path.abspath(directory),
              "chunks": len(chunk_files), "stereo_frames": stereo_count,
              "imu_samples": imu_count, "dims": dims_seen,
              "shapes_mixed": shapes_mixed, "size_mismatch": size_mismatch}
    report.update(_aggregate(frames, dips, base, edge_base, imu_steps, lost,
                             grid_off, seq_back, ts_phases))
    if native_log:
        report["native_log"] = _cross_check(native_log, dips)
    return report


def _static_pairs(window):
    """窗内「本来就有」的逐字节相同的正常帧对 —— 有的话，精确相同这个证据就废了
    （画面自己就在重复，下陷帧与前一帧相同说明不了任何事）。真机不会这样：静场
    实测相邻帧差 3.59 灰度级，从不逐字节相同。"""
    count = 0
    for pos in range(1, len(window)):
        if window[pos].is_dip or window[pos - 1].is_dip:
            continue
        if window[pos].digest() == window[pos - 1].digest():
            count += 1
    return count


def _edge_control(window):
    """对照曲线：同一个窗里**正常帧**的 e(j,j-k)/e(j,j-1) 中位数，按 k 分开。

    没有这个对照，任何一个比值都读不出来 —— 运动快慢、纹理强弱、噪声底都会
    改变 e 的绝对量级（现场对照实测 1.03~1.29，而拿单帧去比就会以为「1.2 就是
    像」）。下陷帧必须和**同一时段的正常帧**比，这就是本函数存在的理由。
    """
    top = len(window) - 2                     # window[-1] 是下陷帧自己
    steps = {}
    for pos in range(1, top + 1):
        if window[pos].is_dip or window[pos - 1].is_dip:
            continue
        steps[pos] = edge_mad(window[pos].payload, window[pos - 1].payload,
                              window[pos].dims)
    ratios = collections.defaultdict(list)
    for back in range(2, len(window)):
        for pos in range(back, top + 1):
            span = window[pos - back:pos + 1]
            if any(item.is_dip for item in span):
                continue
            if (span[-1].seq - span[0].seq != back
                    or span[-1].ts_ns - span[0].ts_ns != back * GRID_NS):
                continue
            step, value = steps.get(pos), edge_mad(span[-1].payload,
                                                   span[0].payload,
                                                   span[-1].dims)
            if step and value is not None:
                ratios[back].append(value / step)
    return {back: _median(values) for back, values in ratios.items()}


def _describe_dip(window, frame):
    """给一个下陷帧算出与往前各帧的差（裸 mad 与边缘差两把尺子）、最像的前驱
    （按边缘图定）、同窗对照曲线、以及 H1/H2 判读。"""
    lag = frame.lag
    if lag is not None and float(lag).is_integer():
        lag = int(lag)          # 戳落在 20ms 格子上 ⇒ lag 是整帧数
    others = window[:-1]
    mads, edges = {}, {}
    for back in DEFAULT_LAGS:
        if len(others) >= back:
            mads[back] = mad(frame.payload, others[-back].payload)
            value = edge_mad(frame.payload, others[-back].payload, frame.dims)
            if value is not None:
                edges[back] = value
    best = min(edges, key=edges.get, default=None)
    entry = {"index": frame.index, "seq": frame.seq, "ts_ns": frame.ts_ns,
             "lag": lag, "mads": mads, "edges": edges, "best_back": best,
             "edge_control": _edge_control(window),
             "static_pairs": _static_pairs(window),
             "prev_is_dip": bool(others and others[-1].is_dip),
             "exact_back": [], "verdict": None, "detail": ""}
    for back in DEFAULT_LAGS:
        if len(others) >= back and others[-back].digest() == frame.digest():
            entry["exact_back"].append(back)
    return entry


def _classify(entry):
    """按「下陷帧的边缘差曲线 vs 同窗正常帧的对照曲线」判 H1（只戳错）/ H2（重投递）。

    判据不是 mad 的绝对落点，而是三件事同时成立：结构最像往前第 L 帧、这一相似
    度明显好过同窗正常帧的对照、且不是「画面本来就在重复」。
    """
    lag = entry["lag"]
    if lag is None or lag < 1 or lag > max(DEFAULT_LAGS) or lag != int(lag):
        entry["verdict"] = "SKIP"
        entry["detail"] = f"lag={lag} 不在可比对范围（1..{max(DEFAULT_LAGS)} 整帧）"
        return entry
    lag = int(lag)
    edges = entry["edges"]
    if len(edges) < 3:
        entry["verdict"] = "SKIP"
        entry["detail"] = "载荷不是单通道图（或窗太短），边缘图判据用不了"
        return entry
    if entry["static_pairs"] >= STATIC_PAIRS:
        entry["verdict"] = "QUIET"
        entry["detail"] = (f"窗内本来就有 {entry['static_pairs']} 对相邻帧逐字节相同"
                           f" —— 画面自己在重复，「相同」没有信息量")
        return entry
    if entry["exact_back"]:
        entry["verdict"] = "H2_EXACT"
        entry["detail"] = ("与往前第 %s 帧 sha256 完全相同"
                           % "/".join(map(str, entry["exact_back"])))
        return entry
    smallest, largest = min(edges.values()), max(edges.values())
    if largest <= 0 or smallest / largest > 1 / EDGE_FLAT_RATIO:
        entry["verdict"] = "QUIET"
        entry["detail"] = (f"各 lag 的边缘差都挤在 {smallest:.2f}~{largest:.2f}"
                           f"（≤{EDGE_FLAT_RATIO} 倍）⇒ 场景太静，判不了")
        return entry
    if entry["prev_is_dip"]:
        entry["verdict"], entry["detail"] = "SKIP", "前一帧本身也是下陷帧"
        return entry
    step, at_lag = edges.get(1), edges.get(lag)
    if not step or at_lag is None:
        entry["verdict"], entry["detail"] = "SKIP", "比对窗不足"
        return entry
    ratio = at_lag / step
    ref = entry["edge_control"].get(lag)
    argmin = min(edges, key=edges.get)
    entry["edge_ratio"], entry["edge_ref"], entry["edge_argmin"] = ratio, ref, argmin
    if ref is None:
        entry["verdict"] = "UNDETERMINED"
        entry["detail"] = f"窗内凑不出 lag={lag} 的正常帧对做对照"
        return entry
    if ratio < H2_EDGE_FACTOR * ref and argmin == lag:
        entry["verdict"] = "H2"
        entry["detail"] = (f"边缘差 e({lag})/e(1)={ratio:.2f}，只有同窗对照 {ref:.2f} 的 "
                           f"{ratio / ref:.0%}，且最小 lag 正是 {lag}（往前 "
                           f"{lag * GRID_NS / 1e6:.0f}ms）——载荷的结构是那一瞬的")
    elif argmin == 1 and H1_EDGE_BAND[0] * ref <= ratio <= H1_EDGE_BAND[1] * ref:
        entry["verdict"] = "H1"
        entry["detail"] = (f"边缘差 e({lag})/e(1)={ratio:.2f} 与同窗对照 {ref:.2f} "
                           f"同量级，最小 lag 是 1（最像前一帧）——载荷是新的")
    else:
        entry["verdict"] = "UNDETERMINED"
        entry["detail"] = (f"e({lag})/e(1)={ratio:.2f} vs 同窗对照 {ref:.2f}；"
                           f"最小 lag={argmin}（戳说的是 {lag}）——两者对不上")
    return entry


def _aggregate(frames, dips, base, edge_base, imu_steps, lost, grid_off, seq_back,
               ts_phases=None):
    phases = ts_phases or collections.Counter()
    report = {"stereo_frames": len(frames), "lost_frames": lost,
              "grid_off": grid_off, "seq_back": seq_back,
              "ts_phase_kinds": len(phases),
              "ts_phase_ns": (phases.most_common(1)[0][0]
                              if len(phases) == 1 else None),
              "dips": len(dips),
              "base": {lag: {"median": _median(values), "n": len(values),
                             "p10": float(np.percentile(values, 10)),
                             "p90": float(np.percentile(values, 90))}
                       for lag, values in sorted(base.items())},
              "edge_base": {lag: {"median": _median(values), "n": len(values)}
                            for lag, values in sorted(edge_base.items())}}
    if frames:
        span_s = (frames[-1].mono_ns - frames[0].mono_ns) / 1e9
        # 首末帧之间只有 n-1 个间隔，别拿 n 去除
        frame_time_s = (len(frames) - 1) * GRID_NS / 1e9
        report["span_seconds"] = span_s
        report["frame_time_seconds"] = frame_time_s
        report["dip_per_second"] = len(dips) / span_s if span_s > 0 else None
        report["dip_fraction"] = len(dips) / len(frames)
        report["rate_deficit"] = 1 - frame_time_s / span_s if span_s > 0 else None
    if imu_steps:
        steps = np.array(imu_steps, dtype=np.int64)
        report["imu"] = {"count": len(steps) + 1,
                         "median_step_ns": float(np.median(steps)),
                         "min_step_ns": int(steps.min()),
                         "max_step_ns": int(steps.max()),
                         "nonpositive": int((steps <= 0).sum())}
    lags = collections.Counter(entry["lag"] for entry in dips)
    report["lag_histogram"] = {str(key): count
                               for key, count in lags.most_common()}
    report["best_back_histogram"] = dict(collections.Counter(
        entry["best_back"] for entry in dips))
    verdicts = collections.Counter()
    # 模型无关的旁证：只看「最像哪一帧」（按边缘图），不套任何对照 —— 快慢无关。
    # 「近乎为零」单独数：那是逐字节重投递那一档（真机的重渲染帧只到 0.3~0.7，
    # 结构同源但不是同一幅），不能拿它当 H2 的总数，否则结论行会自相矛盾。
    prev_best = lag_best = near_zero_earlier = 0
    for entry in dips:
        _classify(entry)
        verdicts[entry["verdict"]] += 1
        best = entry["best_back"]
        value = entry["edges"].get(best) if best else None
        step = entry["edges"].get(1)
        if best == 1:
            prev_best += 1
        elif value is not None and step and best == entry["lag"]:
            lag_best += 1
            if value <= H2_ZERO_RATIO * step:
                near_zero_earlier += 1
    report["verdicts"] = dict(verdicts)
    report["argmin_evidence"] = {"best_is_prev": prev_best,
                                "best_is_lag": lag_best,
                                "best_is_lag_and_near_zero": near_zero_earlier}
    report["dip_details"] = dips
    return report


def _cross_check(native_log, dips):
    """把 `[TIME_DROP]` 日志与本次取证的判定按 seq 对上 —— 唯一能证明「抓到的
    就是现场那个毛病」的手段。"""
    import re
    pattern = re.compile(r"\[TIME_DROP\].*?delta=(-?[\d.]+) seq=(\d+) prev_seq=(\d+)")
    logged = []
    with open(native_log, errors="replace") as stream:
        for line in stream:
            match = pattern.search(line)
            if match:
                logged.append({"delta": float(match.group(1)),
                               "seq": int(match.group(2)),
                               "prev_seq": int(match.group(3))})
    seqs = {entry["seq"] for entry in dips}
    matched = [item for item in logged if item["seq"] in seqs]
    # 取证钩子在 stereoCallback 入口（g_img_queue 之前），守卫在下游、且只看
    # 3 取 5 的 3 帧 —— 所以正常情况下取证数应约为日志数的 5/3。
    # 队列是 FIFO，下游只排队不重排：日志里有、取证里没有 = 我读错了字段。
    if not logged and not dips:
        note = "两边都是 0：这一段本来就没抓到毛病，不是判读结论"
    elif not logged and dips:
        note = (f"取证 {len(dips)} 个、日志 0 条：这些帧被 3 取 5 抽帧丢掉了，"
                f"逐帧明细仍然有效")
    elif logged and not dips:
        note = ("⚠ 日志里有、取证里没有 —— 回退不是设备原始流带来的。"
                "取证钩子是回调入口的最上游，队列只排队不重排，"
                "所以更可能是我读错了字段，别信这次判读")
    else:
        ratio = len(dips) / len(logged)
        note = (f"取证/日志 = {ratio:.2f}（期望 ≈1.67 = 5/3，守卫只看抽帧后的 3 帧）"
                + ("，量级对得上 → 抓到的就是现场那个毛病"
                   if 1.0 <= ratio <= 2.5 else "，量级对不上 → 先别下结论"))
    return {"path": os.path.abspath(native_log), "logged": len(logged),
            "matched_in_capture": len(matched),
            "capture_dips": len(dips), "note": note}


# ── 输出 ────────────────────────────────────────────────────────────────

def print_report(report):
    print(f"=== 取证目录 {report['directory']} ===")
    meta = os.path.join(report["directory"], "capture.json")
    if os.path.exists(meta):
        with open(meta) as stream:
            print(f"capture.json        {stream.read().strip()}")
    else:
        print("capture.json        缺失（取证未正常收尾？帧数以下面数出来的为准）")
    dims = report["dims"] or (0, 0, 0, 0, 0)
    print(f"分片                {report['chunks']} 个   帧 {report['stereo_frames']}"
          f"   {dims[0]}×{dims[1]}×{dims[2]} 载荷 {dims[4]} B"
          f"（step {dims[3]}）   异形帧 {report['shapes_mixed']}")
    if report.get("size_mismatch"):
        print(f"  ✗ 载荷长度 ≠ step×height 的帧有 {report['size_mismatch']} 个 ——"
              f"头部解析对不上，下面的结论不可信")
    if report.get("span_seconds"):
        print(f"宿主时长            {report['span_seconds']:.1f} s"
              f"（帧时间 {report['frame_time_seconds']:.1f} s）")
        print(f"流内缺号            {report['lost_frames']} 帧（Δseq>1 且非下陷）")
        print(f"帧率缺口            {report['rate_deficit'] * 100:+.2f}%"
              f"（1 − 帧时间/宿主时长：真实丢帧与设备帧率偏低都算在这里）")
    phase = report.get("ts_phase_ns")
    phase_text = ("恒定相位 %+.3f ms（差值全在 20ms 格点上）" % (phase / 1e6)
                  if phase is not None else
                  f"相位取值 {report.get('ts_phase_kinds')} 种（差值不在格点上）")
    print(f"ts 网格             {phase_text}    "
          f"差值偏离 {report['grid_off']} 帧    seq 回退 {report['seq_back']} 帧")
    if report.get("imu"):
        imu = report["imu"]
        print(f"IMU                 {imu['count']} 个   Δts 中位 "
              f"{imu['median_step_ns'] / 1e6:.3f} ms  ["
              f"{imu['min_step_ns'] / 1e6:.3f}, {imu['max_step_ns'] / 1e6:.3f}]"
              f"   非正步进 {imu['nonpositive']}")
    print(f"\n下陷帧              {report['dips']} 个"
          f"（{report.get('dip_per_second') or 0:.3f}/s，"
          f"占输入帧 {report.get('dip_fraction', 0) * 100:.2f}%）")
    print(f"  L 直方图          {report['lag_histogram']}")
    print(f"  最像的前驱 j*     {report['best_back_histogram']}")
    print("  基线（正常帧对的平均绝对差 mad / 边缘差 e，灰度级）")
    for lag, stat in report["base"].items():
        edge = report.get("edge_base", {}).get(lag)
        edge_text = (f"   e 中位 {edge['median']:.2f}（n={edge['n']}）"
                     if edge and edge["median"] is not None else "")
        print(f"    lag={lag}  n={stat['n']:5d}  中位 {stat['median']:.2f}"
              f"  p10 {stat['p10']:.2f}  p90 {stat['p90']:.2f}{edge_text}")

    print("\n=== 逐帧判读（最多 15 个）===")
    print("  #      seq       L   mad(i,i-1)  mad(i,i-L)   e(i,i-1)  e(i,i-L)"
          "   e(L)/e(1)  对照   最小lag  sha  判读")
    for entry in report["dip_details"][:15]:
        lag = entry["lag"]
        exact = ("同" + "/".join(map(str, entry["exact_back"]))
                 if entry["exact_back"] else "—")
        whole = lag is not None and float(lag).is_integer()
        mad_at_lag = entry["mads"].get(int(lag)) if whole else None
        e_prev, e_lag = entry["edges"].get(1), entry["edges"].get(int(lag) if whole else None)
        ratio, ref = entry.get("edge_ratio"), entry.get("edge_ref")
        print(f"  {entry['index']:<6d} {entry['seq']:<9d} {lag!s:<4}"
              f" {entry['mads'].get(1, float('nan')):>9.2f}"
              f" {(mad_at_lag if mad_at_lag is not None else float('nan')):>11.2f}"
              f" {(e_prev if e_prev is not None else float('nan')):>9.2f}"
              f" {(e_lag if e_lag is not None else float('nan')):>9.2f}"
              f" {(ratio if ratio is not None else float('nan')):>10.2f}"
              f" {(ref if ref is not None else float('nan')):>7.2f}"
              f" {entry.get('edge_argmin')!s:>7}  {exact:<5} {entry['verdict']}")

    evidence = report["argmin_evidence"]
    print(f"  旁证（只看结构最像哪一帧，不套对照）"
          f"  像前一帧 {evidence['best_is_prev']} 个（H1 形态）")
    print(f"                                     "
          f"  像往前第 L 帧 {evidence['best_is_lag']} 个（H2 形态，"
          f"其中 e ≤ {H2_ZERO_RATIO}× 的逐字节级重合 "
          f"{evidence['best_is_lag_and_near_zero']} 个）")

    print("\n=== 结论 ===")
    verdicts = report["verdicts"]
    for name in ("H2_EXACT", "H2", "H1", "UNDETERMINED", "QUIET", "SKIP"):
        if verdicts.get(name):
            print(f"  {name:<13} {verdicts[name]}")
    replay = verdicts.get("H2_EXACT", 0) + verdicts.get("H2", 0)
    fresh = verdicts.get("H1", 0)
    if not report["dips"]:
        print("  → **本次取证里一个下陷帧都没有** —— 这不是结论，是没样本。"
              "陈旧帧只在机器吃紧时冒头（现场日志里那几段是 0.5~0.6 条/秒，"
              "空连的安静时段是 0），换个「真在录制 / 机器被占满」的时段重抓。")
    elif replay and not fresh:
        print("  → **重投递（H2）**：下陷帧的图像是往前第 L 帧那一瞬的**几何**"
              "（80ms 前）。重新渲染过的不算逐字节相同（真机实测就这种），"
              "但结构同源。今天的丢帧是对的，**不该**把戳修回来 —— 修回来"
              "等于把 80ms 前的 measurement 当成当前帧喂进跟踪器。")
    elif fresh and not replay:
        print("  → **只戳错（H1）**：下陷帧的图像是新的。修复方案成立。")
    elif replay and fresh:
        print("  → 两种都有，逐帧看上面的明细（可能不止一个机制）。")
    else:
        print("  → 判不出来：看是不是场景太静 / 比对窗不足 / 事件数太少。")
    for entry in report["dip_details"]:
        if entry["detail"]:
            print(f"  例 #{entry['index']}: {entry['detail']}")
            break
    if report.get("native_log"):
        cross = report["native_log"]
        print(f"\n与 {os.path.basename(cross['path'])} 对照：日志 {cross['logged']} 条 "
              f"TIME_DROP，其中 {cross['matched_in_capture']} 条的 seq 出现在本次取证里"
              f"（{cross['note']}）")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="陈旧帧取证：判 H1（只戳错）还是 H2（重投递）")
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("preflight", help="开录前预检 + 打印 export 行")
    one.add_argument("--dir", default="/tmp/ksq-frame-trace")
    one.add_argument("--seconds", type=float, default=120.0)
    one.add_argument("--cpus", default="10,11")

    two = sub.add_parser("analyze", help="分析取证目录")
    two.add_argument("directory")
    two.add_argument("--native-log", default=None,
                     help="同一次会话的 logs/slam_native/*_slam_stdout.log，用于按 seq 交叉验证")
    two.add_argument("--json", default=None, help="把完整结果另存为 json")

    args = parser.parse_args(argv)
    if args.command == "preflight":
        return 0 if preflight(args.dir, args.seconds, args.cpus) else 1

    report = analyze(args.directory, args.native_log)
    print_report(report)
    if args.json:
        with open(args.json, "w") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        print(f"\n完整结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
