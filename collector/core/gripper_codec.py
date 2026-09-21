"""夹爪触觉力矩阵编码 —— 落盘契约的中立家。

从 ui/main_window.py 搬来（2026-09-16），原因：极简版（lite）也要录夹爪，
但 lite 的导入黑名单不含 ui/main_window（它拖进主程序全部 UI 与重依赖），
而力矩阵编码是**数据契约**，绝不能在两份界面代码里各存一份。

命名镜像既有的 core/depth_codec.py（同为「算法在家、UI 只管显示」的分工）。

★ 本模块**只能**依赖 numpy / config.settings / config.i18n。
  绝不可 import core.gripper —— core/gripper/__init__.py 在导入期就会拉
  fays_runtime（顶层 `import fcntl`），而 fcntl 在 Windows 上不存在，
  一旦引入，主程序 `import ui.main_window` 会在 Windows 上直接崩
  （ui/main_window.py 从这里 re-export 这两个函数）。
"""

from __future__ import annotations

import numpy as np

from config import settings
from config.i18n import tr


def encode_gripper_force_matrix(m: np.ndarray, spec: str = "int16") -> list:
    """触觉力矩阵编码（P4 泵线程，返回扁平数值列表）。

    **只是 `encode_gripper_force_matrix_array(...).tolist()` 的皮**：编码只有
    一份实现（在 array 版里），本函数保证与它逐位同值。落盘主路径已改走
    array 版（见那边的说明），本函数留给历史调用方与测试。

    spec 取 settings.GRIPPER_FORCE_MATRIX_SPECS 之一。

    **注意**：省略 spec 时走的是函数签名默认 "int16"（兼容历史调用的冻结
    契约），**不跟随工具栏的默认档**；生产路径（P4 泵）一律显式传入录制
    开始时锁定的 spec，绝不省略。

    "int16"（既有契约）：不缩放，与
      online/recording/lerobot_v3.quantize_force_matrix 一致的有符号补码行
      差分（mod 2^16 可逆），扩展到 (250,250,3) 三力平面：每行 250×3 个
      元素横向差分；超出 ±32767 mN 饱和截断。**逐点 astype(int16) 是向零
      截断，小数部分全部丢失**——实测真实录制里逐点值只剩 -3…8 的整数电平，
      亚毫牛梯度（接触形状）不可恢复。保留向零截断是为了与历史录制逐位一致。

    "int16xN"（N=10/100/1000，定标量化）：先 ×N 再四舍五入到整数存 int16，
      解码端除以 N 还原，分辨率 1/N mN。饱和上限相应变为 ±32767/N mN
      （×1000 即 ±32.767 mN）。定标档用 rint 而非截断：没有历史包袱，且
      四舍五入把最大误差减半、不留系统性偏零。行差分与 int16 档完全相同，
      所以仍然压得很小（×10 实测 0.122 字节/点，float32 的 1/6.5）。

    "float32"（原值直存）：不做任何量化，把 (250,250,3) 按行展平成 250×750
      的 float32 列表。无截断、无饱和，误差为 float32 本身的 ~1e-7 相对精度。
      体积换精度：实测 0.797 字节/点（本段数据约为 int16 档的 119 倍）。

    反解：整数档 reshape(-1,750) → cumsum(axis=1) → reshape(250,250,3)
          （cumsum 走 int32 再截断回 int16 恢复 mod 2^16 回绕），
          再按倍率 ÷N；float32 档 reshape(-1,750) → (250,250,3)。

    ★ 返回的 Python 元素类型只区分「家族」：int = int16 系（**倍率无法从
      元素类型看出**，×10 和 ×1000 都是 int），float = float32。
      倍率必须由 info.json features.scale 携带，writer 与 demo 都按
      scale + 列类型共同判别，不要只看元素类型。
    """
    return encode_gripper_force_matrix_array(m, spec).tolist()


def encode_gripper_force_matrix_array(m: np.ndarray,
                                      spec: str = "int16") -> np.ndarray:
    """同上，但返回**一维 ndarray**（不 .tolist()，值逐位相同）。

    为什么要多这一版（2026-09-18 实测）：writer 建 `list<int16>` 列时，
    pyarrow 对 Python list 是**逐值装箱**——185 帧 × 187500 点 = 538ms 且
    全程持 GIL；换成 ndarray 后同一列 13.5ms、最长停摆 3.6ms（噪声底
    0.9ms）。那 538ms 正好落在收尾窗口，把原始流接收线程挡在 `recv()`
    外面，服务端 16 包队列（≈0.53s）一满就主动 close ⇒ 停止即断链
    （实测 55 次「完成」54 次断链，中止路径不写 parquet ⇒ 0/11）。
    所以省掉的不只是 0.6s，是那条断链的成因。

    ★ **返回值一定是自己的副本**，绝不是 `m` 的视图：`m` 来自桥接的
      latest-wins 单槽（下一帧原地改写），交视图出去会让整段落盘变成
      最后一帧的值——不报错、只是数据全错。

    ★ 落盘契约没变：元素仍是 int16 系 / float32 系两家族，倍率照旧只靠
      info.json features.scale 携带（见上面 list 版的说明）。
    """
    if spec not in settings.GRIPPER_FORCE_MATRIX_SPECS:
        raise ValueError(f"未知力矩阵规格 {spec!r}")
    arr = np.asarray(m, dtype=np.float32)
    if spec == "float32":
        # 与整数档同为「扁平 250×750」，只有元素类型不同：
        # 读取端统一 reshape(-1,750) 后再按 dtype 决定是否 cumsum。
        # copy=True：m 可能就是调用方那块复用缓冲，必须拷出来。
        return np.array(arr.reshape(-1), dtype=np.float32, copy=True)
    scale = settings.GRIPPER_FORCE_MATRIX_SCALES[spec]
    rows = int(arr.shape[0])
    flat = arr.reshape(rows, -1)
    if scale == 1:
        q = np.clip(flat, -32767.0, 32767.0).astype(np.int16)
    else:
        # rint 后已是整数值，astype 的向零截断不再丢任何东西
        q = np.clip(np.rint(flat * np.float32(scale)),
                    -32767.0, 32767.0).astype(np.int16)
    d = np.empty_like(q)
    d[:, 0] = q[:, 0]
    d[:, 1:] = q[:, 1:] - q[:, :-1]   # int16 mod 2^16 回绕
    # d 是这里新分配的，直接当返回值交出去（不是 m 的视图）
    return d.reshape(-1)


def describe_gripper_matrix_spec(spec: str) -> str:
    """规格 → 人话（工具栏提示与日志共用，别在两处各写一份）。"""
    if spec not in settings.GRIPPER_FORCE_MATRIX_SPECS:
        return tr("未知规格 {}", spec)
    scale = settings.GRIPPER_FORCE_MATRIX_SCALES.get(spec)
    if scale is None:
        return tr("float32 原值直存（无截断无饱和，体积最大）")
    if scale == 1:
        return tr("int16 行差分量化（1 mN 台阶、向零截断，体积最小）")
    return tr("int16 定标 ×{}（分辨率 {} mN，饱和上限 ±{} mN）",
              scale, 1.0 / scale, 32767.0 / scale)
