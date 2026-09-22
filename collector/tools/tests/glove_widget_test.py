"""手套并入统一体系测试（mock BLE，无真机）。

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/glove_widget_test.py

覆盖:
  1. GloveWidget: 连接流程 → 仿生手掌渲染帧落画面 + write_sensor 列名正确
  2. 主窗口集成: data_ble 开关 → 网格画面 + 传感器注册 + device_meta；
     关闭 → 画面/注册/条目全部撤销
  3. 无数据蓝牙开关 → 占位文案（不进传感器注册）
  4. 旧底部传感器 dock 已移除（_sensor_dock 不存在）
"""
import os
import sys
import time
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication, QWidget

from config import settings
from config.i18n import tr

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        FAILS.append(msg)


class FakePipeline:
    """录制管线替身：记录 write_sensor / record_event 调用。"""
    is_recording = True

    def __init__(self):
        self.sensor_writes = []
        self.events = []
        self._sensor_names = []

    def write_sensor(self, data, ts, sensor_name=""):
        self.sensor_writes.append((sensor_name, data.copy()))

    def record_event(self, dev, ev):
        self.events.append((dev, ev))

    def register_sensor(self, name):
        if name not in self._sensor_names:
            self._sensor_names.append(name)

    def unregister_sensor(self, name):
        if name in self._sensor_names:
            self._sensor_names.remove(name)


class MockBLE(QObject):
    """SensorBLEEngine 替身：真实 Qt 信号 + 合成压力矩阵。"""
    connected = pyqtSignal(str)
    disconnected = pyqtSignal()
    fps_updated = pyqtSignal(float)
    calibration_progress = pyqtSignal(int)
    error_occurred = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.addr = None
        self.is_calibrating = False
        self.hardware_fps = 30.0
        self.base_noise_gate = 500
        self.dynamic_noise_ratio = 0.0
        self.spatial_filter_enabled = True
        self.drift_baseline_val = 0
        self.latest_data_ts_us = int(time.time() * 1_000_000)

    def connect_device(self, address):
        self.addr = address
        self.connected.emit(address)

    def disconnect(self):
        self.disconnected.emit()

    def process_frame(self):
        data = np.random.rand(16, 16).astype(np.float32) * 3000
        return data, float(data.max())


class FakeGlove(QWidget):
    """主窗口集成测试用的手套控件替身。"""
    created = []

    def __init__(self, slot, address, role, label, parent=None,
                 engine=None, on_log=None):
        super().__init__(parent)
        self.slot, self.address, self.role, self.label = slot, address, role, label
        self.engine = engine
        self.on_log = on_log
        self.started = None
        self.stopped = False
        self.pipeline = None
        FakeGlove.created.append(self)

    def start(self, addr):
        self.started = addr

    def stop(self):
        self.stopped = True

    def set_pipeline(self, p):
        self.pipeline = p


def _mk_dev(key, kind, display_name, address=""):
    from core.device_detector import DeviceInfo
    return DeviceInfo(key=key, kind=kind, display_name=display_name,
                      address=address)


