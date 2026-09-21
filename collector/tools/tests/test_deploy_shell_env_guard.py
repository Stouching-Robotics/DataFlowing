#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署脚本的**虚拟环境契约**：venv 坏了自己修，用错解释器直接拦住。

    python tools/tests/test_deploy_shell_env_guard.py

**为什么要有这份测试**：客户报的「部署失败」绝大多数不是网络问题，而是
venv 本身坏了或用错了环境（详见 使用手册.md 附录 B）。这些保护全写在四个
分发壳（start.bat / start_lite.bat / start.sh / start_lite.sh）和两个入口
（main.py / main_lite.py）里，而它们**没有任何单元测试覆盖** —— 谁顺手把
那句 `pip install --upgrade pip` 加回来、或者把入口守卫挪到 PyQt5 import
之后，都不会有测试变红。这份测试就是那道闸。

覆盖：
  [1] .bat 编码与行尾（GBK + CRLF —— 改错了 cmd 会乱码并静默失败）
  [2] 静默升级 pip 已绝迹（它是「半装 pip」的唯一来源）
  [3] venv 体检 + 离线自愈（ensurepip）+ 重建兜底，且体检在装依赖之前
  [4] 环境隔离（PYTHONHOME / PYTHONPATH / 用户级 site-packages / conda 提示）
  [5] 入口守卫：在 PyQt5 之前、规格与分发壳一致
  [6] 守卫行为：缺依赖时退出码 2、开关能跳过
  [7] 文档同步（使用手册.md 两个语种都写了 pip._internal.cli 这条）
  [8] lite_package/ 里的副本没掉队（存在才查）

退出码 0 = 全部通过。
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _BASE)

BAT_SHELLS = ["start.bat", "start_lite.bat"]
SH_SHELLS = ["start.sh", "start_lite.sh"]
ENTRIES = {"main.py": "main", "main_lite.py": "lite"}

_FAILS: list = []


def check(tag: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"   ok  {tag}")
    else:
        print(f"   FAIL {tag}: {detail}")
        _FAILS.append(f"{tag}: {detail}")


def read(path: str) -> str:
    """按各自编码读原文（newline='' 保留 CRLF，才能查行尾）。"""

    enc = "gbk" if path.endswith(".bat") else "utf-8"
    with open(os.path.join(_BASE, path), "r", encoding=enc, newline="") as fh:
        return fh.read()


def code_lines(text: str, shell: str) -> str:
    """去掉注释行 —— 注释里出现「曾经有过那句命令」是允许的（也是刻意的）。"""

    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if shell.endswith(".bat") and stripped.lower().startswith("rem"):
            continue
        if shell.endswith(".sh") and stripped.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def shell_paths() -> list:
    """四个分发壳，外加 lite_package/ 里的副本（存在才查）。"""

    paths = BAT_SHELLS + SH_SHELLS
    for extra in ("lite_package/start_lite.bat", "lite_package/start_lite.sh"):
        if os.path.exists(os.path.join(_BASE, extra)):
            paths.append(extra)
    return paths


def test_bat_encoding() -> None:
    print("[1] .bat 编码与行尾（GBK + CRLF）")
    bats = BAT_SHELLS + (["lite_package/start_lite.bat"]
                         if os.path.exists(
                             os.path.join(_BASE, "lite_package/start_lite.bat"))
                         else [])
    for name in bats:
        with open(os.path.join(_BASE, name), "rb") as fh:
            raw = fh.read()
        try:
            raw.decode("gbk")
            gbk_ok = True
        except UnicodeDecodeError:
            gbk_ok = False
        check(f"{name} 是 GBK", gbk_ok, "按 GBK 解不开（被存成 UTF-8 了？）")
        check(f"{name} 无 BOM", not raw.startswith(b"\xef\xbb\xbf"),
              "带 UTF-8 BOM，cmd 会报错")
        check(f"{name} 全 CRLF", b"\n" not in raw.replace(b"\r\n", b""),
              "有裸 LF 行尾")


def test_no_silent_pip_upgrade() -> None:
    print("[2] 静默升级 pip 已绝迹")
    for name in shell_paths():
        body = code_lines(read(name), name)
        hits = [ln.strip() for ln in body.splitlines()
                if re.search(r"pip\s+install\s+--upgrade\s+pip", ln)]
        check(f"{name} 无 `pip install --upgrade pip`", not hits,
              f"又在自动升级 pip（半装 pip 的来源）: {hits[:1]}")


def test_venv_health_and_heal() -> None:
    print("[3] venv 体检 + 离线自愈 + 重建兜底")
    for name in shell_paths():
        text = code_lines(read(name), name)
        lite = "lite" in name
        venv = "venv_lite" if lite else "venv"

        check(f"{name} 体检 pip", "-m pip --version" in text, "没有 pip 健康探测")
        check(f"{name} 离线自愈", "ensurepip --upgrade" in text,
              "没有 ensurepip 自救")
        rebuild = ("rmdir /s /q" if name.endswith(".bat") else "rm -rf")
        check(f"{name} 重建兜底", rebuild in text, f"没有 {rebuild} 重建路径")
        check(f"{name} 指向 {venv}", f'"{venv}"' in text or f" {venv}" in text,
              f"没提到 {venv}")

        # 体检必须发生在装依赖之前 —— 顺序反了就成了「用坏 pip 去装依赖」
        probe = text.find("-m pip --version")
        install = text.find(":install_deps") if name.endswith(".bat") else \
            text.find("install_req requirements")
        check(f"{name} 体检先于装依赖", -1 < probe < install,
              f"probe@{probe} install@{install}")


