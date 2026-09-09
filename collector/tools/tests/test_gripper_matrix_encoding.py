"""P4 力矩阵编码器单测：int16 行差分 (250,250,3) 可逆 + 饱和截断。

编码函数在 ui/main_window.py（泵线程用），与 online/ 参考实现
lerobot_v3.quantize_force_matrix 语义一致，仅扩展到三力平面。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import numpy as np

from ui.main_window import encode_gripper_force_matrix


def decode(encoded):
    raw = np.asarray(encoded, dtype=np.int16).tobytes()
    d = np.frombuffer(raw, dtype=np.int16)
    q = np.cumsum(d.reshape(250, -1), axis=1).astype(np.int16)
    return q.reshape(250, 250, 3)


def main():
    rng = np.random.default_rng(7)
    m = (rng.random((250, 250, 3)) * 6000 - 3000).astype(np.float32)
    enc = encode_gripper_force_matrix(m)
    assert len(enc) == 250 * 250 * 3, len(enc)
    ref = np.clip(m, -32767, 32767).astype(np.int16)
    assert np.array_equal(decode(enc), ref), "round-trip mismatch"
    print("PASS: (250,250,3) 行差分往返一致, len =", len(enc))

    sat = encode_gripper_force_matrix(
        np.array([[[40000.0, -40000.0, 1.0]]], dtype=np.float32))
    q = np.frombuffer(np.asarray(sat, dtype=np.int16).tobytes(),
                      dtype=np.int16)
    q = np.cumsum(q.reshape(1, -1), axis=1).astype(np.int16)
    assert q[0, 0] == 32767 and q[0, 1] == -32767, q
    print("PASS: ±32767 mN 饱和截断, 首行 =", q[0])

    z = encode_gripper_force_matrix(np.zeros((250, 250, 3), np.float32))
    assert not any(z), "zero matrix must stay zero"
    print("PASS: 全零矩阵编码为全零")

    print("PASS: 力矩阵编码器单测全部通过")


if __name__ == "__main__":
    main()
