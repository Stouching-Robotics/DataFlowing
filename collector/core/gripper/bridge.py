"""夹爪 rig 与主程序的 Qt 桥（后台线程采集 → 队列信号回主线程）。

各采集线程（SLAM stdout、raw 双目流、RGB 读取、触觉结果泵）只向本对象的
内部事件队列投递；由**唯一**的 emitter 线程串行调用 pyqtSignal.emit ——
多线程并发 emit 在 PyQt5 下存在竞态（实测 RGB 线程与主线程同时 emit
触发段错误），因此所有信号出口收敛到一个线程。主窗口用队列连接把信号
调度回 GUI 线程。open/close 由主窗口的 _open_gripper/_close_gripper 按
生命周期顺序调用（见 main_window）。

帧类事件（rgb/tactile/stereo_left）走 latest-wins 单槽：
GUI 卡顿时只丢旧帧不积压内存；控制类事件（log/error/opened/closed/
pose/state）入队列保序。触觉录制数据不走本桥信号（P4 的 record_queue
直连泵线程）。

P2 已接入：UvcCameraServiceManager 相机组选择 → Sightac Flash 参数缓存 →
DECXIN RGB 常驻读取线程 → 触觉输入/计算双进程（spawn）。
P3 已接入：SingleFaysLease.acquire 全链（USB3 速度校验/官方探针/flock）→
SlamProcessController（wait_sdk_ready 等 [FAYS-CALIB] 标定标记）→
FaysRawStreamClient（双目分包拆分，只取左目投 UI；IMU 原始数据不外送）。
P4 已接入：GripperSerial（? 握手 + C/I/F 信号，失败不拆已就绪链路）、
GripState（10ms 力检查循环 + 300mN 锁存）、力矩阵最新值（录制泵线程
30ms 轮询 matrix_seq 取走，int16 行差分编码在泵线程完成）。
open() 在独立线程执行（相机服务启动/首帧重试/触觉 warmup/SLAM 初始化
都是秒级阻塞），ready/error 经信号回主线程。
"""

from __future__ import annotations

import os
import queue
import threading
import time

import cv2
import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal

from config import settings
from core.s80m_manager import s80m_drop_watch, STEREO_DROP_ALERT_RATE
from core.gripper import affinity, paths
from core.gripper.fays_runtime import build_fays_runtime_env
from core.gripper.fays_single import SingleFaysLease
from core.gripper.control.grip_state import GripState
from core.gripper.devices.gripper_connection import (
    ConnectionState,
    GripperConnectionController,
)
from core.gripper.devices.gripper_serial import GripperSerial
from core.gripper.devices.serial_command_worker import SerialCommandWorker
from core.gripper.devices.tactile_discovery import TactileDiscovery
from core.gripper.devices.tactile_process_manager import (
    TactileProcessManager,
    TactileState,
)
from core.gripper.devices.uvc_camera_service import UvcCameraServiceManager
from core.gripper.recording.fays_raw_client import FaysRawStreamClient
from core.gripper.slam.process_controller import (
    SlamProcessController,
    SlamState,
)

# ── P5 双 rig CPU 分区（本机 Ryzen 9900X：12 物理核 × SMT，兄弟对
#    (0,12)…(11,23)）。rig1：触觉 0/2 + SLAM 任务集 {4,5,7,8,9}——
#    物理核 6 不得进入：其 SMT 兄弟 18 挂着 xhci 摄像头中断风暴
#    （流式期间 3000+/s），热角色落在 6 上会拖垮双目取帧（实测
#    left_orb 在 6 时空桶 23-29%，移出后 0.0%）。background
#    （local-map 线程，40-90% 单核负载）必须独享核 9——与 track
#    共享同一核会令 SLAM 首 pose 无法出现（复现 2/2）。input 与
#    track 均轻载，共享核 4。rig2 独立分区：触觉 1/3 + SLAM
#    任务集 {10,11,13,15,22,23}——两 rig 零物理核共享。机器 <24
#    逻辑 CPU（或进程亲和范围不含 rig2 分区）时回退 rig1 表共享
#    （见 _select_cpu_partition）。 ──
_TACTILE_CPU_MAP_RIG1 = {"left": 0, "right": 2}
_TACTILE_CPU_MAP_RIG2 = {"left": 1, "right": 3}
_SLAM_CPU_ROLES_RIG1 = {
    "fays_input": (4,),
    "fays_prepare": (5,),
    "fays_left_orb": (7,),
    "fays_right_orb": (8,),
    "fays_track": (4,),
    "fays_background": (9,),
}
_SLAM_CPU_ROLES_RIG2 = {
    "fays_input": (10,),
    "fays_prepare": (22,),
    "fays_left_orb": (13,),
    "fays_right_orb": (15,),
    "fays_track": (11,),
    "fays_background": (23,),
}
# rig2 分区所需的全部逻辑 CPU（触觉 + SLAM 六角色 + taskset 启动域）
_RIG2_PARTITION_CPUS = frozenset({
    1, 3, 10, 11, 13, 15, 22, 23,
})


