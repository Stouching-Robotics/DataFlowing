#!/usr/bin/env python3
"""⚠️ **本脚本尚未跟上 2026-09-21 的 SDK v2.1.0 迁移，当前跑不通也产不出正确产物。**
   迁移计划 S7 整体重写。重写时必须满足下面这条**位置契约**：

       zip 名      wheels/toolkit/glove_sdk.zip
       zip 顶层    裸的 `glove_sdk/`（**不含** tools/ 前缀 —— 产物与位置无关）
       解压目标    tools/      ← 由 start.sh / start.bat / start_lite.* 指定
       落点        <项目根>/tools/glove_sdk/  （core/glove_sdk_boot.py 的
                   find_sdk_dir() 找的就是这里）

   位置只写在分发壳那一处，zip 本身不含路径假设 —— 再挪地方不用重打包。

裁剪手套工具包（stouch_glove_toolkit*）供随包分发，使普通版一键部署自带骨架解算。

背景: 骨架解算依赖第三方手套工具包（项目根同级的 stouch_glove_toolkit*，
core/glove_keypoint_solver.py 的 find_toolkit_dir() 按目录名找）。本地参考副本
2.1G，其中 2.1G 是 .venv/、7.3M 是 MANO 模型的 .pkl —— 解算路径两者都不需要，
真正的运行时闭包约 6M。

本脚本按白名单裁一份，并在裁剪产物上**实跑一遍解算链**（导入 → 构造 HandSolver
→ 喂帧到 warmup 完成 → 校验 (21,3) 关键点），确认裁掉的东西确实用不上。

用法:
    python scripts/pack_toolkit.py                  # 裁一份 → wheels/toolkit/glove_toolkit.zip
    python scripts/pack_toolkit.py --no-verify      # 跳过实跑校验（没装依赖时）
    python scripts/pack_toolkit.py --src DIR        # 指定源工具包目录
    python scripts/pack_toolkit.py --keep DIR       # 同时留一份展开的目录
    python scripts/pack_toolkit.py --out DIR        # 自定义输出目录

产物:
    <out>/toolkit/glove_toolkit.zip   解压后即为项目根同级的 stouch_glove_toolkit*/
    <out>/toolkit/<工具包名>/         --keep 时的展开副本（可直接拷到项目根）

注意: zip 必须放在 wheels/ 的**子目录**里 —— 放在 wheels/ 根会让
start.bat 的 `pip install --find-links wheels` 把它当成源码包尝试解析。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLKIT_PREFIX = "stouch_glove_toolkit"

# 解算路径的运行时闭包（白名单）。目录整体拷贝，单文件按名拷贝。
KEEP_DIRS = [
    "glove_sdk",              # HandSolver / RawImuFrame 等公开接口
    "glove_core",             # 算法与受保护运行时（含 glove_lite 手模型）
    "assets/hand_geometry",   # hand_measured_runtime_v1.json（几何）
    "assets/hand",            # HAND_LEFT/RIGHT_PINKY_PLUS_2MM.npz（手模型）
]
KEEP_FILES = [
    "99-stm32-glove.rules",       # Linux 免 root 访问 USB CDC 的 udev 规则
    "glove_devices.json",         # 设备配置（采集侧同款）
    "config.json",                # 工具包自带标定配置（param_inst_calib）
    "config_left.json",
    "bimanual_display_config.json",
    "requirements.txt",
    "README-SDK.md",
    "QUICKSTART.md",
]
# 明确不要的（白名单之外的重物，注释写明原因，避免以后有人"顺手加回来"）
DROP_NOTE = {
    ".venv": "工具包自带的虚拟环境 2.1G；依赖由本仓库 requirements.txt 统一装",
    "apps": "GUI/标定界面，解算链不导入（已实测）",
    "firmware": "固件源码，上位机不需要",
    "glove_io": "独立 IO 程序",
    "glove_runtime": "独立运行时壳",
    "tools": "工具包自带小工具",
    "assets/hand/models": "MANO_*.pkl 7.3M；HandNumpy 只读 .npz（已实测）",
    "calibration": "只留两个 *_default.json —— 其它是开发机上这只手套的标定，"
                   "带过去会被 pick_calibration 按时间戳优先选中（见下）",
    "imu_log.csv": "1.4M 实测日志",
    "hand_kinematics_guide.html": "文档",
    "pyarmor.bug.log": "打包日志",
    "ENCRYPTION.md": "加密说明（本地明文版用不到）",
}


def find_toolkit_dir(src: Path | None) -> Path:
    """定位源工具包目录（默认扫项目根，与 find_toolkit_dir() 同口径）。"""
    if src is not None:
        if not src.is_dir():
            sys.exit(f"[错误] --src 不是目录: {src}")
        return src
    cands = sorted(p for p in ROOT.iterdir()
                   if p.is_dir() and p.name.lower().startswith(TOOLKIT_PREFIX))
    if not cands:
        sys.exit(f"[错误] 项目根未找到 {TOOLKIT_PREFIX}* 目录（用 --src 指定）")
    if len(cands) > 1:
        print(f"[警告] 找到多个工具包目录，取第一个: {cands[0].name}")
        print(f"       全部: {', '.join(p.name for p in cands)}")
    return cands[0]


def copy_trimmed(src: Path, dst: Path) -> None:
    """按白名单把 src 裁进 dst（dst 会被清空重建）。"""
    if dst.exists():
        shutil.rmtree(dst)
    # models/ 是 assets/hand/models/MANO_*.pkl（7.3M），HandNumpy 只读 .npz，
    # 拷贝时就排除，省得先拷后删
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "models")
    for rel in KEEP_DIRS:
        s = src / rel
        if not s.is_dir():
            sys.exit(f"[错误] 源工具包缺少必需目录: {rel}")
        shutil.copytree(s, dst / rel, ignore=ignore)
    for name in KEEP_FILES:
        s = src / name
        if s.is_file():
            shutil.copy2(s, dst / name)
    # 标定：只留随包默认标定。带时间戳的标定属于开发机上那只手套，
    # 一旦被带到客户机，core/glove_keypoint_solver.py 的 pick_calibration
    # 会按 generated_at 优先选中它 —— 骨架会解成别人的手。
    cal_src, cal_dst = src / "calibration", dst / "calibration"
    cal_dst.mkdir(parents=True, exist_ok=True)
    kept = []
    for f in sorted(cal_src.glob("*_default.json")):
        shutil.copy2(f, cal_dst / f.name)
        kept.append(f.name)
    if not kept:
        sys.exit("[错误] 源工具包 calibration/ 下没有 *_default.json")
    print(f"  calibration/: 保留 {len(kept)} 个默认标定 → {', '.join(kept)}")


def _python() -> str:
    """校验用的解释器：优先本仓库 venv（依赖齐全）。"""
    for rel in ("venv/bin/python", "venv/Scripts/python.exe"):
        p = ROOT / rel
        if p.exists():
            return str(p)
    return sys.executable


VERIFY_SNIPPET = r'''
import os, sys, time
import numpy as np
TK = sys.argv[1]
sys.path.insert(0, TK)
from glove_sdk.interfaces.solver import HandSolver
from glove_sdk.types import RawImuFrame

for side in ("left", "right"):
    calib = os.path.join(TK, "calibration", f"imu_calibration_{side}_default.json")
    geom = os.path.join(TK, "assets", "hand_geometry", "hand_measured_runtime_v1.json")
    s = HandSolver(side, calib, geom)
    q = np.zeros((16, 4)); q[:, 3] = 1.0        # 16×单位四元数（静止姿）
    warm = None
    for i in range(1, 601):
        kf = s.process(RawImuFrame(
            sequence=i, device_timestamp_us=i * 10000,
            host_timestamp_us=time.time_ns() // 1000,
            quaternions_xyzw=q,
            present_mask=np.ones(16, bool), valid_mask=np.ones(16, bool)))
        if warm is None and getattr(getattr(kf, "status", None),
                                    "details", {}).get("warmup_completed"):
            warm = i
        if warm and i == warm + 3:
            j = np.asarray(kf.joints_m, np.float64)
            assert j.shape == (21, 3), f"{side}: 关键点形状 {j.shape} != (21,3)"
            assert np.isfinite(j).all(), f"{side}: 关键点含 NaN/Inf"
            span = j.max(0) - j.min(0)
            assert 0.02 < float(span.max()) < 0.5, f"{side}: 手尺寸异常 {span}"
            print(f"  {side}: warmup {warm} 帧, 关键点 (21,3), "
                  f"包围盒 {np.round(span, 3)} m")
            break
    else:
        raise SystemExit(f"{side}: 600 帧内未完成 warmup")
'''


def verify(toolkit: Path) -> bool:
    """在裁剪产物上实跑解算链（左右手各一遍）。返回是否通过。"""
    py = _python()
    print(f"  校验解释器: {py}")
    # 校验会 import 整条链，默认会往产物里写 __pycache__/*.pyc —— 那是构建垃圾，
    # 别让它进交付物（客户机 Python 小版本不同还会被判为陈旧而重编）
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([py, "-c", VERIFY_SNIPPET, str(toolkit)],
                          capture_output=True, text=True, env=env)
    ok = proc.returncode == 0
    for line in (proc.stdout or "").splitlines():
        if line.strip():
            print(f"  {line}" if line.startswith("  ") else f"  {line}")
    if not ok:
        print("  [失败] 裁剪产物跑不了解算链，stderr 末 20 行:")
        for line in (proc.stderr or "").strip().splitlines()[-20:]:
            print(f"    {line}")
    return ok


def make_zip(toolkit: Path, zip_path: Path) -> None:
    """打包成 zip（含工具包目录名一层，解压即落在项目根）。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(toolkit.rglob("*")):
            # 防御性过滤：任何路径下都不收字节码缓存
            if not p.is_file() or p.suffix in (".pyc", ".pyo"):
                continue
            if "__pycache__" in p.parts:
                continue
            z.write(p, Path(toolkit.name) / p.relative_to(toolkit))
            n += 1
    print(f"  已打包: {zip_path}  ({n} 个文件, "
          f"{zip_path.stat().st_size / 1e6:.1f} MB)")


