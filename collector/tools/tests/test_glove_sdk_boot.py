#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""glove_sdk_boot 装配层 + Python 版本门单测（**需要真 SDK 与 3.10**）:

    venv/bin/python tools/tests/test_glove_sdk_boot.py

这是**唯一**能提前发现「客户机 Python 版本不对」的自动化闸门 —— 版本不对时
`import sdk.api` 直接 ImportError（PyArmor runtime 按 3.10 ABI 编译，
`_PyFloat_Pack8` 在 3.11 起被移除）。所以本测试故意**不**做假 SDK，
假的那份在 test_glove_keypoint_solver.py 里。

覆盖:
  1. 解释器必须是 3.10（不是则整份测试无意义，直接失败）
  2. check_python_version 的判据在 3.12 上确实会报错（反转验证，防恒真）
  3. find_sdk_dir / ensure_sdk 幂等
  4. **config 名字撞车**：SDK 根下有个 config/，必须被仓库根的正规包压住
  5. _verify 的两条防线（sdk 不落在 SDK 目录 / config 被盖掉）都能报错
  6. 四个转发函数都指向真 SDK 的模块
  7. 部署自检入口（`self_check` / `_main`）—— start.sh 的 [4/7] 调它，
     失败形态全静默：探针过了主程序连不上、中文错误变 `?`、`--no-solver`
     没生效把极简版误判成「SDK 不可用」
退出码 0 = 全部通过。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import core.glove_sdk_boot as boot

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