def _select_cpu_partition(rig_index: int) -> tuple:
    """按 rig 序号选 CPU 分区，返回 (tactile_cpu_map, slam_cpu_roles, 说明)。

    亲和范围不含 rig2 分区（如少于 24 逻辑 CPU 的机器）时回退 rig1 表：
    两 rig 同表共享，功能不受影响，仅性能退回共享（待 probe 实测）。
    """
    if rig_index >= 2:
        try:
            allowed = {
                int(cpu) for cpu in os.sched_getaffinity(0)
            }
        except (OSError, AttributeError):
            allowed = None
        if allowed is not None and _RIG2_PARTITION_CPUS <= allowed:
            return (dict(_TACTILE_CPU_MAP_RIG2),
                    dict(_SLAM_CPU_ROLES_RIG2), None)
        return (dict(_TACTILE_CPU_MAP_RIG1),
                dict(_SLAM_CPU_ROLES_RIG1),
                "[Gripper] CPU 亲和范围不含 rig2 分区 "
                f"{sorted(_RIG2_PARTITION_CPUS)}，rig2 退回 rig1 核表共享"
                "（双 SLAM 同表，性能可能受资源竞争影响）")
    return (dict(_TACTILE_CPU_MAP_RIG1),
            dict(_SLAM_CPU_ROLES_RIG1), None)


# 轨迹滚动窗口（与上位机 PoseState 2000 点上限一致）
# 显示轨迹窗口上限。GUI 用锚定索引 0 的固定网格降采样（见 ui/pose_view_qt.py），
# 依赖窗口头部不滑动——窗口一旦滚动，网格顶点索引会整体错位导致全线漂移。
# 60000 点 @30fps ≈ 33 分钟，典型会话不会触发；存储侧轨迹由 SLAM 进程独立写盘，
# 与此显示窗口无关。
TRAJECTORY_POINT_CAP = 60000

# 队列事件名 → 信号属性
_CONTROL_SIGNALS = {
    "log": "log",
    "error": "error",
    "opened": "opened",
    "closed": "closed",
    "pose": "pose_ready",
    "gripper_state": "gripper_state_ready",
}

# 帧类信号在 emitter 里的派发方式（slot 名 = 事件 kind；
# 右目不投递：只显示左目，SLAM 解算在桥接进程内部完成）
_FRAME_EMITTERS = {
    "rgb": lambda self_, payload: self_.rgb_frame_ready.emit(*payload),
    "stereo_left": lambda self_, payload: (
        self_.stereo_frame_ready.emit(self_._stereo_slot, *payload)),
    "tactile": lambda self_, payload: self_.tactile_ready.emit(*payload),
}


class _SerialLifecycleAdapter:
    """传感器链路已由 bridge 先行启动；串口连接器只做状态适配。"""

    def prepare_connect(self):
        return None

    def connected(self):
        return None

    def connect_failed(self):
        return None

    def disconnect(self):
        return None

    def verify_prepared_control_port(self, device_path):
        return None


