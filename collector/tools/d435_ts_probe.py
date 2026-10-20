#!/usr/bin/env python3
"""D435 时间戳取证探针（真机）。

完全复刻 core/d435_camera.py 的流配置：
    color rgb8 1280x720@30 + depth z16 848x480@30 + infrared#1 y8 848x480@30

逐帧记录：
  - frames.get_timestamp() / get_frame_timestamp_domain()
  - 每一路(color/depth/ir1) 的 supports_frame_metadata(frame_timestamp)
    与其值、frame_counter、sensor_timestamp、get_frame_number()
  - wait_for_frames 返回瞬间的宿主单调钟

用法:
    venv/bin/python3 tools/d435_ts_probe.py [秒数] [输出csv]
"""
import csv
import os
import sys
import time

import pyrealsense2 as rs

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
OUT = sys.argv[2] if len(sys.argv) > 2 else "/home/stouch/collector/tmp_docfill/ts_audit/live/probe.csv"

DUR = 848
RES = (848, 480)
RGB = (1280, 720)
FPS = 30


def meta(frame, key):
    """安全读元数据：不支持 → None。"""
    try:
        if frame is None or not frame.supports_frame_metadata(key):
            return None
        v = frame.get_frame_metadata(key)
        return None if v is None else float(v)
    except Exception:
        return None


def supports(frame, key):
    try:
        return bool(frame is not None and frame.supports_frame_metadata(key))
    except Exception:
        return None


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    pipe = rs.pipeline()
    cfg = rs.config()
    cfg.enable_stream(rs.stream.color, RGB[0], RGB[1], rs.format.rgb8, FPS)
    cfg.enable_stream(rs.stream.depth, RES[0], RES[1], rs.format.z16, FPS)
    cfg.enable_stream(rs.stream.infrared, 1, RES[0], RES[1], rs.format.y8, FPS)
    prof = pipe.start(cfg)
    dev = prof.get_device()
    print(f"device={dev.get_info(rs.camera_info.name)} "
          f"serial={dev.get_info(rs.camera_info.serial_number)} "
          f"fw={dev.get_info(rs.camera_info.firmware_version)}", flush=True)

    ftm = rs.frame_metadata_value.frame_timestamp
    fcm = rs.frame_metadata_value.frame_counter
    stm = rs.frame_metadata_value.sensor_timestamp

    cols = ["i", "host_mono_ns", "fs_ts_ms", "fs_domain",
            "c_sup_ts", "c_ts", "c_cnt", "c_sensor", "c_num",
            "d_sup_ts", "d_ts", "d_cnt", "d_sensor", "d_num",
            "i_sup_ts", "i_ts", "i_cnt", "i_sensor", "i_num"]
    fh = open(OUT, "w", newline="")
    w = csv.writer(fh)
    w.writerow(cols)

    t_end = time.monotonic() + SECONDS
    n = 0
    prev = {}
    print(f"采集中 {SECONDS:.0f}s → {OUT}", flush=True)
    while time.monotonic() < t_end:
        try:
            fs = pipe.wait_for_frames(timeout_ms=2000)
        except RuntimeError:
            continue
        host = time.monotonic_ns()
        c, d, ir = (fs.get_color_frame(), fs.get_depth_frame(),
                    fs.get_infrared_frame(1))
        try:
            dom = fs.get_frame_timestamp_domain().name
        except Exception:
            dom = "?"
        row = [n, host, fs.get_timestamp(), dom,
               supports(c, ftm), meta(c, ftm), meta(c, fcm), meta(c, stm),
               c.get_frame_number() if c else None,
               supports(d, ftm), meta(d, ftm), meta(d, fcm), meta(d, stm),
               d.get_frame_number() if d else None,
               supports(ir, ftm), meta(ir, ftm), meta(ir, fcm), meta(ir, stm),
               ir.get_frame_number() if ir else None]
        w.writerow(row)
        # 实时异常提示
        for tag, idx in (("depth", 10), ("color", 5)):
            v = row[idx]
            k = tag
            if v is not None and k in prev:
                dv = v - prev[k]
                if dv == 0 or (tag == "depth" and dv > 50000):
                    print(f"  [{n:6d}] {tag} Δmeta={dv:12.0f}us cnt={row[idx+1]} "
                          f"num={row[idx+3]}", flush=True)
            if v is not None:
                prev[k] = v
        n += 1
    fh.close()
    pipe.stop()
    print(f"完成：{n} 帧", flush=True)


if __name__ == "__main__":
    main()
