"""启动环境守卫 —— 拦住「用错 Python 解释器」这一类启动失败。

客户现场最常见的两类「一启动就报错」都不是程序问题:

  ① 没激活 venv —— 直接 `python main.py`，或双击 .py 文件（Windows 用
     系统 Python 打开）；
  ② 已经激活了别的环境 —— conda / 另一个项目的 venv，依赖装到了别处。

这两种情况的真实报错是 ``ModuleNotFoundError: No module named 'PyQt5'``
之类，用户既看不懂也不知道该做什么。本模块在**任何重依赖 import 之前**
做一次体检: 缺关键依赖就打印可照抄的启动命令并以退出码 2 退出，而不是
让它崩在后面某个 import 上。

判定只看「关键依赖是不是真的缺」，所以:
  * 开发机用自己的 Python 环境跑（依赖齐全）→ 只多一行提示，不拦截；
  * 真正坏掉的环境（空壳 venv、从别的机器拷来的 venv、依赖装错地方）
    → 一定拦得住。

设 ``DAQ_SKIP_VENV_GUARD=1`` 可完全跳过 —— 工具脚本 / 单元测试需要
import 入口模块时不希望被拦。

本模块必须保持「零第三方依赖」（只用标准库），否则它就自身难保了。
"""

from __future__ import annotations

import os
import sys

# 每个入口一份规格: 自带 venv 目录名、关键依赖、入口文件名、分发脚本名。
# 关键依赖清单只放「本入口顶层就要用、且客户环境不会恰好自带」的包。
SPECS = {
    "main": {
        "venv": "venv",
        "modules": ("PyQt5.QtWidgets", "qt_material"),
        "entry": "main.py",
        "launcher_win": "start.bat",
        "launcher_unix": "./start.sh",
    },
    "lite": {
        "venv": "venv_lite",
        "modules": ("PyQt5.QtWidgets", "pyarrow"),
        "entry": "main_lite.py",
        "launcher_win": "start_lite.bat",
        "launcher_unix": "./start_lite.sh",
    },
}

_ASCII_FALLBACK = """\
============================================================
 {headline_en}
============================================================
 interpreter: {exe}
 missing:     {missing}

 Launch with one of these instead:
   * Windows:  {launcher_win}
   * Linux:    {launcher_unix}
   * or run the bundled venv directly:
       Windows:  {venv}\\Scripts\\python.exe {entry}
       Linux:    {venv}/bin/python {entry}

 First run deploys dependencies (3-10 min): {launcher_win} / {launcher_unix}
 An activated conda/other venv is ignored by these launchers.
============================================================
"""


def in_project_venv(base_dir: str, venv_name: str) -> bool:
    """当前解释器是不是项目自带的那只 venv（仅用于提示，不用于拦截）。"""

    expected = os.path.normcase(
        os.path.realpath(os.path.join(base_dir, venv_name)))
    current = os.path.normcase(os.path.realpath(sys.prefix))
    return current == expected


def missing_modules(modules) -> list:
    """逐个试着 import，返回缺的那些（不装、不改 sys.path）。"""

    missing = []
    for name in modules:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    return missing


def _print(text: str, fallback: str) -> None:
    """打印；控制台编码装不下时（英文 Windows 的 cp437 等）退到 ASCII 版。"""

    try:
        print(text)
    except UnicodeEncodeError:
        print(fallback)


def enforce(base_dir: str, app: str) -> None:
    """体检 ``app``（"main" / "lite"）需要的运行环境。

    依赖齐全: 只在「用的不是项目 venv」时打一行提示，然后正常返回。
    依赖缺失: 打印启动指引并以退出码 2 结束进程。
    """

    if os.environ.get("DAQ_SKIP_VENV_GUARD") == "1":
        return

    spec = SPECS[app]
    venv_name = spec["venv"]
    missing = missing_modules(spec["modules"])
    in_venv = in_project_venv(base_dir, venv_name)

    if missing:
        # 两种「缺依赖」要分开讲，否则会把人指错方向:
        #   · 解释器就不是本项目 venv（没激活 / 激活了别人的）→ 换启动方式；
        #   · 解释器就是本项目 venv，只是依赖没装完（装到一半断网/关窗口）
        #     → 换启动方式没用，得让分发脚本把它补齐。报错文案必须说清这点。
        if in_venv:
            headline = "启动环境不完整: 项目自带 venv 里缺少依赖"
            headline_en = "INCOMPLETE ENVIRONMENT (deps missing in bundled venv)"
            why = f""" 解释器是对的（项目自带 {venv_name}），但里面缺依赖。
 多半是上一次依赖安装没装完（断网 / 关窗口 / 被杀软打断）。

 重新运行下面任一脚本即可补齐（会自动修复，约 1-10 分钟）:"""
        else:
            headline = "启动环境不对: 当前用的 Python 不是本项目的运行环境"
            headline_en = "WRONG PYTHON INTERPRETER"
            why = f""" 请改用下面任一方式启动（任选其一）:"""

        fallback = _ASCII_FALLBACK.format(
            headline_en=headline_en, exe=sys.executable,
            missing=", ".join(missing), launcher_win=spec["launcher_win"],
            launcher_unix=spec["launcher_unix"], venv=venv_name,
            entry=spec["entry"])
        indented = "\n".join(f" {line}" for line in fallback.splitlines())
        _print(f"""
============================================================
 {headline}
============================================================
 解释器: {sys.executable}
 缺少依赖: {", ".join(missing)}

{why}
   · Windows:  双击 {spec["launcher_win"]}
   · Linux:    {spec["launcher_unix"]}
   · 或用项目自带 venv 的解释器直接跑:
       Windows:  {venv_name}\\Scripts\\python.exe {spec["entry"]}
       Linux:    {venv_name}/bin/python {spec["entry"]}

 首次运行需要先部署依赖（约 3-10 分钟），上面两个脚本会自动完成。

 已经激活了别的虚拟环境（conda / 其它 venv）也没关系 ——
 本项目不使用它，上面两条命令不受影响，直接执行即可。
============================================================
""", indented)
        sys.exit(2)

    if not in_venv:
        print(f"[提示] 未使用项目自带 {venv_name}（当前: {sys.prefix}）"
              f" —— 仅开发调试时这样运行；交付/客户环境请用 "
              f"{spec['launcher_win']} / {spec['launcher_unix']}")
