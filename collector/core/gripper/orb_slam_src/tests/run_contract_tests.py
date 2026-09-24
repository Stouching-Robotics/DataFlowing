#!/usr/bin/env python3
"""跑本目录的 12 个 Python 契约测试（只读 ORB/桥接源码，不需要硬件）。

为什么需要这个加载器，而不是 `python test_x.py`：这 12 份里有 4 份
（test_connect_debug、test_fays_sdk_shutdown、test_orb_stereo_baseline、
test_slam_offline_evaluation）**没有 `unittest.main()` 入口** —— 直接执行等于
只 import 一遍就退出，**rc=0、零输出**，看起来全绿。本加载器按模块加载，
没有入口的照样跑，import 期就 SkipTest 的（纯 clone 上缺上位机 runtime）
如实报 SKIP 与原因。

用法：
    python tests/run_contract_tests.py            # 从本树任意位置
    python core/gripper/orb_slam_src/tests/run_contract_tests.py

退出码：0 = 全部加载成功（含已知的 2 个 RED —— 陈旧契约，见 README）；
        1 = 有模块 import 失败（那才是环境问题）。
"""
import importlib.util
import io
import sys
import unittest
from pathlib import Path

TESTS = [
    "test_codec_benchmark",
    "test_connect_debug",
    "test_fays_calibration_container",
    "test_fays_factory_calibration_contract",
    "test_fays_historical_orb_contract",
    "test_fays_input_trace",
    "test_fays_sdk_shutdown",
    "test_orb_frame_lastkf_contract",
    "test_orb_mp_cleanup_contract",
    "test_orb_preintegration_guard_contract",
    "test_orb_stereo_baseline",
    "test_slam_offline_evaluation",
]


def verdict(path: Path) -> tuple[str, bool]:
    """→ (一行结论, 是否 import 失败)"""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)       # import 期的 SkipTest 在这里抛
    except unittest.SkipTest as exc:
        return f"SKIP  {exc}", False
    except Exception as exc:                  # noqa: BLE001
        return f"IMPORT-ERROR  {type(exc).__name__}: {exc}", True
    suite = unittest.TestLoader().loadTestsFromModule(module)
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    total = suite.countTestCases()
    if result.wasSuccessful():
        skipped = len(result.skipped)
        tail = f"（skip {skipped}）" if skipped else ""
        return f"OK    {total - skipped}/{total}{tail}", False
    names = " ".join(t.id().split(".")[-1]
                     for t, _ in result.failures + result.errors)
    return f"RED   {len(result.failures)}F+{len(result.errors)}E  {names}", False


def main() -> int:
    here = Path(__file__).resolve().parent
    broken = False
    red = 0
    for stem in TESTS:
        text, failed = verdict(here / f"{stem}.py")
        broken = broken or failed
        red += text.startswith("RED")
        print(f"  {stem:44s} {text}")
    # 红模块数**数出来**，不写字面量：加一个模块就手改一次数字，早晚改成假的
    print(f"\n  共 {len(TESTS)} 个模块；RED 的 {red} 个是**本来就红**的陈旧契约"
          f"（见 README「Python 契约测试」）。")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
