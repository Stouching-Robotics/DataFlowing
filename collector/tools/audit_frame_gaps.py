#!/usr/bin/env python
"""帧空洞全库审计：录制里「视频静止后跳变」的所有段落（只读）。

背景：episode-099 报「前 40 帧静止不动」，实测开头 45 帧是真帧、真正的缺陷是
row44→45 之间少了 4.68 秒（hardware_ns 与 wall_time 同步跳）。一次录制丢一段，
事后在视频里表现为「静止 → 跳变」。本脚本把**全库**这种段落一次数清楚，作为
后续修复的基线与回归对照。

用法:
    venv/bin/python tools/audit_frame_gaps.py [任务目录] [选项]
默认 data/recordings/UMIGripper_Action_AI（相对**仓库根**，可从任意 cwd 运行）

选项:
    --min-gap-ms 100   相邻两帧间隔 ≥ 该值才算一次异常（正常帧距 29.9~37.5ms）
    --clock-tol-ms 100 双钟一致阈值：|Δhw − Δwall| ≤ 该值才算「两个钟都这么说」
    --merge-rows 15    相隔 ≤ 这么多行的异常合并成**一次**事件（≤15 行内两侧都
                       停 = 整机被抢同一件事，取大不加总；**相隔更远**的一墙一戳
                       是两次独立停顿，见下「两类时钟」）
    --no-video-check   跳过**画面复核**与 mp4 帧数校验（快，但只剩钟口径）
    --all              连零异常的段也列出来
    --camera-logs DIR  关联 logs/camera_service/ 的留档日志（秒级粗相关，默认关）
    --json             输出机器可读结果

三层判据（由强到弱）
--------------------
1. **画面复核（主判据）**：把整段视频降采样成灰度，比较事件前后两帧的画面差
   （mad）与同一段视频的「邻域逐帧差中位」和「远端饱和差中位」：

   ============  ==========================  ==============================
   画面连续       边界差 ≈ 邻域逐帧差（≤2×）    **没丢**：画面是连的（帧到得晚，
                                              不是画面上少了一截）
   画面跳变       边界差 ≥ 3× 邻域或 ≥ 0.7×饱和 **丢了**：画面真断了一截
   不定           介于两者之间                回落第 2/3 层判
   ============  ==========================  ==============================

   2026-09-18 标定：真丢的两处 ep-099 row45 = 18.6×、ep-098 row40 = 91.6×（对
   饱和 0.75/1.62×）；而 15 个「双钟都跳 ~250ms」的事件全是 0.16~1.60×，即边界
   差**正好是一帧的运动量**（钟却说过了 8 帧）——它们不是丢帧，是画面本来就接得上。
2. **双钟（次判据）**：Δhw ≈ Δwall ≥ 阈值 ⇒ 两侧都这么说（无画面时按疑似计）。
3. **单钟（定「哪一侧停了」）**：只有墙钟跳 ⇒ **写侧打嗝**（帧按 33ms 到了、
   只是写晚）；只有戳跳 ⇒ **读侧停顿**（帧晚到，写侧靠队列垫住）。两者都只是
   停顿，**是不是丢帧仍然由第 1 层说了算**。

两类时钟（不认识这个会得出错结论）
----------------------------------
两个戳都取自**宿主钟**，但取的地方不同 —— 这是本脚本最要紧的一条：

* ``hardware_ns`` 由采集线程在**相机 read() 返回处**盖（bridge._rgb_run 的
  time.monotonic_ns()）⇒ 它量的是**取到帧的时刻**：读侧停顿、帧晚到都记在这里。
  - ``hw64``    ≥079 段：完整宿主单调钟，值域远超 2^32，单调递增，可信。
  - ``trunc32`` ≤078 段：同一个钟被截成**有符号 32 位**纳秒（±2^31，每 4.295 秒
    锯齿一次）⇒ 判空洞前必须先按 2^32 解卷，否则每次过零都是一次天文数字级的
    假空洞。
  - ``none``    hardware_ns 全 0（无时间戳槽位，如 lite 路径）⇒ 判不了，只报数。
* ``wall_time`` 由**写入线程**在**建行落盘处**盖（egodata_writer.py 的
  time.time()）⇒ 它量的是**写下来的时刻**（墙钟，会被 NTP 校时）。

于是 **Δwall − Δhw 就是「队列滞留」的变化量**（两个钟的固定偏移在差里抵消）。
2026-09-18 全库验过：ep-011 row162 偏移 +138.7ms、row180 −131.3ms ⇒ 先写侧积压
139ms、随后读侧一段空窗把积压排空；ep-014/018/007 是同一种形状。**两侧各停
一次、是两件事**，不是同一个洞的两半——据此删掉了早期的「配对」层（它会把
两次停顿捏成一次、把账记到错的桶里）。墙钟单独跳也可能是 NTP 校时，但本库的
墙钟事件都成对出现且互相抵消，不是校时。

口径：空洞 = 相邻两帧间隔 − 标称间隔（33.3ms），与录制侧同一个类
（core/frame_gap.GapWatch）——实时告警「[录制] RGB 帧空洞 …ms」与 parquet 里的
``*_gap_ms`` 报的就是这个数（日志里是**空洞**不是间隔，差一个标称间隔）。

退出码：发现画面复核判定为真丢的段 → 1；否则 0（与 tools/check_segment_seam.py 同）。
"""
import argparse
import datetime
import glob
import json
import os
import re
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.frame_gap import GapWatch  # noqa: E402

