"""极简采集版无硬件冒烟测试（离屏 Qt，两条路径）。

路径 A（UI + 假 D435 worker 注入）:
    LiteWindow(d435_worker_cls=FakeD435Worker) → 开启 D435 → 预览 ring
    收到 RGB/深度帧 → 录制 5s → 停止 → pyarrow 校验 mp4/parquet/info.json。

路径 B（纯管线，无 UI）:
    CameraPipeline + 外部帧源/深度伪相机 + 假手套数据写入 →
    校验 parquet 含手套触觉列 + IMU 四元数列、hand_pose 占位列全零
    （lite 无骨架解算，egodata_writer 恒写零占位）。

路径 C（UI + 假 UVC，无 D435）:
    LiteWindow(uvc_capture=FakeCapture) → 开启 UVC → 录制守卫允许
    UVC 作主视频源 → 校验 uvc_rgb 视频 / parquet / info.json devices。

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/lite_smoke_test.py
"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pyarrow.parquet as pq
from PyQt5.QtCore import QCoreApplication, QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication

from config import settings
from core.device_detector import DeviceInfo
from core.helpers import (episode_video_files, pooled_data_parquet_path,
                          pooled_info_path)
from core.pipeline import CameraPipeline
from ui.lite_window import LiteUploadManager, LiteWindow

OUT_ROOT_A = "/tmp/lite_smoke_ui"
OUT_ROOT_B = "/tmp/lite_smoke_pipe"
OUT_ROOT_C = "/tmp/lite_smoke_uvc"
DURATION_A = 5.0
DURATION_B = 4.0
DURATION_C = 3.5


def wait(app, ms):
    t_end = time.time() + ms / 1000.0
    while time.time() < t_end:
        app.processEvents()
        time.sleep(0.01)


# ── 假 D435 worker（信号口径与 core.d435_camera.D435Worker 一致）──

class FakeD435Worker(QObject):
    frames_ready = pyqtSignal(str, np.ndarray, object, list)
    error_occurred = pyqtSignal(str)
    status_changed = pyqtSignal(str)

    def __init__(self, width=None, height=None, fps=None, parent=None,
                 rgb_width=None, rgb_height=None, serial=None,
                 model_name=None, rgb_slot=None, depth_slot=None,
                 exposure=None, **kwargs):
        super().__init__(parent)
        self._rgb_slot = rgb_slot
        self._depth_slot = depth_slot
        self._rgb = np.zeros((rgb_height, rgb_width, 3), dtype=np.uint8)
        self._depth = np.full((height, width), 1000, dtype=np.uint16)
        self._i = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._emit_frames)
        self._timer.start(33)

    def _emit_frames(self):
        self._i += 1
        self._rgb[..., 0] = (self._i * 5) & 255
        self.frames_ready.emit(self._rgb_slot, self._rgb.copy(), 0, [])
        self._depth = (self._depth + 1) % 3000 + 500
        self.frames_ready.emit(self._depth_slot, self._depth.copy(), 0, [])

    def get_calibration(self):
        return None

    def start(self):
        pass

    def stop(self):
        self._timer.stop()


# ── 假 UVC capture（cv2.VideoCapture 鸭子替身，注入 LiteUvcPump）──

class FakeCapture:
    def __init__(self):
        self._i = 0
        self._frame = np.zeros((480, 640, 3), dtype=np.uint8)

    def isOpened(self):
        return True

    def read(self):
        self._i += 1
        self._frame[..., 2] = (self._i * 7) & 255
        return True, self._frame.copy()

    def set(self, *args):
        return True

    def release(self):
        pass


# ── 路径 A: UI + 假 D435 ──

def path_a():
    shutil.rmtree(OUT_ROOT_A, ignore_errors=True)
    app = QApplication(sys.argv)
    settings.UPLOAD_AUTO_SYNC = False   # 测试期间不触发真实上传
    settings.ENCODER_PROBE_ENABLED = False   # lite 口径：直接 x264，测试提速

    pipe = CameraPipeline(output_dir=OUT_ROOT_A)
    finished = []
    pipe.recording_finished.connect(lambda sid, path: finished.append(path))
    win = LiteWindow(
        pipeline=pipe,
        upload_manager=LiteUploadManager("http://127.0.0.1:9"),
        d435_worker_cls=FakeD435Worker)
    win.show()
    wait(app, 200)

    dev = DeviceInfo(key="d435:FAKE01", kind="d435",
                     display_name="Intel RealSense D435", serial="FAKE01")
    assert win._open_d435(dev), "假 D435 开启失败"
    wait(app, 1500)
    assert win._ring.get("d435_rgb") is not None, "预览 ring 未收到 RGB 帧"
    assert win._ring.get("d435_depth") is not None, "预览 ring 未收到深度帧"
    print(f"  A: 预览 ring 收到帧 (rgb {win._ring['d435_rgb'].shape}, "
          f"depth {win._ring['d435_depth'].shape})")

    win._task_edit.setText("lite_smoke")
    win._start_recording()
    assert pipe.is_recording, "录制未启动"
    wait(app, int(DURATION_A * 1000))
    win._stop_recording()
    t_end = time.time() + 10
    while not finished and time.time() < t_end:
        wait(app, 50)
    assert finished, "未收到 recording_finished"
    task_dir = finished[0]
    ep = pipe.last_episode_index
    assert ep > 0, "episode 序号异常"
    win.close()
    wait(app, 100)

    # 视频/parquet/info.json 结构校验
    videos = episode_video_files(task_dir, ep)
    assert "d435_rgb" in videos, f"RGB 视频缺失: {videos}"
    assert "d435_depth" in videos, f"深度视频缺失: {videos}"
    for key in ("d435_rgb", "d435_depth"):
        assert os.path.getsize(videos[key]) > 1000, f"{key} 视频过小"
    parquet = pooled_data_parquet_path(task_dir, ep)
    tbl = pq.read_table(parquet)
    assert tbl.num_rows > 0, "parquet 无行"
    info = json.load(open(pooled_info_path(task_dir), encoding="utf-8"))
    kinds = [d.get("kind") for d in info.get("devices", [])]
    assert "d435" in kinds, f"info.json devices 缺 d435: {info.get('devices')}"
    print(f"  A: 录制 {DURATION_A}s → {len(videos)} 个视频 / "
          f"{tbl.num_rows} 行 parquet / info.json devices={kinds}")
    print("  A: PASS")
    return 0


# ── 路径 B: 纯管线 + 假手套数据 ──

def path_b():
    shutil.rmtree(OUT_ROOT_B, ignore_errors=True)
    app = QCoreApplication([])
    settings.UPLOAD_AUTO_SYNC = False
    settings.ENCODER_PROBE_ENABLED = False   # lite 口径：直接 x264，测试提速

    pipe = CameraPipeline(output_dir=OUT_ROOT_B)
    finished = []
    pipe.recording_finished.connect(lambda sid, path: finished.append(path))
    pipe.register_external_source("d435_rgb", (720, 1280), fps=15)
    pipe.set_depth_camera("d435_depth", (480, 848), fps=15,
                          master_slot="d435_rgb",
                          heatmap_near_mm=300, heatmap_far_mm=4000)
    pipe.register_sensor("right_glove")
    pipe.start_recording("d435_rgb", task_name="lite_pipe",
                         device_meta=[{"key": "d435:FAKEB", "kind": "d435",
                                       "name": "Fake", "serial": "FAKEB",
                                       "slots": ["d435_rgb", "d435_depth"]}])

    rgb = np.zeros((720, 1280, 3), dtype=np.uint8)
    depth = np.full((480, 848), 1000, dtype=np.uint16)
    tactile = (np.random.rand(16, 16) * 4000).astype(np.float32)
    quats = np.tile(np.array([1, 0, 0, 0], dtype=np.float32), (16, 1))
    valid = np.ones(16, dtype=np.float32)
    t0 = time.time()

    def feed():
        nonlocal rgb, depth
        rgb[..., 0] = (rgb[..., 0].astype(int) + 1) & 255
        depth = (depth + 1) % 3000 + 500
        pipe.write_external_frame("d435_rgb", rgb.copy(), hardware_ns=0)
        pipe.write_depth(depth.copy(), depth_slot="d435_depth")
        # write_sensor 需 writer 就绪（start_recording 后台建 writer）
        if pipe._writer is not None:
            pipe.write_sensor(tactile, sensor_name="right_glove")
            pipe.write_glove_imu("right_glove", quats, valid)
        if time.time() - t0 >= DURATION_B:
            feed_timer.stop()
            pipe.finish_recording("")

    feed_timer = QTimer()
    feed_timer.timeout.connect(feed)
    feed_timer.start(30)

    t_end = time.time() + 15
    while not finished and time.time() < t_end:
        app.processEvents()
        time.sleep(0.01)
    assert finished, "未收到 recording_finished"
    task_dir = finished[0]
    ep = pipe.last_episode_index
    assert ep > 0

    tbl = pq.read_table(pooled_data_parquet_path(task_dir, ep))
    cols = tbl.column_names
    assert "observation.right_glove" in cols, f"缺触觉列: {cols}"
    assert "observation.right_glove_imu_quat" in cols, f"缺 IMU 四元数列"
    assert "observation.right_glove_imu_valid" in cols, f"缺 IMU 有效掩码列"
    # lite 无骨架解算 → hand_pose 占位列恒在但必须全零
    for pose in (settings.HAND_POSE_LEFT, settings.HAND_POSE_RIGHT):
        name = f"observation.{pose}"
        assert name in cols, f"缺 hand_pose 占位列 {name}"
        flat = [v for row in tbl.column(name).to_pylist() for v in row]
        assert all(v == 0.0 for v in flat), f"{name} 应全零（无解算）"
    # 触觉列有非零数据（噪声门可能压掉个别帧，取整列判断）
    tactile_all = np.asarray(
        [v for row in tbl.column("observation.right_glove").to_pylist()
         for v in row])
    assert np.any(tactile_all != 0), "触觉列全零（写入失败）"
    print(f"  B: {tbl.num_rows} 行 parquet，触觉/IMU 列在位，"
          f"hand_pose 占位全零")
    print("  B: PASS")
    return 0


# ── 路径 C: UI + 假 UVC（无 D435，UVC 作主视频源）──

def path_c():
    shutil.rmtree(OUT_ROOT_C, ignore_errors=True)
    app = QApplication(sys.argv)
    settings.UPLOAD_AUTO_SYNC = False   # 测试期间不触发真实上传
    settings.ENCODER_PROBE_ENABLED = False

    pipe = CameraPipeline(output_dir=OUT_ROOT_C)
    finished = []
    pipe.recording_finished.connect(lambda sid, path: finished.append(path))
    win = LiteWindow(
        pipeline=pipe,
        upload_manager=LiteUploadManager("http://127.0.0.1:9"),
        d435_worker_cls=FakeD435Worker,
        uvc_capture=FakeCapture())
    win.show()
    wait(app, 200)

    dev = DeviceInfo(key="uvc:FAKE01", kind="uvc",
                     display_name="Fake UVC Camera", serial="",
                     video_index=0)
    assert win._open_uvc(dev), "假 UVC 开启失败"
    wait(app, 1500)
    assert win._ring.get("uvc_rgb") is not None, "预览 ring 未收到 UVC 帧"
    # 只开 UVC 时预览源应自动切到 uvc_rgb（否则界面显示"无信号"）
    assert win._src_combo.currentData() == "uvc_rgb", (
        f"预览源未自动切换: {win._src_combo.currentData()}")
    assert win._preview_frame() is not None, "预览取帧为空（应显示画面）"
    print(f"  C: 预览 ring 收到 UVC 帧 {win._ring['uvc_rgb'].shape}，"
          f"预览源自动切到 {win._src_combo.currentData()}")

    win._task_edit.setText("lite_uvc")
    win._start_recording()   # 未开 D435 → 守卫放行，UVC 作主视频源
    assert pipe.is_recording, "录制未启动"
    wait(app, int(DURATION_C * 1000))
    win._stop_recording()
    t_end = time.time() + 10
    while not finished and time.time() < t_end:
        wait(app, 50)
    assert finished, "未收到 recording_finished"
    task_dir = finished[0]
    ep = pipe.last_episode_index
    assert ep > 0, "episode 序号异常"
    win.close()
    wait(app, 100)

    videos = episode_video_files(task_dir, ep)
    assert "uvc_rgb" in videos, f"UVC 视频缺失: {videos}"
    assert os.path.getsize(videos["uvc_rgb"]) > 1000, "UVC 视频过小"
    tbl = pq.read_table(pooled_data_parquet_path(task_dir, ep))
    assert tbl.num_rows > 0, "parquet 无行"
    info = json.load(open(pooled_info_path(task_dir), encoding="utf-8"))
    kinds = [d.get("kind") for d in info.get("devices", [])]
    assert "uvc" in kinds, f"info.json devices 缺 uvc: {info.get('devices')}"
    print(f"  C: 录制 {DURATION_C}s → uvc_rgb 视频 / {tbl.num_rows} 行 parquet"
          f" / info.json devices={kinds}")
    print("  C: PASS")
    return 0


def main():
    for fn in (path_a, path_b, path_c):
        rc = fn()
        if rc:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
