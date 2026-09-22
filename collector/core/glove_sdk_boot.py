#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数采手套厂商 SDK 装配层 —— 全进程唯一的 sys.path 注入点。

背景（2026-09-21 迁移）：主程序的手套链路从「fork 自旧工具包的
`core/glove_usb`」换成厂商 SDK v2.1.0（`tools/glove_sdk/`）。

SDK 是**顶层多包**布局（`sdk/ common/ glove_io/ gui/ algorithm/`），靠把
SDK 根目录插进 `sys.path` 才能 import —— 所以注入点必须全进程唯一，每个
模块各插一次早晚会插重、插乱顺序。所有 `import sdk.*` 都从这里走。

两条实测出来的纪律（都别再踩）：

1. **SDK 目录 append 到 sys.path 末尾，不要 insert 到 0。**
   SDK 根下有一个 `config/`，与仓库根的 `config/`（主程序自己的配置包）
   **同名**。实测（Python 3.10，`tools/tests/test_glove_sdk_boot.py`）两种
   顺序下解析结果其实都对：SDK 的 `config/` 没有 `__init__.py`，只是个
   namespace portion，而 namespace portion 在任何位置都会输给别处找到的
   **正规包**（仓库的 `config/__init__.py`）。但那是 CPython 的一条细节
   规则，不该拿它保命 —— append 让仓库根的条目天然优先，再靠 `_verify()`
   每次注入后复查一遍，把「静默错」变成「当场报错」。

2. **必须有 `_verify()`。** 名字撞车的失败形态是静默的：`import config`
   拿到 SDK 的空目录，主程序读不到设置项却不报错。所以注入后立刻确认
   `sdk` 落在 SDK 目录里、`config` 仍落在仓库根，不对就拒绝启用。

