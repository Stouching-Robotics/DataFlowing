"""发布位姿死区（最小可报告位移）单测（v1.3.14）。

用户报的「大幅漂移没了，但静止时轨迹线/末端点还有小抖」= 估计器的毫米级游走。
实测（69061 条发布位姿 / 20 个会话，见 protocol.POSE_PUBLISH_DEADBAND_M 注释）：
静止段位置游走半径逐会话 p50 1.06-1.85mm、max 1.39-4.98mm（严格口径：
10s 窗净位移<2mm **且**窗内每点距窗中位<5mm）。默认视野 327px/m 下看不出来，放大
4-10× 就是 3-10px 的连续抖动。

这里钉住四件事：
  ① 死区是**对上次发布值的累积位移**，不是相邻样本差 —— 静止游走必须被冻住，
     而真实慢速移动（每帧都不到死区）必须照常推进；这是它与跳变门的分别。
  ② 保持时**发布上次的值**（不是不发布）：采样节拍/时间戳不变，轨迹不再加噪。
  ③ 死区不得吃掉真实运动：单帧 ≥3mm 的位移一律照发（最快的手动速度也不受影响）。
  ④ 会话首帧 / 重设原点后（参照为 None）必须放行。

用法:
    venv/bin/python tools/tests/test_pose_publish_deadband.py
"""
import glob
import math
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.gripper.slam import protocol as proto  # noqa: E402

EPS = proto.POSE_PUBLISH_DEADBAND_M
FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def publish_stream(positions, epsilon=EPS):
    """按发布点语义跑一遍：返回 (发布值序列, 是否保持的掩码)。"""
    out, held, ref = [], [], None
    for p in positions:
        if proto.pose_below_deadband(p, ref, epsilon):
            out.append(ref)
            held.append(True)
        else:
            ref = p
            out.append(p)
            held.append(False)
    return out, held


# ---------------------------------------------------------------- ① 语义

def test_helper_semantics():
    ref = (0.0, 0.0, 0.0)
    check(
        proto.pose_below_deadband((1.0, 2.0, 3.0), None) is False,
        "参照为 None（首帧/重设原点）→ 放行",
    )
    check(
        proto.pose_below_deadband((EPS * 0.5, 0.0, 0.0), ref) is True,
        f"位移 {EPS/2*1000:.1f}mm < 死区 {EPS*1000:.0f}mm → 保持",
    )
    check(
        proto.pose_below_deadband((EPS * 1.001, 0.0, 0.0), ref) is False,
        f"位移稍超死区 → 放行（判据是 <，等于死区的真实位移不被吞）",
    )
    check(
        proto.pose_below_deadband((0.0, 0.0, EPS * 2), ref) is False,
        "沿单轴 2×死区 → 放行（三轴等权）",
    )
    check(
        proto.pose_below_deadband(
            (EPS * 0.6, EPS * 0.6, EPS * 0.6), ref) is False,
        "三轴各 0.6×死区 → 合成 1.04×死区 → 放行（按欧氏距离，不逐轴判）",
    )
    check(
        math.isclose(
            proto.POSE_PUBLISH_DEADBAND_M, 0.003, abs_tol=1e-12),
        f"死区常量 = {proto.POSE_PUBLISH_DEADBAND_M*1000:.1f}mm（实测值 3mm）",
    )


# ---------------------------------------------------------------- ② 静止游走

