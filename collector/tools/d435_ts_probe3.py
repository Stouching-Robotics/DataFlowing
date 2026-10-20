#!/usr/bin/env python3
"""探针 3：单流 vs 多流，判定重复帧是设备重发还是 syncer 补帧。

  场景 1  只开 color          → 无跨流可拼，syncer 没有补帧的理由
  场景 2  只开 depth
  场景 3  color + depth + ir1 → 生产链路口径

每场景统计该流 frame_counter 的增量分布：
  若场景 1/2 里 Δcounter 恒为 1，而场景 3 出现 0/2 → 重复帧是 **syncer 拼帧**造成的。
"""
import collections
import sys
import time

import pyrealsense2 as rs

SEC = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0
DUR, RGB, FPS = (848, 480), (1280, 720), 30
CNT = rs.frame_metadata_value.frame_counter


def scenario(tag, streams, watch):
    cfg = rs.config()
    for st in streams:
        if st == rs.stream.color:
            cfg.enable_stream(rs.stream.color, RGB[0], RGB[1], rs.format.rgb8, FPS)
        elif st == rs.stream.depth:
            cfg.enable_stream(rs.stream.depth, DUR[0], DUR[1], rs.format.z16, FPS)
        else:
            cfg.enable_stream(rs.stream.infrared, 1, DUR[0], DUR[1], rs.format.y8, FPS)
    pipe = rs.pipeline()
    pipe.start(cfg)
    # 预热 2s（自动曝光/流稳定）
    t0 = time.monotonic()
    while time.monotonic() - t0 < 2.0:
        try:
            pipe.wait_for_frames(timeout_ms=1000)
        except RuntimeError:
            pass
    rec = collections.defaultdict(list)
    t_end = time.monotonic() + SEC
    n_fs = 0
    while time.monotonic() < t_end:
        try:
            fs = pipe.wait_for_frames(timeout_ms=2000)
        except RuntimeError:
            continue
        n_fs += 1
        for st, nm in ((rs.stream.color, "color"), (rs.stream.depth, "depth"),
                       (rs.stream.infrared, "infrared")):
            try:
                f = (fs.get_infrared_frame(1) if st == rs.stream.infrared
                     else fs.get_color_frame() if st == rs.stream.color
                     else fs.get_depth_frame())
            except Exception:
                f = None
            if f is None:
                continue
            try:
                rec[nm].append((time.monotonic_ns(),
                                f.get_frame_metadata(CNT)))
            except Exception:
                pass
    pipe.stop()
    print(f"\n--- {tag} ---  frameset={n_fs}")
    for nm in watch:
        v = rec.get(nm) or []
        if len(v) < 5:
            print(f"  {nm:9s} 样本不足")
            continue
        c = [int(x[1]) for x in v]
        d = [b - a for a, b in zip(c, c[1:])]
        span = (v[-1][0] - v[0][0]) / 1e9
        print(f"  {nm:9s} 帧={len(c):5d}  {len(c)/span:5.2f} fps  "
              f"counterΔ 分布 {dict(sorted(collections.Counter(d).items()))}")
    return rec


if __name__ == "__main__":
    scenario("场景1  只开 color", [rs.stream.color], ["color"])
    scenario("场景2  只开 depth", [rs.stream.depth], ["depth"])
    scenario("场景3  color+depth+ir1（生产链路）",
             [rs.stream.color, rs.stream.depth, rs.stream.infrared],
             ["color", "depth", "infrared"])
