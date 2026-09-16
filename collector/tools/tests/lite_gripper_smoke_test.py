"""极简版 UMI 夹爪冒烟测试（无硬件，离屏 Qt，三条路径）。

路径 D（UI + 假桥注入）:
    LiteWindow(gripper_bridge_cls=FakeGripperBridge) → 只开夹爪（无 D435/UVC）
    → 夹爪 RGB 作主视频源 → 录制 3s → 停 → 校验 gripper_rgb 视频 /
    parquet 的力/力矩阵/夹爪状态/SLAM 轨迹列 / info.json 的 devices 与
    features.scale。顺带断言「只录不显」契约（预览区没有被夹爪污染）。

路径 E（纯管线，无 UI）:
    rig2 前缀（gripper_2_*）的完整落盘链：力 / 力矩阵 / 状态 / 轨迹 +
    set_force_matrix_spec → 校验双夹爪命名空间与倍率登记。

路径 F（编码器搬家回归）:
    core.gripper_codec.encode_gripper_force_matrix 必须**就是**
    ui.main_window 里那个函数对象（re-export 而非复制第二份）。
    venv_lite 装不下 ui.main_window 的重依赖，那里跳过。

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/lite_gripper_smoke_test.py
    （包内副本用 lite_package/venv_lite/bin/python 跑，验证闭包完整性）
"""
import json
import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pyarrow.parquet as pq
from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication, QMessageBox

from config import settings
from core.device_detector import DeviceInfo
from core.gripper_codec import encode_gripper_force_matrix
from core.helpers import (episode_video_files, pooled_data_parquet_path,
                          pooled_info_path)
from core.pipeline import CameraPipeline
from ui.lite_window import LiteUploadManager, LiteWindow

OUT_ROOT_D = "/tmp/lite_gripper_smoke_ui"
OUT_ROOT_E = "/tmp/lite_gripper_smoke_pipe"
DURATION_D = 3.0
DURATION_E = 3.0

# 力矩阵列名（rig1 无前缀；rig2 加 "gripper_2_"）
MATRIX_LEN = settings.GRIPPER_FORCE_MATRIX_DIM ** 2 * 3


def wait(app, ms):
    t_end = time.time() + ms / 1000.0
    while time.time() < t_end:
        app.processEvents()
        time.sleep(0.01)


