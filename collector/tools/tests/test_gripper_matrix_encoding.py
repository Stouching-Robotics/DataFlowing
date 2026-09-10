"""P4 力矩阵编码器单测：五档落盘规格。

  int16（默认）  行差分 (250,250,3) 可逆 + 饱和截断 + 向零截断的精度损失
  int16×10/100/1000  定标档：先 ×N 再 rint，解码 ÷N 还原，分辨率 1/N mN
  float32        原值直存往返无损 + 元素类型为 float

编码函数在 ui/main_window.py（泵线程用），int16 分支与 online/ 参考实现
lerobot_v3.quantize_force_matrix 语义一致，仅扩展到三力平面。

★ 元素类型只区分「家族」：四档 int16 系都给 int，倍率看不出来。
  writer / demo 靠 info.json features.scale 还原，本文件校验那条契约。
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

    # ── int16 截断的精度损失（默认规格的代价，float32 规格存在的理由）──
    fine = np.full((250, 250, 3), 0.9, np.float32)      # 亚毫牛信号
    dec = decode(encode_gripper_force_matrix(fine))
    assert not dec.any(), "0.9 应被向零截断为 0"
    print("PASS: int16 规格下 0.9 mN 整幅归零（亚毫牛信号丢失）")

    mixed = np.zeros((250, 250, 3), np.float32)
    mixed[10, 10, 2] = 5.75
    got = decode(encode_gripper_force_matrix(mixed))[10, 10, 2]
    assert got == 5, got
    print(f"PASS: int16 规格下 5.75 → {int(got)}（小数位不可恢复）")

    # ── 定标档：×10/×100/×1000 解码 ÷N 还原，精度随倍率提升 ──
    # 0.9 mN 在 int16 档整幅归零；×10 应还原成 0.9（rint(9)=9 → ÷10）
    for scale in (10, 100, 1000):
        spec = f"int16x{scale}"
        got = decode(encode_gripper_force_matrix(fine, spec)) \
            .astype(np.float32) / np.float32(scale)
        assert np.allclose(got, fine, atol=0.5 / scale), \
            f"{spec} 应还原 0.9 mN（容差半个 LSB），得到 {got[0, 0, 0]}"
    print("PASS: ×10/×100/×1000 定标档把 0.9 mN 还原（int16 档归零）")

    # 5.75 mN：int16 档丢成 5；×10 存 58 → 5.8（rint 进整，误差 ≤0.05）
    # 注意定标档用 rint 而非向零截断，所以是 5.8 不是 5.7
    got10 = decode(encode_gripper_force_matrix(mixed, "int16x10")) \
        .astype(np.float32) / np.float32(10)
    assert abs(float(got10[10, 10, 2]) - 5.8) < 1e-6, got10[10, 10, 2]
    print(f"PASS: ×10 档 5.75 → {float(got10[10, 10, 2]):.1f}（rint 进整，非截断）")

    # 定标档的元素同样是 int（倍率分辨不出，只能靠 scale）
    enc10 = encode_gripper_force_matrix(mixed, "int16x10")
    assert isinstance(enc10[0], int), \
        f"定标档元素应为 int，得到 {type(enc10[0])}"

    # 定标档的饱和上限随倍率下降：±32767/倍率 mN（×1000 即 ±32.767 mN）
    satm = np.zeros((250, 250, 3), np.float32)
    satm[10, 10, 2] = 40.0                          # 40 mN
    q10 = decode(encode_gripper_force_matrix(satm, "int16x10"))
    assert q10[10, 10, 2] == 400, q10[10, 10, 2]    # 40×10=400，远未饱和
    q1000 = decode(encode_gripper_force_matrix(satm, "int16x1000"))
    assert q1000[10, 10, 2] == 32767, q1000[10, 10, 2]   # 40×1000=40000 饱和
    print("PASS: 定标档饱和上限随倍率下降（40 mN 在 ×1000 饱和、×10 不饱和）")

    # ── float32 规格：往返无损、无饱和、元素类型是 float ──
    enc32 = encode_gripper_force_matrix(fine, "float32")
    assert len(enc32) == 250 * 250 * 3, len(enc32)
    assert isinstance(enc32[0], float), \
        f"float32 规格必须给 float 元素（writer 据此定型），得到 {type(enc32[0])}"
    back = np.asarray(enc32, np.float32).reshape(250, 250, 3)
    assert np.array_equal(back, fine), "float32 往返应逐位一致"
    assert np.array_equal(back, np.asarray(fine.reshape(-1).tolist(),
                                           np.float32).reshape(250, 250, 3))
    print("PASS: float32 规格往返逐位无损，0.9 mN 保留")

    # int16 规格的元素必须是 int（家族靠元素类型区分，不能靠数值巧合）
    assert isinstance(enc[0], int), \
        f"int16 规格必须给 int 元素，得到 {type(enc[0])}"

    big = np.array([[[40000.0, -40000.0, 1.0]]], dtype=np.float32)
    enc_big = encode_gripper_force_matrix(big, "float32")
    assert enc_big[0] == 40000.0 and enc_big[1] == -40000.0, enc_big[:3]
    print("PASS: float32 规格无 ±32767 饱和（40000 原样保留）")

    assert np.array_equal(
        np.asarray(encode_gripper_force_matrix(m, "float32"),
                   np.float32).reshape(250, 250, 3), m), \
        "float32 规格对典型幅值也应逐位无损"
    print("PASS: float32 规格对 ±3000 随机矩阵往返无损")

    # ── 参数默认 = int16，且与历史契约逐位一致（既有录制的可复现性）──
    # 这是编码器签名的兼容默认，与工具栏默认档（settings 的 SPEC_DEFAULT）无关
    assert encode_gripper_force_matrix(m) == encode_gripper_force_matrix(m, "int16")
    print("PASS: 省略 spec 等于 int16 档（编码器兼容默认，非工具栏默认档）")

    # 未知规格要显式报错，不能静默按某档处理
    try:
        encode_gripper_force_matrix(m, "int16x7")
    except ValueError as exc:
        assert "int16x7" in str(exc), exc
        print("PASS: 未知规格被拒绝（ValueError）")
    else:
        raise AssertionError("未知规格应报错")

    print("PASS: 力矩阵编码器单测全部通过")


if __name__ == "__main__":
    main()