def main():
    app = QApplication(sys.argv)

    # ── 1. GloveWidget 渲染 + 录制列名 ──
    print("── 1. GloveWidget 仿生手掌渲染 / write_sensor ──")
    import ui.glove_widget as gw
    orig_cls = gw.SensorBLEEngine
    gw.SensorBLEEngine = MockBLE
    try:
        w = gw.GloveWidget("sensor:ble:AA:11:22:33:44:55",
                           "AA:11:22:33:44:55", "right_glove", "右手手套")
        pipe = FakePipeline()
        w.set_pipeline(pipe)
        w.start()
        app.processEvents()
        check(w._engine.addr == "AA:11:22:33:44:55",
              "start() 发起连接指定 MAC")
        check(w.video_widget._status_text
              == tr("已连接: {}…", "AA:11:22:33:44:55"[:12]),
              f"连接状态文案: {w.video_widget._status_text}")
        w._render_tick()
        app.processEvents()
        check(w.video_widget._has_frame, "仿生手掌渲染帧落画面")
        # 30ms 渲染定时器可能在 processEvents 期间已先行写入 → 只看最近一条
        last = pipe.sensor_writes[-1] if pipe.sensor_writes else None
        check(last is not None
              and last[0] == "right_glove"
              and last[1].shape == (16, 16),
              f"write_sensor 列名/形状: {(last[0], last[1].shape) if last else '-'}")
        w.stop()
        app.processEvents()
        check(w.video_widget._status_text == tr("已断开"),
              f"stop() 状态文案: {w.video_widget._status_text}")
        check(pipe.events and pipe.events[-1] == ("right_glove", "disconnected"),
              f"连接/断开事件: {pipe.events}")
        # 左手套 → 左手判（触觉网格按传感器列名判手；两只手共用一套坐标、
        # 谁都不翻，见 core/render_engine.canonical_pressure_matrix）
        from core.render_engine import glove_side_of
        wl = gw.GloveWidget("sensor:ble:BB:22:33:44:55:66",
                            "BB:22:33:44:55:66", "left_glove", "左手套")
        check(w.side == "right" and wl.side == "left",
              f"role → 左右手判手: {w.side} / {wl.side}")
        check(glove_side_of("glove") == "right", "无左右标识默认右手")
    finally:
        gw.SensorBLEEngine = orig_cls

    # ── 2/3/4. 主窗口集成（开关 → 网格/注册/meta → 关闭撤销；占位；dock 移除） ──
    print("── 2-4. 主窗口集成 ──")
    import ui.main_window as mw
    mw.GloveWidget = FakeGlove
    # 隔离设备命名持久化（避免污染真实 data/device_names.json）
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp_path = tmp.name
    tmp.close()
    orig_names_file = settings.DEVICE_NAMES_FILE
    settings.DEVICE_NAMES_FILE = tmp_path
    win = mw.MainWindow()
    win.show()
    app.processEvents()
    try:
        check(not hasattr(win, "_sensor_dock"),
              "旧底部传感器 dock 已移除（无 _sensor_dock 属性）")

        glove = _mk_dev("ble:AA:11:22:33:44:55", "data_ble", "右手手套",
                        address="AA:11:22:33:44:55")
        win._on_device_toggled(glove, True)
        app.processEvents()
        slot = "sensor:ble:AA:11:22:33:44:55"
        check(slot in win.grid.slot_ids(), f"手套画面进主网格: {win.grid.slot_ids()}")
        entry = win._workers.get(glove.key, {})
        role = entry.get("sensor_column", "")
        check(entry.get("kind") == "data_ble" and role == "right_glove",
              f"worker 条目 kind/sensor_column: {entry}")
        check(role in win._pipeline._sensor_names,
              f"传感器列已注册: {win._pipeline._sensor_names}")
        meta = {d["key"]: d for d in win._build_device_meta()}
        check(meta[glove.key]["kind"] == "data_ble"
              and meta[glove.key]["slots"] == []
              and meta[glove.key].get("sensor_column") == "right_glove",
              f"device_meta 手套条目: {meta.get(glove.key)}")

        win._on_device_toggled(glove, False)
        app.processEvents()
        check(slot not in win.grid.slot_ids(), "关闭后网格画面移除")
        check(role not in win._pipeline._sensor_names, "关闭后传感器列注销")
        check(glove.key not in win._workers, "关闭后 worker 条目移除")

        # 无数据蓝牙占位
        ear = _mk_dev("ble:CC:33:44:55:66:77", "ble", "蓝牙耳机",
                      address="CC:33:44:55:66:77")
        win._on_device_toggled(ear, True)
        app.processEvents()
        bslot = f"ble:{ear.key}"
        bw = win.grid.camera_widget(bslot)
        check(bslot in win.grid.slot_ids()
              and bw is not None
              and bw.video_widget._status_text == tr("该设备无可视化数据"),
              f"无数据蓝牙占位: {bw.video_widget._status_text if bw else '-'}")
        check(not any(n in win._pipeline._sensor_names
                      for n in settings.SENSOR_NAMES),
              "占位设备不进传感器注册")
        win._on_device_toggled(ear, False)
        app.processEvents()
        check(bslot not in win.grid.slot_ids(), "占位关闭后移除")

        # teardown 全清
        win._on_device_toggled(glove, True)
        app.processEvents()
        win._teardown_all_workers()
        check(not win._workers and not win.grid.slot_ids(),
              "teardown 清理手套画面与条目")

        # ── 5. USB (Type-C) 手套：仿生手掌 + 骨架画面进主网格 ──
        print("── 5. USB 手套画面进主网格（GloveWidget + UsbGloveEngine） ──")
        import core.device_detector as dd
        import core.usb_glove_engine as uge
        orig_prefer = dd.usb_glove_prefer_side
        orig_engine = uge.UsbGloveEngine
        dd.usb_glove_prefer_side = lambda serial: "left_glove"

        class MockUSBEngine:
            instances = []

            def __init__(self, port=""):
                self.port = port
                MockUSBEngine.instances.append(self)

            def connect_device(self, address=""):
                self.address = address

            def disconnect(self):
                pass

        uge.UsbGloveEngine = MockUSBEngine
        FakeGlove.created = []
        try:
            usb_dev = _mk_dev("usbglove:2067376F3032", "usb_glove",
                              "USB 手套·左手", address="/dev/ttyACM0")
            win._on_device_toggled(usb_dev, True)
            app.processEvents()
            usb_slot = "sensor:usbglove:2067376F3032"
            check(usb_slot in win.grid.slot_ids(),
                  f"USB 手套画面进主网格: {win.grid.slot_ids()}")
            entry = win._workers.get(usb_dev.key, {})
            check(entry.get("kind") == "data_ble"
                  and entry.get("sensor_column") == "left_glove",
                  f"worker 条目 kind/角色: {entry.get('kind')}/"
                  f"{entry.get('sensor_column')}")
            check(len(FakeGlove.created) == 1
                  and FakeGlove.created[0].slot == usb_slot
                  and FakeGlove.created[0].role == "left_glove",
                  "GloveWidget 以 slot/角色创建")
            w = FakeGlove.created[0]
            check(w.started == "/dev/ttyACM0", f"画面控件已启动: {w.started}")
            check(w.pipeline is win._pipeline, "画面控件绑定录制管线")
            check(isinstance(w.engine, MockUSBEngine)
                  and w.engine.port == "/dev/ttyACM0",
                  "UsbGloveEngine 以串口路径创建")
            check(w.on_log == win._log,
                  "on_log 接主窗口日志（与旧 GloveDataPump 同口径）")
            check("left_glove" in win._pipeline._sensor_names,
                  "传感器列已注册")
            # 幂等：重复打开不建第二个控件
            win._on_device_toggled(usb_dev, True)
            app.processEvents()
            check(len(FakeGlove.created) == 1, "重复打开幂等（单控件）")
            # 关闭：控件停、画面撤、列注销、条目移除
            win._on_device_toggled(usb_dev, False)
            app.processEvents()
            check(w.stopped, "关闭时画面控件停止")
            check(usb_slot not in win.grid.slot_ids(), "关闭后网格画面移除")
            check("left_glove" not in win._pipeline._sensor_names,
                  "关闭后传感器列注销")
            check(usb_dev.key not in win._workers, "关闭后 worker 条目移除")
        finally:
            dd.usb_glove_prefer_side = orig_prefer
            uge.UsbGloveEngine = orig_engine
    finally:
        settings.DEVICE_NAMES_FILE = orig_names_file
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        win.close()
        app.processEvents()

    print("── 6. GloveDataPump._tick: IMU + 骨架关键点写入 ──")
    import ui.glove_widget as gwp

    class TickEngine:
        def __init__(self):
            self.imu = (np.zeros(64, np.float32), np.ones(16, np.float32),
                        123456789)
            self.latest_data_ts_us = 999

        def process_frame(self):
            return np.zeros((16, 16), np.float32), 0.0

        def latest_imu(self):
            return self.imu

    class RecPipeline(FakePipeline):
        def __init__(self):
            super().__init__()
            self.imu_writes = []
            self.kpts_writes = []

        def write_glove_imu(self, sn, q, v):
            self.imu_writes.append(sn)

        def write_glove_keypoints(self, sn, k):
            self.kpts_writes.append((sn, k))

    class FakeSolver:
        def __init__(self, kpts):
            self.kpts = kpts

        def available(self):
            return self.kpts is not None

        def process(self, q, v, ts):
            return self.kpts

    kpts63 = np.arange(63, dtype=np.float32) / 63.0
    pipe = RecPipeline()
    pump = gwp.GloveDataPump("slot", "left_glove", TickEngine(),
                             on_log=lambda m: None)
    pump._running = True
    pump.set_pipeline(pipe)
    pump._solver = FakeSolver(kpts63)
    pump._tick()
    check(len(pipe.sensor_writes) == 1
          and pipe.sensor_writes[0][0] == "left_glove",
          "_tick 写触觉列")
    check(pipe.imu_writes == ["left_glove"], "_tick 写 IMU 列")
    check(pipe.kpts_writes == [("left_glove", kpts63)]
          and np.array_equal(pipe.kpts_writes[0][1], kpts63),
          "_tick 写骨架关键点")
    pipe2 = RecPipeline()
    pump2 = gwp.GloveDataPump("slot", "left_glove", TickEngine(),
                              on_log=lambda m: None)
    pump2._running = True
    pump2.set_pipeline(pipe2)
    pump2._solver = FakeSolver(None)
    pump2._tick()
    check(pipe2.kpts_writes == [] and pipe2.imu_writes == ["left_glove"],
          "解算器不可用: 只写 IMU 不写骨架")

    print("── 7. GloveWidget USB: 骨架小窗叠加 + write_glove_keypoints ──")

    class RenderUSBEngine:
        """UsbGloveEngine 替身：渲染所需的全部属性 + latest_imu。"""

        def __init__(self):
            self.imu = (np.zeros(64, np.float32), np.ones(16, np.float32),
                        123456789)
            self.latest_data_ts_us = 999
            self.is_calibrating = False
            self.hardware_fps = 60.0
            self.base_noise_gate = 500
            self.dynamic_noise_ratio = 0.0
            self.spatial_filter_enabled = True
            self.drift_baseline_val = 0
            # UsbGloveEngine 独有：覆盖条据此多画一行（BLE 引擎没有，
            # 状态带位置也随之不同 —— 见 GloveWidget._display_frame）
            self.imu_present_count = 16
            self.tactile_fps = 30.0

        def process_frame(self):
            return np.random.rand(16, 16).astype(np.float32) * 3000, 3000.0

        def latest_imu(self):
            return self.imu

    class RecPipeline2(FakePipeline):
        def __init__(self):
            super().__init__()
            self.imu_writes = []
            self.kpts_writes = []

        def write_glove_imu(self, sn, q, v):
            self.imu_writes.append(sn)

        def write_glove_keypoints(self, sn, k):
            self.kpts_writes.append((sn, k))

    logs = []
    wgt = gw.GloveWidget("sensor:usbglove:X", "/dev/ttyACM0", "right_glove",
                         "USB 右手套", engine=RenderUSBEngine(),
                         on_log=lambda m: logs.append(m))
    pipe3 = RecPipeline2()
    wgt.set_pipeline(pipe3)
    wgt._running = True
    kpts213 = np.arange(63, dtype=np.float32).reshape(21, 3) / 63.0
    wgt._solver = FakeSolver(kpts213)
    wgt._render_tick()
    check(pipe3.kpts_writes == [("right_glove", kpts213)]
          and np.array_equal(pipe3.kpts_writes[0][1], kpts213),
          "_render_tick 写骨架关键点（录制中）")
    check(pipe3.imu_writes == ["right_glove"], "_render_tick 写 IMU 列")
    pm = wgt.video_widget._pixmap
    check(pm is not None, "画面帧已落 widget")
    qimg = pm.toImage()
    ptr = qimg.constBits()
    ptr.setsize(qimg.byteCount())
    frame = np.frombuffer(ptr, np.uint8).reshape(
        qimg.height(), qimg.bytesPerLine())[:, :qimg.width() * 3].reshape(
        qimg.height(), qimg.width(), 3)
    # USB 画面 = 触觉面板 1280x720 + 面板**下方**的状态带 68px：三行状态
    # 画在 (0,0) 时条高 68px > 网格上边距 54px，会切掉最上一行（y=15 指尖）
    # 左起约 6 格的顶部 14px —— 挪到面板外才不会压住任何格区
    check(frame.shape == (788, 1280, 3),
          f"USB 画面 = 面板 720 + 状态带 68: {frame.shape}")
    seg = frame[54:68, 54:314]        # 网格首行左起约 6 格的顶部 14px
    check(not np.any(np.all(seg == 0, axis=-1)),
          "状态带不再压住网格首行（该区域无纯黑像素）")
    band = frame[720:788, 0:300]
    check(int(band.max()) > 40, f"面板下方画出了状态带（max {int(band.max())}）")
    # 骨架小窗位置随触觉网格改到右下空白区（见 GloveWidget._SKEL_POS）
    region = frame[400:700, 930:1270]
    check(int(region.max()) > 40,
          f"右下角骨架小窗出现（区域 max {int(region.max())}）")
    # 旧位置已被触觉网格占用：网格底色/格线都比纯黑亮
    check(int(frame[412:712, 8:348].max()) > 40, "左下角已有触觉网格内容")
    # 解算器不可用: 不写骨架、不叠小窗
    wgt._kpts = None
    wgt._solver = FakeSolver(None)
    pipe4 = RecPipeline2()
    wgt.set_pipeline(pipe4)
    wgt._render_tick()
    check(pipe4.kpts_writes == [] and pipe4.imu_writes == ["right_glove"],
          "解算器不可用: 只写 IMU 不写骨架")

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} 项未通过")
        return 1
    print("PASS: 手套并入统一体系测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