class FakeGripperBridge(QObject):
    """GripperBridge 替身（鸭子类型，信号口径与 core/gripper/bridge.py 一致）。

    ★ 刻意**不提供 stereo_frame_ready** —— lite 只录不显，不接双目信号；
    哪天有人给 lite 接上它，这里会直接 AttributeError 暴露。
    力矩阵走与真桥相同的 latest-wins 单槽 + 序号口径（泵线程按 seq 判新）。
    """

    rgb_frame_ready = pyqtSignal(object, object)
    tactile_ready = pyqtSignal(str, object, object, object, object)
    pose_ready = pyqtSignal(object, object, object, object, object)
    gripper_state_ready = pyqtSignal(object)
    opened = pyqtSignal()
    log = pyqtSignal(str)
    error = pyqtSignal(str)
    closed = pyqtSignal()

    TICK_MS = 33
    MATRIX_EVERY = 5      # 力矩阵降频：30Hz 全量（每帧 187500 个数）太占地

    def __init__(self, parent=None, stereo_slot="gripper_stereo_left",
                 rig_index=1):
        super().__init__(parent)
        self.stereo_slot = stereo_slot
        self.rig_index = int(rig_index)
        self.serial = ""
        self.closed_count = 0
        self._lock = threading.Lock()
        self._seq = {"left": 0, "right": 0}
        self._matrix = {}          # side → (capture_ns, ndarray)
        self._tick = 0
        self._dropped = 0
        self._elapsed = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._emit)

    # ── 生命周期（UI 调）──

    def open(self, esp_serial):
        self.serial = esp_serial
        self._timer.start(self.TICK_MS)
        self.opened.emit()

    def close(self):
        self._timer.stop()
        self.closed_count += 1
        self.closed.emit()

    # ── 力矩阵泵线程读的口（真桥为加锁单槽 latest-wins）──

    def matrix_seq(self, side):
        with self._lock:
            return self._seq.get(side, 0)

    def latest_matrix_stamped(self, side):
        with self._lock:
            entry = self._matrix.get(side)
        return None if entry is None else (int(entry[0]), entry[1])

    def stereo_drop_snapshot(self):
        return (self._dropped, self._elapsed)

    def reset_stereo_drop_watch(self):
        self._dropped = 0
        self._elapsed = 0

    def clear_trajectory(self):
        pass

    # ── 样本生产（模拟 30Hz 的相机/触觉/串口三条流）──

    @staticmethod
    def make_matrix(seed):
        m = np.zeros((settings.GRIPPER_FORCE_MATRIX_DIM,
                      settings.GRIPPER_FORCE_MATRIX_DIM, 3), dtype=np.float32)
        i = 100 + (seed % 20)
        m[i:i + 10, i:i + 10, 0] = 5.0 + (seed % 7)
        return m

    def _emit(self):
        self._tick += 1
        now_ns = time.monotonic_ns()
        self._elapsed += 1
        if self._tick % 9 == 0:        # 模拟偶发双目空桶（走汇总日志分支）
            self._dropped += 1
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[..., 2] = (self._tick * 5) & 255
        self.rgb_frame_ready.emit(frame, now_ns)
        for side in ("left", "right"):
            force = [float(100 + self._tick), 2.0, 3.0]
            matrix = None
            if self._tick % self.MATRIX_EVERY == 0:
                matrix = self.make_matrix(self._tick)
                with self._lock:
                    self._seq[side] += 1
                    self._matrix[side] = (now_ns, matrix)
            self.tactile_ready.emit(
                side, np.zeros((240, 320, 3), dtype=np.uint8), force,
                matrix, now_ns)
        pos = (0.001 * self._tick, 0.002 * self._tick, 0.0)
        self.pose_ready.emit(pos, (0.0, 0.0, 0.0, 1.0), (pos,),
                             float(self._tick) * 0.033,
                             287607700000000 + self._tick * 33333333)
        self.gripper_state_ready.emit(
            {"pct": 40.0 + self._tick % 10, "gripped": True, "fz": 1200.0})


# ── 路径 D: UI + 假夹爪桥（只开夹爪，无 D435/UVC）──

