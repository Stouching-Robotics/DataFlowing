#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SDK 的 Python 版本门（迁移计划 T1b）:

    venv/bin/python tools/tests/test_sdk_python_version.py

**必须用项目自己的 venv 跑** —— 被测对象就是「跑这个脚本的解释器」。

这是全仓唯一能提前发现「客户机的 Python 版本不对」的自动化闸门。它守的那
个坑也是最贵的一个：老客户机是 3.12 venv，而 `start.bat` 现状还会主动下载
安装 3.12 —— 不修则**所有客户机在升级后以 ImportError 的方式静默失去骨架
解算**（不是"骨架不能用"，是**整个 SDK 不可用**，因为 `sdk.api` 顶层就
import 解算链）。

失败长这样（实测 `venv_old312`：3.12.3 + scipy 齐全）：

    ImportError: .../pyarmor_runtime_000000/linux_x86_64/pyarmor_runtime.so:
                 undefined symbol: _PyFloat_Pack8

`_PyFloat_Pack8` 是 CPython **3.11 起移除**的私有 C API 符号 ⇒ 这份
`pyarmor_runtime.so` 是按 3.10 ABI 编的，换解释器没有别的出路。

第 4 条是**前提断言**：这条纪律的全部代价（3.10 于 2026-10 EOL，见计划
§风险 7）都源于那一个符号。厂商哪天重编出 3.12 的 runtime，这里会红 ——
那时「全栈迁 3.10」就可以整体回退，值得当场知道。

不需要真机。退出码 0 = 全部通过。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import core.glove_sdk_boot as boot                       # noqa: E402

FAILS = []
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def main():
    # ── 1. 解释器版本：唯一的硬门槛 ──
    check(sys.version_info[:2] == (3, 10),
          f"解释器是 3.10（实际 {sys.version.split()[0]}，{sys.executable}）")
    check(os.path.abspath(sys.executable).startswith(REPO_ROOT + os.sep),
          f"跑的是项目自己的解释器而不是系统 Python: {sys.executable}")

    # ── 2. 应用侧的门与这里口径一致 ──
    # 两边各写一份版本判据早晚会分叉（改了 venv 忘了改 check_python_version，
    # 启动脚本放行而主程序报错），所以钉住它们同进同出。
    check(boot.check_python_version() == "",
          f"glove_sdk_boot.check_python_version() 放行: "
          f"{boot.check_python_version()!r}")

    # 反向验证：把版本伪造成 3.12，门必须**关上**（否则上面那条恒真）
    real_sys = boot.sys

    class _Fake312:
        version_info = (3, 12, 3)
        version = "3.12.3 (main, fake)"

    boot.sys = _Fake312()
    try:
        msg = boot.check_python_version()
    finally:
        boot.sys = real_sys
    check(msg != "" and "3.10" in msg,
          f"反证：版本伪造成 3.12 时门确实关上（{msg[:34]}…）⇒ 上面不是恒真")

    # ── 3. 真实导入链：这才是「能不能用」的定义 ──
    # 只查版本不够 —— 载荷缺文件、被裁坏、依赖没装齐，都得在这里现形。
    err = boot.ensure_sdk()
    check(err == "", f"ensure_sdk() 通过: {err!r}")
    if not err:
        try:
            import sdk.api                                   # noqa: F401
            import sdk
            ver = getattr(sdk, "get_version", lambda: "")()
            check(True, f"import sdk.api 成功（SDK 版本 {ver or '未报告'}）")
        except Exception as exc:
            check(False, f"import sdk.api 失败: {type(exc).__name__}: {exc}")

    # ── 4. 前提：runtime 确实引用了 3.10 才有的私有符号 ──
    # 只扫原始字节：release 版的 .so 里这个串只出现在动态符号表
    # （实测出现 1 次）。用字节扫而不是 `nm` 是为了不依赖外部工具。
    so = os.path.join(boot.find_sdk_dir(), "pyarmor_runtime_000000",
                      "linux_x86_64", "pyarmor_runtime.so")
    if not os.path.exists(so):
        check(False, f"前提：找不到 {so} ⇒ SDK 载荷不完整")
    else:
        with open(so, "rb") as fh:
            hits = fh.read().count(b"_PyFloat_Pack8")
        check(hits >= 1,
              f"前提成立：pyarmor_runtime.so 引用了 3.11 起被移除的 "
              f"_PyFloat_Pack8（{hits} 处）⇒ 3.10 ABI 是硬约束、换解释器 "
              f"无出路。若这条变红，说明厂商重编了 runtime，"
              f"「全栈迁 3.10」可以整体回退")

    # ── 5. 部署自检入口（start.sh/bat 的 [4/7] 跑的就是它）──
    err = boot.self_check(with_solver=True)
    check(err == "", f"self_check(with_solver=True) 通过: {err!r}")

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print(f"PASS: SDK Python 版本门全部通过（{sys.version.split()[0]}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