DEFAULT_TASK_DIR = os.path.join(ROOT, "data", "recordings",
                                "UMIGripper_Action_AI")
COLUMNS = ("frame_index", "hardware_ns", "wall_time")
GRAY_SIZE = (160, 120)          # 画面复核用的降采样尺寸（实测够分辨 33ms 的运动）
EVIDENCE_WINDOW = 15            # 邻域取多少帧算「逐帧运动水平」
SAT_STRIDE = 60                 # 远端饱和差：隔这么多帧取一对
SAT_STEP = 37                   # 取样步长（质数，避开周期性画面）
CONTINUOUS_RATIO = 2.0          # 边界差 / 邻域逐帧差 ≤ 此值 ⇒ 画面连续
JUMP_RATIO = 3.0                # ≥ 此值（或达饱和 0.7）⇒ 画面跳变
SAT_LEVEL = 0.7
STATIC_FLOOR = 1.5              # 8 位灰度 mad 的噪声地板（静止场景实测 0.45）
# 留档日志名：%Y%m%d_%H%M%S_<tag>_camera-service.log（见 uvc_camera_service.py）
_ARCHIVE_RE = re.compile(r"^(\d{8}_\d{6})_.*camera-service\.log$")
_ARCHIVE_WINDOW_S = 600.0     # 留档发生在会话结束时，故只往后找
_ARCHIVE_BACK_S = 5.0

_KIND_LABEL = {"both": "两侧同停", "wall": "写侧打嗝", "hw": "读侧停顿",
               "mismatch": "两侧不齐"}


# ── 时钟与解卷 ──────────────────────────────────────────────
def clock_class(hw):
    """判本段 hardware_ns 是哪一种钟（见模块 docstring）。"""
    if not any(hw):
        return "none"
    low, high = min(hw), max(hw)
    if low >= -2 ** 31 and high < 2 ** 31 and (low < 0 or high - low > 2 ** 31):
        return "trunc32"
    return "hw64"


def _unwrap(delta):
    """把有符号 32 位截断钟的差解卷回 [-2^31, 2^31)。"""
    return ((delta + 2 ** 31) % 2 ** 32) - 2 ** 31


def deltas(hw, cls):
    """本段逐行 hw 间隔（trunc32 解卷；其余原样）。"""
    out = [0] * len(hw)
    for index in range(1, len(hw)):
        delta = hw[index] - hw[index - 1]
        out[index] = _unwrap(delta) if cls == "trunc32" else delta
    return out


