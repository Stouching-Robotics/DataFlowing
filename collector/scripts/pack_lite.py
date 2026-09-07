"""极简采集版发布包收集脚本 —— 把 lite 运行时闭包收集到一个目录。

只收集 start_lite.bat 真正需要的最小集合（约 1.5 MB 代码），
不拷贝 venv/venv_lite/data/wheels（--with-wheels 除外）、tools 下的
demo/其它测试、stouch_glove_toolkit-*/ 等与 lite 无关的内容。

代码文件以仓库根为源；start_lite.bat/.sh 与 使用说明_lite.md 是分发壳，
只住在 lite_package/（主目录不放），收集时从那里原样复制。

--force 只重建代码部分（main_lite/requirements-lite/config/core/ui/tools），
venv_lite/、wheels/、data/ 与分发壳原样保留 —— lite_package 可同时当
"发布模板"和"本机测试部署目录"用（在包内跑过 start_lite.sh 装的环境
不会被冲掉）。

收集完成后自动做导入自检：在目标目录内 `import main_lite`，
缺任何被闭包引用的模块都会当场报错（与 start_lite.bat 的错误 E 同口径）。

闭包清单与 tools/tests/lite_import_guard_test.py 的 WHITELIST 一致，
外加 egodata_writer/d435_camera 实际 import 的 core.calibration。

用法:
    python scripts/pack_lite.py                收集到 lite_package/
    python scripts/pack_lite.py --out D:/lite  指定目录
    python scripts/pack_lite.py --with-wheels  连同 wheels/ 离线依赖包一起
    python scripts/pack_lite.py --force        重建代码部分（保留环境）
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 分发壳的常驻目录（start_lite.bat/.sh、使用说明_lite.md 只住在这里）
DIST_SOURCE = os.path.join(ROOT, "lite_package")

# 代码文件（仓库根为源，收集时拷入包目录）
CODE_FILES = [
    "main_lite.py",
    "requirements-lite.txt",
]

# 分发壳文件（从 DIST_SOURCE 原样复制，主目录不放）
DIST_FILES = [
    "start_lite.bat",
    "start_lite.sh",
    "使用说明_lite.md",
]

# 整目录拷贝（core.glove_usb 是包，内部有 __init__.py 与互引模块）
WHOLE_DIRS = [
    "core/glove_usb",
]

# 单文件拷贝：目录 → 文件列表（只收 lite 闭包，主程序 UI/重依赖模块不带）
MODULE_FILES = {
    "ui": ["__init__.py", "lite_window.py"],
    "config": ["__init__.py", "settings.py", "i18n.py"],
    "core": [
        "__init__.py",
        "pipeline.py", "egodata_writer.py", "encoder_probe.py",
        "depth_codec.py", "d435_manager.py", "stereo_depth.py",
        "d435_camera.py", "camera.py", "device_detector.py",
        "ble_engine.py", "usb_glove_engine.py", "uploader.py",
        "api_client.py", "database.py", "helpers.py",
        # egodata_writer.py:41 / d435_camera.py:447 实际 import
        "calibration.py",
    ],
}

# 自检测试（客户机可选跑：无硬件验证导入隔离与采集链）
SELFTEST_FILES = [
    "tools/tests/lite_import_guard_test.py",
    "tools/tests/lite_smoke_test.py",
]


def collect(out_dir: str, with_wheels: bool, force: bool) -> None:
    out_dir = os.path.abspath(out_dir)
    if force:
        # 只重建代码路径；venv_lite/wheels/data 与分发壳原样保留
        for rel in (CODE_FILES + ["config", "core", "ui", "tools"]):
            p = os.path.join(out_dir, rel)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.isfile(p):
                os.remove(p)
    elif os.path.isfile(os.path.join(out_dir, "main_lite.py")):
        print(f"[跳过] 目标目录已存在: {out_dir}")
        print("  --force 只重建代码部分，venv_lite/wheels/data 会保留")
        sys.exit(0)

    copied = []
    for rel in CODE_FILES + SELFTEST_FILES:
        src = os.path.join(ROOT, rel)
        if not os.path.isfile(src):
            print(f"[警告] 缺文件: {rel}")
            continue
        dst = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)

    for rel in DIST_FILES:
        src = os.path.join(DIST_SOURCE, rel)
        dst = os.path.join(out_dir, rel)
        if os.path.abspath(src) == os.path.abspath(dst):
            copied.append(rel)   # 源即目标（lite_package 自身）
            continue
        if not os.path.isfile(src):
            print(f"[警告] 缺分发文件: {rel}（应在 lite_package/ 下）")
            continue
        shutil.copy2(src, dst)
        copied.append(rel)

    for rel in WHOLE_DIRS:
        src = os.path.join(ROOT, rel)
        dst = os.path.join(out_dir, rel)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        copied.append(rel + "/")

    for sub, files in MODULE_FILES.items():
        for name in files:
            rel = os.path.join(sub, name)
            src = os.path.join(ROOT, rel)
            if not os.path.isfile(src):
                print(f"[警告] 缺文件: {rel}")
                continue
            dst = os.path.join(out_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)

    if with_wheels:
        src = os.path.join(ROOT, "wheels")
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(out_dir, "wheels"))
            copied.append("wheels/")
        else:
            print("[提示] --with-wheels 但仓库没有 wheels/ 目录，跳过")

    # 自检：在目标目录内 import main_lite（与 start_lite.bat 错误 E 同口径）
    env = dict(os.environ, PYTHONPATH=out_dir,
               QT_QPA_PLATFORM=os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    proc = subprocess.run(
        [sys.executable, "-c", "import main_lite"],
        cwd=out_dir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        print("[失败] 目标目录内 import main_lite 自检未通过：")
        print(proc.stderr.decode("utf-8", "replace"))
        sys.exit(1)

    total = sum(os.path.getsize(os.path.join(out_dir, r))
                for r in copied if os.path.isfile(os.path.join(out_dir, r)))
    print(f"[完成] 收集 {len(copied)} 个文件到 {out_dir}/，"
          f"代码共 {total / 1024:.0f} KB，import main_lite 自检通过")
    print("  客户机落盘 = 本目录 + venv_lite(自动安装, ~750MB)"
          + (" + wheels/" if with_wheels else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="收集极简采集版发布包")
    parser.add_argument("--out", default=os.path.join(ROOT, "lite_package"),
                        help="输出目录（默认仓库根 lite_package/）")
    parser.add_argument("--with-wheels", action="store_true",
                        help="连同 wheels/ 离线依赖包一起收集")
    parser.add_argument("--force", action="store_true",
                        help="重建代码部分（保留 venv_lite/wheels/data）")
    args = parser.parse_args()
    collect(os.path.abspath(args.out), args.with_wheels, args.force)


if __name__ == "__main__":
    main()