def test_env_isolation() -> None:
    print("[4] 环境隔离（外部环境串味）")
    for name in shell_paths():
        text = read(name)
        win = name.endswith(".bat")
        check(f"{name} 清 PYTHONHOME",
              ('set "PYTHONHOME="' in text) if win else ("unset PYTHONHOME" in text),
              "没有清 PYTHONHOME")
        check(f"{name} 清 PYTHONPATH",
              ('set "PYTHONPATH="' in text) if win else ("PYTHONPATH" in text),
              "没有清 PYTHONPATH")
        check(f"{name} 屏蔽用户级 site-packages", "PYTHONNOUSERSITE" in text,
              "没有 PYTHONNOUSERSITE=1")
        check(f"{name} 提示外部环境",
              "VIRTUAL_ENV" in text and "CONDA_PREFIX" in text,
              "没有 conda/venv 提示")


def test_entry_guard() -> None:
    print("[5] 入口守卫（在 PyQt5 之前 + 规格一致）")
    from core.startup_guard import SPECS

    for entry, app in ENTRIES.items():
        text = read(entry)
        guard_at = text.find("_enforce_env(")
        pyqt_at = text.find("from PyQt5")
        check(f"{entry} 调用守卫", guard_at != -1, "没有调用 startup_guard.enforce")
        check(f"{entry} 守卫在 PyQt5 之前", -1 < guard_at < pyqt_at,
              f"guard@{guard_at} pyqt@{pyqt_at}")
        check(f"{entry} 传入 app={app!r}", f'"{app}"' in text, "app key 不对")

    # 规格里的 venv 名与分发脚本名必须和分发壳实际使用的一致
    for app, spec in SPECS.items():
        for shell, key in (("start.bat" if app == "main" else "start_lite.bat",
                            "launcher_win"),
                           ("start.sh" if app == "main" else "start_lite.sh",
                            "launcher_unix")):
            check(f"SPECS[{app}][{key}] 与壳一致",
                  spec[key].lstrip("./") == shell, f"{spec[key]} != {shell}")
        shell_text = read("start.bat" if app == "main" else "start_lite.bat")
        vpy_marker = 'set "VPY=' + spec["venv"]
        check(f"SPECS[{app}][venv] 与壳一致", vpy_marker in shell_text,
              f"壳里的 VPY 不是 {spec['venv']}")


def test_guard_behaviour() -> None:
    print("[6] 守卫行为（缺依赖退出码 2 / 开关能跳过）")
    from core import startup_guard

    fake = {"venv": "venv", "modules": ("no_such_module_for_test_xyz",),
            "entry": "main.py", "launcher_win": "start.bat",
            "launcher_unix": "./start.sh"}
    startup_guard.SPECS["__test__"] = fake
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                startup_guard.enforce(_BASE, "__test__")
                code = None
            except SystemExit as exc:
                code = exc.code
        check("缺依赖 → 退出码 2", code == 2, f"实际 {code!r}")
        out = buf.getvalue()
        check("提示里给出启动命令", "start.bat" in out and "./start.sh" in out,
              "没有可照抄的命令")
        check("提示里点了缺什么", "no_such_module_for_test_xyz" in out, "没报缺哪个")

        os.environ["DAQ_SKIP_VENV_GUARD"] = "1"
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                startup_guard.enforce(_BASE, "__test__")
            check("开关能跳过", buf.getvalue() == "", "开关没生效")
        finally:
            del os.environ["DAQ_SKIP_VENV_GUARD"]
    finally:
        startup_guard.SPECS.pop("__test__", None)

    # 依赖齐全时不该拦（开发机用自己的环境跑）
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        startup_guard.SPECS["__test2__"] = dict(fake, modules=("os", "sys"))
        try:
            startup_guard.enforce(_BASE, "__test2__")
        finally:
            startup_guard.SPECS.pop("__test2__", None)
    check("依赖齐全不拦", "WRONG PYTHON" not in buf.getvalue(), "被误拦了")


def test_docs() -> None:
    print("[7] 文档同步")
    manual = read("使用手册.md")
    check("手册写了 pip 损坏这条", manual.count("pip._internal.cli") >= 2,
          f"只出现 {manual.count('pip._internal.cli')} 次（中英各一）")
    for name in (BAT_SHELLS + SH_SHELLS):
        check(f"{name} 错误 D 提到 pip 损坏",
              "pip._internal.cli" in read(name), "错误 D 没讲 pip 损坏这条")


def main() -> int:
    print(f"项目根: {_BASE}\n")
    test_bat_encoding()
    test_no_silent_pip_upgrade()
    test_venv_health_and_heal()
    test_env_isolation()
    test_entry_guard()
    test_guard_behaviour()
    test_docs()

    print()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} 项")
        for item in _FAILS:
            print("   -", item)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