# ── 取数 ────────────────────────────────────────────────────
def episode_stem(name):
    """'episode-099.parquet' / '…/episode-099.mp4' → '099'。"""
    return re.sub(r"^episode-|\.(parquet|mp4)$", "", os.path.basename(name))


def _episode_paths(task_dir):
    """(段号, parquet, mp4) 列表，按段号排序；两边存在性各自独立。

    键必须归一到**段号**：parquet 与 mp4 的文件名后缀不同，直接拿 basename
    当键会让同一段分裂成两条（一条只有 parquet、一条只有 mp4），全库统计
    随之翻倍且两边都缺一半证据。
    """
    parquets = {}
    for path in glob.glob(os.path.join(
            task_dir, "data", "chunk-*", "episode-*.parquet")):
        parquets[episode_stem(path)] = path
    videos = {}
    for path in glob.glob(os.path.join(
            task_dir, "videos", "chunk-*", "*", "episode-*.mp4")):
        videos.setdefault(episode_stem(path), []).append(path)
    for key, paths in videos.items():
        # 同一段号可能有多个视频槽位（stereo_left / gripper_rgb）。本工具审计的
        # 是夹爪 RGB，优先取槽位名带 rgb 的那个；并列时按路径排序保证稳定。
        videos[key] = sorted(paths, key=lambda item: ("rgb" not in item, item))[0]
    names = sorted(set(parquets) | set(videos))
    return [(name, parquets.get(name), videos.get(name)) for name in names]


def _load_rows(parquet_path):
    """读三列 → [fi, hw, wall]；缺列/读不动时返回 (None, 原因)。"""
    import pyarrow.parquet as pq
    try:
        table = pq.ParquetFile(parquet_path).read(columns=list(COLUMNS))
    except Exception as exc:                      # noqa: BLE001 - 只读取证工具
        return None, f"读取失败: {exc}"
    # 用 Table.schema.names；ParquetFile.schema.names 是扁平化的另一套名字
    names = table.schema.names
    missing = [name for name in COLUMNS if name not in names]
    if missing:
        return None, f"缺列 {missing}"
    return [[table.column(name).to_pylist() for name in COLUMNS], None]


# ── 画面复核 ────────────────────────────────────────────────
def probe_frames(video_path):
    """mp4 帧数（CAP_PROP，不解码）。读不了返回 None。"""
    if video_path is None or not os.path.isfile(video_path):
        return None
    import cv2
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return count if count > 0 else None


def load_video_gray(video_path):
    """整段视频降采样灰度缓存；读不了时返回 None（调用方回落钟口径）。"""
    if video_path is None or not os.path.isfile(video_path):
        return None
    import cv2
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    frames = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frames.append(cv2.cvtColor(cv2.resize(frame, GRAY_SIZE),
                                       cv2.COLOR_BGR2GRAY).astype("int16"))
    finally:
        cap.release()
    return frames or None


def _mad(first, second):
    import numpy as np
    return float(np.mean(np.abs(first - second)))


def screen_hit(frames, row):
    """画面复核一个事件：返回 (evidence, 边界差, 邻域逐帧差, 饱和差)。

    evidence: "continuous"（画面连续 ⇒ 没丢）/ "jump"（画面跳了 ⇒ 丢了）/
              "unknown"（读不到画面、落在两端、或**整窗静止** ⇒ 回落钟口径）
    """
    if frames is None or row < 1 or row >= len(frames):
        return "unknown", None, None, None
    count = len(frames)
    near = [j for j in range(max(1, row - EVIDENCE_WINDOW), row)
            if j < count]
    near += [j for j in range(row + 1, min(count, row + 1 + EVIDENCE_WINDOW))]
    base = statistics.median([_mad(frames[j - 1], frames[j]) for j in near]) \
        if near else 0.0
    sat = [_mad(frames[j], frames[j + SAT_STRIDE])
           for j in range(0, count - SAT_STRIDE, SAT_STEP)]
    saturation = statistics.median(sat) if sat else 0.0
    boundary = _mad(frames[row - 1], frames[row])
    # 整窗静止（邻域与边界都在传感器噪声地板以下）：画面本身就无信息，事件处
    # 少了 8 帧静止画面也看不出来——此时**不能**判「没丢」，回落钟口径
    if base < STATIC_FLOOR and boundary < STATIC_FLOOR:
        return "unknown", round(boundary, 2), round(base, 2), \
            round(saturation, 2)
    ratio = boundary / max(base, 0.05)
    if ratio <= CONTINUOUS_RATIO:
        evidence = "continuous"
    elif ratio >= JUMP_RATIO or boundary >= SAT_LEVEL * saturation:
        evidence = "jump"
    else:
        evidence = "unknown"
    return evidence, round(boundary, 2), round(base, 2), round(saturation, 2)


