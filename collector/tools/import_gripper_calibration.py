"""接入新夹爪：把上位机生成的 per-serial 标定搬进 core/gripper/native/。

collector 不生成夹爪标定，只按现场探到的产品序列号拼文件名去找：

    core/gripper/native/gripper_version1/fays_config/fays_vikit_<serial>.yaml
    core/gripper/native/dist/fays_opencv48/s80m_<serial>_stereo_inertial.yaml

序列号没出现过的夹爪（没这两个文件）必须先在上位机跑 device_setup /
标定生成它们，再用本脚本搬过来并校验。序列号本身不用手填也能拿：
--detect 现场读一次（需先关闭主程序的夹爪，否则撞 SDK 初始化锁）。

用法:
    venv/bin/python tools/import_gripper_calibration.py --list
    venv/bin/python tools/import_gripper_calibration.py --detect
    venv/bin/python tools/import_gripper_calibration.py 3500000262300099
    venv/bin/python tools/import_gripper_calibration.py 3500000262300099 \
        --source /path/to/online [--force] [--dry-run]

--source 缺省为本仓库的 online/（上位机根目录）。也可以直接指向
上位机产物目录、或同时含两个 yaml 的任意目录。同名目标已存在且内容
不同时必须加 --force，旧文件先备份成 *.bak_<时间戳>。

注意：native/ 不入库（见 .gitignore），搬进来的标定只存在本机；
换机器/重装要重新搬一次。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.gripper import paths  # noqa: E402

_SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
_DEFAULT_SOURCE = os.path.join(_ROOT, "online")


def _target_paths(serial):
    """collector 实际读取的两个目标路径（与 paths.per_device_fays_yamls 同源）。"""
    return (
        os.path.join(paths.FAYS_CONFIG_DIR, f"fays_vikit_{serial}.yaml"),
        os.path.join(
            paths.ORB_DEVICE_CONFIG_DIR,
            f"s80m_{serial}_stereo_inertial.yaml",
        ),
    )


def _source_paths(source_root, serial):
    """在上位机根目录下按候选布局找源文件，返回 (sdk_yaml, orb_yaml) 或 None。"""
    layouts = (
        ("gripper_version1/fays_config", "dist/fays_opencv48"),
        ("fays_config", "fays_opencv48"),
        (".", "."),
    )
    for sdk_dir, orb_dir in layouts:
        sdk_yaml = os.path.join(
            source_root, sdk_dir, f"fays_vikit_{serial}.yaml")
        orb_yaml = os.path.join(
            source_root, orb_dir, f"s80m_{serial}_stereo_inertial.yaml")
        if os.path.isfile(sdk_yaml) and os.path.isfile(orb_yaml):
            return sdk_yaml, orb_yaml
    return None


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(path):
    with open(path, encoding="utf-8") as stream:
        return stream.read()


def _validate_sdk_yaml(text, path):
    """SDK 模板必须含两个端口字段的值——materialize_fays_device_config
    靠 `key: <值>` 渲染本次运行配置，字段缺值同样会在打开时炸。"""
    for key in ("stereo_dev_port", "imu_dev_port"):
        if re.search(
                r"^\s*" + key + r"\s*:\s*[^\s#]", text,
                re.MULTILINE) is None:
            raise RuntimeError(
                f"SDK YAML 缺少有效字段 {key}（需形如 {key}: /dev/videoN）: {path}")


def _validate_orb_yaml(text, path):
    """ORB 标定必须含 IMU.T_b_c1（ORB-SLAM3 的 body→cam1 外参）。"""
    if "IMU.T_b_c1" not in text:
        raise RuntimeError(f"ORB YAML 缺少 IMU.T_b_c1: {path}")
    if not text.lstrip().startswith("%YAML"):
        print(f"  [警告] ORB YAML 缺少 %YAML 头，请确认是 OpenCV FileStorage: {path}")


def _copy_verified(source, destination, *, force, dry_run):
    """原子拷贝 + sha256 复核；目标已存在时按内容决定跳过/备份/覆盖。"""
    same = (os.path.isfile(destination)
            and _sha256(source) == _sha256(destination))
    if same:
        print(f"  已是最新，跳过  {destination}")
        return "skipped"
    if os.path.isfile(destination) and not force:
        raise RuntimeError(
            f"目标已存在且内容不同，加 --force 覆盖（会先备份）: {destination}")
    if dry_run:
        print(f"  [dry-run] 将写入  {destination}")
        return "dry-run"
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    if os.path.isfile(destination):
        backup = "{}.bak_{}".format(
            destination, time.strftime("%Y%m%dT%H%M%S"))
        shutil.copy2(destination, backup)
        print(f"  旧文件已备份  {backup}")
    temporary = f"{destination}.tmp.{os.getpid()}"
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise RuntimeError(f"写入失败 {destination}: {exc}") from exc
    if _sha256(source) != _sha256(destination):
        raise RuntimeError(f"拷贝后校验不一致: {destination}")
    print(f"  已写入        {destination}")
    return "copied"


def _covered_serials():
    """native/ 里两个目录各自覆盖的序列号。"""
    sdk = {
        name[len("fays_vikit_"):-len(".yaml")]
        for name in os.listdir(paths.FAYS_CONFIG_DIR)
        if name.startswith("fays_vikit_") and name.endswith(".yaml")
    } if os.path.isdir(paths.FAYS_CONFIG_DIR) else set()
    orb = {
        name[len("s80m_"):-len("_stereo_inertial.yaml")]
        for name in os.listdir(paths.ORB_DEVICE_CONFIG_DIR)
        if name.startswith("s80m_") and name.endswith("_stereo_inertial.yaml")
    } if os.path.isdir(paths.ORB_DEVICE_CONFIG_DIR) else set()
    return sdk, orb


def _cmd_list():
    sdk, orb = _covered_serials()
    print("native/ 已覆盖的序列号（SDK 模板 / ORB 标定）:")
    for serial in sorted(sdk | orb):
        marks = ("有" if serial in sdk else "缺",
                 "有" if serial in orb else "缺")
        flag = "" if serial in sdk and serial in orb else "   ← 不完整，夹爪打不开"
        print(f"  {serial}  SDK={marks[0]}  ORB={marks[1]}{flag}")
    if not sdk and not orb:
        print("  （空）")
    print(f"\nSDK 目录: {paths.FAYS_CONFIG_DIR}")
    print(f"ORB 目录: {paths.ORB_DEVICE_CONFIG_DIR}")
    return 0


def _cmd_detect():
    """现场读一遍插着的 Fays 产品序列号（会调用官方 SDK 探针）。"""
    from core.gripper.fays_runtime import (
        build_fays_probe_env,
        discover_fays_device_groups,
    )
    from core.gripper.fays_serial_probe import probe_product_serial

    groups = discover_fays_device_groups()
    if not groups:
        print("未发现完整的 Fays S80M（检查 USB3 是否插好）")
        return 1
    sdk, orb = _covered_serials()
    print(f"发现 {len(groups)} 台完整 S80M，逐台读取产品序列号"
          "（主程序的夹爪请先关闭）:")
    failures = 0
    for group in groups:
        ports = group["ports"]
        try:
            serial = probe_product_serial(
                ports, environment=build_fays_probe_env())
        except Exception as exc:      # 探针失败不影响其他设备
            failures += 1
            print(f"  {group['physical_usb_path']}  {ports}  探测失败: {exc}")
            continue
        covered = "已覆盖" if serial in sdk and serial in orb else "未覆盖 → 需标定"
        print(f"  {group['physical_usb_path']}  {ports}  serial={serial}  {covered}")
    return 1 if failures and failures == len(groups) else 0


def _cmd_import(serial, source_root, *, force, dry_run):
    if not _SERIAL_PATTERN.match(serial):
        print(f"序列号格式不合法: {serial!r}")
        return 2
    found = _source_paths(source_root, serial)
    if found is None:
        print(f"在 {source_root} 找不到序列号 {serial} 的标定文件。")
        print("需要（两者之一存在即可）:")
        print(f"  <源>/gripper_version1/fays_config/fays_vikit_{serial}.yaml")
        print(f"  <源>/dist/fays_opencv48/s80m_{serial}_stereo_inertial.yaml")
        print("新夹爪请先在上位机运行 device_setup / 标定生成这两个文件；"
              "或用 --source 指向产物目录。")
        return 1

    sdk_source, orb_source = found
    sdk_target, orb_target = _target_paths(serial)
    print(f"序列号 {serial}")
    print(f"  源  SDK: {sdk_source}")
    print(f"  源  ORB: {orb_source}")
    _validate_sdk_yaml(_read_text(sdk_source), sdk_source)
    _validate_orb_yaml(_read_text(orb_source), orb_source)

    results = []
    for source, target in ((sdk_source, sdk_target), (orb_source, orb_target)):
        results.append(_copy_verified(
            source, target, force=force, dry_run=dry_run))

    if dry_run:
        print("\n[dry-run] 未写入任何文件")
        return 0

    # 用 collector 自己的解析函数复核：这一步过了，插上夹爪就能开。
    resolved_sdk, resolved_orb = paths.per_device_fays_yamls(serial)
    print("\ncollector 解析校验通过:")
    print(f"  {resolved_sdk}")
    print(f"  {resolved_orb}")
    if all(result == "skipped" for result in results):
        print("（两个文件本来就一致，无需改动）")
    print("提醒: native/ 不入库，这些标定只存在本机；换机器要重新搬一次。")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="把上位机生成的 Fays per-serial 标定搬进 core/gripper/native/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "serial", nargs="?", help="Fays 产品序列号（如 3500000262300099）")
    parser.add_argument(
        "--source", default=_DEFAULT_SOURCE,
        help=f"上位机根目录（缺省 {_DEFAULT_SOURCE}）")
    parser.add_argument(
        "--list", action="store_true", help="列出 native/ 已覆盖的序列号")
    parser.add_argument(
        "--detect", action="store_true",
        help="现场探测插着的 Fays 序列号（需关闭主程序夹爪）")
    parser.add_argument(
        "--force", action="store_true",
        help="目标已存在且内容不同时覆盖（旧文件备份成 *.bak_<时间戳>）")
    parser.add_argument(
        "--dry-run", action="store_true", help="只报告，不写文件")
    args = parser.parse_args(argv)

    if args.list:
        return _cmd_list()
    if args.detect:
        return _cmd_detect()
    if not args.serial:
        parser.error("需要序列号，或 --list / --detect")
    try:
        return _cmd_import(
            args.serial, os.path.abspath(args.source),
            force=args.force, dry_run=args.dry_run)
    except RuntimeError as exc:
        print(f"失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
