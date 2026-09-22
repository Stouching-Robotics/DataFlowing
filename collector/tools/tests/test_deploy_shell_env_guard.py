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
  [1] .bat 编码、行尾、百分号转义与跳转标签（改错了 cmd 会乱码 / 静默不生效）
  [2] 静默升级 pip 已绝迹（它是「半装 pip」的唯一来源）
  [3] venv 体检 + 离线自愈（ensurepip）+ 重建兜底，且体检在装依赖之前
  [4] 环境隔离（PYTHONHOME / PYTHONPATH / 用户级 site-packages / conda 提示）
  [5] 入口守卫：在 PyQt5 之前、规格与分发壳一致
  [6] 守卫行为：缺依赖时退出码 2、开关能跳过
  [7] 文档同步（使用手册.md 两个语种都写了 pip._internal.cli 这条）
  [8] lite_package/ 里的副本没掉队（存在才查）
  [9] run.bat / run.sh 未部署时给得出方向（不教用户手工建 venv）
  [10] Python 3.10 门（手套 SDK 的加密链按 3.10 ABI 编译，**不是"及以上"**）
  [11] [4/7] / [4/6] 手套 SDK 段（探针走装配层 / type 打原文 / 陈旧戳自愈）
  [12] requirements 的 PEP 263 声明（cp936 的 pip 23.0.1 会栽在这）

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
    print("[1] .bat 编码、行尾、百分号转义与跳转标签（GBK + CRLF）")
    bats = BAT_SHELLS + (["lite_package/start_lite.bat"]
                         if os.path.exists(
                             os.path.join(_BASE, "lite_package/start_lite.bat"))
                         else []) + (["run.bat"]
                                     if os.path.exists(
                                         os.path.join(_BASE, "run.bat"))
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

    # %%VAR%% 只在 for 循环里才是「循环变量」；循环外是两个转义百分号，
    # 比较的是字面量 "%VAR%" —— 语句能跑、不报错、但**永远为假**。
    # 这里踩过一次: PY 清空那句写成了 %%PY%%，reinstall 的修复于是静默失效。
    for name in bats:
        lines = read(name).splitlines()
        bad = [f"{i}:{ln.strip()}" for i, ln in enumerate(lines, 1)
               if re.search(r"%%\w+%%", ln) and not ln.strip().lower().startswith("for")]
        check(f"{name} 无循环外的 %%VAR%%", not bad,
              f"永远为假的条件: {bad[:1]}")

        # goto/call 指向不存在的标签时 cmd 会**直接终止脚本**，什么都不说。
        # 这里没有 Windows 可跑，静态解析是唯一的网。
        labels = {ln.strip()[1:].split()[0].lower()
                  for ln in lines if re.match(r"^\s*:[A-Za-z_]", ln)}
        dangling = [f"{i}:{m.group(1)}" for i, ln in enumerate(lines, 1)
                    if not ln.strip().lower().startswith("rem")
                    for m in re.finditer(r"(?:goto|call)\s+:(\w+)", ln, re.I)
                    if m.group(1).lower() not in labels]
        check(f"{name} 跳转标签都存在", not dangling, f"悬空: {dangling[:2]}")

        # **同名标签**：cmd 的 goto 跳到**第一个**同名标签，后面那些是死代码。
        # 比悬空标签更毒 —— 脚本照跑、不报错，只是跳去了旧那段。这里踩过
        # 一次：删旧的手套工具包段时只删了一半，:toolkit_missing 剩两份，
        # goto 落在旧那份上，而旧那份的条件恰好把流程送回自己 ⇒ 死循环。
        seen, dups = {}, []
        for i, ln in enumerate(lines, 1):
            m = re.match(r"^\s*:([A-Za-z_]\w*)", ln)
            if not m:
                continue
            key = m.group(1).lower()
            if key in seen:
                dups.append(f"{key}({seen[key]},{i})")
            else:
                seen[key] = i
        check(f"{name} 无同名标签", not dups,
              f"goto 只会跳到第一个，其余是死代码/死循环: {dups[:3]}")


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

        # ensurepip 只在「没装 pip」时才装: 半装 pip 的 dist-info 还在、版本又恰好等于
        # Python 自带的那只 wheel → 它判「已满足」直接跳过，根本修不动（2026-09-21 在
        # Wine 的真 cmd 里实测到：修完 pip --version 照旧报错）。所以每一处 ensurepip
        # 的前一行必须是清残缺 pip 的那步 —— 这不是风格，是「修不修得动」的分界。
        purge = "call :purge_pip" if name.endswith(".bat") else "purge_pip"
        lines_ = [ln.strip() for ln in text.splitlines()]
        naked = []
        for i, ln in enumerate(lines_):
            if "ensurepip --upgrade" not in ln:
                continue
            before = [x for x in lines_[:i] if x]
            if not before or before[-1] != purge:
                naked.append(f"{i + 1}行前是 {before[-1][:28]!r}")
        check(f"{name} ensurepip 前先清残缺 pip", not naked,
              f"没清就直接 ensurepip（判「已满足」修不动）: {naked[:1]}")

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

        # 解释器**就是**项目 venv、只是依赖没装完时，文案必须换一套 ——
        # 否则会告诉用户「你的 Python 不对、换个方式启动」，而他换哪种方式
        # 都是在用同一只 venv，问题一点没解决（客户就是这么被绕晕的）。
        orig = startup_guard.in_project_venv
        startup_guard.in_project_venv = lambda *a, **k: True
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                try:
                    startup_guard.enforce(_BASE, "__test__")
                except SystemExit:
                    pass
            out = buf.getvalue()
            check("venv 内缺依赖 → 换文案", "启动环境不完整" in out,
                  "还在说「不是本项目的运行环境」")
            check("venv 内缺依赖 → 不误导", "不是本项目的运行环境" not in out,
                  "两套文案混在一起了")
        finally:
            startup_guard.in_project_venv = orig

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


def test_run_launchers() -> None:
    """run.bat / run.sh 是手册里写明的「日常启动」方式，也必须给得出方向。

    它们跳过全部部署检查（这是它们的卖点），但「压根没部署」这一种情况得
    拦住: 否则 cmd 只甩一句「系统找不到指定的路径。」。**尤其不能教用户
    手工 python -m venv** —— 那条路绕过离线 wheels/ 与依赖签名，装完照样
    缺包，客户就会再来一轮「装了还是报错」。
    """

    print("[9] run 启动器（缺 venv 时指向 start 脚本）")
    for name, launcher in (("run.bat", "start.bat"), ("run.sh", "./start.sh")):
        if not os.path.exists(os.path.join(_BASE, name)):
            continue
        text = code_lines(read(name), name)
        check(f"{name} 未部署时指向 {launcher}", launcher in text,
              f"没告诉用户去跑 {launcher}")
        # 反模式是**让用户照着抄**那句手工部署命令（建 venv + 装 requirements）:
        # 绕过离线 wheels/、依赖签名与工具包展开，装完照样缺包。
        # 「不要手工建 venv」这类**劝阻**文案不算 —— 所以看的是组合，不是词。
        hand_rolled = re.search(r"python -m venv\s+venv", text) and \
            re.search(r"pip install\s+-r\s+requirements", text)
        check(f"{name} 不教手工建 venv", not hand_rolled,
              "让用户手工建 venv 再装 requirements（会绕过 wheels/ 与依赖签名）")
        check(f"{name} 提示会停住窗口", "pause" in text if name.endswith(".bat")
              else True, "双击时窗口一闪而过，看不到提示")


def test_lite_package_sync() -> None:
    """lite_package/ 里的分发壳副本必须与仓库根的逐字节一致。

    这两份是 pack_lite.py 的**回落源**（仓库根优先，找不到才用它们），平时
    没人跑、也没人看 —— 上一次性命攸关的修复（reinstall 用已删除的解释器建
    venv）就只落在了仓库根，副本悄悄落后了一整轮。存在就逐字节比对。
    """

    print("[8] lite_package/ 副本没掉队")
    for name in ("start_lite.sh", "start_lite.bat"):
        dst = os.path.join(_BASE, "lite_package", name)
        if not os.path.exists(dst):
            continue
        with open(os.path.join(_BASE, name), "rb") as fh:
            root = fh.read()
        with open(dst, "rb") as fh:
            copy = fh.read()
        check(f"lite_package/{name} 与根一致", root == copy,
              "副本落后了（pack_lite.py 的回落源，会把老 bug 一起发出去）")


def test_python310_gate() -> None:
    """四个分发壳的解释器门必须是**恰好 3.10**，不是「3.10 及以上」。

    手套 SDK 的 `algorithm/` 是 PyArmor 按 3.10 ABI 加密的
    （`pyarmor_runtime.so` 引用了 3.11 起移除的 `_PyFloat_Pack8`），而
    `sdk.api` **顶层**就 import 解算链 ⇒ 3.12 下不是「少个骨架」，是
    **整个 SDK 不可用**。三个后果各一条断言：

      · 判据写成 `>= (3, 10)` → 3.12 的机器被放行 → 静默坏
      · 候选顺序把 3.12 排前面 → 有 3.10 的机器反而挑中 3.12
      · 已存在的 venv 不校验版本 → 老客户机（3.12 venv）升级后直接坏
    """

    print("[10] Python 3.10 门（SDK 加密链的 ABI 要求）")
    for name in shell_paths():
        text = read(name)
        code = code_lines(text, name)
        # 判据：两处 `sys.version_info[:2] == (3, 10)` 字面量 —— 定位解释器与
        # venv 体检各一处（`.bat` 的定位走 :try_py，所以也是两处）。
        exact = len(re.findall(r"version_info\[:2\]\s*==\s*\(3,\s*10\)", code))
        loose = re.findall(r"version_info\[:2\]\s*>=?\s*\(3,\s*10\)", code)
        check(f"{name} 版本判据是「恰好 3.10」",
              exact >= 2 and not loose,
              f"恰好判据 {exact} 处（要 ≥2: 定位 + 体检），"
              f"宽松判据 {len(loose)} 处（≥(3,10) 会放行 3.12）")
        # 光有判据不够：判据必须接到「自动重建」那条路上 —— 老客户机上是
        # 3.12 的 venv，只报错不重建 = 用户升个级就永久失去手套功能。
        check(f"{name} 版本不符时自动重建",
              re.search(r"不是 Python 3\.10|不是 3\.10", text) is not None
              and re.search(r"reinstall|重建", code) is not None,
              "体检发现版本不符但没有重建分支")
        # 注释与用户提示里出现 3.12 是**对的**（要告诉用户 3.12 不行）；
        # 这里只钉「会真去装/真去找的那个版本号」。
        if name.endswith(".bat"):
            check(f"{name} 装的 Python 是 3.10.11",
                  'set "PY_VER=3.10.11"' in code
                  and not re.search(r"PY_VER=3\.(1[12]|[0-9])\b(?!\.)", code,
                                    re.I)
                  and "%PY_VER%" in code,
                  "PY_VER 还指着别的版本（老客户机会被静默装上 3.12）")
            check(f"{name} 先试 py -3.10",
                  re.search(r"call :try_py py -3\.10", code) is not None,
                  "候选顺序里没有 py -3.10 打头")
        else:
            m = re.search(r"for c in ([^;]+); do", code)
            check(f"{name} 候选顺序 3.10 打头",
                  bool(m) and m.group(1).split()[0] == "python3.10"
                  and not re.search(r"python3\.1[12]", m.group(1)),
                  f"候选 = {m.group(1).strip() if m else '<找不到>'}")


def test_glove_sdk_section() -> None:
    """[4/7] / [4/6] 手套 SDK 段的契约（本次部署最容易翻车处）。

    这一段有三个**静默**失败形态，全都不会让脚本报错：

      · 探针手写 `sys.path` + import（而不是走 `core.glove_sdk_boot`）→
        探针过了，主程序照样连不上（装配层还有 config 撞名等自检）
      · 错误文本用 `for /f` 读 → cmd 按控制台代码页转一次，中文全变 `?`
      · 陈旧戳（zip 在、目录被删）→ 永远不解压，还叫用户去开发机重打包
    """

    print("[11] 手套 SDK 段（[4/7] / [4/6]）")
    for name in shell_paths():
        text = read(name)
        code = code_lines(text, name)
        is_lite = "lite" in name
        step = "[4/6]" if is_lite else "[4/7]"
        # 旧工具包的名字一个都不该剩（注释里也不该，那会误导排障）
        check(f"{name} 无旧工具包残留",
              "stouch_glove_toolkit" not in text and "glove_toolkit.zip" not in text,
              "还印着旧工具包的名字")
        check(f"{name} 有 {step} 段", step in code, f"没有 {step} 段")
        check(f"{name} 探针走装配层",
              "core.glove_sdk_boot" in code and "core\\glove_sdk_boot" in code
              or "core.glove_sdk_boot" in code,
              "探针没走 core.glove_sdk_boot（手写 sys.path 能过而主程序仍失败）")
        # 极简版不能查解算链：它的载荷本来就没有 algorithm/，查了会把
        # 「极简版一切正常」误判成「SDK 不可用」
        check(f"{name} --no-solver 用法正确",
              ("--no-solver" in code) == is_lite,
              "极简版漏了 --no-solver（会误报 SDK 不可用）"
              if is_lite else "普通版带了 --no-solver（解算链就不查了）")
        # 陈旧戳自愈：zip 换了 **或** 目录不在，都要重新解压
        check(f"{name} 陈旧戳会重新解压",
              "TK_NEED" in code and re.search(r"TK_NEED=1", code)
              and len(re.findall(r"TK_NEED=1", code)) >= 2,
              "只按戳判断（戳说已展开就跳过），目录被删后永不自愈")
        # 只在真解出目录时才落戳，否则下次启动会自动重试
        check(f"{name} 解压成功才落戳",
              re.search(r"if not defined TOOLKIT goto|\[ -z \"\$TOOLKIT\" \]|\n\s*\[ -n \"\$TOOLKIT\" \]",
                        code) is not None,
              "无条件落戳（解压失败也会写「已完成」）")

    # .bat 的错误文本必须用 type 原样打：cmd 的 for /f 会按控制台代码页
    # 转换内容，中文全变成 ?（Wine 实测，chcp 936 也一样）。
    for name in ("start.bat", "start_lite.bat"):
        if not os.path.exists(os.path.join(_BASE, name)):
            continue
        code = code_lines(read(name), name)
        check(f"{name} 用 type 打错误原文",
              re.search(r'type\s+"%TK_ERR%"', code) is not None,
              "没用 type 原样输出（for /f 会把中文转成 ?）")
        check(f"{name} 不用 for /f 读错误文件",
              not re.search(r'for\s+/f.*TK_ERR', code, re.I),
              "for /f 读错误文件会按控制台代码页转换内容")


def test_requirements_encoding() -> None:
    """三份 requirements 必须带 PEP 263 编码声明（第 1 或第 2 行）。

    这是 2026-09-21 真机上炸过的一条：Python 3.10 的 ensurepip 自带
    pip **23.0.1**，它的 `auto_decode` 在没有编码声明时回落到
    `locale.getpreferredencoding(False)`；中文 Windows 上那是 **cp936**，
    于是一读到 UTF-8 的中文注释就 `UnicodeDecodeError: 'gbk' codec ...`
    —— 部署卡在 [3/7]，报的还是 pip 的内部错。3.12 自带 pip 25.0.1
    （默认 UTF-8）所以从没暴露过；这是**迁 3.10 引入的回归**。
    """

    print("[12] requirements 的编码声明（中文 Windows 的 pip 23.0.1）")
    paths = ["requirements.txt", "requirements-lite.txt"]
    if os.path.exists(os.path.join(_BASE, "lite_package/requirements-lite.txt")):
        paths.append("lite_package/requirements-lite.txt")
    for name in paths:
        with open(os.path.join(_BASE, name), "rb") as fh:
            raw = fh.read()
        head = raw.split(b"\n")[:2]
        cookie = any(re.match(rb"^#.*coding[:=]\s*utf-8", ln, re.I) for ln in head)
        check(f"{name} 有 PEP 263 声明", cookie,
              "缺编码声明 → cp936 的 pip 23.0.1 读中文注释即 UnicodeDecodeError")
        # 声明存在还不够：pip ≤23.x 用 auto_decode 时**先看声明**，所以
        # 声明与内容必须真的一致，否则只是把报错挪了个位置
        try:
            raw.decode("utf-8")
            check(f"{name} 是 UTF-8", True)
        except UnicodeDecodeError as exc:
            check(f"{name} 是 UTF-8", False, f"解不开: {exc}")


def main() -> int:
    print(f"项目根: {_BASE}\n")
    test_bat_encoding()
    test_no_silent_pip_upgrade()
    test_venv_health_and_heal()
    test_env_isolation()
    test_entry_guard()
    test_guard_behaviour()
    test_docs()
    test_lite_package_sync()
    test_run_launchers()
    test_python310_gate()
    test_glove_sdk_section()
    test_requirements_encoding()

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