class GripperBridge(QObject):
    """One open gripper session; owns workers, emits events."""

    stereo_frame_ready = pyqtSignal(str, object, object)
    # slot("gripper_stereo_left"), bgr ndarray, host_monotonic_ns
    rgb_frame_ready = pyqtSignal(object, object)  # bgr ndarray, host_monotonic_ns
    tactile_ready = pyqtSignal(str, object, object, object, object)
    # side("left"|"right"), heatmap(320x240x3 RGB，worker 已 BGR→RGB),
    # force(fx,fy,fz mN),
    # force_matrix(250x250x3 float32) 或 None（矩阵回传关闭时）,
    # capture_ns(宿主单调钟纳秒，本样本的采集时刻)
    #
    # ★ 时间戳一律走 object：PyQt5 队列信号会把 Python int 按 C++ qint32
    #   封送，超过 2^31（≈2.147s 的纳秒数）即**静默截断成负数**。曾经
    #   rgb/stereo 两处写 int，落盘的 hardware_ns 因此每 2.147s 翻一次
    #   符号，下游拿它做不了任何对齐（且解回卷在 ±2.147s 歧义边界上
    #   猜错会凭空造出上百帧的假漂移）。与 main_window 的
    #   stereo_frame_ready 用 object 同理。
    pose_ready = pyqtSignal(object, object, object, object, object)
    # pos(x,y,z), quat(qx,qy,qz,qw), trajectory(位置点不可变元组，新点追加才重建),
    # timestamp(SLAM 秒，与轨迹 txt 第一列同源——轨迹并入 episode parquet 后
    # 每点 8 值 [t,x,y,z,qx,qy,qz,qw] 需要它)
    gripper_state_ready = pyqtSignal(object)  # {pct,gripped,fz,...}
    opened = pyqtSignal()  # 相机组 + 触觉 + SLAM 链路就绪
    log = pyqtSignal(str)
    error = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, parent=None, stereo_slot="gripper_stereo_left",
                 rig_index=1):
        super().__init__(parent)
        # 双夹爪：每台 bridge 发自己的双目槽名（rig1 保持旧名
        # "gripper_stereo_left"，rig2 为 "gripper_2_stereo_left"），
        # 主窗口按 dev.key 路由到各自条目。
        self._stereo_slot = stereo_slot
        # P5 双 rig CPU 分区：rig1 旧核表 / rig2 独立物理核分区
        # （亲和范围不足时回退 rig1 表共享，见 _select_cpu_partition）。
        # cpu_note 等 emitter 队列就绪后再发（_log 依赖 _events）
        self._rig_index = int(rig_index)
        self._tactile_cpu_map, self._slam_cpu_roles, self._cpu_note = (
            _select_cpu_partition(self._rig_index))
        # P5 双目取帧空桶看门狗（30fps 口径，复用主程序 s80m_drop_watch）：
        # raw 双目包按 wall 时钟 1/30s 桶计数，空桶率超阈值告警一次；
        # 录制开始由 main_window 重置（与 s80m 口径一致）
        self._stereo_drop_watch = {
            "last_mono_ns": None,
            "dropped": 0,
            "elapsed": 0,
            "alerted": False,
        }
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._open_thread = None
        self._rgb_thread = None
        self._uvc = None
        self._reservation = None
        self._tactile = None
        self._rgb_camera = None
        self._lease = None
        self._slam = None
        self._raw_client = None
        self._opened = False
        # 双目 raw 流拆包状态（首包判定模式，open 期间有效）
        self._stereo_mode = None  # "side_by_side" | "paired_packets"
        self._stereo_pending = None
        # P4 串口/夹爪状态（heartbeat 与 grip 循环线程写，信号出）
        self._serial = None
        self._serial_worker = None
        self._serial_controller = None
        self._grip_state = GripState()
        self._latest_force = {
            "left": (0.0, 0.0, 0.0),
            "right": (0.0, 0.0, 0.0),
        }
        self._last_state_payload = None
        self._last_state_post = 0.0
        # P4 力矩阵最新值 + 序号（录制泵线程轮询，序号变化才取走）
        self._matrix_lock = threading.Lock()
        self._latest_matrix = {"left": None, "right": None}
        self._matrix_seq = {"left": 0, "right": 0}
        # SLAM 轨迹：有效位姿逐点累积（滚动窗口，恒存不可变元组快照）
        self._trajectory_points = []
        self._trajectory_cache = ()
        self._matrix_first_logged = set()
        # 事件收敛：唯一 emitter 线程串行 emit（多线程并发 emit 会段错误）
        self._events = queue.Queue()
        self._frame_lock = threading.Lock()
        self._frame_slots = {}
        self._frame_pending = set()
        self._emitter = threading.Thread(
            target=self._emit_loop,
            name="gripper-bridge-emit",
            daemon=True,
        )
        self._emitter.start()
        if self._cpu_note:
            self._log(self._cpu_note)

    # ---- 事件投递（任意线程可调） ----
    def _post(self, name, *args):
        self._events.put((name, args))

    def _post_frame(self, kind, *payload):
        """latest-wins 帧槽：GUI 卡顿时丢旧帧不积压。"""
        with self._frame_lock:
            self._frame_slots[kind] = payload
            if kind in self._frame_pending:
                return
            self._frame_pending.add(kind)
        self._events.put(("frame", (kind,)))

    def _emit_loop(self):
        while True:
            name, args = self._events.get()
            if name == "_stop":
                return
            if name == "frame":
                kind = args[0]
                with self._frame_lock:
                    payload = self._frame_slots.get(kind)
                    self._frame_slots[kind] = None
                    self._frame_pending.discard(kind)
                if payload is None:
                    continue
                emitter = _FRAME_EMITTERS.get(kind)
                if emitter is not None:
                    emitter(self, payload)
                continue
            attr = _CONTROL_SIGNALS.get(name)
            if attr is not None:
                getattr(self, attr).emit(*args)

    # ---- 生命周期 ----
    def open(self, esp_serial):
        with self._lock:
            if self._open_thread is not None:
                raise RuntimeError("GripperBridge.open 已在运行")
            if not self._emitter.is_alive():
                raise RuntimeError("GripperBridge 已关闭，不可复用")
            self._stop.clear()
            self._opened = False
            self._trajectory_path = None
            self._grip_state.reset_latch()
            self._latest_force = {
                "left": (0.0, 0.0, 0.0),
                "right": (0.0, 0.0, 0.0),
            }
            with self._matrix_lock:
                self._latest_matrix = {"left": None, "right": None}
                self._matrix_seq = {"left": 0, "right": 0}
            self._trajectory_points = []
            self._trajectory_cache = ()
            self._matrix_first_logged = set()
            self._stereo_drop_watch = {
                "last_mono_ns": None,
                "dropped": 0,
                "elapsed": 0,
                "alerted": False,
            }
            thread = threading.Thread(
                target=self._open_run,
                args=(str(esp_serial),),
                name="gripper-bridge-open",
                daemon=False,
            )
            self._open_thread = thread
        thread.start()

    def _log(self, msg):
        self._post("log", msg)

    def _open_run(self, esp_serial):
        try:
            # P3 全链租约：ESP 唯一匹配 → rig 内唯一 S80M → USB3 速度校验
            # → 官方 SDK 探针读序列号 → flock → materialize 运行时 yaml
            self._lease = SingleFaysLease(logger=self._log)
            selected = self._lease.acquire(esp_serial)
            # 轨迹 txt 由桥接进程直写；录制结束后 main_window 拷贝到
            # meta/（P4）。open() 置 None 防旧会话残留。
            self._trajectory_path = selected["trajectory"]
            assignment = {
                "esp32": {"serial": str(selected["esp_serial"])},
                "fays": {
                    "product_serial": selected["product_serial"],
                    "physical_usb_path": selected["physical_usb_path"],
                },
            }
            self._log(
                "[Gripper] Fays 租约已持有: serial={} "
                "usb={:g}M".format(
                    selected["product_serial"],
                    selected["usb_speed_mbps"]))

            self._uvc = UvcCameraServiceManager(logger=self._log)
            self._reservation = self._uvc.select(lambda: assignment)
            discovery = TactileDiscovery(
                sightac_root=paths.SIGHTAC_SDK_ROOT,
                logger=self._log,
            )
            # Sightac Flash 参数入缓存：TactileProcessManager 的
            # _resolve_inputs 依赖 cache_snapshot()，必须先于 start
            for side in ("left", "right"):
                camera = self._reservation.camera(side)
                discovery.cache_external_flash_params(
                    camera.node.video_index, camera.flash_params)

            # DECXIN RGB：claim 内置首帧重试；成功即启动常驻读取线程
            decxin_camera = self._reservation.camera("decxin")
            ipc_camera, first_frame, actual_format = decxin_camera.claim()
            self._rgb_camera = ipc_camera
            self._log(
                "[Gripper] DECXIN RGB 就绪: {}x{}@{}fps {}".format(
                    actual_format[0], actual_format[1],
                    actual_format[2], actual_format[3]))
            self._post_frame("rgb", first_frame, time.monotonic_ns())
            self._rgb_thread = threading.Thread(
                target=self._rgb_run,
                name="gripper-rgb-read",
                daemon=True,
            )
            self._rgb_thread.start()

            # 触觉输入/计算双进程（spawn，父进程不建 TouchSensor；
            # materialize_force_matrix：力矩阵回传主进程，P4 泵线程落盘）
            self._tactile = TactileProcessManager(
                TactileState(),
                discovery,
                cpu_map=dict(self._tactile_cpu_map),
                materialize_force_matrix=True,
                publish_result=self._on_tactile_result,
                logger=self._log,
            )
            self._tactile.start_connected(
                left=self._reservation.camera("left"),
                right=self._reservation.camera("right"),
                asynchronous=True,
            )
            if not self._tactile.wait_ready(
                    timeout=self._tactile.PROCESS_READY_TIMEOUT):
                snapshot = self._tactile.snapshot()
                details = "; ".join(
                    f"{side}={getattr(snapshot, side).error}"
                    for side in ("left", "right")
                    if getattr(snapshot, side).error)
                raise RuntimeError(
                    "触觉双进程未就绪: " + (details or "超时"))

            # SLAM 桥接进程（stdbuf+taskset 包官方 ORB 二进制，stdout
            # 逐行协议；wait_sdk_ready 等 [FAYS-CALIB] 标定标记）
            ipc_dir = selected["ipc_dir"]

            def slam_runtime_env():
                # 原生二进制按 KSQ_FAYS_IPC_DIR 定位 raw 流 socket/位姿
                # JSON（默认 /dev/shm，会与租约的实例目录错位）；
                # 原程序由 FaysInstanceManager.runtime_environment() 注入
                env = build_fays_runtime_env()
                env["KSQ_FAYS_IPC_DIR"] = ipc_dir
                env["KSQ_FAYS_CALIBRATION_SERIAL"] = (
                    selected["product_serial"])
                return env

            slam = SlamProcessController(
                SlamState(),
                executable=selected["orb_binary"],
                vocabulary=paths.ORB_VOCABULARY,
                settings=selected["orb_yaml"],
                device_config=selected["runtime_config"],
                trajectory=selected["trajectory"],
                work_dir=selected["work_dir"],
                native_log_path=(
                    f"{selected['work_dir']}/slam_stdout.log"),
                runtime_env_factory=slam_runtime_env,
                cpu_roles=dict(self._slam_cpu_roles),
                on_pose=self._on_slam_pose,
                log=self._log,
                pose_paths=(
                    f"{ipc_dir}/orb_pose.json.tmp",
                    f"{ipc_dir}/orb_pose.json",
                ),
            )
            slam.start()
            self._slam = slam
            self._log("[Gripper] SLAM 桥接进程已启动，等待 SDK 标定…")
            if not slam.wait_sdk_ready(
                    timeout=settings.GRIPPER_SLAM_READY_TIMEOUT_S):
                raise RuntimeError(
                    "SLAM SDK 初始化超时（未等到 [FAYS-CALIB] 标定标记；"
                    "确认夹爪 USB3 头插在 USB3 口）")

            # 原始双目/IMU 流（AF_UNIX）：双目分包拆分 + IMU 批随左目
            # raw_cpu：接收线程专用 SMT 核（P5 补丁，见 core/gripper/
            # affinity.py）——保证 native 30fps 取帧不因接收侧丢包而降频
            raw_cpu = affinity.raw_cpu(self._rig_index)
            raw_client = FaysRawStreamClient(
                f"{ipc_dir}/orb_raw_stream.sock",
                on_stereo=self._on_stereo_packet,
                logger=self._log,
                on_error=lambda message: self._log(
                    f"[Gripper-Raw] {message}"),
                on_reconnected=self._on_raw_reconnected,
                raw_cpu=raw_cpu,
            )
            raw_client.start()
            self._raw_client = raw_client

            # 串口（P4）：8 次 ? 握手 + C/I/F 信号；heartbeat/grip 循环
            # 自带线程。所有传感器链路已先行就绪，lifecycle 只做占位
            # 适配；握手失败不拆已就绪链路（日志可见，触觉/SLAM 保持）。
            tty = str(selected.get("esp_tty") or "").strip()
            if tty:
                serial_owner = GripperSerial(logger=self._log)
                worker = SerialCommandWorker(serial_owner.send)
                worker.start()
                controller = GripperConnectionController(
                    ConnectionState(),
                    serial_owner,
                    worker,
                    _SerialLifecycleAdapter(),
                    on_board_update=self._on_board_update,
                    on_grip_check=self._on_grip_check,
                )
                self._serial = serial_owner
                self._serial_worker = worker
                self._serial_controller = controller
                if not controller.connect_sync(tty):
                    self._log(
                        "[Gripper] ESP32 串口连接失败: {} "
                        "（相机/触觉/SLAM 链路保持）".format(
                            serial_owner.last_error
                            or "handshake timeout"))
            else:
                self._log("[Gripper] 未找到 ESP32 串口，跳过夹爪控制")

            if self._stop.is_set():
                # 启动期间被 close()：回收后不再发 opened
                self._teardown()
                return
            self._opened = True
            self._log("[Gripper] 相机 + 触觉 + SLAM 链路就绪")
            self._post("opened")
        except Exception as exc:
            if self._stop.is_set():
                # close() 路径触发（如 wait_ready 期间被回收）：静默退出
                return
            self._log("[Gripper] 启动失败: {}".format(exc))
            self._teardown()
            self._post("error", str(exc))

    def _rgb_run(self):
        while not self._stop.is_set():
            ok, frame = self._rgb_camera.read()
            if not ok:
                if not self._stop.is_set():
                    time.sleep(0.005)
                continue
            self._post_frame("rgb", frame, time.monotonic_ns())

    def _on_tactile_result(self, side, heatmap, force, force_matrix):
        # 采集时刻在回调入口取（最接近样本真实到达时刻），随信号一起送到
        # 落盘侧：力/矩阵与 RGB 走的是两条互不相干的支路（力是 latest-wins
        # 单槽、RGB 是 FIFO 队头最旧帧），两者在行内的先后完全由各自的队列
        # 滞留决定。只有把各自的采集时刻都记下来，下游才能按时间重对齐。
        capture_ns = time.monotonic_ns()
        if side in ("left", "right"):
            try:
                self._latest_force[side] = tuple(
                    float(value) for value in force[:3])
            except (TypeError, ValueError, IndexError):
                pass
            if force_matrix is not None:
                with self._matrix_lock:
                    # 与 matrix 同锁存：泵线程取出的 ns 恒与矩阵配对，
                    # 不会出现「新矩阵配了旧时刻」
                    self._latest_matrix[side] = (capture_ns, force_matrix)
                    self._matrix_seq[side] += 1
                if side not in self._matrix_first_logged:
                    self._matrix_first_logged.add(side)
                    self._log(
                        "[Gripper-Tactile] {} 侧力矩阵首帧回传 shape={}".format(
                            side, force_matrix.shape))
        self._post_frame("tactile", side, heatmap, force, force_matrix,
                         capture_ns)

    # ---- P4 串口/夹爪状态（heartbeat 与 grip 循环线程调用） ----
    def _on_board_update(self, snapshot):
        """串口 STATE 响应 → GripState 板字段。"""
        try:
            self._grip_state.update_firmware_state(
                dict(snapshot.board_fields or {}),
                updated=snapshot.updated)
        except Exception:
            pass
        self._publish_grip_state()

    def _on_grip_check(self):
        """10ms 力检查循环：双路 Fz 阈值 → 锁存 → 状态事件。"""
        event = self._grip_state.evaluate_forces(
            self._latest_force["left"], self._latest_force["right"])
        self._publish_grip_state(event=event)

    def _publish_grip_state(self, event=None):
        snap = self._grip_state.snapshot()
        left = self._latest_force["left"]
        right = self._latest_force["right"]
        try:
            fz = max(float(left[2]), float(right[2]))
        except (TypeError, ValueError, IndexError):
            fz = 0.0
        payload = {
            "pct": snap.percent,
            "gripped": snap.grip_latched,
            "fz": fz,
            "event": str(event or ""),
            "board": dict(snap.board_state),
        }
        now = time.monotonic()
        if (payload != self._last_state_payload
                or now - self._last_state_post >= 0.1):
            self._last_state_payload = payload
            self._last_state_post = now
            self._post("gripper_state", payload)

    def matrix_seq(self, side):
        with self._matrix_lock:
            return self._matrix_seq.get(side, 0)

    def latest_matrix_stamped(self, side):
        """最新力矩阵连同它的采集时刻 → (capture_ns, matrix)，无则 None。

        ns 与 matrix 在同一次加锁里取出，**恒配对**（分两次取会出现
        「拿到新矩阵、配了旧时刻」）。泵线程用这个版本落盘。
        """
        with self._matrix_lock:
            entry = self._latest_matrix.get(side)
        if entry is None:
            return None
        ns, matrix = entry
        return int(ns), matrix

    def latest_matrix(self, side):
        """最新力矩阵（不含时刻；需要时刻用 latest_matrix_stamped）。"""
        stamped = self.latest_matrix_stamped(side)
        return None if stamped is None else stamped[1]

    def _on_slam_pose(self, pose):
        """SlamProcessController 工作线程回调：位姿进队列保序。

        原协议用 None 表示"无位姿/进程停止"（start() 的首个 stop()
        与进程退出都会回调一次），桥接直接丢弃。
        轨迹在此累积（与上位机 PoseState 同款滚动窗口），新点追加才
        重建不可变元组——UI 侧按对象同一性省去无变化重绘。
        """
        if pose is None:
            return
        self._trajectory_points.append(tuple(pose.position))
        if len(self._trajectory_points) > TRAJECTORY_POINT_CAP:
            self._trajectory_points = (
                self._trajectory_points[-TRAJECTORY_POINT_CAP:])
        self._trajectory_cache = tuple(self._trajectory_points)
        self._post("pose", tuple(pose.position), tuple(pose.rotation),
                   self._trajectory_cache, pose.timestamp,
                   # 取样帧的宿主单调钟纳秒（与 hardware_ns 同时基）；旧
                   # native 二进制不打印 Host 字段 → None，下游按未知处理。
                   # getattr 而非直接取属性：测试夹具/外部实现可能只有
                   # pos/quat/ts 三个字段，缺戳不应让整条位姿断流。
                   getattr(pose, "host_mono_ns", None))

    def _on_stereo_packet(
        self, payload, sensor_ts_ns, host_mono_ns, sequence,
        width, height, channels, step, encoding,
    ):
        """raw 流双目包：首包判定拆分模式，只取左目转 BGR 投帧槽。

        右目不投递（主程序只显示左目；SLAM 解算在桥接进程内部完成，
        左右目原始视频与 IMU 均不落盘，只存 SLAM 位姿/轨迹）。
        模式一 side_by_side：单包宽 >= 3×高（左右拼合，如 1280×400），
        取左半。
        模式二 vertical_stacked：单包高 > 宽（上下拼合，实测 640×800
        = 2×640×400），取上半（上半→左目，真机核对后定）。
        模式三 paired_packets：单包即单目（如 640×400），相邻两包配对
        （先左后右为暂定约定），右目包直接丢弃。

        P5 每个 raw 包（含各模式）先过 30fps 空桶看门狗：SLAM 进程被
        资源竞争拖慢时包以「空洞+突发」到达，wall 桶统计即可检出。
        """
        self._stereo_drop_tick(host_mono_ns)
        if self._stereo_mode is None:
            if width >= height * 3:
                self._stereo_mode = "side_by_side"
            elif height > width:
                self._stereo_mode = "vertical_stacked"
            else:
                self._stereo_mode = "paired_packets"
            self._log(
                "[Gripper-Raw] 双目包模式判定: {} ({}x{}x{})".format(
                    self._stereo_mode, width, height, channels))
        buffer = np.frombuffer(
            payload, dtype=np.uint8,
            count=step * height,
        ).reshape(height, step)
        if channels == 1:
            frame = cv2.cvtColor(
                buffer[:, :width].reshape(height, width),
                cv2.COLOR_GRAY2BGR)
        else:
            frame = buffer[:, :width * channels].reshape(
                height, width, channels).copy()
        if self._stereo_mode == "side_by_side":
            half = width // 2
            self._post_frame("stereo_left", frame[:, :half].copy(),
                             host_mono_ns)
            return
        if self._stereo_mode == "vertical_stacked":
            half = height // 2
            self._post_frame("stereo_left", frame[:half].copy(),
                             host_mono_ns)
            return
        pending = self._stereo_pending
        if pending is None:
            self._stereo_pending = (frame, host_mono_ns)
            return
        left, left_ns = pending
        self._stereo_pending = None
        self._post_frame("stereo_left", left, left_ns)

    def _stereo_drop_tick(self, mono_ns):
        """双目取帧 30fps 空桶看门狗（口径复用主程序 s80m_drop_watch）。

        wall 时钟 1/30s 桶计数：SLAM 进程取帧掉速时包间隔拉大，跨 k≥2
        桶计 k-1 个空桶。累计 ~3 秒样本后空桶率超
        STEREO_DROP_ALERT_RATE 记一次日志告警（每段录制重置一次，
        与 s80m 告警同口径；日志面板可见，不弹窗打扰）。
        """
        interval_ns = int(settings.STEREO_RECORD_MIN_INTERVAL_S * 1e9)
        # s80m_drop_watch 的 entry 契约带 drop_watch 键（与 s80m 注册表
        # 条目同形状），这里按同形状包一层，纯函数口径原样复用
        _, dropped, elapsed = s80m_drop_watch(
            {"drop_watch": self._stereo_drop_watch},
            mono_ns, interval_ns)
        w = self._stereo_drop_watch
        if w["alerted"] or elapsed < 90:
            return
        rate = dropped / elapsed
        if rate < STEREO_DROP_ALERT_RATE:
            return
        w["alerted"] = True
        self._log(
            "[夹爪-SLAM告警] 双目取帧不稳定：近 3 秒空桶率 {:.0f}%"
            "（{} 空桶 / {} 桶）——30fps 采集受影响，SLAM 轨迹将卡顿；"
            "若持续偏高请检查 USB 带宽与 CPU 争用".format(
                rate * 100, dropped, elapsed))

    def stereo_drop_snapshot(self):
        """(累计空桶, 累计桶数)——录制结束汇总口径，与主程序
        _s80m_drop_summary 一致。"""
        w = self._stereo_drop_watch
        return (w["dropped"], w["elapsed"])

    def reset_stereo_drop_watch(self):
        """录制开始重置空桶统计（告警放行下一段录制）。"""
        self._stereo_drop_watch = {
            "last_mono_ns": None,
            "dropped": 0,
            "elapsed": 0,
            "alerted": False,
        }

    def clear_trajectory(self):
        """录制开始清空 GUI 轨迹滚动窗口（只显示本段录制的点）。

        parquet 的 slam_trajectory 列由 writer 在录制期独立累积
        （见 pipeline.write_slam_trajectory），不受此窗口影响；这里只管
        显示侧。UI 侧配合 pose_view.reset() 同步清空渲染缓存。
        """
        self._trajectory_points = []
        self._trajectory_cache = ()

    def _on_raw_reconnected(self):
        """raw 客户端断线重连成功回调。

        断连窗口（服务端原始队列溢出主动断开，通常是录制停止时
        主进程瞬时卡顿所致）的空桶属于显示侧问题，不计入取帧质量：
        清掉残留的 paired 待配对帧（断点后流序重来），并重置空桶
        看门狗统计。
        """
        self._stereo_pending = None
        self.reset_stereo_drop_watch()

    def _teardown(self):
        """逆序回收：串口 → raw 流 → SLAM 进程 → RGB 线程 → 触觉子进程
        → 相机租约 → 服务进程 → Fays 租约。"""
        with self._lock:
            self._stop.set()
            serial_controller = self._serial_controller
            self._serial_controller = None
            self._serial_worker = None
            self._serial = None
            raw_client = self._raw_client
            self._raw_client = None
            slam = self._slam
            self._slam = None
            rgb_thread = self._rgb_thread
            self._rgb_thread = None
            rgb_camera = self._rgb_camera
            self._rgb_camera = None
            tactile = self._tactile
            self._tactile = None
            reservation = self._reservation
            self._reservation = None
            uvc = self._uvc
            self._uvc = None
            lease = self._lease
            self._lease = None
        if serial_controller is not None:
            try:
                serial_controller.close()
            except Exception:
                pass
        if raw_client is not None:
            try:
                raw_client.stop()
            except Exception:
                pass
        if slam is not None:
            try:
                slam.stop(join=False)
            except Exception:
                pass
        if rgb_thread is not None and rgb_thread.is_alive():
            rgb_thread.join(timeout=3.0)
        if rgb_camera is not None:
            try:
                rgb_camera.release()
            except Exception:
                pass
        if tactile is not None:
            try:
                tactile.stop()
            except Exception:
                pass
        if reservation is not None:
            try:
                reservation.release_unclaimed()
            except Exception:
                pass
        if uvc is not None:
            try:
                uvc.stop()
            except Exception:
                pass
        if lease is not None:
            try:
                lease.release()
            except Exception:
                pass

    def close(self):
        with self._lock:
            if self._open_thread is None and not self._opened:
                return
            self._open_thread = None
        self._teardown()
        self._opened = False
        self._post("closed")
        # 哨兵事件让 emitter 线程优雅退出：守护线程若挂在
        # queue.get() 上，解释器退出时仍在写 stderr 会核心转储
        self._events.put(("_stop", ()))
        emitter = self._emitter
        if emitter is not threading.current_thread():
            emitter.join(timeout=2.0)

    @property
    def running(self):
        return self._opened

    @property
    def trajectory_path(self):
        """桥接进程直写的轨迹 txt 路径（open 成功后才非 None）。"""
        return self._trajectory_path