# ── 事件合并与定性 ──────────────────────────────────────────
def _classify(raw_events, min_ns, tol_ns, merge_rows):
    """把逐行异常合并成事件并定性（只按两个钟，画面复核在此之后）。

    raw_events: [(row, d_hw, d_wall)]，均为 ns，已按行号升序。
    返回事件列表，每个 {"row","rows","d_hw","d_wall","kind"}（ns，未减标称）。
    """
    events = []
    for row, d_hw, d_wall in raw_events:
        if events and row - events[-1]["rows"][-1] <= merge_rows:
            # 同一件事的两侧（≤15 行内两侧都停 = 整机被抢）：各自取最大，不做
            # 加法（加法会把同一个洞数两遍）
            events[-1]["rows"].append(row)
            events[-1]["d_hw"] = max(events[-1]["d_hw"], d_hw)
            events[-1]["d_wall"] = max(events[-1]["d_wall"], d_wall)
            continue
        events.append({"rows": [row], "d_hw": d_hw, "d_wall": d_wall})
    for event in events:
        d_hw, d_wall = event["d_hw"], event["d_wall"]
        hw_big = d_hw >= min_ns
        wall_big = d_wall >= min_ns
        if hw_big and wall_big:
            kind = "both" if abs(d_hw - d_wall) <= tol_ns else "mismatch"
        elif wall_big:
            kind = "wall"
        elif hw_big:
            kind = "hw"
        else:
            kind = "mismatch"        # 合并后反而都不过阈值（异常被吸收）
        event["kind"] = kind
        event["row"] = event["rows"][0]
    return events


def _account(result):
    """按事件定性归账（四桶，互斥；顺序即优先级）。

    1. 画面复核说跳变     → gap_ms    **丢了**（>任何钟口径，画面是硬证据）
    2. 只有墙钟跳         → wall_ms   写侧打嗝（帧没少、只是写晚）
    3. 画面复核说连续     → stamp_ms  没丢：有停顿、画面接得上（帧到得晚/写得晚）
    4. 其余               → pending_ms 判不了/未复核（钟互证的疑似落这里）

    只有第 1 桶算「证实丢了」；第 4 桶单列，绝不计入损失——「钟说跳了」不等于
    「画面少了帧」，本库 15 个双钟跳 ~250ms 的事件画面全是连续的（见模块 docstring）。
    桶名 stamp_ms 说的是「钟上的洞、画面上的连续」，不是「戳错了」。
    """
    sums = {"gap_ms": 0.0, "stamp_ms": 0.0, "pending_ms": 0.0, "wall_ms": 0.0}
    for event in result["events"]:
        kind, evidence = event["kind"], event["evidence"]
        if evidence == "jump":
            sums["gap_ms"] += event["gap_ms"]
        elif kind == "wall":
            sums["wall_ms"] += event["gap_ms"]
        elif evidence == "continuous":
            sums["stamp_ms"] += event["gap_ms"]
        else:
            sums["pending_ms"] += event["gap_ms"]
    for key, value in sums.items():
        result[key] = round(value, 1)
    return result


