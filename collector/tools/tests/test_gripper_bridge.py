"""GripperBridge P3 接线单元测试（无硬件，mock 租约/相机服务/触觉/SLAM）。

用法:
    QT_QPA_PLATFORM=offscreen venv/bin/python tools/tests/test_gripper_bridge.py

覆盖:
  - open 成功链：lease.acquire → select → flash 缓存 → RGB claim/线程 →
    触觉 start_connected → wait_ready → SlamProcessController 启动 +
    wait_sdk_ready → FaysRawStreamClient → opened；RGB/触觉/双目/位姿信号投递
  - 双目拆包：side_by_side（1280×400 左右拼合）与 paired_packets
    （640×400 相邻两包配对，IMU 批随左目）
  - open 失败链：select 抛错 → error 信号 + 无 opened
  - wait_ready 超时 → error 信号（含两侧错误文案）
  - SLAM wait_sdk_ready 超时 → error 含 USB3 提示
  - close 逆序回收：raw client → slam.stop → RGB 线程 → tactile.stop →
    reservation → uvc.stop → lease.release
  - P5：rig2 独立物理核分区 / rig1 毒核 6 移出任务集（探针实测
    左 ORB 落 6 时空桶 23-29%，移出后 0.0%）/ 亲和范围不足回退
    rig1 表 / 双目 30fps 空桶看门狗（91 桶缺口告警一次、正常节奏
    不误计、reset 放行下一段）
"""

import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt5.QtCore import QCoreApplication

from core.gripper.bridge import GripperBridge

FAILS = []

# QCoreApplication 必须存活于整个测试进程：局部引用随 _run_open_flow 返回
# 被回收会销毁 app，挂起的排队信号（RGB 帧）随之丢失
APP = QCoreApplication.instance() or QCoreApplication([])


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL: {msg}")


def _pump_until(predicate, timeout=3.0):
    """泵主线程事件循环直到谓词成立或超时。"""
    app = APP
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class _FakeNode:
    def __init__(self, role):
        self.role = role
        self.video_index = 1 if role == "left" else 2
        self.flash_params = {"device_type": "bevel",
                             "fx_params": [1] * 5, "fy_params": [1] * 5,
                             "fz_params": [1] * 21, "ft_params": [1] * 20,
                             "fw_params": [1] * 9,
                             "flash_state": "programmed",
                             "calibration_source": "hardware-live-flash"}


