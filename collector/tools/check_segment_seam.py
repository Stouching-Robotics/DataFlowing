#!/usr/bin/env python
"""检查每段视频的开头是不是上一段的延续（RGB 段首旧帧验收工具）。

背景：`_external_queues` 曾在录制边界不清空，导致上一段残留 + 本段启动窗口
累积的帧被写成新 mp4 的开头。取证时用的判据就是「新段首帧与上一段末帧高度
相似」——修好后这个相似度应该塌到无关帧的水平。

用法:
    venv/bin/python tools/check_segment_seam.py [录制任务目录]
默认 data/recordings/UMIGripper_Action_AI（相对**仓库根**，可从任意 cwd 运行）

输出每段：首帧 vs 上一段末帧的匹配分（0=完全相同，越大越无关）。判据是
**相对**的——真实场景里相邻两段常在同一个桌面/同一位置开录，首帧本来就可能
相似，所以看的是「首帧 vs 上段末帧」是否显著低于「首帧 vs 本段自己的倒数第
30 帧」：残留恰好是上段末尾的连续 ~28 帧，越靠近末帧越像。
"""
import os
import sys
import glob

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DEFAULT_TASK_DIR = os.path.join(ROOT, "data", "recordings",
                                "UMIGripper_Action_AI")


def first_last_frames(path, n=1):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return [], 0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    ok, head = cap.read()
    head = head if ok else None
    tail = {}
    for back in (1, 5, 15, 30):
        idx = total - back
        if idx < 0:
            continue
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, f = cap.read()
        if ok:
            tail[back] = f
    cap.release()
    return (head, total, tail)


def score(a, b):
    """归一化灰度差的相似分（0=完全相同，越大越不同）。"""
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY).astype(np.float32)
    if ga.shape != gb.shape:
        gb = cv2.resize(gb, (ga.shape[1], ga.shape[0]))
    return float(np.abs(ga - gb).mean())


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TASK_DIR
    eps = sorted(glob.glob(os.path.join(root, "videos", "chunk-*", "*",
                                        "episode-*.mp4")))
    if len(eps) < 2:
        print(f"未找到成对的 episode 视频：{root}")
        print(f"（相对路径按仓库根 {ROOT} 解析；也可显式传入录制任务目录）")
        return 1
    # 按 (相机目录, 段号) 分组，同一相机内相邻段才有继承关系
    groups = {}
    for p in eps:
        cam = os.path.dirname(p)
        groups.setdefault(cam, []).append(p)

    bad = 0
    for cam, paths in sorted(groups.items()):
        paths = sorted(paths)[-12:]          # 只看最近 12 段
        print(f"\n{cam.replace(root + '/', '')}")
        print(f"  {'本段':<14}{'首帧 vs 上段末帧':>16}{'vs 倒数5':>10}"
              f"{'vs 倒数15':>11}{'vs 倒数30':>11}   判定")
        prev = None
        for p in paths:
            head, total, tail = first_last_frames(p)
            name = os.path.basename(p)
            if head is None:
                print(f"  {name:<14}  读取失败")
                continue
            if prev is not None:
                prow, ptotal, ptail = prev
                cur = {b: score(head, f) for b, f in tail.items() if b in ptail}
                pcur = {b: score(head, ptail[b]) for b in ptail}
                # 残留判据：与本段自己队内第 30 帧比，跟上一段末帧比反而更像
                seam = pcur.get(1)
                own30 = cur.get(30)
                verdict = "?"
                if seam is not None and own30 is not None:
                    verdict = ("⚠ 疑似残留" if seam < own30 - 2.0
                               else "OK 无继承")
                    if seam < own30 - 2.0:
                        bad += 1
                print(f"  {name:<14}{pcur.get(1, float('nan')):>16.2f}"
                      f"{pcur.get(5, float('nan')):>10.2f}"
                      f"{pcur.get(15, float('nan')):>11.2f}"
                      f"{pcur.get(30, float('nan')):>11.2f}   {verdict}")
            prev = (head, total, tail)

    print()
    if bad:
        print(f"FAIL: {bad} 段疑似以「录制开始前」的帧开头")
        return 1
    print("PASS: 未发现段首继承上一段末尾的迹象")
    return 0


if __name__ == "__main__":
    sys.exit(main())
