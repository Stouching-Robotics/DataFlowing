"""触觉就绪门的契约测试 —— 「事件置位」不等于「触觉可用」。

用法:
    venv/bin/python tools/tests/test_tactile_ready_gate.py

为什么要有它：`TactileProcessManager._initialize_all_input()` 的**失败路径
上也调 `ready_event.set()`**（为了让 `wait_ready()` 不必干等满超时），于是
`wait_ready()` 原先只看事件置位就返回 True —— 两路全灭也报「就绪」。主程序
据此打「相机 + 触觉 + SLAM 链路就绪」，触觉面板空白，而日志里那两条
`[Tactile:left|right] ERROR:` 没人会去看。

2026-09-23 实锤：sightac 的 PyArmor runtime 按 3.12 编、venv 却是 3.10，
输入子进程一 spawn 就 `undefined symbol: _PyCode_Validate`，两路全灭；从
09-21 16:22（最后一次正常）到 09-23 修好，整整两天每次连接都报「已就绪」。

这里钉两条：
  1. 两路都失败 → `wait_ready()` 必须返回 False（桥接那句
     `RuntimeError("触觉双进程未就绪: …")` 才有机会被执行到）；
  2. 只灭一路 → 仍返回 True（半边触觉也比没有强），但调用方要能从
     `snapshot()` 里读到是哪一路，好把话说准。

纯 Python，不碰硬件：状态直接按失败路径的形状摆好。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from unittest.mock import MagicMock                      # noqa: E402

from core.gripper.devices.tactile_process_manager import (  # noqa: E402
    TactileProcessManager,
    TactileState,
)

_FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail else ""))
    if not cond:
        _FAILS.append(name)


def manager_with(errors):
    """按 `_initialize_all_input` 失败路径的形状摆状态并返回 manager。

    关键就在这：失败路径**同时**做两件事 —— 写 `sides[side].error`、
    `ready_event.set()`。只照抄其中一件就复现不出这个坑。
    """
    state = TactileState()
    manager = TactileProcessManager(state, discovery=MagicMock())
    with state.lock:
        for side, error in zip(("left", "right"), errors):
            if error is not None:
                state.sides[side].error = error
            state.sides[side].ready_event.set()
    return manager, state


def main():
    print("[1] 失败路径的事件置位骗不过 wait_ready")
    manager, _ = manager_with(["左路炸了", "右路也炸了"])
    check("两路都失败 → False", manager.wait_ready(timeout=0.05) is False)

    print("[2] 只灭一路仍然放行（半边触觉也比没有强）")
    manager, state = manager_with([None, "右路炸了"])
    check("只灭一路 → True", manager.wait_ready(timeout=0.05) is True)
    snapshot = manager.snapshot()
    check("但那一路的 error 读得出来（调用方据此改口径）",
          getattr(snapshot, "right").error == "右路炸了"
          and getattr(snapshot, "left").error is None,
          f"left={getattr(snapshot, 'left').error!r} "
          f"right={getattr(snapshot, 'right').error!r}")

    print("[3] 两路都好照旧放行（没被上一条误伤）")
    manager, _ = manager_with([None, None])
    check("两路都好 → True", manager.wait_ready(timeout=0.05) is True)

    print("[4] 超时仍然是 False（原有语义不许被改掉）")
    state = TactileState()
    manager = TactileProcessManager(state, discovery=MagicMock())
    with state.lock:
        state.sides["left"].ready_event.set()      # 只置位一路，另一路永不置位
    check("等不到第二路 → False", manager.wait_ready(timeout=0.05) is False)

    print()
    if _FAILS:
        print(f"FAILED: {len(_FAILS)} 项 — {_FAILS}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