def audit_episode(parquet_path, video_path, nominal_ns, min_gap_ms,
                  clock_tol_ms, merge_rows, video_check=True):
    """单段审计 → dict（字段即 --json 的展开）。"""
    episode = episode_stem(parquet_path or video_path or "episode-?")
    result = {"episode": episode, "rows": 0, "status": "ok", "note": "",
              "clock": "", "gap_ms": 0, "stamp_ms": 0, "pending_ms": 0,
              "wall_ms": 0, "resyncs": 0, "video_frames": None, "video_ok": None,
              "screened": False,
              "events": []}
    if parquet_path is None:
        result.update(status="skip", note="只有视频没有 parquet")
        return result
    rows, err = _load_rows(parquet_path)
    if rows is None:
        result.update(status="skip", note=err)
        return result
    frame_index, hw, wall = rows
    result["rows"] = len(frame_index)
    # 单流判定：帧序号无重复且严格 0..N-1。多槽 episode 的行按槽各自从 0 编号，
    # 无法唯一拆出「哪几行属于这个 mp4」——宁可拒答也不给个靠位置猜出来的数
    if sorted(frame_index) != list(range(len(frame_index))):
        result.update(status="skip", note="多槽 episode，无法唯一拆分")
        return result
    cls = clock_class(hw)
    result["clock"] = cls
    if cls == "none":
        result.update(status="skip", note="hardware_ns 全 0（无时间戳）")
        return result

    # 逐行异常 → 合并 → 定性。门槛用 max(Δhw, Δwall)：只看一个钟会漏掉
    # 「写侧打嗝」（戳不动）与「戳单独跳」（墙钟不动）这两种签名
    min_ns = min_gap_ms * 1_000_000
    dh = deltas(hw, cls)
    raw = []
    for index in range(1, len(hw)):
        d_wall = int(round((wall[index] - wall[index - 1]) * 1e9))
        if dh[index] < min_ns and d_wall < min_ns:
            continue
        raw.append((index, dh[index], d_wall))
    tol_ns = clock_tol_ms * 1_000_000
    events = _classify(raw, min_ns, tol_ns, merge_rows)

    # hw 口径的空洞统计交给录制侧同一个类：解卷后的戳喂进去，口径逐字一致
    watch = GapWatch(nominal_ns, min_ns)
    base = hw[0]
    for index in range(1, len(hw)):
        base += dh[index]
        watch.note(base, "hw")
    result["resyncs"] = watch.snapshot()["resyncs"]

    # 帧数校验一律做（只读 CAP_PROP，不解码）：行与帧必须一一对应，画面复核才
    # 成立；等于顺手把「mp4 帧数 = 行数」这条完整性也一并核了。有事件时才真解
    # 码（解一段 30s 视频是秒级开销，只为没有事件的段解纯属浪费）。
    frames = None
    if video_check and probe_frames(video_path) is not None:
        count = probe_frames(video_path)
        result["video_frames"] = count
        result["video_ok"] = count == len(frame_index)
        if not result["video_ok"]:
            result.update(status="warn",
                          note=f"mp4 帧数 {count} ≠ 行数 {len(frame_index)}，"
                               f"画面复核不可用")
        elif events:
            frames = load_video_gray(video_path)
            if frames is None or len(frames) != len(frame_index):
                result["video_ok"] = False
                frames = None
                result.update(status="warn",
                              note="mp4 解码帧数与行数不符，画面复核不可用")
            else:
                result["screened"] = True

    for event in events:
        size_ns = max(event["d_hw"], event["d_wall"])
        row = event["row"]
        evidence, boundary, near, saturation = screen_hit(frames, row)
        result["events"].append({
            "row": row,
            "rows": event["rows"],
            "rel_s": round(wall[row - 1] - wall[0], 3),
            "wall_abs": round(wall[row - 1], 3),
            "kind": event["kind"],
            "evidence": evidence,
            "boundary": boundary,
            "near": near,
            "saturation": saturation,
            "d_hw_ms": round(event["d_hw"] / 1e6, 1),
            "d_wall_ms": round(event["d_wall"] / 1e6, 1),
            "gap_ms": round((size_ns - nominal_ns) / 1e6, 1),
            "interval_ms": round(size_ns / 1e6, 1),
        })
    # 画面复核优先：判定为「连续」的事件不必再看钟口径
    for event in result["events"]:
        if event["evidence"] == "jump":
            event["verdict"] = "丢了"
        elif event["evidence"] == "continuous":
            event["verdict"] = "没丢(画面连续)"
        else:
            event["verdict"] = _KIND_LABEL[event["kind"]]
    return _account(result)