def test_rest_wander_frozen():
    """按实测统计量合成静止游走。

    实测静止段是**有界的小球**而不是无界随机游走：每帧步长 p50≈0.14mm、
    p99≈0.45mm，但 10s 窗内的游走半径只有 p50 1.06-1.85mm、max 1.39-4.98mm —— 即
    高频来回（均值回复），不是越走越远。用 AR(1)（系数 0.96）复现这个形状：
    步长与半径都对得上，纯随机游走会走到 10mm+，那就不是用户看到的抖了。
    """
    rng = random.Random(20261013)
    a, sigma = 0.96, 0.00010
    pos = [0.0, 0.0, 0.0]
    positions = []
    for _ in range(600):            # 20s @30fps
        pos = [
            a * pos[0] + rng.gauss(0.0, sigma),
            a * pos[1] + rng.gauss(0.0, sigma),
            a * pos[2] + rng.gauss(0.0, sigma),
        ]
        positions.append(tuple(pos))
    steps = [math.dist(positions[i], positions[i - 1])
             for i in range(1, len(positions))]
    raw_radius = max(math.dist(p, (0.0, 0.0, 0.0)) for p in positions)
    out, held = publish_stream(positions)
    frozen = sum(1 for i, h in enumerate(held) if h and out[i] == out[max(0, i - 1)])
    distinct = len({o for o in out})

    check(
        0.0005 < raw_radius < 0.004,
        f"合成游走半径 {raw_radius*1000:.2f}mm 落在实测量级"
        f"（逐会话 p50 1.06-1.85mm / max 1.39-4.98mm）",
    )
    check(
        sorted(steps)[len(steps) // 2] < EPS,
        f"合成每帧步长 p50 {sorted(steps)[len(steps)//2]*1000:.3f}mm < 死区"
        "（单帧全部在死区之下 —— 这正是跳变门管不着的那类噪声）",
    )
    check(
        sum(held) / len(held) > 0.95,
        f"死区冻住了 {100*sum(held)/len(held):.1f}% 的帧（实测会话 99.8-100%）",
    )
    check(
        distinct < 0.05 * len(positions),
        f"600 帧静止只发出 {distinct} 个不同位置（<5%，原来 600 个各不相同）",
    )
    check(
        frozen == sum(held),
        "保持帧发布的确实是上一次的值（末端点不动，而不是发了别的值）",
    )


def test_slow_motion_still_advances():
    """真实慢速移动：1mm/帧（≈3cm/s @30fps）必须照常前进，只是量化到死区。"""
    positions = [(i * 0.001, 0.0, 0.0) for i in range(300)]
    out, held = publish_stream(positions)
    err = max(abs(out[i][0] - positions[i][0]) for i in range(len(positions)))
    check(
        abs(out[-1][0] - positions[-1][0]) < EPS,
        f"300 帧后末端仍在真值 {EPS*1000:.0f}mm 内"
        f"（走了 {out[-1][0]*1000:.1f}mm，真值 {positions[-1][0]*1000:.0f}mm）",
    )
    check(
        err < EPS,
        f"全程误差 {err*1000:.2f}mm < 死区（量化不累积）",
    )
    check(
        sum(held) < 0.9 * len(held),
        f"移动时确实在发新值（保持率 {100*sum(held)/len(held):.0f}%，"
        "不是把运动也冻住）",
    )
    # 单调前进且在推：每 3 帧至少推进一次
    advances = sum(
        1 for i in range(1, len(out)) if out[i][0] > out[i - 1][0])
    check(
        advances >= 90,
        f"单调推进 {advances} 次（300 帧里按死区每 3 帧推一次是下限）",
    )


# ---------------------------------------------------------------- ③ 不吃运动

def test_real_motion_never_blocked():
    """单帧位移 ≥ 死区一律照发 —— 最大实测真实单帧步长 0.0601m 也不例外。"""
    for step in (EPS, 0.01, 0.0601, 0.1):
        held = proto.pose_below_deadband((step, 0.0, 0.0), (0.0, 0.0, 0.0))
        check(
            held is False,
            f"单帧 {step*1000:.1f}mm 位移照发（死区只压 <{EPS*1000:.0f}mm 的噪声）",
        )
    # 静止 10s 后突然快移：第一帧就该发出去，不因"冻久了"而迟滞
    positions = [(0.0, 0.0, 0.0)] * 300 + [(0.05, 0.0, 0.0)]
    out, held = publish_stream(positions)
    check(
        held[-1] is False and out[-1][0] == 0.05,
        "静止后突然移动 50mm：第一帧立即发布（无迟滞）",
    )


# ---------------------------------------------------------------- ④ 真日志回放

def test_replay_real_session():
    """拿真会话回放：静止窗冻结率、全段误差、运动不丢。日志不在就跳过。"""
    cands = sorted(glob.glob(os.path.join(
        ROOT, "logs/slam_native/*_slam_stdout.log")))
    picked = [p for p in cands
              if "20261013_114841" in p] or cands[-1:]
    if not picked:
        print("  SKIP: 没有 logs/slam_native/*.log（本机数据不入库）")
        return
    path = picked[0]
    positions = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            event = proto.parse_slam_line(line)
            if event is not None and event.kind == "pose":
                positions.append(tuple(event.payload.position))
    if len(positions) < 300:
        print(f"  SKIP: {os.path.basename(path)} 只有 {len(positions)} 条位姿")
        return

    # 静止窗用**净位移**独立判定（与死区的判据无关，避免自证）
    W = 300
    rest = [False] * len(positions)
    for i in range(0, len(positions) - W, 30):
        if math.dist(positions[i + W - 1], positions[i]) < 0.002:
            for k in range(i, i + W):
                rest[k] = True
    out, held = publish_stream(positions)
    held = list(held)
    rest_hold = (sum(1 for i in range(len(held)) if rest[i] and held[i])
                 / max(1, sum(rest)))
    err = max(math.dist(out[i], positions[i]) for i in range(len(positions)))
    raw_path = sum(math.dist(positions[i], positions[i - 1])
                   for i in range(1, len(positions)))
    pub_path = sum(math.dist(out[i], out[i - 1])
                   for i in range(1, len(out)))

    print(f"  （回放 {os.path.basename(path)}：{len(positions)} 条位姿，"
          f"静止帧占 {100*sum(rest)/len(rest):.0f}%）")
    check(
        rest_hold > 0.95,
        f"真会话静止窗冻结率 {100*rest_hold:.1f}% > 95%（下游末端点不再抖）",
    )
    check(
        err < EPS + 1e-9,
        f"全段发布误差 max {err*1000:.2f}mm < {EPS*1000:.0f}mm（量化不超死区）",
    )
    check(
        pub_path < raw_path,
        f"发布路径长 {pub_path:.3f}m < 原始 {raw_path:.3f}m"
        f"（掉掉的 {100*(1-pub_path/raw_path):.0f}% 全是静止游走）",
    )
    check(
        pub_path > 0.5 * raw_path,
        f"发布路径长仍有原始的 {100*pub_path/raw_path:.0f}%（没把运动也削掉）",
    )


def main():
    print("发布位姿死区单测（v1.3.14）")
    print("[1] 语义（对上次发布值的累积位移 / None 放行 / 边界）")
    test_helper_semantics()
    print("[2] 静止游走必须冻住")
    test_rest_wander_frozen()
    print("[3] 真实慢速移动必须照常推进（量化不累积）")
    test_slow_motion_still_advances()
    print("[4] 不吃真实运动（无迟滞）")
    test_real_motion_never_blocked()
    print("[5] 真会话回放")
    test_replay_real_session()

    if FAILS:
        print(f"\nFAIL: {len(FAILS)} 条")
        for f in FAILS:
            print("  -", f)
        return 1
    print(
        f"\nPASS: 死区 {EPS*1000:.0f}mm 冻住静止游走（>95% 帧保持且保持的是上次的值），"
        "真实移动照常推进且误差不超死区、无迟滞")
    return 0


if __name__ == "__main__":
    sys.exit(main())
