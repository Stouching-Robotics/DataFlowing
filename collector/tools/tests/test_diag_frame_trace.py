#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""陈旧帧取证脚本的离线自检（不碰相机、不碰原生程序）。

用法:
    venv/bin/python tools/tests/test_diag_frame_trace.py

背景：`tools/diag_frame_trace.py` 要判的是「下陷帧的图像是新的（H1 只戳错）
还是旧的（H2 重投递）」。真机取证要相机 + 三分钟，所以先用**合成取证**把判读
逻辑钉住 —— 合成流按现场模型造：设备戳 = 帧序号 × 20ms 的格子（现场 239907
行轨迹零偏差），下陷帧交的是往前 L 帧的戳，图像则按真假两种情形分别给。

覆盖:
  1. 头部常量与 core/gripper/recording/fays_raw_client.py（真机验证过的读法）
     逐字一致，且 struct 尺寸 == C++ 的 static_assert(sizeof == 80)
  2. H2（重投递）：下陷帧载荷 = 往前 L 帧载荷 → H2_EXACT，且 H1 为 0
  3. H1（只戳错）：下陷帧载荷是新图像 → H1，且 H2 为 0
  4. 合成 L 直方图与现场一致（L=4 占多数）
  5. 场景静止（相邻帧本来就一样）→ QUIET，**不硬判**成 H1/H2
  6. L 超出比对窗（>8）→ SKIP，不误判
  7. 载荷来自往前 5 帧而戳的 lag 是 4 → 仍报重投递（exact_back=5），
     因为「逐字节相同」比 lag 更强
  8. 丢帧（Δseq=2、Δts=40ms）不算下陷
  9. 多分片、残尾、缺 capture.json 都不影响分析
 10. 与 [TIME_DROP] 日志按 seq 交叉验证
 11. print_report 跑得通且结论行写给的是「重投递」
 12. 恒定相位偏移（真机就是这样）不算「偏离格点」
 13. 一个下陷帧都没有时，结论行说的是「没样本」而不是判读
 14. **重渲染旧帧**（纹理是往前 L 帧的、整幅电平是新的）：裸 mad 会判成 H1，
     边缘图判据必须判成 H2 —— 09-17 真机 4 个事件里 3 个是这个形态
 15. 判读依据是**同窗正常帧的对照**，不是全场基线：同一条流里快慢两段，
     下陷帧都该按自己那一段的对照判出来
 16. 合成场景模型自身没有伪影：电平三角波的折返点不许落在下陷帧前 0~2 帧
     （会把 lag-4 的电平差抵消掉，造出假的「不在带内」），8 位不溢出