Python 版本：SDK 的 `algorithm/` 是 PyArmor 按 **3.10 ABI** 加密的
（`pyarmor_runtime.so` 引用了 3.11 起移除的 `_PyFloat_Pack8`），3.12 下
`import sdk.api` 直接 `ImportError`。`config/settings.py` 的
`GLOVE_PYTHON_REQUIRED` 记着这条，`check_python_version()` 给出人话报错。
"""
from __future__ import annotations

import os
import sys
import threading

#: SDK 目录名 —— 完整位置是 `<仓库根>/tools/glove_sdk/`（见 SDK_PARENT）。
#: 打包后由 `wheels/toolkit/glove_sdk.zip` 解压到这里（zip 里是裸的
#: `glove_sdk/`，由分发壳指定解压到 tools/）。
#:
#: **改位置要动五处**：本文件的 SDK_PARENT + 四个分发壳（start.sh /
#: start.bat / start_lite.sh / start_lite.bat）里的探测与解压目标。契约写在
#: `scripts/pack_toolkit.py` 的模块 docstring 里（含 zip 顶层形状）。
SDK_DIRNAME = "glove_sdk"

#: SDK 目录的父目录（相对仓库根）。**独立成一个常量**是因为它同时出现在
#: 报错原文里 —— 位置改了而报错还指旧路，是"照着报错去补装却补了个寂寞"。
SDK_PARENT = "tools"

#: 传输 + 触觉链必需的顶层包 —— `_verify()` 逐个确认它们解析到 SDK 目录里。
#: **不含 `algorithm`**：实测（2026-09-21）`import sdk` / `glove_io.streams` /
#: `gui.tactile_processing` / `common.types` 只拉 numpy，scipy+loguru+pydantic
#: 全是解算链（`sdk.solver` → `algorithm` → PyArmor）带进来的。极简版只录
#: 手套 IMU、不解算骨架，所以可以不带 algorithm 与那三个重依赖 —— 要求它
#: 反而会让裁剪过的 lite 载荷在 `ensure_sdk()` 就被判失败。
_SDK_PACKAGES = ("sdk", "common", "glove_io", "gui")

#: 解算链额外需要的包（`solver_parts()` 用）
_SOLVER_PACKAGES = ("algorithm",)

#: 仓库根（`core/` 的上一级）—— `config` 必须解析回这里
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_lock = threading.RLock()
_sdk_dir = None          # None = 还没找过；"" = 找过没有；其余 = 绝对路径
_ensure_error = None     # None = 还没试过；"" = 成功；其余 = 失败描述


# ── 定位 ──────────────────────────────────────────────

def sdk_relpath() -> str:
    """SDK 相对仓库根的路径（`tools/glove_sdk`）。

    报错原文、测试与文档共用这一份 —— 位置改了而报错还指旧路，就是
    "照着报错去补装，补了个寂寞"。分隔符按平台（Windows 上出 `tools\\glove_sdk`，
    那正是那边该看到的写法）。
    """
    return os.path.join(SDK_PARENT, SDK_DIRNAME)


def find_sdk_dir() -> str:
    """`tools/glove_sdk/` 目录的绝对路径；没有返回 ""。

    取代旧工具包时代的 `find_toolkit_dir()`（按 `stouch_glove_toolkit*`
    通配找）。命名与位置对齐 `core/device_detector.py` 的查找口径，两处
    必须指同一个目录。
    """
    global _sdk_dir
    if _sdk_dir is not None:
        return _sdk_dir
    candidate = os.path.join(_REPO_ROOT, SDK_PARENT, SDK_DIRNAME)
    _sdk_dir = candidate if os.path.isdir(candidate) else ""
    return _sdk_dir


# ── 注入 ──────────────────────────────────────────────

def _verify(sdk_dir: str, packages=_SDK_PACKAGES) -> str:
    """注入后复查解析结果；返回错误描述（"" = 正常）。

    这一步是**防静默错**的：SDK 根下有 `config/`，与仓库根的 `config/`
    同名，解析错了不会报错，只会让主程序读不到设置项。
    """
    import importlib.util
    for name in packages:
        spec = importlib.util.find_spec(name)
        origin = getattr(spec, "origin", None) or ""
        if not origin.startswith(sdk_dir + os.sep):
            return (f"{name} 解析到了 {origin or '<namespace>'}，"
                    f"不在 SDK 目录 {sdk_dir} 内")
    spec = importlib.util.find_spec("config")
    origin = getattr(spec, "origin", None) or ""
    if not origin.startswith(_REPO_ROOT + os.sep):
        return (f"config 被 SDK 盖掉了：解析到 {origin or '<namespace>'}，"
                f"应在本仓库 {_REPO_ROOT} 内")
    return ""


def ensure_sdk() -> str:
    """确保 SDK 已可导入；返回错误描述（"" = 成功）。幂等、线程安全。

    成功后再调用直接返回 ""，不重复走 sys.path 与 import。
    """
    global _ensure_error
    with _lock:
        if _ensure_error is not None:
            return _ensure_error
        sdk_dir = find_sdk_dir()
        if not sdk_dir:
            _ensure_error = f"未找到厂商 SDK 目录（{sdk_relpath()}/）"
            return _ensure_error
        if sdk_dir not in sys.path:
            sys.path.append(sdk_dir)      # 末尾，不是开头 —— 见模块 docstring
        try:
            err = _verify(sdk_dir)
        except Exception as exc:          # find_spec 自己炸（权限/坏文件）
            err = f"SDK 注入自检异常: {type(exc).__name__}: {exc}"
        _ensure_error = err
        return err


def check_python_version() -> str:
    """SDK 加密链要求 Python 3.10；不满足时返回人话描述（"" = 通过）。

    不做版本分支回退 —— `pyarmor_runtime.so` 是按 3.10 ABI 编译的，
    别的版本上是**整个 SDK 不可用**（不是"骨架不能用"），只能换解释器。
    """
    if sys.version_info[:2] == (3, 10):
        return ""
    return (f"手套 SDK 需要 Python 3.10（当前 {sys.version.split()[0]}）。"
            f"SDK 的解算核心按 3.10 ABI 加密，其它版本下 import 即失败。"
            f"请用项目自带 venv（./start.sh reinstall 可重建）")


# ── 后端开关（迁移期回退用，验收后随 fork 一起删）────────

def backend() -> str:
    """当前手套串口后端："sdk"（厂商 SDK）或 "fork"（旧 core/glove_usb）。"""
    try:
        from config.settings import GLOVE_USB_BACKEND
        return GLOVE_USB_BACKEND
    except Exception:
        return os.environ.get("GLOVE_USB_BACKEND", "sdk")


def raw_imu_stream_cls():
    """手套串口流类 —— 两个后端的构造签名与帧类型都兼容（见迁移计划 §2.3）。"""
    if backend() == "fork":
        from core.glove_usb.streams import RawImuStream
        return RawImuStream
    err = ensure_sdk()
    if err:
        raise ImportError(err)
    from glove_io.streams import RawImuStream
    return RawImuStream


def stream_errors():
    """→ (StreamClosedError, StreamTimeoutError)，按后端取。"""
    if backend() == "fork":
        from core.glove_usb.errors import StreamClosedError, StreamTimeoutError
        return StreamClosedError, StreamTimeoutError
    err = ensure_sdk()
    if err:
        raise ImportError(err)
    from common.errors import StreamClosedError, StreamTimeoutError
    return StreamClosedError, StreamTimeoutError


def tactile_preprocessor_cls():
    """触觉降噪预处理器 —— 两个后端契约与默认值逐项相同（计划 §2.4）。"""
    if backend() == "fork":
        from core.glove_usb.tactile_processing import TactilePreprocessor
        return TactilePreprocessor
    err = ensure_sdk()
    if err:
        raise ImportError(err)
    from gui.tactile_processing import TactilePreprocessor
    return TactilePreprocessor


# ── 解算链 ────────────────────────────────────────────

def solver_parts():
    """→ (HandSolver, RawImuFrame)；SDK 不可用时抛 ImportError。

    比 `ensure_sdk()` 多验一道 `algorithm/`（解算链独有）—— 极简版的载荷
    里可能没有它，那时应当报「这份载荷不含解算链」而不是 import 时报
    「No module named 'algorithm'」。
    """
    err = ensure_sdk()
    if err:
        raise ImportError(err)
    err = _verify(find_sdk_dir(), _SDK_PACKAGES + _SOLVER_PACKAGES)
    if err:
        raise ImportError(f"该 SDK 载荷不含解算链: {err}")
    from sdk.solver import HandSolver
    from common.types import RawImuFrame
    return HandSolver, RawImuFrame


# ── 一键部署的自检入口 ─────────────────────────────────

def self_check(with_solver: bool = True) -> str:
    """跑一遍真实导入链；返回错误描述（"" = 通过）。

    给 start.sh / start.bat 的 [4/7] 用。三条纪律：

    1. 必须走**真实装配**（本模块的 ensure_sdk），不是脚本里手写
       `sys.path.insert` + import —— 后者能过而主程序仍会失败。
    2. 普通版要覆盖解算链（`with_solver=True`）。
    3. 极简版**不能**查解算链：它的载荷本来就没有 `algorithm/`，
       查了会把「极简版一切正常」误判成「SDK 不可用」。
    """
    err = check_python_version()
    if err:
        return err
    err = ensure_sdk()
    if err:
        return err
    try:
        raw_imu_stream_cls()
        tactile_preprocessor_cls()
        if with_solver:
            solver_parts()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""


def _main(argv) -> int:
    """`python -m core.glove_sdk_boot <错误文件> [--no-solver]`。

    错误原文由**本模块自己写文件**，脚本再用 `type` 原样打出来。不能用
    `2>` 重定向 + cmd 的 `for /f` 读那个文件：`for /f` 会按控制台代码页
    对内容做一次转换，中文全变成 `?`（Wine 实测，`chcp 936` 也一样），
    而 `type` 是原样输出字节。写文件用的编码取 locale，与控制台一致。
    """
    import locale
    path = ""
    with_solver = "--no-solver" not in argv
    for arg in argv:
        if not arg.startswith("-"):
            path = arg
            break
    err = self_check(with_solver=with_solver)
    if not err:
        return 0
    if path:
        enc = locale.getpreferredencoding(False) or "utf-8"
        with open(path, "w", encoding=enc, errors="replace") as fh:
            fh.write(err.rstrip() + "\n")
    else:
        print(err)
    return 1


__all__ = [
    "SDK_DIRNAME", "SDK_PARENT", "sdk_relpath", "find_sdk_dir",
    "ensure_sdk", "check_python_version",
    "backend", "raw_imu_stream_cls", "stream_errors",
    "tactile_preprocessor_cls", "solver_parts", "self_check",
]


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