# ── camera-service 留档相关 ─────────────────────────────────
def _archive_entries(directory):
    entries = []
    for name in sorted(os.listdir(directory)):
        match = _ARCHIVE_RE.match(name)
        if not match:
            continue
        try:
            stamp = datetime.datetime.strptime(
                match.group(1), "%Y%m%d_%H%M%S").timestamp()
        except ValueError:
            continue
        entries.append((stamp, os.path.join(directory, name)))
    return entries


def correlate_camera_logs(directory, results):
    """给每个事件挂上候选留档日志（秒级粗相关）。

    留档发生在**会话结束时**，所以只往后找：事件墙钟时刻 ≤ 留档时刻 ≤ +10min。
    只给候选——判读要看文件内容（[UVC] camera-service 告警摘录已在 main.log）。
    """
    entries = _archive_entries(directory)
    for row in results:
        for event in row["events"]:
            event["camera_logs"] = [
                {"file": os.path.basename(path),
                 "delta_s": round(stamp - event["wall_abs"], 1)}
                for stamp, path in entries
                if -_ARCHIVE_BACK_S <= stamp - event["wall_abs"]
                <= _ARCHIVE_WINDOW_S]
    return len(entries)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="帧空洞全库审计（只读）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task_dir", nargs="?", default=DEFAULT_TASK_DIR,
                        help="录制任务目录（默认 data/recordings/"
                             "UMIGripper_Action_AI；相对仓库根解析）")
    parser.add_argument("--min-gap-ms", type=float, default=100.0,
                        help="计入异常的间隔下限（默认 100）")
    parser.add_argument("--clock-tol-ms", type=float, default=100.0,
                        help="双钟一致阈值（默认 100；0 = 只认严格相等）")
    parser.add_argument("--merge-rows", type=int, default=15,
                        help="相隔 ≤ 这么多行的异常合并成一次事件（默认 15）")
    parser.add_argument("--no-video-check", action="store_true",
                        help="跳过画面复核（快，但只剩钟口径；结论会标「未复核」）")
    parser.add_argument("--all", action="store_true", help="连零异常的段也列出")
    parser.add_argument("--camera-logs", default=None,
                        help="logs/camera_service 目录（关联留档日志）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    task_dir = args.task_dir
    if not os.path.isdir(task_dir):
        print(f"任务目录不存在: {task_dir}")
        print(f"（相对路径按仓库根 {ROOT} 解析；也可显式传入任务目录）")
        return 2
    if not os.path.isdir(os.path.join(task_dir, "data")):
        print(f"不是任务目录（缺 data/）: {task_dir}")
        return 2

    nominal_ns = int(1e9 / 30.0)
    nom_ms = nominal_ns / 1e6
    episodes = _episode_paths(task_dir)
    results = []
    for name, parquet, video in episodes:
        results.append(audit_episode(
            parquet, video, nominal_ns, args.min_gap_ms, args.clock_tol_ms,
            args.merge_rows, video_check=not args.no_video_check))

    if args.camera_logs:
        if not os.path.isdir(args.camera_logs):
            print(f"留档目录不存在: {args.camera_logs}")
            return 2
        correlate_camera_logs(args.camera_logs, results)

    hit = [r for r in results if r["gap_ms"] > 0]
    stamps = [r for r in results if r["stamp_ms"] > 0]
    pending = [r for r in results if r["pending_ms"] > 0]
    hiccups = [r for r in results if r["wall_ms"] > 0]
    skipped = [r for r in results if r["status"] == "skip"]
    warned = [r for r in results if r["status"] == "warn"]
    total_ms = round(sum(r["gap_ms"] for r in hit), 1)
    stamp_ms = round(sum(r["stamp_ms"] for r in stamps), 1)
    pending_ms = round(sum(r["pending_ms"] for r in pending), 1)
    wall_ms = round(sum(r["wall_ms"] for r in hiccups), 1)
    worst = max(hit, key=lambda r: r["gap_ms"], default=None)
    clocks = {}
    for row in results:
        clocks[row["clock"]] = clocks.get(row["clock"], 0) + 1

    if args.json:
        print(json.dumps({
            "task_dir": task_dir,
            "nominal_ms": round(nom_ms, 2),
            "min_gap_ms": args.min_gap_ms,
            "clock_tol_ms": args.clock_tol_ms,
            "merge_rows": args.merge_rows,
            "video_checked": not args.no_video_check,
            "episodes": results,
            "totals": {
                "episodes": len(results),
                "lost_episodes": len(hit), "lost_ms": total_ms,
                "lost_max_ms": worst["gap_ms"] if worst else 0.0,
                "lost_max_episode": worst["episode"] if worst else "",
                "stamp_only_ms": stamp_ms, "stamp_only_episodes": len(stamps),
                "pending_ms": pending_ms, "pending_episodes": len(pending),
                "writer_hiccup_ms": wall_ms, "writer_hiccup_episodes": len(hiccups),
                "clocks": clocks,
            },
        }, ensure_ascii=False, indent=2))
        return 1 if (hit or pending) else 0

    print(f"任务目录: {task_dir}")
    print(f"口径: 空洞 = 相邻两帧间隔 − 标称 {nom_ms:.1f}ms（core/frame_gap."
          f"GapWatch，与录制侧「[录制] RGB 帧空洞 …ms」同一个数）")
    print(f"      间隔 ≥ {args.min_gap_ms:.0f}ms 才算异常；双钟 |Δhw − Δwall| ≤ "
          f"{args.clock_tol_ms:.0f}ms 才算互证；≤{args.merge_rows} 行的异常"
          f"合并成一次事件")
    print(f"      主判据: " + ("画面复核（跳变=丢了 / 连续=没丢）"
                              if not args.no_video_check else
                              "关（--no-video-check，只剩钟口径）"))
    print(f"      时钟: " + "、".join(
        f"{key} {value} 段" for key, value in sorted(clocks.items())))
    verified = [r for r in results if r["video_ok"]]
    no_video = [r for r in results if r["status"] != "skip"
                and r["video_frames"] is None]
    screened = [r for r in results if r["screened"]]
    print(f"      画面: mp4 帧数与行数一致 {len(verified)} 段"
          + (f"；**不符 {len(warned)} 段**（画面复核不可用）" if warned else "")
          + (f"；无 mp4 {len(no_video)} 段（画面未复核）" if no_video else "")
          + f"；内容复核实际覆盖 {len(screened)} 段（有事件的段）")
    if skipped:
        print(f"      跳过 {len(skipped)} 段（另计，不进下表）："
              + "；".join(f"{r['episode']}({r['note']})" for r in skipped[:4])
              + ("…" if len(skipped) > 4 else ""))
    print()
    print(f"  {'段':>4}{'行数':>6}{'钟':>9}{'证实丢了':>9}{'钟跳没丢':>8}"
          f"{'事件':>5}{'打嗝':>8}{'待复核':>8}  判定")
    for row in results:
        if not (row["events"] or args.all):
            continue
        if row["gap_ms"] > 0:
            verdict = "实际丢了"
        elif row["status"] == "skip":
            verdict = row["note"]
        elif row["events"]:
            labels = []
            for event in row["events"]:
                if event["verdict"] not in labels:
                    labels.append(event["verdict"])
            verdict = "、".join(labels)
        else:
            verdict = "OK"
        print(f"  {row['episode']:>4}{row['rows']:>6}{row['clock']:>9}"
              f"{row['gap_ms']:>9.1f}{row['stamp_ms']:>8.1f}"
              f"{len(row['events']):>5}{row['wall_ms']:>8.1f}"
              f"{row['pending_ms']:>8.1f}  {verdict}")

    print()
    print(f"合计 {len(results)} 段：**画面证实丢了 {len(hit)} 段 / "
          f"{total_ms:.1f}ms**"
          + (f"（最大 {worst['gap_ms']:.1f}ms @ 段{worst['episode']}）"
             if worst else ""))
    lost_at = sorted(event["rel_s"] for row in hit for event in row["events"]
                     if event["evidence"] == "jump")
    if lost_at:
        print(f"  · **全部 {len(lost_at)} 起都落在开录后 {lost_at[0]:.2f}~"
              f"{lost_at[-1]:.2f}s**（启动窗口）—— 推测是这段还没排队、一次停顿"
              "就直接在画面里留下空洞（后期同样的停顿都被队列垫住了）"
              "；这是排查方向，不是随机掉帧")
    if stamps:
        print(f"  · 钟跳但画面连续（**没丢**）：{len(stamps)} 段 / {stamp_ms:.1f}ms"
              " —— 有停顿、画面接得上：帧到得晚（读侧停顿）或写得晚（写侧打嗝），"
              "队列把它垫住了；这些点上的「间隔」是钟的、不是画面的")
    if hiccups:
        print(f"  · 写侧打嗝（墙钟洞而戳只走一帧 ⇒ 帧数没少、只是写晚）："
              f"{len(hiccups)} 段 / {wall_ms:.1f}ms")
    if pending:
        print(f"  · 待复核（画面判不了或未复核，**不计入**上面的数）："
              f"{len(pending)} 段 / {pending_ms:.1f}ms —— "
              + ("本次 --no-video-check，全部事件都在这一桶；"
                 if args.no_video_check else
                 "画面灰区/读不了视频；")
              + "真机日志才能定性")
    if warned:
        print(f"  · 视频帧数校验不通过 {len(warned)} 段："
              + "；".join(f"{r['episode']}({r['note']})" for r in warned[:5]))

    print()
    print("逐事件明细（画面复核：boundary=事件处两帧差，near=邻域逐帧差中位，"
          "sat=远端饱和差中位）")
    print(f"  {'段':>4}{'行':>6}{'开录后':>9}{'判定':<11}{'间隔ms':>9}"
          f"{'Δhw':>9}{'Δwall':>8}{'空洞ms':>9}{'boundary':>9}{'near':>7}"
          f"{'sat':>7}  camera-service")
    for row in results:
        for event in row["events"]:
            logs = ""
            if "camera_logs" in event:
                logs = ("、".join(f"{item['file']}(+{item['delta_s']}s)"
                                  for item in event["camera_logs"])
                        or "无同期留档")

            def _fmt(value):
                return "-" if value is None else f"{value:.1f}"

            print(f"  {row['episode']:>4}{event['row']:>6}"
                  f"{event['rel_s']:>8.2f}s{event['verdict']:<11}"
                  f"{event['interval_ms']:>9.1f}{event['d_hw_ms']:>9.1f}"
                  f"{event['d_wall_ms']:>8.1f}{event['gap_ms']:>9.1f}"
                  f"{_fmt(event['boundary']):>9}{_fmt(event['near']):>7}"
                  f"{_fmt(event['saturation']):>7}  {logs}")
    print()
    if hit:
        print(f"FAIL: {len(hit)} 段画面真丢了时间（上表「证实丢了」列）"
              + (f"；另有 {len(pending)} 段钟口径疑似待复核" if pending else "")
              + "，详见 docs/postmortem_trajectory_and_rgb.md")
        return 1
    if pending:
        print(f"FAIL: 无画面证实的丢帧，但 {len(pending)} 段 / {pending_ms:.1f}ms "
              f"钟口径疑似（画面判不了或未复核）——不上报等于把未知当没事")
        return 1
    print("PASS: 未发现画面复核判定为真丢帧的段落")
    return 0


if __name__ == "__main__":
    sys.exit(main())