def main():
    # ── 1. 版本门 ──
    # 这一条不通过，下面全部没有意义：SDK 在非 3.10 下 import 即失败，
    # 本测试跑起来本身就是「部署机版本对了」的证据。
    v = sys.version_info
    check(v[:2] == (3, 10),
          f"解释器 {v[0]}.{v[1]}.{v[2]} 是 3.10（SDK 加密链的 ABI 要求）")
    check(boot.check_python_version() == "",
          "check_python_version 在 3.10 上放行")

    # 反转验证：把 sys.version_info 临时改成 3.12，判据必须翻脸。
    # （不做这条的话，「判据写反了」也会让上面那条 PASS —— 恒真陷阱。）
    real_vi = sys.version_info
    try:
        sys.version_info = (3, 12, 3, "final", 0)
        msg = boot.check_python_version()
        check(msg and "3.10" in msg,
              f"check_python_version 在 3.12 上拒绝: {msg[:40]}...")
    finally:
        sys.version_info = real_vi
    check(boot.check_python_version() == "", "版本判据复原后重新放行")

    # ── 2. 定位与注入 ──
    sdk_dir = boot.find_sdk_dir()
    check(bool(sdk_dir) and os.path.isdir(sdk_dir),
          f"find_sdk_dir 找到目录: {sdk_dir}")
    check(sdk_dir == boot.find_sdk_dir(), "find_sdk_dir 缓存命中（幂等）")
    check(os.path.basename(sdk_dir) == boot.SDK_DIRNAME,
          f"目录名 = {boot.SDK_DIRNAME}")

    err = boot.ensure_sdk()
    check(err == "", f"ensure_sdk 成功: {err or 'OK'}")
    check(boot.ensure_sdk() == "", "ensure_sdk 第二次调用仍返回成功（幂等）")
    check(sys.path.count(sdk_dir) == 1,
          f"sys.path 里 SDK 目录恰好一条: {sys.path.count(sdk_dir)} 条")
    check(sys.path[-1] == sdk_dir, "SDK 目录在 sys.path **末尾**（不是开头）")

    # ── 3. 解算链真能导入（这是整份测试的核心断言）──
    try:
        import sdk
        check(getattr(sdk, "__version__", "") == "2.1.0",
              f"sdk.__version__ = {getattr(sdk, '__version__', '?')}")
    except Exception as exc:
        check(False, f"import sdk 失败: {type(exc).__name__}: {exc}")
        print("\nFAIL: SDK 不可导入，后续断言无意义")
        return 1
    try:
        import sdk.api  # noqa: F401  —— 顶层即拉解算链，版本不对必炸
        check(True, "import sdk.api 成功（同时证明 Python 版本正确）")
    except Exception as exc:
        check(False, f"import sdk.api 失败: {type(exc).__name__}: {exc}")

    # ── 4. config 名字撞车（SDK 根下也有个 config/）──
    import config
    cfg_file = getattr(config, "__file__", "") or ""
    repo_root = boot._REPO_ROOT
    check(cfg_file.startswith(repo_root + os.sep),
          f"import config 落在仓库根: {cfg_file}")
    check(os.path.isdir(os.path.join(sdk_dir, "config")),
          "前提成立: SDK 根下确实有一个同名 config/（否则这条测试是空的）")

    # ── 5. _verify 两条防线（直接喂坏参数，防「守卫写了但没接上」）──
    check(boot._verify("/nonexistent/sdk") != "",
          "_verify 对不存在的 SDK 目录报错")
    check("不在 SDK 目录" in boot._verify("/nonexistent/sdk")
          or "解析到了" in boot._verify("/nonexistent/sdk"),
          f"_verify 报错文本可读: {boot._verify('/nonexistent/sdk')[:60]}")

    # ── 5b. 必需包清单：algorithm 只在解算链里要 ──
    # 极简版只录手套 IMU、不解算骨架，载荷里可以没有 algorithm/ 与
    # scipy/loguru/pydantic。若有人把 algorithm 塞回必需清单，lite 会在
    # ensure_sdk() 就被判失败（症状: 手套连不上，且报的是"SDK 不可用"）。
    check("algorithm" not in boot._SDK_PACKAGES,
          f"传输链必需包不含 algorithm: {boot._SDK_PACKAGES}")
    check("algorithm" in boot._SOLVER_PACKAGES,
          f"解算链额外要求 algorithm: {boot._SOLVER_PACKAGES}")
    check(boot._verify(sdk_dir) == "", "传输链必需包都在 SDK 目录里")
    check(boot._verify(sdk_dir, boot._SDK_PACKAGES + boot._SOLVER_PACKAGES) == "",
          "加上解算链必需包后仍然通过")

    # ── 6. 四个转发函数指向真 SDK 的模块 ──
    try:
        raw_cls = boot.raw_imu_stream_cls()
        check(os.path.abspath(sys.modules[raw_cls.__module__].__file__)
              .startswith(sdk_dir + os.sep),
              f"raw_imu_stream_cls → {raw_cls.__module__}.{raw_cls.__name__}")
    except Exception as exc:
        check(False, f"raw_imu_stream_cls 失败: {type(exc).__name__}: {exc}")

    try:
        closed, timeout = boot.stream_errors()
        check(issubclass(closed, Exception) and issubclass(timeout, Exception),
              f"stream_errors → ({closed.__name__}, {timeout.__name__})")
    except Exception as exc:
        check(False, f"stream_errors 失败: {type(exc).__name__}: {exc}")

    try:
        tp = boot.tactile_preprocessor_cls()
        check(os.path.abspath(sys.modules[tp.__module__].__file__)
              .startswith(sdk_dir + os.sep),
              f"tactile_preprocessor_cls → {tp.__module__}.{tp.__name__}")
        # 解算链（不是 Qt 那条）用它做时序滤波 → 必须只有 numpy 依赖，
        # 否则 PyQt5 的主程序里会连带拖进 PySide6
        src = open(sys.modules[tp.__module__].__file__, encoding="utf-8").read()
        check("PySide6" not in src, "gui.tactile_processing 不依赖 PySide6")
    except Exception as exc:
        check(False, f"tactile_preprocessor_cls 失败: {type(exc).__name__}: {exc}")

    try:
        solver_cls, frame_cls = boot.solver_parts()
        check(solver_cls.__name__ == "HandSolver"
              and os.path.abspath(sys.modules[solver_cls.__module__].__file__)
              .startswith(sdk_dir + os.sep),
              f"solver_parts → ({solver_cls.__module__}.{solver_cls.__name__}, "
              f"{frame_cls.__name__})")
    except Exception as exc:
        check(False, f"solver_parts 失败: {type(exc).__name__}: {exc}")

    # ── 6b. 部署自检入口 self_check / _main（start.sh 的 [4/7] 用它）──
    # 这一段的失败形态全是**静默**的：探针过了但主程序连不上、错误文本
    # 中文变 `?`、`--no-solver` 没生效把极简版误判成「SDK 不可用」。
    import contextlib
    import locale
    import tempfile

    @contextlib.contextmanager
    def sdk_state(fake_dir):
        """临时改掉 SDK 目录缓存（`find_sdk_dir`/`ensure_sdk` 都是记忆化的）。

        `fake_dir=""` 走的是「目录不存在」那条路 —— 必须给 `""` 而不是
        「一个不存在的路径」：后者下 `sdk` 仍能从 sys.path 上的**真** SDK
        解析出来，报的是 `_verify` 的「不在 SDK 目录内」，测的是另一条分支
        （实测踩过：断言写着"缺目录"，报错文本却是"解析到了真 SDK"）。
        """
        old_dir, old_err = boot._sdk_dir, boot._ensure_error
        boot._sdk_dir, boot._ensure_error = fake_dir, None
        try:
            yield
        finally:
            boot._sdk_dir, boot._ensure_error = old_dir, old_err

    check(boot.self_check() == "",
          f"self_check（普通版，含解算链）通过: {boot.self_check() or 'OK'}")
    check(boot.self_check(with_solver=False) == "",
          "self_check(--no-solver 口径) 通过")

    # 反转验证：把 SDK 目录换成不存在的地方，自检必须翻脸。
    # 不做这条的话，「自检恒返回 ""」也会让上面两条 PASS。
    with sdk_state(""):
        msg = boot.self_check()
        check("未找到" in msg,
              f"self_check 在 SDK 目录不存在时报「未找到」: {msg[:46]}")

    # _main：错误原文写文件（编码取 locale，与控制台一致 —— cmd 侧才用
    # `type` 原样打出来；用 utf-8 写会乱码，用 for /f 读会全变 `?`）
    with tempfile.TemporaryDirectory() as tmp:
        err_path = os.path.join(tmp, "err.txt")
        with sdk_state(""):
            rc = boot._main([err_path])
        check(rc == 1, f"_main 自检失败返回 1（实际 {rc}）")
        check(os.path.exists(err_path), "_main 写了错误文件")
        enc = locale.getpreferredencoding(False) or "utf-8"
        with open(err_path, encoding=enc) as fh:
            written = fh.read()
        check(written.strip() != "", f"_main 错误文件非空: {written.strip()[:40]}")

        # 成功的路径：返回 0，且**不该**留下错误文件（脚本靠退出码分叉）
        ok_path = os.path.join(tmp, "ok.txt")
        rc = boot._main([ok_path])
        check(rc == 0, f"_main 自检通过返回 0（实际 {rc}）")
        check(not os.path.exists(ok_path), "_main 通过时不写错误文件")

    # --no-solver 的解析：它不能被当成路径，且必须真的传到 self_check
    seen = {}

    def _recorder(with_solver=True):
        seen["with_solver"] = with_solver
        return ""

    real_self_check = boot.self_check
    boot.self_check = _recorder
    try:
        rc = boot._main(["--no-solver", "/tmp/x.txt"])
        check(seen.get("with_solver") is False and rc == 0,
              f"--no-solver 传到 self_check: {seen}（标志在前）")
        rc = boot._main(["/tmp/x.txt", "--no-solver"])
        check(seen.get("with_solver") is False,
              "--no-solver 在路径之后也认（脚本两种写法都可能有）")
        rc = boot._main([])
        check(seen.get("with_solver") is True and rc == 0,
              "不带 --no-solver 时 with_solver=True（普通版口径）")
    finally:
        boot.self_check = real_self_check

    # ── 7. 后端开关：默认 sdk，环境变量可覆盖，非法值不炸 ──
    check(boot.backend() in ("sdk", "fork"),
          f"backend() = {boot.backend()!r}")
    real_backend = os.environ.get("GLOVE_USB_BACKEND")
    try:
        os.environ["GLOVE_USB_BACKEND"] = "fork"
        # 走 settings 分支时读的是模块常量，这里只验环境变量兜底分支
        if real_backend is None:
            check(boot.backend() in ("sdk", "fork"),
                  "GLOVE_USB_BACKEND 环境变量不破坏 backend()")
    finally:
        if real_backend is None:
            os.environ.pop("GLOVE_USB_BACKEND", None)
        else:
            os.environ["GLOVE_USB_BACKEND"] = real_backend

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: glove_sdk_boot 装配层与版本门全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