def path_d():
    shutil.rmtree(OUT_ROOT_D, ignore_errors=True)
    app = QApplication(sys.argv)
    settings.UPLOAD_AUTO_SYNC = False
    settings.ENCODER_PROBE_ENABLED = False   # lite 口径：直接 x264，测试提速

    pipe = CameraPipeline(output_dir=OUT_ROOT_D)
    finished = []
    pipe.recording_aborted.connect(lambda sid: finished.append("ABORTED"))
    pipe.recording_finished.connect(lambda sid, path: finished.append(path))
    win = LiteWindow(
        pipeline=pipe,
        upload_manager=LiteUploadManager("http://127.0.0.1:9"),
        gripper_bridge_cls=FakeGripperBridge)
    # 守卫若拦下会弹模态框 —— 离屏下没人点确定，测试会永久挂住；
    # 换成收集器，让「被拦」表现为可读的断言失败
    guard_msgs = []
    QMessageBox.warning = staticmethod(
        lambda *a, **k: guard_msgs.append(a[2] if len(a) > 2 else ""))
    win.show()
    wait(app, 200)

    dev = DeviceInfo(key="gripper:FAKE01", kind="gripper",
                     display_name="UMI 夹爪", serial="FAKE01",
                     address="/dev/ttyACM0")
    assert win._open_gripper(dev), "假夹爪开启失败"
    bridge = win._workers[dev.key]["bridge"]
    assert isinstance(bridge, FakeGripperBridge)
    assert bridge.serial == "FAKE01", f"open() 未拿到序列号: {bridge.serial}"
    wait(app, 800)

    entry = win._workers[dev.key]
    assert entry["slot_map"]["rgb"] in entry["_video_registered"], \
        "夹爪 RGB 槽未惰性注册（首帧没到？）"
    # 只录不显契约：预览区/预览源完全没有夹爪
    assert win._src_combo.count() == 3, f"预览源被改动: {win._src_combo.count()}"
    assert not [s for s in win._ring if "gripper" in s], \
        f"预览 ring 收到夹爪帧: {list(win._ring)}"
    assert "pose_view" not in entry and "side_widgets" not in entry, \
        "lite 条目不该有显示控件"
    meta = win._device_meta()          # 显式分支：不许 KeyError
    gmeta = [m for m in meta if m["kind"] == "gripper"]
    assert len(gmeta) == 1 and len(gmeta[0]["slots"]) == 5, f"meta 异常: {meta}"
    print(f"  D: 夹爪已开启（{bridge.stereo_slot} / rig{bridge.rig_index}），"
          f"RGB 槽 {entry['slot_map']['rgb']} 已注册，预览区未受影响")

    # 只开夹爪时录制守卫必须放行（夹爪 RGB 作主视频源）
    assert win._master_video_slot() == "gripper_rgb", \
        f"主视频源选择错误: {win._master_video_slot()}"
    win._task_edit.setText("lite_gripper")
    win._start_recording()
    assert not guard_msgs, f"录制守卫拦下了只开夹爪的录制: {guard_msgs}"
    assert pipe.is_recording, "只开夹爪时录制未启动（守卫拦住了？）"
    wait(app, int(DURATION_D * 1000))
    win._stop_recording()
    t_end = time.time() + 15
    while not finished and time.time() < t_end:
        wait(app, 50)
    assert finished and finished[0] != "ABORTED", f"录制异常: {finished}"
    task_dir = finished[0]
    ep = pipe.last_episode_index
    assert ep > 0, "episode 序号异常"
    assert win._workers[dev.key]["matrix_pump_stop"] is None, \
        "录制结束未停力矩阵泵"
    win.close()
    wait(app, 200)
    assert bridge.closed_count == 1, "关窗未关闭桥接（进程会残留）"
    assert dev.key not in win._workers, "关窗未回收夹爪条目"

    videos = episode_video_files(task_dir, ep)
    assert "gripper_rgb" in videos, f"夹爪 RGB 视频缺失: {videos}"
    assert os.path.getsize(videos["gripper_rgb"]) > 1000, "夹爪视频过小"
    tbl = pq.read_table(pooled_data_parquet_path(task_dir, ep))
    assert tbl.num_rows > 0, "parquet 无行"
    cols = tbl.column_names
    want = ["observation.gripper_state",
            "observation.gripper_left_force", "observation.gripper_right_force",
            "observation.gripper_left_force_matrix",
            "observation.gripper_right_force_matrix",
            "observation.gripper_left_force_ns",
            "observation.gripper_left_force_matrix_ns",
            "observation.slam_trajectory",
            # 逐点宿主时刻：这条覆盖的是完整 lite UI 接线（假桥 pose_ready
            # 第 5 参 → lite_window lambda → _on_gripper_pose → pipeline），
            # 信号参数个数或 lambda 形参写错这里就会缺列
            "observation.slam_trajectory_ns"]
    for name in want:
        assert name in cols, f"缺列 {name}（有: {cols}）"
    # 轨迹点列与其时刻列必须逐行等长（下标即配对）
    _traj = tbl.column("observation.slam_trajectory").to_pylist()
    _ns = tbl.column("observation.slam_trajectory_ns").to_pylist()
    assert any(_ns), "slam_trajectory_ns 全空（假桥发了戳却没落盘）"
    assert all(len(a) // 8 == len(b) for a, b in zip(_traj, _ns)), \
        "slam_trajectory 与 slam_trajectory_ns 长度不同步"
    mat = tbl.column("observation.gripper_left_force_matrix").to_pylist()
    rows = [r for r in mat if r]
    assert rows and len(rows[0]) == MATRIX_LEN, \
        f"力矩阵长度异常: {len(rows[0]) if rows else 0} != {MATRIX_LEN}"
    traj = [r for r in tbl.column("observation.slam_trajectory").to_pylist() if r]
    assert traj and len(traj[0]) % 8 == 0, "轨迹列不是 8 值/点"
    firsts = [row[0] for row in traj]
    assert firsts == sorted(firsts), "轨迹时间戳非单调"
    info = json.load(open(pooled_info_path(task_dir), encoding="utf-8"))
    kinds = [d.get("kind") for d in info.get("devices", [])]
    assert "gripper" in kinds, f"info.json devices 缺 gripper: {kinds}"
    spec = settings.GRIPPER_FORCE_MATRIX_SPEC
    scale = settings.GRIPPER_FORCE_MATRIX_SCALES.get(spec)
    feat = info["features"]["observation.gripper_left_force_matrix"]
    assert feat["scale"] == (scale or 1), \
        f"features.scale 与规格不符: {feat} vs spec={spec}"
    print(f"  D: {DURATION_D}s → gripper_rgb {os.path.getsize(videos['gripper_rgb']) // 1024}KB"
          f" / {tbl.num_rows} 行 / 力矩阵 {len(rows)} 行 x{MATRIX_LEN}"
          f" / 轨迹 {len(traj)} 行 / scale={feat['scale']}")
    print("  D: PASS")
    return 0


# ── 路径 E: 纯管线 + rig2 命名空间（无 UI、无桥）──

def path_e():
    shutil.rmtree(OUT_ROOT_E, ignore_errors=True)
    app = QApplication.instance() or QApplication(sys.argv)
    settings.UPLOAD_AUTO_SYNC = False
    settings.ENCODER_PROBE_ENABLED = False

    prefix = "gripper_2_"          # 第二台夹爪的数据列命名空间
    spec = settings.GRIPPER_FORCE_MATRIX_SPEC
    pipe = CameraPipeline(output_dir=OUT_ROOT_E)
    finished = []
    pipe.recording_finished.connect(lambda sid, path: finished.append(path))
    # 倍率只能挂在 recording_started 之后登记：start_recording 立刻返回，
    # writer 由后台线程创建（pipeline._start_async）且在 start_episode 里
    # 会清空已登记的倍率 —— 在 start_recording 之后紧跟着调是**静默无效**的
    pipe.recording_started.connect(
        lambda sid: pipe.set_force_matrix_spec(prefix, spec))
    pipe.register_external_source(f"{prefix}rgb", (480, 640), fps=30.0)
    pipe.start_recording(f"{prefix}rgb", task_name="lite_gripper2",
                         device_meta=[{"key": "gripper:FAKE02",
                                       "kind": "gripper", "name": "UMI 夹爪",
                                       "serial": "FAKE02",
                                       "slots": [f"{prefix}rgb"]}])

    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    matrix = FakeGripperBridge.make_matrix(7)
    t0 = time.time()
    state = {"n": 0}

    def feed():
        state["n"] += 1
        rgb[..., 1] = (state["n"] * 3) & 255
        pipe.write_external_frame(f"{prefix}rgb", rgb.copy(), hardware_ns=0)
        if pipe._writer is None:
            return
        ns = time.monotonic_ns()
        pipe.write_tactile_force("left", [1.5, 2.5, 3.5], prefix=prefix,
                                 capture_ns=ns)
        pipe.write_tactile_force("right", [4.5, 5.5, 6.5], prefix=prefix,
                                 capture_ns=ns)
        if state["n"] % 5 == 0:
            pipe.write_tactile_force_matrix(
                "left", encode_gripper_force_matrix(matrix, spec),
                prefix=prefix, capture_ns=ns)
            pipe.write_tactile_force_matrix(
                "right", encode_gripper_force_matrix(matrix, spec),
                prefix=prefix, capture_ns=ns)
        pipe.write_gripper_state([55.0, 1.0, 900.0], prefix=prefix)
        pipe.write_slam_trajectory([0.1, 0.2, 0.3, 0.0, 0.0, 0.0, 1.0,
                                    float(state["n"]) * 0.03], prefix=prefix,
                                   timestamp=float(state["n"]) * 0.03,
                                   host_ns=time.monotonic_ns())
        if time.time() - t0 >= DURATION_E:
            feed_timer.stop()
            pipe.finish_recording("")

    feed_timer = QTimer()
    feed_timer.timeout.connect(feed)
    feed_timer.start(30)

    t_end = time.time() + 20
    while not finished and time.time() < t_end:
        app.processEvents()
        time.sleep(0.01)
    assert finished, "未收到 recording_finished"
    task_dir = finished[0]
    ep = pipe.last_episode_index

    tbl = pq.read_table(pooled_data_parquet_path(task_dir, ep))
    cols = tbl.column_names
    for side in ("left", "right"):
        for suffix in ("force", "force_ns", "force_matrix", "force_matrix_ns"):
            name = f"observation.{prefix}gripper_{side}_{suffix}"
            assert name in cols, f"缺 rig2 列 {name}（有: {cols}）"
    for bare in ("observation.gripper_state", "observation.slam_trajectory",
                 "observation.gripper_left_force"):
        assert bare not in cols, f"rig2 录制不该有裸 rig1 列 {bare}"
    # rig2 的逐点宿主时刻列：与轨迹同序等长（下标即配对）是本列的硬契约
    ns_col = f"observation.{prefix}slam_trajectory_ns"
    assert ns_col in cols, f"缺 rig2 时刻列 {ns_col}（有: {cols}）"
    traj2 = tbl.column(f"observation.{prefix}slam_trajectory").to_pylist()
    ns2 = tbl.column(ns_col).to_pylist()
    assert all(len(a) // 8 == len(b) for a, b in zip(traj2, ns2)), \
        "rig2 轨迹点列与时刻列长度不同步"
    assert any(ns2), "rig2 时刻列全空（写了 host_ns 却没落盘）"
    assert all(all(v > 0 for v in b) for a, b in zip(traj2, ns2) if a), \
        "rig2 有点的行出现了 0 时刻（与「0=未知」的约定混淆）"
    info = json.load(open(pooled_info_path(task_dir), encoding="utf-8"))
    feat = info["features"][
        f"observation.{prefix}gripper_left_force_matrix"]
    expect = settings.GRIPPER_FORCE_MATRIX_SCALES.get(spec) or 1
    assert feat["scale"] == expect, f"rig2 倍率未登记: {feat} != {expect}"
    print(f"  E: rig2 前缀列全在（{tbl.num_rows} 行），"
          f"无 rig1 裸列，features.scale={feat['scale']}")
    print("  E: PASS")
    return 0


# ── 路径 F: 编码器搬家回归（主程序 re-export 同一函数对象）──

def path_f():
    try:
        import ui.main_window as mw
    except Exception as exc:
        print(f"  F: 跳过（ui.main_window 不可导入: {type(exc).__name__}: {exc}）")
        return 0
    assert mw.encode_gripper_force_matrix is encode_gripper_force_matrix, \
        "ui.main_window 未从 core.gripper_codec re-export（出现第二份实现）"
    assert (mw.describe_gripper_matrix_spec
            is __import__("core.gripper_codec", fromlist=["x"]
                          ).describe_gripper_matrix_spec), \
        "describe_gripper_matrix_spec 未 re-export"
    print("  F: ui.main_window 的编码器就是 core.gripper_codec 那个函数对象")
    print("  F: PASS")
    return 0


def main():
    for fn in (path_d, path_e, path_f):
        rc = fn()
        if rc:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