def dir_size(p: Path) -> tuple[int, int]:
    files = [f for f in p.rglob("*") if f.is_file()]
    return len(files), sum(f.stat().st_size for f in files)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="裁剪手套工具包供随包分发（普通版一键部署自带骨架解算）")
    ap.add_argument("--src", type=Path, help="源工具包目录（默认扫项目根）")
    ap.add_argument("--out", type=Path, default=ROOT / "wheels",
                    help="输出目录（默认 wheels/，与离线包同处便于一起拷给客户）")
    ap.add_argument("--keep", type=Path, metavar="DIR",
                    help="同时留一份展开的目录（可直接拷到项目根）")
    ap.add_argument("--no-verify", action="store_true", help="跳过实跑校验")
    ap.add_argument("--no-zip", action="store_true", help="只产出展开目录，不打包")
    args = ap.parse_args()

    src = find_toolkit_dir(args.src)
    n0, s0 = dir_size(src)
    print(f"源工具包: {src.name}  ({n0} 个文件, {s0 / 1e6:.0f} MB)")

    with tempfile.TemporaryDirectory(prefix="pack_toolkit_") as tmp:
        staged = Path(tmp) / src.name
        print("按白名单裁剪 ...")
        copy_trimmed(src, staged)
        n1, s1 = dir_size(staged)
        print(f"裁剪后: {n1} 个文件, {s1 / 1e6:.1f} MB "
              f"(省 {(1 - s1 / max(s0, 1)) * 100:.1f}%)")
        print("未随包的部分及原因:")
        for name, why in DROP_NOTE.items():
            print(f"  - {name}: {why}")

        if not args.no_verify:
            print("实跑解算链校验 ...")
            if not verify(staged):
                sys.exit("[错误] 校验未通过 —— 白名单漏了东西，请补 KEEP_DIRS/KEEP_FILES")

        if args.keep:
            dst = args.keep / src.name
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(staged, dst)
            print(f"  展开副本: {dst}")

        if not args.no_zip:
            make_zip(staged, args.out / "toolkit" / "glove_toolkit.zip")

    print()
    print("完成。随包分发: 把整个 wheels/ 拷到客户机项目根目录，"
          "start.bat / start.sh 会自动把工具包展开到项目根。")
    print("客户机若已有 stouch_glove_toolkit*/ 目录则原样不动。")


if __name__ == "__main__":
    main()