class _FakeReservedCamera:
    def __init__(self, role):
        self.node = _FakeNode(role)
        self.transport = "libuvc-ipc"
        self.flash_params = self.node.flash_params
        self._camera = MagicMock()
        self._released = False

    def claim(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        if self.node.role == "decxin":
            frame = np.zeros((960, 1280, 3), dtype=np.uint8)
        self._camera.read.return_value = (True, frame)
        return self._camera, frame, (480, 640, 30.0, "MJPG")

    def release(self):
        self._released = True


class _FakeReservation:
    def __init__(self):
        self._cameras = {role: _FakeReservedCamera(role)
                         for role in ("left", "right", "decxin")}
        self.released = False

    def camera(self, role):
        return self._cameras[role]

    def release_unclaimed(self):
        self.released = True
        for cam in self._cameras.values():
            cam.release()


def _selected_dict():
    return {
        "esp_serial": "ESP-SN",
        "esp_tty": "/dev/ttyACM0",
        "product_serial": "3500000262300098",
        "physical_usb_path": "1-3.4.1",
        "usb_speed_mbps": 5000.0,
        "ipc_dir": "/dev/shm/ksq-gripper-test",
        "orb_binary": "/fake/orb_binary",
        "orb_yaml": "/fake/s80m_orb.yaml",
        "runtime_config": "/fake/runtime.yaml",
        "trajectory": "/fake/traj.txt",
        "work_dir": "/fake/work",
    }


def _stereo_payload(width, height, channels=3):
    step = width * channels
    return np.zeros((height, step), dtype=np.uint8).tobytes()


def _run_open_flow(fail_select=None, wait_ready=True, ready_errors=(),
                   slam_ready=True):
    """跑一次 open 流程，返回 (bridge, fakes)。"""
    app = APP
    bridge = GripperBridge()
    stats = {"rgb": 0, "rgb_ns": [], "stereo": [], "tactile": [], "pose": [],
             "state": [], "opened": 0, "error": None}
    fakes = {}

    reservation = _FakeReservation()
    fakes["reservation"] = reservation

    fake_tactile = MagicMock()
    fake_tactile.PROCESS_READY_TIMEOUT = 10.0
    fake_tactile.wait_ready.return_value = wait_ready
    fake_tactile.snapshot.return_value = MagicMock(
        left=MagicMock(error=ready_errors[0] if len(ready_errors) > 0
                       else None),
        right=MagicMock(error=ready_errors[1] if len(ready_errors) > 1
                        else None),
    )
    fakes["tactile"] = fake_tactile
    fakes["uvc"] = MagicMock()

    fake_slam = MagicMock()
    fake_slam.wait_sdk_ready.return_value = slam_ready
    fakes["slam"] = fake_slam
    fake_raw = MagicMock()
    fakes["raw"] = fake_raw
    # P4 串口链（无硬件）：controller 握手成功，worker/serial 不触真串口
    fake_serial = MagicMock()
    fake_serial.last_error = None
    fake_worker = MagicMock()
    fake_controller = MagicMock()
    fake_controller.connect_sync.return_value = True
    fakes["serial"] = fake_serial
    fakes["worker"] = fake_worker
    fakes["controller"] = fake_controller

    def fake_select(provider):
        fakes["assignment"] = provider()
        if fail_select is not None:
            raise fail_select
        return reservation

    uvc_instance = MagicMock()
    uvc_instance.select.side_effect = fake_select
    fakes["uvc_instance"] = uvc_instance

    discovery = MagicMock()
    fakes["discovery"] = discovery

    bridge.rgb_frame_ready.connect(
        lambda _f, _t: (stats.__setitem__("rgb", stats["rgb"] + 1),
                        stats["rgb_ns"].append(_t)))
    bridge.stereo_frame_ready.connect(
        lambda slot, _f, _t: stats["stereo"].append(slot))
    bridge.tactile_ready.connect(
        lambda s, h, f, m, ns: stats["tactile"].append(
            (s, tuple(f), m, ns)))
    bridge.pose_ready.connect(
        lambda p, q, t, ts: stats["pose"].append(
            (tuple(p), tuple(q), tuple(t), ts)))
    bridge.gripper_state_ready.connect(
        lambda payload: stats["state"].append(payload))
    bridge.opened.connect(lambda: stats.__setitem__(
        "opened", stats["opened"] + 1))
    bridge.error.connect(lambda m: stats.__setitem__("error", m))

    selected = _selected_dict()
    with patch("core.gripper.bridge.SingleFaysLease") as lease_cls, \
         patch("core.gripper.bridge.TactileDiscovery",
               return_value=discovery) as disc_cls, \
         patch("core.gripper.bridge.TactileProcessManager",
               return_value=fake_tactile) as tactile_cls, \
         patch("core.gripper.bridge.UvcCameraServiceManager",
               return_value=uvc_instance) as uvc_cls, \
         patch("core.gripper.bridge.SlamProcessController",
               return_value=fake_slam) as slam_cls, \
         patch("core.gripper.bridge.FaysRawStreamClient",
               return_value=fake_raw) as raw_cls, \
         patch("core.gripper.bridge.GripperSerial",
               return_value=fake_serial) as serial_cls, \
         patch("core.gripper.bridge.SerialCommandWorker",
               return_value=fake_worker) as worker_cls, \
         patch("core.gripper.bridge.GripperConnectionController",
               return_value=fake_controller) as controller_cls:
        fake_lease = MagicMock()
        fake_lease.acquire.return_value = selected
        lease_cls.return_value = fake_lease
        fakes["lease"] = fake_lease
        fakes["slam_cls"] = slam_cls
        fakes["raw_cls"] = raw_cls
        fakes["controller_cls"] = controller_cls
        bridge.open("ESP-SN")
        _pump_until(lambda: stats["opened"] or stats["error"], timeout=10.0)
    fakes["stats"] = stats
    return bridge, fakes


def main():
    print("── 1. open 成功链 + RGB/触觉/双目/位姿信号 ──")
    bridge, fakes = _run_open_flow()
    stats = fakes["stats"]
    selected = _selected_dict()
    fakes["lease"].acquire.assert_called_once_with("ESP-SN")
    check(fakes["assignment"]["esp32"]["serial"] == selected["esp_serial"],
          "assignment 由 selected（lease.acquire）构建")
    check(fakes["assignment"]["fays"]["product_serial"]
          == selected["product_serial"],
          "assignment.fays 携带 product_serial")
    check(stats["opened"] == 1 and stats["error"] is None,
          f"opened 且无 error: opened={stats['opened']} error={stats['error']}")
    check(stats["rgb"] >= 1, f"RGB 首帧送达 (rgb={stats['rgb']})")
    # hardware_ns 必须是**未截断**的 64 位宿主单调钟。曾经 rgb_frame_ready
    # 声明为 pyqtSignal(object, int)，PyQt5 按 C++ qint32 封送 → 每 2.147s
    # 翻符号变负、落盘后下游无法做任何时间对齐。断言 >2^31 才验得出这个坑
    # （等于 2^31 以下的值截断与否看不出来）。
    check(stats["rgb_ns"] and all(ns > (1 << 31) for ns in stats["rgb_ns"]),
          f"RGB hardware_ns 未被 int32 截断 (首帧={stats['rgb_ns'][:1]})")
    check(fakes["tactile"].start_connected.called,
          "触觉 start_connected 已调用")
    left_arg = fakes["tactile"].start_connected.call_args
    check(left_arg is not None
          and left_arg.kwargs.get("left") is fakes["reservation"].camera("left")
          and left_arg.kwargs.get("right") is fakes["reservation"].camera("right"),
          "start_connected 传 reservation 的 left/right 保留相机")
    check(fakes["discovery"].cache_external_flash_params.call_count == 2,
          "左右 Sightac Flash 参数各缓存一次")
    check(fakes["tactile"].wait_ready.called, "wait_ready 已等待")
    # SLAM 控制器构造参数来自 selected 全链
    slam_kwargs = fakes["slam_cls"].call_args.kwargs
    check(slam_kwargs.get("executable") == selected["orb_binary"]
          and slam_kwargs.get("settings") == selected["orb_yaml"]
          and slam_kwargs.get("device_config") == selected["runtime_config"]
          and slam_kwargs.get("trajectory") == selected["trajectory"]
          and slam_kwargs.get("work_dir") == selected["work_dir"],
          "SlamProcessController 构造参数取自 selected")
    check(fakes["slam"].wait_sdk_ready.called, "wait_sdk_ready 已等待")
    check(fakes["raw"].start.called, "FaysRawStreamClient 已启动")

    # 触觉结果投递（发布回调 → emitter 线程队列 → 信号）
    result_count = len(stats["tactile"])
    heatmap = np.zeros((240, 320, 3), dtype=np.uint8)
    matrix = np.zeros((250, 250, 3), dtype=np.float32)
    bridge._on_tactile_result("left", heatmap, (12.5, -3.0, 88.0), matrix)
    _pump_until(lambda: len(stats["tactile"]) == result_count + 1)
    check(len(stats["tactile"]) == result_count + 1
          and stats["tactile"][-1][:3] == ("left", (12.5, -3.0, 88.0), matrix),
          "tactile_ready 信号携带 side/force/matrix")
    # 第 4 个载荷是本样本的采集时刻，同样必须未被 int32 截断。
    tactile_ns = stats["tactile"][-1][3]
    check(isinstance(tactile_ns, int) and tactile_ns > (1 << 31),
          f"tactile_ready 携带 64 位采集时刻 ({tactile_ns})")

    # 双目 side_by_side：单包 1280×400 只取左半投左目（右目/IMU 不外送）
    stereo_base = len(stats["stereo"])
    bridge._on_stereo_packet(
        _stereo_payload(1280, 400), 2000, 3000, 7,
        1280, 400, 3, 1280 * 3, 0)
    _pump_until(lambda: len(stats["stereo"]) == stereo_base + 1)
    got = stats["stereo"][stereo_base:]
    check(bridge._stereo_mode == "side_by_side",
          f"首包判定 side_by_side (mode={bridge._stereo_mode})")
    check(got == ["gripper_stereo_left"],
          "side_by_side 只投左目槽")

    # 双目 vertical_stacked：640×800 上下拼合（真机实测），一包即一对
    bridge_v, fakes_v = _run_open_flow()
    stats_v = fakes_v["stats"]
    bridge_v._on_stereo_packet(
        _stereo_payload(640, 800), 2050, 3050, 7,
        640, 800, 3, 640 * 3, 0)
    _pump_until(lambda: len(stats_v["stereo"]) == 1)
    got_v = stats_v["stereo"]
    check(bridge_v._stereo_mode == "vertical_stacked",
          f"首包判定 vertical_stacked (mode={bridge_v._stereo_mode})")
    check(got_v == ["gripper_stereo_left"],
          "vertical_stacked 只投上半左目槽")
    bridge_v.deleteLater()

    # 双目 paired_packets：640×400 相邻两包配对（先左后右），右目包丢弃
    bridge2, fakes2 = _run_open_flow()
    stats2 = fakes2["stats"]
    bridge2._on_stereo_packet(
        _stereo_payload(640, 400), 2100, 3100, 8,
        640, 400, 3, 640 * 3, 0)
    _pump_until(lambda: len(stats2["stereo"]) == 0 or
                bridge2._stereo_mode is not None)
    check(bridge2._stereo_mode == "paired_packets"
          and len(stats2["stereo"]) == 0,
          "paired 首包挂起不发射")
    bridge2._on_stereo_packet(
        _stereo_payload(640, 400), 2200, 3200, 9,
        640, 400, 3, 640 * 3, 0)
    _pump_until(lambda: len(stats2["stereo"]) == 1)
    got2 = stats2["stereo"]
    check(got2 == ["gripper_stereo_left"],
          "paired 两包配对只发左目、右目包丢弃")
    bridge2.deleteLater()

    # SLAM 位姿投递（pos3 + quat4 + 轨迹元组 + timestamp）
    pose_count = len(stats["pose"])
    pose = SimpleNamespace(position=(1.0, 2.0, 3.0),
                           rotation=(0.0, 0.0, 0.0, 1.0), timestamp=0.5)
    bridge._on_slam_pose(pose)
    _pump_until(lambda: len(stats["pose"]) == pose_count + 1)
    check(stats["pose"][-1] == ((1.0, 2.0, 3.0), (0.0, 0.0, 0.0, 1.0),
                                ((1.0, 2.0, 3.0),), 0.5),
          "pose_ready 信号携带 pos/quat/traj/timestamp")
    # 第二个位姿追加进轨迹（同一元组缓存对象随新点重建）
    pose2 = SimpleNamespace(position=(2.0, 3.0, 4.0),
                            rotation=(0.0, 0.0, 0.0, 1.0), timestamp=0.6)
    bridge._on_slam_pose(pose2)
    _pump_until(lambda: len(stats["pose"]) == pose_count + 2)
    check(stats["pose"][-1][2] == ((1.0, 2.0, 3.0), (2.0, 3.0, 4.0)),
          "轨迹随位姿滚动累积")

    # P4 串口链接线：构造注入 on_board_update/on_grip_check，握手用 esp_tty
    controller_kwargs = fakes["controller_cls"].call_args.kwargs
    check(callable(controller_kwargs.get("on_board_update"))
          and callable(controller_kwargs.get("on_grip_check")),
          "GripperConnectionController 注入板更新/力检查回调")
    check(fakes["controller"].connect_sync.called
          and fakes["controller"].connect_sync.call_args.args
          == (selected["esp_tty"],),
          f"connect_sync 使用 esp_tty: {selected['esp_tty']!r}")

    # P4 GripState：板字段 ST=1 → 力超阈值 → 锁存 → gripper_state 事件
    bridge._on_board_update(MagicMock(
        board_fields={"ST": "1", "PCT": "50"}, updated="10:00:00"))
    bridge._latest_force["left"] = (0.0, 0.0, 400.0)
    bridge._on_grip_check()
    _pump_until(lambda: len(stats["state"]) >= 1)
    check(len(stats["state"]) >= 1
          and stats["state"][-1]["gripped"] is True
          and abs(stats["state"][-1]["pct"] - 50.0) < 0.01
          and abs(stats["state"][-1]["fz"] - 400.0) < 0.01,
          f"Fz 超阈值锁存: {stats['state'][-1] if stats['state'] else None}")

    # P4 力矩阵最新值 + 序号（泵线程轮询语义）
    matrix = np.zeros((250, 250, 3), dtype=np.float32)
    left_seq_before = bridge.matrix_seq("left")
    bridge._on_tactile_result("right", heatmap, (0.0, 0.0, 0.0), matrix)
    check(bridge.matrix_seq("right") == 1
          and bridge.latest_matrix("right") is matrix,
          "矩阵最新值/序号递增（右侧）")
    check(bridge.matrix_seq("left") == left_seq_before,
          "未收到的侧别序号不动")
    # 带时刻的版本（泵线程按此落盘）：ns 与矩阵在同一次加锁里取出、恒配对，
    # 且 ns 同样不得被 int32 截断。
    stamped = bridge.latest_matrix_stamped("right")
    check(stamped is not None and stamped[1] is matrix
          and stamped[0] > (1 << 31),
          f"latest_matrix_stamped 返回 (采集时刻, 矩阵) ({stamped and stamped[0]})")

    # close 逆序回收：串口 → raw → slam → RGB 线程 → 触觉 → 租约 → 服务
    bridge.close()
    check(fakes["controller"].close.called,
          "close 先回收串口控制器")
    check(fakes["raw"].stop.called
          and fakes["slam"].stop.called
          and fakes["reservation"].released
          and fakes["uvc_instance"].stop.called
          and fakes["tactile"].stop.called
          and fakes["lease"].release.called,
          "close 回收 raw/slam/reservation/uvc/tactile/lease")
    bridge.deleteLater()

    print("── 2. select 失败 → error 信号 ──")
    bridge2b, fakes2b = _run_open_flow(fail_select=RuntimeError("组不存在"))
    check(fakes2b["stats"]["error"] == "组不存在"
          and fakes2b["stats"]["opened"] == 0,
          f"error 透传: {fakes2b['stats']['error']!r}")
    check(not fakes2b["tactile"].start_connected.called,
          "select 失败后不启动触觉")
    bridge2b.deleteLater()

    print("── 3. wait_ready 超时 → error 含两侧文案 ──")
    bridge3, fakes3 = _run_open_flow(
        wait_ready=False, ready_errors=("左路初始化失败", None))
    err = fakes3["stats"]["error"]
    check(err is not None and "左路初始化失败" in err,
          f"错误包含侧别详情: {err!r}")
    bridge3.deleteLater()

    print("── 4. SLAM wait_sdk_ready 超时 → error 含 USB3 提示 ──")
    bridge5, fakes5 = _run_open_flow(slam_ready=False)
    err5 = fakes5["stats"]["error"]
    check(err5 is not None and "USB3" in err5,
          f"SLAM 超时错误含 USB3 提示: {err5!r}")
    check(not fakes5["raw"].start.called,
          "SLAM 未就绪不启动 raw 流")
    bridge5.deleteLater()

    print("── 5. 重复 open 拒绝 ──")
    bridge4 = GripperBridge()
    with patch("core.gripper.bridge.SingleFaysLease") as lease_cls, \
         patch("core.gripper.bridge.TactileDiscovery",
               return_value=MagicMock()), \
         patch("core.gripper.bridge.TactileProcessManager",
               return_value=MagicMock()), \
         patch("core.gripper.bridge.UvcCameraServiceManager",
               return_value=MagicMock()), \
         patch("core.gripper.bridge.SlamProcessController",
               return_value=MagicMock()), \
         patch("core.gripper.bridge.FaysRawStreamClient",
               return_value=MagicMock()), \
         patch("core.gripper.bridge.GripperSerial",
               return_value=MagicMock()), \
         patch("core.gripper.bridge.SerialCommandWorker",
               return_value=MagicMock()), \
         patch("core.gripper.bridge.GripperConnectionController",
               return_value=MagicMock()):
        fake_lease = MagicMock()
        fake_lease.acquire.return_value = _selected_dict()
        lease_cls.return_value = fake_lease
        bridge4.open("ESP-SN")
        try:
            bridge4.open("ESP-SN")
            check(False, "重复 open 应抛 RuntimeError")
        except RuntimeError:
            check(True, "重复 open 抛 RuntimeError")
    bridge4.close()
    bridge4.deleteLater()

    print("── 6. 双夹爪 stereo 槽名参数化 ──")
    bridge6 = GripperBridge(stereo_slot="gripper_2_stereo_left")
    stats6 = {"stereo": []}
    bridge6.stereo_frame_ready.connect(
        lambda slot, _f, _t: stats6["stereo"].append(slot))
    bridge6._on_stereo_packet(
        _stereo_payload(1280, 400), 2200, 3200, 9,
        1280, 400, 3, 1280 * 3, 0)
    _pump_until(lambda: len(stats6["stereo"]) == 1)
    check(stats6["stereo"] == ["gripper_2_stereo_left"],
          f"rig2 bridge 发自己的槽名: {stats6['stereo']}")
    # 未 open 过的 bridge close 早退；手动停 emitter 防解释器退出时核心转储
    bridge6._events.put(("_stop", ()))
    bridge6._emitter.join(timeout=2.0)
    bridge6.deleteLater()

    print("── 7. P5 双 rig CPU 分区 + 30fps 空桶看门狗 ──")
    all24 = {cpu for cpu in range(24)}
    only12 = {cpu for cpu in range(12)}
    with patch("os.sched_getaffinity", return_value=all24):
        bridge7 = GripperBridge(rig_index=2)
        check(bridge7._tactile_cpu_map == {"left": 1, "right": 3}
              and bridge7._slam_cpu_roles == {
                  "fays_input": (10,), "fays_prepare": (22,),
                  "fays_left_orb": (13,), "fays_right_orb": (15,),
                  "fays_track": (11,), "fays_background": (23,)},
              f"rig2 独立物理核分区: {bridge7._tactile_cpu_map} "
              f"{bridge7._slam_cpu_roles}")
        bridge7._events.put(("_stop", ()))
        bridge7._emitter.join(timeout=2.0)
        bridge7.deleteLater()
        bridge7a = GripperBridge(rig_index=1)
        check(bridge7a._slam_cpu_roles == {
                  "fays_input": (4,), "fays_prepare": (5,),
                  "fays_left_orb": (7,), "fays_right_orb": (8,),
                  "fays_track": (4,), "fays_background": (9,)}
              and bridge7a._tactile_cpu_map == {"left": 0, "right": 2},
              "rig1 任务集 {4,5,7,8,9}：毒核 6 移出、track 与 input 共享 4")
        bridge7a._events.put(("_stop", ()))
        bridge7a._emitter.join(timeout=2.0)
        bridge7a.deleteLater()
    with patch("os.sched_getaffinity", return_value=only12):
        # 构造器内选分区后经 _log 发提示（emitter 线程异步消费，
        # 信号侧时序不可断言，直接查 _cpu_note 与回退表）
        bridge7b = GripperBridge(rig_index=2)
        check(bridge7b._tactile_cpu_map == {"left": 0, "right": 2}
              and bridge7b._cpu_note is not None
              and "退回 rig1" in bridge7b._cpu_note,
              f"亲和范围不足回退 rig1 表并记提示: {bridge7b._cpu_note}")
        bridge7b._events.put(("_stop", ()))
        bridge7b._emitter.join(timeout=2.0)
        bridge7b.deleteLater()

    # 空桶看门狗：30fps 桶长 1/30s。第 1 包后隔 91 桶再来 1 包 →
    # elapsed=91、dropped=90、空桶率 99% ≥ 10% → 告警一次
    interval_ns = 33_333_333
    t0 = 1_000_000_000
    logs8 = []
    bridge8 = GripperBridge()
    bridge8.log.connect(logs8.append)
    bridge8._on_stereo_packet(
        _stereo_payload(1280, 400), 2000, t0, 1,
        1280, 400, 3, 1280 * 3, 0)
    bridge8._on_stereo_packet(
        _stereo_payload(1280, 400), 2000, t0 + 91 * interval_ns, 2,
        1280, 400, 3, 1280 * 3, 0)
    _pump_until(lambda: any("空桶率" in m for m in logs8))
    check(any("空桶率 99%" in m for m in logs8),
          f"91 桶缺口触发 30fps 告警: {[m for m in logs8 if '告警' in m]}")
    check(bridge8.stereo_drop_snapshot() == (90, 91),
          f"快照口径与 s80m 一致: {bridge8.stereo_drop_snapshot()}")
    # 正常 30fps 节奏 2 包（每包恰 1 桶）不新增空桶；reset 后重来一轮
    bridge8.reset_stereo_drop_watch()
    check(bridge8.stereo_drop_snapshot() == (0, 0), "重置清空统计")
    bridge8._on_stereo_packet(
        _stereo_payload(1280, 400), 2000, t0, 3,
        1280, 400, 3, 1280 * 3, 0)
    bridge8._on_stereo_packet(
        _stereo_payload(1280, 400), 2000, t0 + interval_ns, 4,
        1280, 400, 3, 1280 * 3, 0)
    check(bridge8.stereo_drop_snapshot() == (0, 1),
          f"正常节奏不误计空桶: {bridge8.stereo_drop_snapshot()}")
    # reset 后 alerted 放行：再来一段 91 桶缺口 → 第二次告警
    bridge8._on_stereo_packet(
        _stereo_payload(1280, 400), 2000, t0 + 92 * interval_ns, 5,
        1280, 400, 3, 1280 * 3, 0)
    _pump_until(lambda: sum(1 for m in logs8 if "空桶率" in m) >= 2)
    check(sum(1 for m in logs8 if "空桶率" in m) == 2,
          f"每段录制告警一次（不重复刷屏）: "
          f"{sum(1 for m in logs8 if '空桶率' in m)}")
    bridge8._events.put(("_stop", ()))
    bridge8._emitter.join(timeout=2.0)
    bridge8.deleteLater()

    if FAILS:
        print(f"\nFAILED: {len(FAILS)}")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("\nPASS: GripperBridge P3 接线测试全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