"""
import importlib.util
import io
import contextlib
import os
import re
import shutil
import struct
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLIENT = os.path.join(ROOT, "core", "gripper", "recording", "fays_raw_client.py")

_spec = importlib.util.spec_from_file_location(
    "diag_frame_trace", os.path.join(ROOT, "tools", "diag_frame_trace.py"))
trace = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trace)

GRID_NS = trace.GRID_NS
SIZE = 640 * 800
# 纹理：沿行方向的平滑随机条纹，每帧整体平移 1 像素 —— 参数是**量出来的**，
# 不是猜的（每一版的失败都写在这，免得下次又踩）：
#   · 每帧挪 8 像素（相关长度 129）：窗口内结构一步就换一大截，真机那种
#     「复重渲染帧的 mad 落在正常帧带内」的陷阱**造不出来**；
#   · 单频正弦：跟像素格拍频，量化后 e(k) 在 k=3 处反而最小 ⇒ 判据失明；
#   · `np.roll` 整幅平移：那是**纯平移**，边缘图跟着一起挪，各 lag 全等
#     （实测恒 7.6039）⇒ 假模型把判据测成「静止」；
#   · 太缓的条纹（每像素 <1 灰度级）：量化后退化成稀疏 ±1 台阶，e(k) 在
#     k=1 就饱和 ⇒ 判据失明（实测 dx 直方图 {0:515, 1:124}）。
# 所以：相关长度 24（每像素斜率 σ/12 ≈ 1.25 灰度级，够密）+ 每帧 1 像素。
WIDTH, HEIGHT = 640, 800
TEXTURE_BASE, TEXTURE_SIGMA, SMOOTH = 100, 15.0, 24
SHIFT_PER_FRAME, SHIFT_PAD = 1, 256
# 整幅电平：**三角波**，每帧 ±4 灰度级，半周期 22 帧（幅度 88）。
#   · 不是锯齿：锯齿每 LEVEL_CYCLE 帧有个 −(N−1)·STEP 的断崖，比对窗跨过它
#     就凭空造出一个「下陷形态」的巨差 ⇒ 测试假红（踩过）；
#   · 三角波每帧只差 ±LEVEL_STEP，和真机的自动曝光漂移同形、幅度有界；
#   · 电平漂移要能与纹理变化匹敌，重渲染帧的 mad 才会落在正常帧带内
#     （STEP 太小时 rm1 会超出 2×m1 的上界，陷阱造不出来 —— 量过）。
LEVEL_BASE, LEVEL_STEP, LEVEL_CYCLE = 8, 4, 22   # 峰值 8+88=96，+纹理峰值 145 = 241 ≤ 255
FAILS = []


def _build_profile():
    """平滑随机条纹（白噪 × 25 抽 Hann 窗），固定种子。

    判据要的是「平移 k 像素的结构差 e(k) 随 k 单调增、且窗口内不会重合」：
    · 单频正弦会**跟像素格拍频**（量化后 e(k) 在 k=3 处反而最小）⇒ 判据失明；
    · 整幅 `np.roll` 是**纯平移** —— 边缘图跟着一起挪，差值只剩接缝伪影，
      实测各 lag 全是 7.6039 ⇒ 假模型会把判据测成「静止」；
    · 条纹太缓（每像素只差 0.2 灰度级）会被 8 位量化成稀疏的 ±1 台阶，
      挪 1 像素就完全不相关 ⇒ e(k) 在 k=1 处就饱和，同样没有分辨力。
    所以相关长度取 25（≈ 每像素 1.25 灰度级，量化后不是台阶），每帧挪 1 像素
    —— 窗口内总位移 8 像素仍远小于相关长度，e(k) 随 k 平缓增长（真机同形：
    对照的 e(4)/e(1) 实测 1.03~1.29）。
    """
    rng = np.random.default_rng(20260917)
    noise = rng.integers(-100, 101, WIDTH + SHIFT_PAD + 2 * SMOOTH).astype(np.float64)
    window = np.hanning(SMOOTH + 1)
    profile = np.convolve(noise, window / window.sum(), mode="same")
    profile = profile[SMOOTH:-SMOOTH]
    profile -= profile.mean()
    return (TEXTURE_BASE + profile * (TEXTURE_SIGMA / profile.std())).astype(np.uint8)


_TEXTURE = _build_profile()
_PADDED = np.concatenate([_TEXTURE, _TEXTURE[:SHIFT_PAD]])   # 够取到最后一帧那一段


def _profile_at(index):
    """按 index 取一段（每帧挪 SHIFT_PER_FRAME 像素）—— 用切片而不是 roll，
    避免整幅环绕的接缝；SHIFT_PAD 远大于窗内总位移，所以窗口里不会绕回来。"""
    start = (index * SHIFT_PER_FRAME) % SHIFT_PAD
    return _PADDED[start:start + WIDTH]


def _level(index):
    """整幅电平（三角波，±LEVEL_STEP 灰度级/帧，半周期 LEVEL_CYCLE 帧）。

    为什么不是锯齿：锯齿每 LEVEL_CYCLE 帧有一个 −(N−1)·STEP 的断崖，比对窗
    跨过它就凭空造出一个巨差，测试会假红（踩过）。三角波每帧只差 ±STEP。

    注意三角波的**折返点**（下标 ≡ 0 或 LEVEL_CYCLE，模 2×LEVEL_CYCLE）：
    它若落在下陷帧前 0~2 帧内，lag-4 的电平差会被抵消掉一部分（实测 i=200
    时 Δ电平(4)=0、i=199 时只有 8 而不是 16）—— 重渲染帧的 mad 就掉到正常帧
    带外，**那是模型伪影不是判据的事**。选下陷帧下标时要避开；
    `test_level_model_has_no_fold_artifacts` 会把这条钉住。
    """
    phase = index % (2 * LEVEL_CYCLE)
    step = phase if phase <= LEVEL_CYCLE else 2 * LEVEL_CYCLE - phase
    return LEVEL_BASE + step * LEVEL_STEP


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        FAILS.append(msg)


def scene(index, level=None):
    """合成图像：平滑随机条纹**每帧平移 1 像素**（结构在动），整幅电平按
    三角波漂 ±4 灰度级/帧（模拟自动曝光/增益漂移）。

    这个模型是冲着判据来的：
      · mad(i,j) 与 e(i,j)（边缘差）都随 |i-j| 单调增 —— 正常帧该有的样子；
      · **整幅电平不影响边缘图**（梯度里抵消）⇒「纹理位置=往前 L 帧、电平=
        当帧」的重渲染会给出「mad 落在正常帧带内、e 却贴着往前第 L 帧」——
        真机抓到的就是这个形态。
    """
    if level is None:
        level = _level(index)
    row = _profile_at(index).astype(np.int32) + level
    return np.tile(row.astype(np.uint8), HEIGHT).tobytes()


def build_frames(dips, total=240, replay=False, replay_from=None, static=False,
                 imu=True, seq0=1000, slot0=100, rerender=False, frozen_before=0):
    """造一条合成流。dips={帧下标: 戳的 lag}；replay=True 时载荷取自往前 L 帧
    （H2，逐字节相同），rerender=True 时取往前 L 帧的**纹理**配当帧的电平
    （H2，但不是逐字节相同），否则是当帧新图像（H1）。

    戳按现场模型给：每帧的戳 = **自己的格子号** × 20ms，下陷帧的戳是往前 L 个
    格子的值（所以 Δseq=1、Δts=(1-L)·20ms，恢复帧则向前跳 (L+1)·20ms）。

    frozen_before=k：前 k 帧纹理冻住（只有电平在漂）⇒ 那一段的边缘差恒为 0，
    用来钉「对照必须取自**同一个窗**」。
    """
    frames, slot, imu_ns = [], slot0, 900_000_000
    for i in range(total):
        lag = dips.get(i)
        stamp_slot = slot - lag if lag else slot
        if static:
            payload = scene(0)
        else:
            # 只有**下陷帧**才可能背着旧载荷；别的帧一律是当帧图像
            back = 0
            if lag is not None:
                if replay_from:
                    back = replay_from
                elif replay or rerender:
                    back = lag           # 背着旧**内容**（replay=逐字节，rerender=重渲染）
            if back and rerender:
                payload = scene(i - back, level=_level(i))
            elif i < frozen_before:
                payload = scene(0, level=_level(i))
            else:
                payload = scene(max(i - back, 0))
        frames.append({"kind": trace.KIND_STEREO,
                       "ts_ns": 1_000_000_000 + stamp_slot * GRID_NS,
                       "mono_ns": 5_000_000_000 + i * GRID_NS,
                       "seq": seq0 + i, "payload": payload})
        if imu:   # 每帧后跟 20 个 IMU 包（1kHz）。IMU 戳走自己的 1ms 格子 ——
                  # 现场它是否跟着双目戳一起跳正是第三个待查假设，别在测试里
                  # 预先替它选边
            for _ in range(20):
                imu_ns += 1_000_000
                frames.append({
                    "kind": trace.KIND_IMU, "ts_ns": imu_ns,
                    "mono_ns": frames[-1]["mono_ns"], "seq": len(frames),
                    "payload": struct.pack("<6d", 0, 0, 9.8, 0, 0, 0)})
        slot += 1
    return frames


def write_capture(directory, frames, per_chunk=None, tail=b""):
    os.makedirs(directory, exist_ok=True)
    chunk, count, stream = 0, 0, None

    def open_next():
        nonlocal stream, chunk, count
        if stream:
            stream.close()
        stream = open(os.path.join(directory, f"samples-{chunk:04d}.bin"), "wb")
        chunk, count = chunk + 1, 0

    open_next()
    for frame in frames:
        if per_chunk and count >= per_chunk:
            open_next()
        stereo = frame["kind"] == trace.KIND_STEREO
        stream.write(trace.HEADER.pack(
            trace.RAW_STREAM_MAGIC, trace.RAW_STREAM_VERSION, frame["kind"],
            trace.HEADER_SIZE, len(frame["payload"]), 0, frame["ts_ns"],
            frame["mono_ns"], 0, frame["seq"],
            640 if stereo else 0, 800 if stereo else 0, 1 if stereo else 0,
            0, 0, 640 if stereo else 0, 0, 0))
        stream.write(frame["payload"])
        count += 1
    if tail:
        stream.write(tail)
    stream.close()


def stereo_frames(frames):
    return [f for f in frames if f["kind"] == trace.KIND_STEREO]


def dip_seqs(frames):
    return [f["seq"] for f in stereo_frames(frames)]


def analyze(frames, **kwargs):
    directory = tempfile.mkdtemp(prefix="trace-test-")
    try:
        write_capture(directory, frames, **kwargs)
        return trace.analyze(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ── 1. 常量与真机验证过的读法一致 ──────────────────────────────────────
def test_constants():
    source = open(CLIENT, encoding="utf-8").read()

    def number(name):
        match = re.search(rf"^{name}\s*=\s*(0x[0-9A-Fa-f]+|\d+)", source, re.M)
        return int(match.group(1), 0) if match else None

    check(trace.HEADER.size == 80, "头部 80 字节（C++ static_assert 同值）")
    check(f'struct.Struct("{trace.HEADER.format}")' in source,
          f"头部格式 {trace.HEADER.format} 与 fays_raw_client.py 逐字一致")
    check(number("RAW_STREAM_MAGIC") == trace.RAW_STREAM_MAGIC == 0x53544F55,
          "magic 一致")
    check(number("RAW_STREAM_VERSION") == trace.RAW_STREAM_VERSION, "version 一致")
    check(number("RAW_PACKET_STEREO") == trace.KIND_STEREO
          and number("RAW_PACKET_IMU") == trace.KIND_IMU, "包类型一致")
    check(number("HEADER_SIZE") == trace.HEADER_SIZE
          and number("IMU_PAYLOAD_SIZE") == trace.IMU_PAYLOAD_SIZE, "尺寸常量一致")


# ── 头部字段读对了没有（错位会让结论看着像样但是假的）───────────────────
def test_header_dims_are_read():
    report = analyze(build_frames({50: 4}))
    check(report["dims"] == (640, 800, 1, 640, 640 * 800),
          f"尺寸/步长/载荷长度都读对（实得 {report['dims']}）")
    check(report["size_mismatch"] == 0 and report["shapes_mixed"] == 0,
          "载荷长度 == step×height，无异形帧")
    check(report["imu_samples"] == 240 * 20,
          f"IMU 包数（实得 {report['imu_samples']}）")


# ── 2/3/4. 两种真假情形，以及 L 直方图 ─────────────────────────────────
def test_replay_is_h2():
    frames = build_frames({50: 4, 120: 4, 200: 4, 210: 6}, replay=True)
    report = analyze(frames)
    check(report["dips"] == 4, f"认出 4 个下陷帧（实得 {report['dips']}）")
    check(report["verdicts"].get("H2_EXACT") == 4,
          f"全部 H2_EXACT（实得 {report['verdicts']}）")
    check(not report["verdicts"].get("H1"), "没有误判成 H1")
    check(all(entry["exact_back"] == [int(entry["lag"])]
              for entry in report["dip_details"]),
          "每个下陷帧都与往前第 L 帧逐字节相同")
    check(report["lag_histogram"].get("4") == 3
          and report["lag_histogram"].get("6") == 1,
          f"L 直方图 {report['lag_histogram']}")


def test_fresh_stamp_is_h1():
    frames = build_frames({50: 4, 120: 4, 199: 6}, replay=False)
    report = analyze(frames)
    check(report["dips"] == 3, f"认出 3 个下陷帧（实得 {report['dips']}）")
    check(report["verdicts"].get("H1") == 3,
          f"全部 H1（实得 {report['verdicts']}）")
    check(not report["verdicts"].get("H2") and not report["verdicts"].get("H2_EXACT"),
          "没有误判成重投递")


def test_replay_depth_wins_over_lag():
    """载荷来自往前 5 帧、戳的 lag 是 4：逐字节相同比 lag 更强，仍报 H2。"""
    frames = build_frames({50: 4}, replay=False, replay_from=5)
    report = analyze(frames)
    detail = report["dip_details"][0]
    check(detail["exact_back"] == [5],
          f"认出载荷来自往前第 5 帧（实得 {detail['exact_back']}）")
    check(detail["verdict"] == "H2_EXACT" and detail["best_back"] == 5,
          f"判为重投递（实得 {detail['verdict']} / j*={detail['best_back']}）")


# ── 5/6/8. 三种「不该硬判」的情形 ──────────────────────────────────────
def test_quiet_scene_is_not_judged():
    report = analyze(build_frames({50: 4}, static=True))
    check(report["dips"] == 1, "下陷帧照样被 ts 认出来")
    check(report["verdicts"].get("QUIET") == 1,
          f"场景静止 → QUIET，不硬判（实得 {report['verdicts']}）")


def test_lag_outside_window_is_skipped():
    report = analyze(build_frames({50: 20}))
    check(report["verdicts"].get("SKIP") == 1,
          f"L=20 超出比对窗 → SKIP（实得 {report['verdicts']}）")
    check(not report["verdicts"].get("H1") and not report["verdicts"].get("H2"),
          "没有硬判")


def test_lost_frame_is_not_a_dip():
    frames = build_frames({}, total=60)
    victim = [i for i, item in enumerate(frames)
              if item["kind"] == trace.KIND_STEREO][30]
    del frames[victim]                    # 抽掉一帧 ⇒ Δseq=2、Δts=40ms
    report = analyze(frames)
    check(report["dips"] == 0, f"丢帧不算下陷（实得 {report['dips']}）")
    check(report["lost_frames"] == 1, f"记了 1 次丢帧（实得 {report['lost_frames']}）")


# ── 9. 分片 / 残尾 / 缺 capture.json ───────────────────────────────────
def test_chunking_and_truncated_tail():
    frames = build_frames({50: 4}, total=80, replay=True)
    directory = tempfile.mkdtemp(prefix="trace-test-")
    try:
        write_capture(directory, frames, per_chunk=21 * 30, tail=b"\x00" * 37)
        report = trace.analyze(directory)
        check(report["chunks"] == 3, f"分成 3 片（实得 {report['chunks']}）")
        check(report["stereo_frames"] == 80,
              f"残尾不影响帧数（实得 {report['stereo_frames']}）")
        check(report["verdicts"].get("H2_EXACT") == 1, "跨片仍判得出重投递")
        check(not os.path.exists(os.path.join(directory, "capture.json")),
              "本用例刻意不写 capture.json")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            trace.print_report(report)
        text = buffer.getvalue()
        check("重投递" in text, "print_report 结论行写的是「重投递」")
        check("capture.json        缺失" in text, "缺 capture.json 时如实说明")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_parent_directory_lists_candidates():
    directory = tempfile.mkdtemp(prefix="trace-test-")
    try:
        inner = os.path.join(directory, "capture-20260917_000000")
        write_capture(inner, build_frames({50: 4}, replay=True))
        try:
            trace.analyze(directory)
            check(False, "指到父目录应当报错并列出候选")
        except SystemExit as exc:
            check("capture-20260917_000000" in str(exc), "指到父目录时列出候选取证目录")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_argmin_evidence():
    """旁证不套基线带：H2 流里不应有「最像前一帧」，反之亦然。

    「像往前第 L 帧」与「逐字节级重合」分开数 —— 重渲染帧（结构同源但不是
    同一幅）只算前者，否则结论行会出现「5 个 H2，旁证 0 个」的自相矛盾。
    """
    replay = analyze(build_frames({50: 4, 120: 4}, replay=True))
    rerender = analyze(build_frames({50: 4, 120: 4}, rerender=True))
    fresh = analyze(build_frames({50: 4, 120: 4}, replay=False))
    check(replay["argmin_evidence"]["best_is_prev"] == 0
          and replay["argmin_evidence"]["best_is_lag"] == 2
          and replay["argmin_evidence"]["best_is_lag_and_near_zero"] == 2,
          f"H2 流（逐字节）旁证 {replay['argmin_evidence']}")
    check(rerender["argmin_evidence"]["best_is_lag"] == 2
          and rerender["argmin_evidence"]["best_is_lag_and_near_zero"] == 2,
          f"H2 流（重渲染）旁证 {rerender['argmin_evidence']}")
    check(fresh["argmin_evidence"]["best_is_prev"] == 2
          and fresh["argmin_evidence"]["best_is_lag"] == 0,
          f"H1 流旁证 {fresh['argmin_evidence']}")


# ── 14. 真机 09-17 的形态：旧纹理 + 新电平 ⇒ 裸 mad 判不出来，边缘图判得出 ──
def test_rerendered_old_frame_is_h2_not_h1():
    """下陷帧的纹理是往前第 L 帧的、整幅电平是当帧的（模拟「旧数据在输出级之前
    又过了一遍」，像素值变了但几何没变）。

    现场 4 个事件里 3 个是这个形态：第一版判据（比 mad 与全场基线的落点）在这
    上面**给过错误结论**（报了「只戳错 H1」）。所以这里不只检新判据，还**当场
    验证旧判据的两个条件都成立** —— 否则这条测试可能因为模型退化而假绿。

    注：真机那次 mad 分不开的**机制**是场景动得慢（对照 mad lag1→lag4 只涨
    1.15 倍），电平其实没动（δ\*=0）；这里额外加 ±4/帧 的电平漂移是为了造一个
    **更严**的人造情形 —— 连「电平换过」这种最难分的形态也要判出来。
    """
    frames = build_frames({50: 4, 120: 4}, rerender=True)
    report = analyze(frames)
    deps = report["dip_details"]
    first = deps[0]
    base1, base4 = report["base"][1]["median"], report["base"][4]["median"]
    check(not first["exact_back"], "载荷与往前第 4 帧**不**逐字节相同")
    check(0.5 * base4 <= first["mads"][4] <= 2 * base4
          and 0.5 * base1 <= first["mads"][1] <= 2 * base1,
          f"裸 mad 落在正常帧带内（mad(i,i-4)={first['mads'][4]:.1f} vs "
          f"base[4]={base4:.1f}；mad(i,i-1)={first['mads'][1]:.1f} vs base[1]={base1:.1f}）"
          f"⇒ 旧判据在这里必判 H1")
    check(first["edges"][4] < 0.2 * first["edges"][1] and first["best_back"] == 4,
          f"但边缘差 e(4)={first['edges'][4]:.2f} 远小于 e(1)={first['edges'][1]:.2f}，"
          f"最像往前第 {first['best_back']} 帧")
    check(report["verdicts"].get("H2") == 2 and not report["verdicts"].get("H1"),
          f"两个都判成重投递 H2（实得 {report['verdicts']}）")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        trace.print_report(report)
    check("重投递" in buffer.getvalue(), "结论行写给的是「重投递」")


# ── 15. 对照必须取自同一个窗，不能是全场基线 ───────────────────────────
def test_control_is_local_to_the_window():
    """前半段纹理冻住（边缘差恒 0）、后半段动起来 —— 后半段的下陷帧照样判得出。

    全场基线会被静场那段拖到 0 附近（除以 0 都算不出来）；同窗对照不受影响。
    这条同时覆盖 `_edge_control` 里 step==0 要跳过的分支。
    """
    frames = build_frames({202: 4}, total=240, frozen_before=120)
    report = analyze(frames)
    check(report["dips"] == 1, f"认出 1 个下陷帧（实得 {report['dips']}）")
    detail = report["dip_details"][0]
    check(detail["edge_ref"] and detail["edge_ref"] > 1.0,
          f"对照取自动段（实得 e(4)/e(1) 对照 {detail.get('edge_ref')}）")
    check(detail["verdict"] == "H1",
          f"动段里的新图像仍判成 H1（实得 {detail['verdict']}）")


# ── 10. 与 [TIME_DROP] 日志交叉验证 ────────────────────────────────────
def test_cross_check_with_native_log():
    frames = build_frames({50: 4, 120: 6}, replay=True)
    seqs = dip_seqs(frames)
    directory = tempfile.mkdtemp(prefix="trace-test-")
    try:
        write_capture(directory, frames)
        log = os.path.join(directory, "native.log")
        with open(log, "w", encoding="utf-8") as stream:
            for seq, delta in ((seqs[50], -0.06), (seqs[120], -0.10)):
                stream.write(
                    '{"time": 1789624680.3, "arrival_monotonic": 275430.8, '
                    f'"line": "[TIME_DROP] ts=13316.9 previous=13316.9 '
                    f'delta={delta} seq={seq} prev_seq={seq - 1} '
                    'consecutive=1 total=1"}\n')
        report = trace.analyze(directory, native_log=log)
        cross = report["native_log"]
        check(cross["logged"] == 2, f"日志里 2 条 TIME_DROP（实得 {cross['logged']}）")
        check(cross["matched_in_capture"] == 2,
              f"两条的 seq 都在取证里对上（实得 {cross['matched_in_capture']}）")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ── IMU 分支 ───────────────────────────────────────────────────────────
def test_imu_stream():
    report = analyze(build_frames({50: 4}))
    imu = report.get("imu")
    check(imu and imu["count"] == 240 * 20, f"IMU 计数（实得 {imu and imu['count']}）")
    check(imu["nonpositive"] == 0 and imu["median_step_ns"] == 1_000_000,
          f"IMU 步进正常（中位 {imu and imu['median_step_ns']}）")


# ── 真实取证的相位偏移：绝对值不在格点上，差值才在 ─────────────────────
def test_offset_phase_is_not_counted_as_off_grid():
    """真机就是这样（1043/1043 帧的 ts%20ms 都是同一个 6.05ms）。

    第一版按**绝对值**判格点，把 4318/4318 帧全报成「偏离 20ms 整数倍」，
    纯属误报 —— 格子性质是差值的事（设备戳 = 常数起点 + 计数×20ms）。
    """
    frames = build_frames({50: 4}, total=120, replay=True)
    for frame in frames:                       # 整体挪 6.05ms，差值不变
        frame["ts_ns"] += 6_050_000
    report = analyze(frames)
    check(report["ts_phase_kinds"] == 1 and report["ts_phase_ns"] == 6_050_000,
          f"认得出恒定相位（实得 {report['ts_phase_kinds']} 种 / "
          f"{report['ts_phase_ns']}）")
    check(report["grid_off"] == 0,
          f"差值仍在格点上 → 不报偏离（实得 {report['grid_off']}）")
    check(report["dips"] == 1 and report["verdicts"].get("H2_EXACT") == 1,
          "相位偏移不影响下陷判定")


# ── 没有下陷帧时的结论行：说「没样本」，不许含糊成判读 ────────────────
def test_no_dip_report_says_no_sample():
    report = analyze(build_frames({}, total=60))
    check(report["dips"] == 0 and not report["verdicts"], "确实没有可判的帧")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        trace.print_report(report)
    text = buffer.getvalue()
    check("一个下陷帧都没有" in text and "不是结论" in text,
          "结论行写的是「没样本」而不是判读")


    total = max(_level(i) for i in range(240))
    check(int(_TEXTURE.max()) + total <= 255 and int(_TEXTURE.min()) + LEVEL_BASE >= 0,
          f"纹理动态范围 + 电平漂移不溢出 8 位"
          f"（纹理 {int(_TEXTURE.min())}~{int(_TEXTURE.max())}，电平峰 {total}）")


# ── 16. 电平模型没有折返伪影（下陷帧下标选在折返点上 ⇒ 测试假红）──────
def test_level_model_has_no_fold_artifacts():
    """三角波的折返点若正好落在下陷帧前 2 帧，lag-4 的电平差会抵消成 0，
    重渲染帧的 mad 就掉到正常帧带外 —— 那是**模型伪影**，不是判据的事。

    这条把本文件用到的下陷帧下标逐个钉住：它们必须落在电平单调的那一段里
    （|Δ电平(4)| 满额 = 4×LEVEL_STEP）。
    """
    used = sorted({50, 120, 202, 210})
    bad = [i for i in used if abs(_level(i) - _level(i - 4)) != 4 * LEVEL_STEP]
    check(not bad, f"用到的下陷帧下标都避开折返点（越界 {bad}）")
    check(all(abs(_level(i) - _level(i - 1)) == LEVEL_STEP for i in used),
          "相邻帧电平差恒为 ±LEVEL_STEP（三角波没有断崖）")
    total = max(_level(i) for i in range(240))
    check(int(_TEXTURE.max()) + total <= 255 and int(_TEXTURE.min()) + LEVEL_BASE >= 0,
          f"纹理动态范围 + 电平漂移不溢出 8 位"
          f"（纹理 {int(_TEXTURE.min())}~{int(_TEXTURE.max())}，电平峰 {total}）")


def main():
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            print(f"\n{name}")
            func()
    if FAILS:
        print(f"\n{len(FAILS)} 项失败：")
        for item in FAILS:
            print(f"  - {item}")
        return 1
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
