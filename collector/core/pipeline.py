"""
摄像机管线 —— 管理多路摄像机和共享录制会话。
所有摄像机录制到同一个 episode（v1.1.0 任务池化布局：每路一个
videos/chunk-NNN/<key>/file-NNN 视频 + 一个 data parquet）。
"""

from __future__ import annotations
import time
import threading
import queue
from typing import Callable, Dict, List, Optional

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal, QTimer

from config import settings
from core.camera import CameraWorker, CameraState
from core.egodata_writer import EgoDataWriter
from core.frame_gap import GapWatch
from core.timing import Stopwatch


class DropStats:
    """录制丢帧统计（v1.0.9：编码背压可见化）。

    线程安全计数器 + 快照。计数路径：采集线程/主线程的 put_nowait 满
    分支（写入线程只读快照）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counts: Dict[str, int] = {}

    def inc(self, key: str, n: int = 1):
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + n

    def note_max(self, key: str, value: int):
        """峰值类指标（最大滞后/最大空洞毫秒）——inc 只能累加，装不下峰值。"""
        value = int(value)
        with self._lock:
            current = self._counts.get(key)
            if current is None or value > current:
                self._counts[key] = value

    def clear(self):
        with self._lock:
            self._counts.clear()

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counts)


# drop_stats 键命名约定（新增键必须遵守；消费端一律用下面的判据，别再手写
# `!= "imu_overflow"` 这类排除逻辑）：
#   ext:<slot> / uvc:<slot> / sensor_queue   队列满丢帧 —— 帧数，可加总
#   *_ms / *_ns / *_count                    时长/次数 —— **不可**加进帧数
#   imu_overflow                             遗留键：防丢缓冲超限次数，非帧
_NON_FRAME_SUFFIXES = ("_ms", "_ns", "_count")
_LEGACY_NON_FRAME_KEYS = frozenset({"imu_overflow"})


def is_frame_drop_key(key: str) -> bool:
    """该键是否表示「可加总的帧数」。

    DROP_WARN 告警的语义是「丢帧 ⇒ 编码器跟不上 ⇒ 换编码器/降分辨率」。
    采集停摆时长、latest-wins 覆盖次数、帧空洞毫秒都与编码器无关，混进
    sum() 会把用户指去调错东西——2026-09-18 那次 4.68s 空洞正是被这类
    混淆掩盖的（所有计数器都是 0，谁也说不清帧丢在哪一层）。
    """
    if key in _LEGACY_NON_FRAME_KEYS:
        return False
    return not key.endswith(_NON_FRAME_SUFFIXES)


def frame_drop_total(stats: Dict[str, int]) -> int:
    """帧数口径的丢帧总数（消费端唯一入口：main_window / lite_window）。"""
    return sum(v for k, v in stats.items() if is_frame_drop_key(k))


class CameraSlot(QObject):
    """
    一个摄像机槽位 —— 相机采集 + 帧缓冲。

    信号
    ----
    frame_ready(str, ndarray)       — slot_id, BGR帧（用于 UI 显示）
    state_changed(str, str)         — slot_id, 新状态
    error_occurred(str, str)        — slot_id, 错误消息
    """

    frame_ready = pyqtSignal(str, object)
    state_changed = pyqtSignal(str, str)
    error_occurred = pyqtSignal(str, str)

    def __init__(self, slot_id: str, camera_index: int,
                 on_queue_full: Optional[Callable[[], None]] = None):
        super().__init__()
        self.slot_id = slot_id
        self.camera_index = camera_index
        self.camera_name = f"Camera {camera_index}"

        # B+ 方案: 帧队列，采集线程直接写入，录制线程消费
        # maxsize=3 保证队列里始终是最新的帧，避免积压
        self.frame_queue = queue.Queue(maxsize=3)
        self.camera = CameraWorker(camera_index, record_queue=self.frame_queue,
                                   on_queue_full=on_queue_full)

        self.camera.frame_ready.connect(self._on_frame)
        self.camera.state_changed.connect(self._on_camera_state)
        self.camera.error_occurred.connect(
            lambda msg: self.error_occurred.emit(self.slot_id, msg)
        )

    def start_camera(self):
        self.camera.start()

    def stop_camera(self):
        self.camera.stop()

    @property
    def camera_state(self) -> str:
        return self.camera.state

    def _on_frame(self, frame):
        # 转发给 UI 显示（录制帧由 frame_queue 独立传递，不受此信号影响）
        self.frame_ready.emit(self.slot_id, frame)

    def _on_camera_state(self, state: str):
        self.state_changed.emit(self.slot_id, state)


class CameraPipeline(QObject):
    """
    管理一组 CameraSlot + 共享 LeRobot 录制会话。

    所有摄像机录制到同一个会话目录，保证 MP4 和 Parquet 完整。
    """

    slot_added = pyqtSignal(str)
    slot_removed = pyqtSignal(str)
    session_changed = pyqtSignal(object)     # VideoRecorder | None
    recording_started = pyqtSignal(str)      # slot_id
    recording_finished = pyqtSignal(str, str) # slot_id, task_dir（v1.1.0 池化）
    recording_aborted = pyqtSignal(str)      # slot_id
    duration_changed = pyqtSignal(str, float)
    error_occurred = pyqtSignal(str, str)
    state_changed = pyqtSignal(str, str)
    recording_log = pyqtSignal(str)   # v1.0.9 编码器探测等录制期日志

    def __init__(self, output_dir: str = None):
        super().__init__()
        self._output_dir = output_dir or settings.RECORDING_DIR
        self._slots: Dict[str, CameraSlot] = {}

        # 共享录制状态
        self._writer: Optional[EgoDataWriter] = None
        self._recording = False
        self._frame_count = 0                     # 全局帧序号（跨摄像机递增）
        self._per_cam_frame: Dict[str, int] = {}   # 每路摄像机的独立帧序号
        self._last_recording_frames: Dict[str, int] = {}  # 上一轮录制的每相机帧数快照
        self._start_time: float = 0.0
        self._episode_start_s: float = 0.0         # episode 起始 Unix 秒（用于相对时间戳）
        self._timer_running = False
        self._codec_name = ""
        self._session_path: Optional[str] = None
        self._last_episode_index: int = 0      # v1.1.0：上一轮录制的全局 episode 序号
        self._recording_slot: Optional[str] = None

        # v1.0.9 丢帧统计与 IMU 防丢
        self._drop_stats = DropStats()
        self._last_drop_stats: Dict[str, int] = {}
        self._pending_imu: Dict[str, list] = {}  # slot → 队列满暂存的 IMU 样本
        self._pending_imu_lock = threading.Lock()
        self._imu_overflow_count = 0  # 防丢缓冲超限次数（每 episode 告警一次）
        # IMU 批只随主槽位写入的槽位集合（S80M 独立双目左右目共享同一份
        # 样本；夹爪链路不存 IMU/双目视频，只落盘 SLAM 位姿/轨迹）
        self._imu_external_slots = {"stereo_left"}

        # v1.3.10 落盘侧帧空洞：slot → GapWatch（写线程私有，主线程只在
        # join 之后读；见 core/frame_gap.py 与 docs/postmortem_trajectory_and_rgb.md）
        self._ext_gap_watches: Dict[str, GapWatch] = {}

        # 已注册的传感器名称列表（决定 parquet 中 observation.<name> 列）
        self._sensor_names: List[str] = []
        self._device_meta: List[dict] = []   # start_recording(device_meta=…) 透传

        # B+ 方案: 传感器数据通过队列传递（主线程 → 写入线程），线程安全
        # 队列元素: (sensor_name, data_array) 元组
        self._sensor_queue: queue.Queue = queue.Queue(maxsize=10)

        # USB 手套 IMU 四元数：sensor_name → (quats 64×f32, valid 16×f32)
        # 最新值覆盖式快照，写入线程每帧取走后清空（与传感器队列同节奏）
        self._glove_imu: Dict[str, tuple] = {}
        self._glove_imu_lock = threading.Lock()
        # USB 手套骨架关键点：sensor_name → 63×f32（21×3 米），同节奏快照
        self._glove_kpts: Dict[str, np.ndarray] = {}
        self._glove_kpts_lock = threading.Lock()
        # UMI 夹爪稀疏列快照（P3/P4）：latest-wins，写入线程每帧取走清空
        # 键：gripper_state / gripper_{left,right}_force /
        #     gripper_{left,right}_force_matrix（P4 泵线程预编码 int16 列表）
        # （slam_pose 已于 2026-09-10 停止落盘，位姿只走 _gripper_traj）
        # 双夹爪 rig2 加 "gripper_2_" 前缀（rig1 旧键不变，见 write_*）
        self._gripper_snapshots: Dict[str, object] = {}
        self._gripper_snapshots_lock = threading.Lock()
        # SLAM 轨迹点按帧窗口累积（每点 8 值 [t,x,y,z,qx,qy,qz,qw] 扁平），
        # 行写入时并入 snapshot——轨迹 txt 侧车由此废弃（并入 episode parquet）
        self._gripper_traj: Dict[str, list] = {}
        # 与 _gripper_traj 逐点同序并行的宿主时刻（int64 纳秒）。**必须与
        # 点列表锁步**：每点恒定追加一个值，无戳时补 0（= 未知，与
        # hardware_ns / *_force_ns 的 0 约定一致），这样两列长度恒相等、
        # 下标即配对。缺戳的点若跳过不追加，两列就会错位且无法察觉。
        self._gripper_traj_ns: Dict[str, list] = {}
        # 夹爪非视频槽设备 ID（pose/force 槽不走 register_external_source，
        # 单独进 status 列）
        self._gripper_device_ids: set = set()

        # 外部帧源（如双目子进程），不经过 CameraWorker
        # 帧通过队列传入写入线程，以固定帧率均匀消费，避免 GIL 导致的写入抖动
        # _external_queues[slot_id] = queue.Queue(maxsize=2)
        # _external_dims[slot_id] = (height, width)
        # _external_fps[slot_id] = fps
        self._external_queues: Dict[str, queue.Queue] = {}
        self._external_dims: Dict[str, tuple] = {}
        self._external_fps: Dict[str, float] = {}

        # B+ 方案: 独立写入线程，用 time.perf_counter() 精确控制 30fps
        self._write_thread: Optional[threading.Thread] = None

        # 录制时长更新定时器（主线程，仅用于 UI 显示）
        self._duration_timer = QTimer(self)
        self._duration_timer.timeout.connect(self._tick_duration)
        self._duration_timer.setInterval(250)

        # 深度帧队列（双目深度计算 → 写入线程）；按深度槽位名分列
        # （S80M 传统路径用 "stereo_left" 作为默认键）
        self._depth_queues: Dict[str, queue.Queue] = {}
        self._depth_frame_idx: Dict[str, int] = {}

        # 深度伪相机元数据（D435 等原生深度相机，多路并存）:
        # _depth_cameras[depth槽名] = {"resolution": (h,w), "fps": f,
        #     "master_slot": 主槽位,
        #     "heatmap_near_mm"/"heatmap_far_mm"/"heatmap_smooth_k"/
        #     "heatmap_temporal_alpha": …}
        # 名字含 "depth" → EgoDataWriter 自动标 type:depth 并跳过视频目录创建
        self._depth_cameras: Dict[str, dict] = {}
        # 最近深度帧（深度源低于录制帧率时补拍节拍：重复写双流使
        # 深度 MKV 帧 i 与 RGB 帧 i 对齐；FFV1 对重复帧增量≈0）
        self._last_depth_frames: Dict[str, np.ndarray] = {}
        # 深度帧随哪路主槽位同步落盘（S80M 默认 stereo_left，D435 为 d435_rgb）
        self._depth_master_slot: str = "stereo_left"
        # 外部帧源标定（D435 运行时从 SDK 提取，录制开始时写入 calibration/）
        self._external_calibration = None
        self._external_calibrations: dict = {}   # device_key → 标定（多路）

        # 录制事件日志（连接/断开标记）
        self._device_status: Dict[str, str] = {}

    def record_event(self, device: str, event_type: str, message: str = ""):
        """记录一个带时间戳的设备事件并更新连接状态。

        CameraPipeline 写入线程会在每帧 parquet 数据中写入 status.<device> 列。

        Args:
            device: 设备标识（如 cam_001, sensors_right）
            event_type: 事件类型（connected, disconnected）
            message: 附加描述
        """
        status = "connected" if event_type == "connected" else "disconnected"
        self._device_status[device] = status

    # ── 槽位管理 ──────────────────────────────────────

    def add_camera(self, slot_id: str, camera_index: int) -> CameraSlot:
        if slot_id in self._slots:
            raise ValueError(f"槽位 {slot_id} 已存在")
        slot = CameraSlot(slot_id, camera_index,
                          on_queue_full=lambda: self._drop_stats.inc(
                              f"uvc:{slot_id}"))
        self._slots[slot_id] = slot
        slot.start_camera()
        self.slot_added.emit(slot_id)
        return slot

    def remove_camera(self, slot_id: str):
        slot = self._slots.pop(slot_id, None)
        if slot:
            if self._recording:
                self.abort_recording(slot_id)
            slot.stop_camera()
            slot.blockSignals(True)
            slot.deleteLater()
            self.slot_removed.emit(slot_id)

    def get_slot(self, slot_id: str) -> Optional[CameraSlot]:
        return self._slots.get(slot_id)

    def slot_ids(self):
        return list(self._slots.keys())

    def slot_count(self) -> int:
        return len(self._slots)

    def remove_all(self):
        for sid in list(self._slots.keys()):
            self.remove_camera(sid)

    @property
    def is_recording(self) -> bool:
        return self._recording

    # ── 外部帧源（双目子进程等） ──────────────────────

    def register_external_source(self, slot_id: str, resolution: tuple,
                                  fps: float = 25.0):
        """注册一个外部帧源（不创建 CameraWorker）。

        外部帧源通过队列将帧传入写入线程，以固定帧率均匀消费。
        每路外部源可使用独立 fps，ffmpeg 用该值设置输出帧率。

        Args:
            slot_id: 槽位标识（如 "stereo_left"）
            resolution: (height, width)
            fps: 该摄像机的原生帧率（用于 ffmpeg -r 参数）
        """
        # 深缓冲（≈1s）：写线程/编码器子进程启动、系统卡顿期间吸收
        # 抽帧后的积压帧，避免 put_nowait 丢帧；写线程按 30fps 均匀
        # 消费，缓冲只延后写入不丢内容（峰值内存 ~90MB/槽，可控）
        self._external_queues[slot_id] = queue.Queue(maxsize=30)
        self._external_dims[slot_id] = resolution
        self._external_fps[slot_id] = fps

    def unregister_external_source(self, slot_id: str):
        """移除外部帧源。"""
        self._external_queues.pop(slot_id, None)
        self._external_dims.pop(slot_id, None)
        self._external_fps.pop(slot_id, None)

    def _drain_external_queues(self):
        """排空外部帧源队列（丢弃未消费的旧帧）。

        外部队列与 CameraSlot.frame_queue 语义相同：生产侧（夹爪 RGB 常驻
        读取线程）从设备打开起就一直在投递，只有录制期间才被写线程消费。
        不在录制边界排空的话，上一段停止时残留的最多 30 帧（≈1s）会被原样
        写成新 mp4 的开头——实测每段开头约 28 帧与上一段 episode 的末帧逐帧
        匹配。队列满时残留帧还占着 ~110MB/槽 内存。
        """
        for q in self._external_queues.values():
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def write_external_frame(self, slot_id: str, frame: np.ndarray,
                             hardware_ns: int = 0,
                             imu_samples: Optional[List] = None):
        """将外部帧推入录制队列（线程安全，非阻塞）。

        帧由写入线程以固定帧率均匀消费，保证输出视频无抖动。

        Args:
            hardware_ns: 帧的 SDK 硬件纳秒时间戳（双目相机，与 IMU 同源）
            imu_samples: 本帧窗口内的 IMU 样本 [(ts_ns, gx,gy,gz, ax,ay,az), ...]
        """
        q = self._external_queues.get(slot_id)
        if q is not None and self._recording:
            try:
                q.put_nowait((frame, hardware_ns, imu_samples))
            except queue.Full:
                # ★ v1.0.9 丢帧保 IMU：帧+hardware_ns 成对丢弃（视频帧与
                # parquet 行同步少一行，序号对齐不受影响），但 stereo_left
                # 的 IMU 批次转入防丢缓冲，随后续帧按时间戳挂靠落盘 ——
                # 丢帧只损失视频帧，不损失 IMU 样本
                self._drop_stats.inc(f"ext:{slot_id}")
                if slot_id in self._imu_external_slots and imu_samples:
                    with self._pending_imu_lock:
                        buf = self._pending_imu.setdefault(slot_id, [])
                        buf.extend(imu_samples)
                        overflow = (len(buf)
                                    - settings.IMU_PENDING_MAX_SAMPLES)
                        if overflow > 0:
                            del buf[:overflow]
                            self._imu_overflow_count += 1
                            if self._imu_overflow_count == 1:
                                self.recording_log.emit(
                                    "[警告] IMU 防丢缓冲超限，最旧样本被丢弃")

    def write_depth(self, depth_frame: np.ndarray,
                    depth_slot: str = "stereo_left"):
        """将深度帧推入录制队列（线程安全，非阻塞，keep-latest）。

        深度图（S80M 视差 / D435 原生毫米深度，均为 uint16）通过此方法传入，
        写入线程在消费该路深度主槽位帧时同步写入。深度帧语义是
        "每帧 RGB 配最新深度"：入队前清掉未消费旧帧——旧 FIFO 丢新帧
        口径在实测 ~27fps 突发产出下丢 35%（突发期队列必然溢出），
        被覆盖的旧帧下游本来就不消费，最新帧覆盖无损失。

        Args:
            depth_frame: uint16 numpy array (H, W)，视差图（值×16）或毫米深度图
            depth_slot: 深度槽位名（默认 "stereo_left" 兼容 S80M 传统路径）
        """
        if not self._recording:
            return
        # 兼容旧单深度调用（如 d435_e2e_test 的 write_depth(frame)）：
        # 名义槽未注册而恰好只声明了一路深度相机时，自动落到该路
        if depth_slot not in self._depth_cameras \
                and len(self._depth_cameras) == 1:
            depth_slot = next(iter(self._depth_cameras))
        q = self._depth_queues.setdefault(depth_slot,
                                          queue.Queue(maxsize=3))
        # keep-latest：深度引擎实测 ~27fps 突发产出（burst 期可达 ~50fps），
        # 与写线程 30fps 消费名义相等——FIFO+丢新帧必然溢出（实机 165/478
        # = 35%）；最新帧覆盖旧帧后突发期无丢帧、每拍都配到最新深度，
        # 引擎停顿期的补拍（重复最近帧）路径不受影响
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass
        q.put_nowait(depth_frame)

    # ── 深度伪相机（D435 等原生深度设备） ──────────────

    def set_depth_camera(self, name: str, resolution: tuple,
                         fps: float, master_slot: str,
                         heatmap_near_mm: float = 0.0,
                         heatmap_far_mm: float = 0.0,
                         heatmap_smooth_k: int = 0,
                         heatmap_temporal_alpha: float = 0.0):
        """声明一个元数据深度相机（不注册外部队列、不建视频目录；多路并存）。

        名字含 "depth" 的相机会被 EgoDataWriter 自动跳过视频创建，
        但进入 metadata.json（type:depth/unit:mm）并启用 write_depth
        12-bit 灰度 MP4 落盘（gray12le 对数码，编码器不支持时回落
        FFV1 灰度 MKV 无损 uint16 毫米）。

        Args:
            name: 深度槽位名（如 "d435_depth"）
            resolution: (height, width)
            fps: 深度帧率
            master_slot: 深度帧随哪路主槽位同步落盘（如 "d435_rgb"）
            heatmap_near_mm: 热力图色标下限（毫米）；0 表示帧内自适应
            heatmap_far_mm: 热力图色标上限（毫米）；0 表示帧内自适应
                near/far 同给时是**真固定色标**（线性映射，帧间颜色
                完全一致），无效值/近端→JET(0) 深蓝、超远→红饱和
                （demo 口径，不置黑）；否则每帧重配（会闪）
            heatmap_smooth_k: 热力图中值滤波核（奇数，0 关闭）
            heatmap_temporal_alpha: 热力图时域 EMA 权重（0 关闭；
                仅影响可视化热力图流，无损深度流不受影响）
        """
        self._depth_cameras[name] = {
            "resolution": resolution,
            "fps": fps,
            "master_slot": master_slot,
            "heatmap_near_mm": heatmap_near_mm,
            "heatmap_far_mm": heatmap_far_mm,
            "heatmap_smooth_k": int(heatmap_smooth_k),
            "heatmap_temporal_alpha": float(heatmap_temporal_alpha),
        }
        self._depth_master_slot = master_slot

    def clear_depth_camera(self, name: str = None):
        """清除深度伪相机（name=None 清全部），S80M 默认主槽位兜底。"""
        if name is None:
            self._depth_cameras.clear()
        else:
            self._depth_cameras.pop(name, None)
        self._depth_master_slot = "stereo_left"

    def set_external_calibration(self, calib):
        """兼容旧调用：设置外部帧源标定（单设备场景 → head_stereo.json）。"""
        self._external_calibration = calib
        self._external_calibrations["_default"] = calib

    def set_device_calibration(self, device_key: str, calib):
        """按设备注册标定（多路场景）。录制开始时：
        首台双目型设备 → calibration/head_stereo.json（服务器/回放依赖），
        其余 → calibration/{slot前缀}_calibration.json。
        """
        self._external_calibrations[device_key] = calib

    def clear_device_calibration(self, device_key: str):
        """设备关闭时清除其标定，避免下个会话串台。"""
        self._external_calibrations.pop(device_key, None)

    # ── 设备注册 ──────────────────────────────────────

    def register_sensors(self, sensor_names: List[str]):
        """注册传感器设备名称。

        必须在 start_recording 之前调用。传感器名称决定 parquet 中
        observation.<name> 列名，如 "right_glove", "left_glove"。
        """
        self._sensor_names = list(sensor_names)

    def register_sensor(self, sensor_name: str):
        """增量注册单个传感器（手套开关 ON 时实时调用，下次录制生效）。"""
        if sensor_name not in self._sensor_names:
            self._sensor_names.append(sensor_name)

    def unregister_sensor(self, sensor_name: str):
        """注销单个传感器（手套开关 OFF）。"""
        if sensor_name in self._sensor_names:
            self._sensor_names.remove(sensor_name)

    def register_gripper_device_id(self, device_id: str):
        """UMI 夹爪非视频槽进 status 列（pose/force 槽，开关时实时调用）。"""
        self._gripper_device_ids.add(device_id)

    def unregister_gripper_device_id(self, device_id: str):
        """注销夹爪非视频槽设备 ID（开关 OFF）。"""
        self._gripper_device_ids.discard(device_id)

    # ── 录制控制 ──────────────────────────────────────

    def start_recording(self, slot_id: str, task_name: str = "",
                        batch_index: int = 0,
                        device_meta: Optional[List[dict]] = None):
        """全部摄像机开始录制到同一个 LeRobot 会话。

        Args:
            slot_id: 触发录制的摄像机槽位 ID
            task_name: 任务标注（如 "grasp_cup"），会嵌入目录名
            batch_index: 任务进度序号（录制完成次数 + 1），用于 episode
                目录序号命名（与本地文件删除无关）
            device_meta: 录制设备信息列表（写入 metadata.json / info.json
                devices 段）: [{"key","kind","name","serial","slots"}]
        """
        if self._recording or (not self._slots and not self._external_queues):
            return
        # 先设标志防重复触发，再后台初始化
        self._recording_slot = slot_id
        self._recording = True
        self._device_status.clear()          # 清空上一轮的状态
        self._start_time = time.monotonic()
        self._timer_running = True
        self._duration_timer.start()
        self._task_name = task_name
        self._batch_index = batch_index
        self._device_meta = list(device_meta) if device_meta else []
        threading.Thread(target=self._start_async, daemon=True).start()

    def _start_async(self):
        """后台线程：创建 EgoData episode 并启动独立写入线程。"""
        # 收集所有摄像机尺寸（含外部帧源如双目）
        cameras = {}
        camera_fps = {}      # per-camera FPS（外部源用原生帧率，其余用录制帧率）
        default_fps = float(settings.RECORDING_FPS)
        for sid, sl in self._slots.items():
            w, h = sl.camera.resolution     # resolution = (width, height)
            cameras[sid] = (h, w)            # EgoData: (height, width)
            camera_fps[sid] = default_fps
        for sid, (h, w) in self._external_dims.items():
            cameras[sid] = (h, w)
            camera_fps[sid] = self._external_fps.get(sid, default_fps)
        # 深度伪相机（D435 等）：只进 metadata，不建视频目录/MP4；多路并存
        for dname, dconf in self._depth_cameras.items():
            dh, dw = dconf["resolution"]
            cameras[dname] = (dh, dw)
            camera_fps[dname] = dconf["fps"]

        w = EgoDataWriter()
        # 探针日志在 start_episode 内 emit → connect 必须在调用之前
        w.log_occurred.connect(self.recording_log)
        all_device_ids = (list(self._slots.keys())
                          + list(self._sensor_names)
                          + sorted(self._gripper_device_ids))
        # 设备标定（多路）：set_device_calibration 注册的按 key 传入；
        # 旧 set_external_calibration 单值路径存 "_default" 键
        calibrations = dict(self._external_calibrations)
        legacy_calib = self._external_calibration if calibrations else None
        ok = w.start_episode(self._output_dir, cameras, default_fps,
                             sensors=self._sensor_names,
                             task_name=getattr(self, '_task_name', ''),
                             device_ids=all_device_ids,
                             calibration=legacy_calib,
                             camera_fps=camera_fps,
                             devices=getattr(self, '_device_meta', []),
                             calibrations=calibrations,
                             # 任务进度序号：按录制完成次数命名（与文件
                             # 删除无关）；0 时 writer 退化为目录扫描
                             batch_index=getattr(self, '_batch_index', 0),
                             # 与旧语义一致:显式注册的深度设备(如 D435/D405)
                             # 打开即录深度;settings.DEPTH_ENABLED 只门控
                             # S80M 视差路径(无注册槽的兜底单槽)
                             depth_enabled=bool(self._depth_cameras),
                             depth_heatmaps={
                                 dn: {"near_mm": dc["heatmap_near_mm"],
                                      "far_mm": dc["heatmap_far_mm"],
                                      "smooth_k": dc["heatmap_smooth_k"],
                                      "temporal_alpha": dc["heatmap_temporal_alpha"]}
                                 for dn, dc in self._depth_cameras.items()},
                             # 深度槽以显式注册为准（set_depth_camera），
                             # 不由槽名是否含 "depth" 猜测
                             depth_slots=list(self._depth_cameras.keys()))
        if not ok:
            self._recording = False
            self._timer_running = False
            self.error_occurred.emit("", "ffmpeg 启动失败")
            return

        self._writer = w
        self._session_path = w.task_dir          # v1.1.0：任务目录（池化布局根）
        self._last_episode_index = w.episode_index
        # v1.0.9 编码器名动态化（如 "HEVC (libx265)"）
        self._codec_name = f"{w.encoder_label} | EgoData + LeRobot v3 Parquet"
        # 丢帧统计/IMU 防丢：每 episode 重置
        self._drop_stats.clear()
        # v1.3.10 落盘侧空洞看门狗同样每段重置（连同时间基座，见 GapWatch）
        self._ext_gap_watches.clear()
        self._pending_imu.clear()
        with self._glove_imu_lock:
            self._glove_imu.clear()
        with self._glove_kpts_lock:
            self._glove_kpts.clear()
        self._imu_overflow_count = 0
        self._frame_count = 0
        self._per_cam_frame = {sid: 0 for sid in self._slots}
        self._depth_frame_idx = {}
        self._last_depth_frames = {}
        self._episode_start_s = time.time()

        # 清空录制前残留的旧帧和传感器数据
        for sid, sl in self._slots.items():
            while not sl.frame_queue.empty():
                try: sl.frame_queue.get_nowait()
                except queue.Empty: break
        while not self._sensor_queue.empty():
            try: self._sensor_queue.get_nowait()
            except queue.Empty: break
        # 外部帧源（夹爪 RGB 等）同一语义：生产线程从设备打开起就一直在投递，
        # 而写线程此刻才启动，中间还要等 start_episode（编码器探测/建目录）。
        # 不清就会把「录制开始前」的画面写成新 mp4 开头的那几十帧
        self._drain_external_queues()
        with self._gripper_snapshots_lock:
            self._gripper_snapshots.clear()
            self._gripper_traj.clear()
            self._gripper_traj_ns.clear()

        # B+ 方案: 启动独立写入线程（精确 30fps，完全不受主线程 UI 影响）
        self._write_thread = threading.Thread(
            target=self._write_loop, daemon=True, name="frame-writer"
        )
        self._write_thread.start()

        self.recording_started.emit(self._recording_slot)
        self.session_changed.emit(self)

    # ── v1.3.10 落盘侧帧空洞（写线程私有，无锁无 IO）──────────
    def _note_ext_gap(self, sid: str, hw_ns: int) -> int:
        """记一帧、返回本次空洞 ns（0 = 正常）。空洞闭合到告警门槛就打一行。

        时间基座由本段首帧锁定：``hw_ns > 0`` 用帧自带时刻（与 parquet 的
        hardware_ns 同源，口径与 tools/audit_frame_gaps.py 逐字一致）；
        ``hw_ns = 0``（无时间戳的槽位，如 lite 路径恒传 0）退化为出队瞬间的
        宿主单调钟。**两者绝不混用**——混源会把两个时钟的基准差当成一次天文
        数字级的空洞，这正是 GapWatch 内部拦掉的那种误判。

        标称间隔恒取 RECORDING_FPS 的倒数：写线程对每个外部槽位每 tick 只取
        一帧，输出行的间距就是 tick 的整数倍，与源的帧率无关。因此日志报的
        毫秒数与事后原样重算 parquet 得到的数字相同。
        """
        nominal_ns = int(1e9 / max(1.0, float(settings.RECORDING_FPS)))
        watch = self._ext_gap_watches.get(sid)
        if watch is None:
            watch = GapWatch(
                nominal_ns, settings.GRIPPER_RGB_GAP_MIN_MS * 1_000_000)
            self._ext_gap_watches[sid] = watch

        if hw_ns and hw_ns > 0:
            ts_ns, source = int(hw_ns), "hw"
        else:
            ts_ns, source = time.monotonic_ns(), "mono"
        gap_ns = watch.note(ts_ns, source)
        if gap_ns < settings.GRIPPER_RGB_GAP_ALERT_MS * 1_000_000:
            return gap_ns

        # 洞起点 = 本帧时刻 − 空洞 − 标称间隔；相对首帧即「开录后多久」
        snap = watch.snapshot()
        start_ns = ts_ns - gap_ns - nominal_ns
        rel_s = (start_ns - snap["first_ns"]) / 1e9 if snap["first_ns"] else 0.0
        self.recording_log.emit(
            f"[录制] RGB 帧空洞 {gap_ns / 1e6:.0f}ms（槽 {sid}，"
            f"开录后 {rel_s:.1f}s 起）——该段视频静止后跳变")
        return gap_ns

    def _inject_ext_gap_stats(self):
        """把落盘侧空洞并入本段 drop_stats（在 end_episode 之前调用）。

        键一律以 ``_ms`` / ``_count`` 结尾 ⇒ 被 ``is_frame_drop_key`` 排除在
        帧数口径之外（见 core/pipeline.py 顶部的键命名约定）。
        """
        for sid, watch in self._ext_gap_watches.items():
            snap = watch.snapshot()
            if not snap["frames"]:
                continue
            # 走 note_drop_stats：零值不落键、*_max_ms 走峰值的规则只此一份
            self.note_drop_stats({
                f"{sid}_gap_ms": int(round(snap["gap_ns"] / 1e6)),
                f"{sid}_gap_max_ms": int(round(snap["gap_max_ns"] / 1e6)),
                f"{sid}_gap_count": snap["gap_count"],
            })

    def note_drop_stats(self, stats: Dict[str, int]):
        """外部（UI/bridge）把采集侧的计数并入本段 drop_stats。

        ``*_max_ms`` 结尾的键是峰值（走 note_max），其余累加。零值不落键——
        与既有各计数器一致（只有出过事才会有键）。由 UI 在 finish_recording
        **之前**调用；落盘侧的空洞由 pipeline 自己注入，两边合起来才是
        「相机 → 视频文件」这条链的完整账。
        """
        for key, value in (stats or {}).items():
            value = int(value)
            if value == 0:
                continue
            if key.endswith("_max_ms"):
                self._drop_stats.note_max(key, value)
            else:
                self._drop_stats.inc(key, value)

    def _write_loop(self):
        """独立写入线程 —— 用 time.perf_counter() 精确按 30fps 写入帧。"""
        frame_interval = 1.0 / settings.RECORDING_FPS  # ~0.0333s
        next_write = time.perf_counter()

        while self._recording and self._writer is not None:
            now = time.perf_counter()

            # 精确等待到下一帧时刻
            sleep_for = next_write - now
            if sleep_for > 0.001:
                # sleep 到目标时间前 ~1ms，然后用 busy-wait 补齐精度
                time.sleep(sleep_for - 0.001)
                while time.perf_counter() < next_write:
                    pass
            elif sleep_for < -frame_interval:
                # 落后超过一帧（系统卡顿），重置时钟防止帧爆发
                next_write = now + frame_interval

            # 推进到下一帧时刻（用 += 防止累积误差）
            next_write += frame_interval

            try:
                # 每帧先排空传感器队列，取最新数据（所有相机共享）
                sensor_data: Dict[str, np.ndarray] = {}
                while True:
                    try:
                        s_name, s_data = self._sensor_queue.get_nowait()
                        sensor_data[s_name] = s_data
                    except queue.Empty:
                        break
                # 手套 IMU/骨架快照随本帧行写入（USB 手套；空 dict 无影响）
                glove_imu = self._pop_glove_imu()
                glove_kpts = self._pop_glove_keypoints()
                # 夹爪稀疏列快照（P3/P4；空 dict 无影响）
                gripper = self._pop_gripper_snapshots()

                # 处理 CameraSlot 队列（排空取最新帧）
                for sid, sl in self._slots.items():
                    try:
                        frame = sl.frame_queue.get_nowait()
                    except queue.Empty:
                        continue
                    while True:
                        try:
                            frame = sl.frame_queue.get_nowait()
                        except queue.Empty:
                            break

                    # 单目相机左右镜像翻转（录制时写入 MP4）
                    if getattr(settings, 'CAMERA_MIRROR_HORIZONTAL', False):
                        frame = np.flip(frame, axis=1)

                    self._write_one_frame(sid, frame, sensor_data,
                                          glove_imu=glove_imu,
                                          glove_kpts=glove_kpts,
                                          gripper=gripper)

                # 处理外部帧源队列（取一帧不排空，保证输出均匀无抖动）
                # 双目帧已在 _on_stereo_frame 完成垂直翻转，此处不再重复翻转
                for sid, q in self._external_queues.items():
                    try:
                        item = q.get_nowait()
                    except queue.Empty:
                        continue
                    if isinstance(item, tuple) and len(item) == 3:
                        frame, hw_ns, imu_s = item
                    else:  # 兼容旧格式（仅帧）
                        frame, hw_ns, imu_s = item, 0, None
                    # v1.3.10 落盘侧空洞计量。放在这里（取到帧之后、写之前）
                    # 而非生产者侧：只统计「确实进了队列、确实写进了视频」的
                    # 帧，队列满丢掉的帧已由 write_external_frame 的 ext: 计数
                    # 认领，两边不重复也不互相掩盖。
                    self._note_ext_gap(sid, hw_ns)
                    # IMU 样本只随主槽位写入（左右目共享同一份，
                    # 避免 data/imu/ 出现重复行；S80M/夹爪各自的主槽）
                    if sid not in self._imu_external_slots:
                        imu_s = None
                    else:
                        # v1.0.9：队列满时暂存的 IMU 批次随后续帧挂靠。
                        # 被丢帧的 IMU 晚于队内旧帧，只挂靠 ts ≤ 本帧 hw_ns
                        # 的样本（旧样本在前，跨行时序单调），其余留在缓冲
                        # 等更晚的帧；hw_ns=0 无时序信息则全量挂靠。
                        with self._pending_imu_lock:
                            pending = self._pending_imu.pop(sid, None)
                        if pending:
                            if hw_ns and hw_ns > 0:
                                due = [s for s in pending if s[0] <= hw_ns]
                                rest = [s for s in pending if s[0] > hw_ns]
                                if rest:
                                    with self._pending_imu_lock:
                                        self._pending_imu.setdefault(
                                            sid, []).extend(rest)
                                pending = due
                            if pending:
                                imu_s = list(pending) + list(imu_s or [])
                    self._write_one_frame(sid, frame, sensor_data,
                                          flip_vertical=False,
                                          hardware_ns=hw_ns,
                                          imu_samples=imu_s,
                                          glove_imu=glove_imu,
                                          glove_kpts=glove_kpts,
                                          gripper=gripper)

            except Exception:
                pass

    def _write_one_frame(self, sid: str, frame: np.ndarray,
                         sensor_data: Dict[str, np.ndarray],
                         flip_vertical: bool = True,
                         hardware_ns: int = 0,
                         imu_samples: Optional[List] = None,
                         glove_imu: Optional[Dict[str, tuple]] = None,
                         glove_kpts: Optional[Dict[str, np.ndarray]] = None,
                         gripper: Optional[Dict] = None):
        """写入单帧到 MP4 + Parquet。

        Args:
            flip_vertical: 是否在 write_video_frame 中上下翻转。
                           单目 CameraSlot 路径为 True（默认）；
                           双目外部帧路径为 False（已在 _on_stereo_frame 完成翻转）。
            hardware_ns: SDK 硬件纳秒时间戳（双目相机帧）
            imu_samples: 本帧窗口的 IMU 样本列表（双目，随 stereo_left 携带）
            glove_imu: {传感器名: (quats 64×f32, valid 16×f32)} USB 手套 IMU 快照
            glove_kpts: {传感器名: 63×f32 骨架关键点} USB 手套骨架快照
            gripper: UMI 夹爪稀疏列快照（P3/P4，见 write_gripper_*）
        """
        cam_frame_idx = self._per_cam_frame.get(sid, 0)
        rel_ts = time.time() - self._episode_start_s
        self._writer.write_video_frame(sid, frame, flip_vertical=flip_vertical)
        self._writer.write_frame_row(
            cam_frame_idx, rel_ts,
            sensors=sensor_data if sensor_data else None,
            connection_status=self._device_status,
            hardware_ns=hardware_ns,
            imu_samples=imu_samples,
            glove_imu=glove_imu,
            glove_kpts=glove_kpts,
            gripper=gripper,
        )
        self._per_cam_frame[sid] = cam_frame_idx + 1
        self._frame_count += 1

        # 深度帧：每路深度相机随自己的主槽位同步写入
        # （S80M: stereo_left；D435 第 n 台: d435_rgb[_n]）
        depth_targets = dict(self._depth_cameras)
        if not depth_targets:
            # S80M 传统路径：无深度伪相机注册，默认随 stereo_left 落盘
            depth_targets = {"stereo_left": {
                "master_slot": "stereo_left"}}
        for dname, dconf in depth_targets.items():
            if sid != dconf["master_slot"]:
                continue
            try:
                depth_frame = self._depth_queues.get(dname,
                                                     queue.Queue()).get_nowait()
                fresh = True
            except queue.Empty:
                # 深度引擎停顿期队列空（实测产出 ~27fps 突发，低于录制
                # 帧率，见 write_depth keep-latest）：重复最近帧补满节拍，
                # 深度视频帧 i 与 RGB 帧 i 严格对齐（12-bit MP4 与 FFV1
                # 回退同口径）；编码器对重复帧增量≈0
                depth_frame = self._last_depth_frames.get(dname)
                if depth_frame is None:
                    continue
                fresh = False
            if fresh:
                idx = self._depth_frame_idx.get(dname, 0)
                self._last_depth_frames[dname] = depth_frame
                self._depth_frame_idx[dname] = idx + 1
            else:
                idx = 0
            self._writer.write_depth_frame(idx, depth_frame, depth_slot=dname)

    def write_sensor(self, data, capture_ts_us: int = 0,
                     sensor_name: str = ""):
        """将传感器数据通过队列传给写入线程（线程安全）。

        Args:
            data: 16×16 float32 压力矩阵
            capture_ts_us: 保留参数（兼容），当前不使用
            sensor_name: 传感器设备名（如 "sensors_right"），
                         为空时默认用 _sensor_names 的第一个
        """
        if self._writer is not None and self._recording:
            if not sensor_name and self._sensor_names:
                sensor_name = self._sensor_names[0]
            if not sensor_name:
                return
            try:
                self._sensor_queue.put_nowait(
                    (sensor_name, data.astype(np.float32).ravel())
                )
            except queue.Full:
                self._drop_stats.inc("sensor_queue")  # 消费不及，丢弃旧数据

    def write_glove_imu(self, sensor_name: str, quats, valid):
        """USB 手套 IMU 四元数快照（主线程 → 写入线程，最新值覆盖）。

        Args:
            sensor_name: 传感器列名（如 "right_glove"）
            quats: 16×4 XYZW 四元数（64×float32 展平）
            valid: 16×bool 有效掩码（展平）
        """
        if self._writer is None or not self._recording or not sensor_name:
            return
        q = np.asarray(quats, dtype=np.float32).ravel()[:64]
        v = np.asarray(valid, dtype=np.float32).ravel()[:16]
        if q.size != 64 or v.size != 16:
            return
        with self._glove_imu_lock:
            self._glove_imu[sensor_name] = (q, v)

    def _pop_glove_imu(self) -> Dict[str, tuple]:
        """取走并清空本帧窗口内的手套 IMU 快照（写入线程调用）。"""
        with self._glove_imu_lock:
            snapshot = dict(self._glove_imu)
            self._glove_imu.clear()
        return snapshot

    def write_glove_keypoints(self, sensor_name: str, kpts):
        """USB 手套骨架关键点快照（主线程 → 写入线程，最新值覆盖）。

        Args:
            sensor_name: 传感器列名（如 "left_glove"）
            kpts: 21×3 关节坐标（米，float32，展平 63 维）
        """
        if self._writer is None or not self._recording or not sensor_name:
            return
        k = np.asarray(kpts, dtype=np.float32).ravel()[:63]
        if k.size != 63 or not np.isfinite(k).all():
            return
        with self._glove_kpts_lock:
            self._glove_kpts[sensor_name] = k

    # ── UMI 夹爪稀疏列快照（P3/P4；latest-wins，写入线程每帧取走）──
    def _put_gripper_snapshot(self, key: str, value):
        if self._writer is None or not self._recording:
            return
        with self._gripper_snapshots_lock:
            self._gripper_snapshots[key] = value

    def write_slam_trajectory(self, pose7, prefix: str = "", timestamp=None,
                              host_ns=None):
        """SLAM 轨迹点 [t,x,y,z,qx,qy,qz,qw] 累积（P3；桥接 pose_ready 回调）。

        prefix 为双夹爪命名空间：rig1 空串（旧键 slam_trajectory 不变），
        rig2 "gripper_2_" → gripper_2_slam_trajectory。

        只累积轨迹、不再落 slam_pose 快照列（2026-09-10 定案）：位姿流是
        20/20/60ms 突发而 parquet 行是 1/30s 均匀网格，定长 7 值列填不满，
        缺位姿的行会被填成 [0]*7 被下游当真实位姿读；轨迹点带真实时间戳、
        信息严格更多，是 SLAM 位姿的唯一落盘形态。轨迹不再单独落 txt
        侧车，全部信息都在一条 episode parquet 里。

        host_ns：该取样帧的宿主单调钟纳秒（native 侧 CLOCK_MONOTONIC，
        与 `hardware_ns` 同时基），逐点并行落成
        `{prefix}slam_trajectory_ns`。**t 与 host_ns 不同源**：t 是相机
        传感器钟，换算到宿主钟要一个只能拟合的偏移（实测三个反推估计
        散布 287607.586/.637/.690，跨度 ~104ms，已超一个帧间隔），所以
        没有 host_ns 就只能靠行号配 slam 点与视频帧。0/None = 无戳。
        """
        if pose7 is None:
            return
        values = [float(value) for value in pose7[:7]]
        if len(values) != 7 or not all(np.isfinite(values)):
            return
        if timestamp is not None:
            try:
                t = float(timestamp)
            except (TypeError, ValueError):
                return
            if not np.isfinite(t):
                return
            try:
                host_value = int(host_ns) if host_ns is not None else 0
            except (TypeError, ValueError, OverflowError):
                host_value = 0
            if host_value < 0:
                host_value = 0
            with self._gripper_snapshots_lock:
                if self._writer is None or not self._recording:
                    return
                key = f"{prefix}slam_trajectory"
                self._gripper_traj.setdefault(key, []).extend([t, *values])
                # 与点列表**锁步**：每点恒定追加一个值（无戳补 0），
                # 两列长度恒等、下标即配对。缺戳就跳过不追加会让两列
                # 错位且下游无从察觉。
                self._gripper_traj_ns.setdefault(key, []).append(host_value)

    def write_gripper_state(self, state3, prefix: str = ""):
        """夹爪状态快照 [open_pct, gripped, fz_mn]（P4；GripState 输出）。"""
        if state3 is None:
            return
        values = [float(value) for value in state3[:3]]
        if len(values) != 3 or not all(np.isfinite(values)):
            return
        self._put_gripper_snapshot(f"{prefix}gripper_state", values)

    def write_tactile_force(self, side: str, force3, prefix: str = "",
                            capture_ns: int = 0):
        """触觉力快照 [fx,fy,fz] mN（P2/P3；tactile_ready 回调）。

        capture_ns：该样本在桥接回调入口取的宿主单调钟纳秒（与
        `hardware_ns` 同一时基）。落成独立列
        `observation.{prefix}gripper_{side}_force_ns`。

        为什么要它：力走 latest-wins 单槽、RGB 走 FIFO 队头最旧帧，
        两条支路的队列滞留不同且在段内持续变化，所以**行号配对本身
        就带偏移**。记下各自真实采集时刻后，下游按时间重采样即可把
        偏移压到半个行周期（≈0.5 帧）以内，而不是听任行号对齐。
        0 表示该帧无时刻（旧调用方/无样本），下游按未知处理。
        """
        if force3 is None or side not in ("left", "right"):
            return
        values = [float(value) for value in force3[:3]]
        if len(values) != 3 or not all(np.isfinite(values)):
            return
        self._put_gripper_snapshot(
            f"{prefix}gripper_{side}_force", values)
        if capture_ns:
            self._put_gripper_snapshot(
                f"{prefix}gripper_{side}_force_ns", int(capture_ns))

    def write_tactile_force_matrix(self, side: str, encoded,
                                   prefix: str = "", capture_ns: int = 0):
        """触觉力矩阵快照（P4 泵线程预编码的行差分/原值列表）。

        capture_ns 同 `write_tactile_force`，落成
        `observation.{prefix}gripper_{side}_force_matrix_ns`。矩阵与
        3 向量力来自同一次回调但落盘时刻不同（矩阵走独立泵线程），
        所以两者**各记各的时刻**，不共用一列。
        """
        if encoded is None or side not in ("left", "right"):
            return
        self._put_gripper_snapshot(
            f"{prefix}gripper_{side}_force_matrix", encoded)
        if capture_ns:
            self._put_gripper_snapshot(
                f"{prefix}gripper_{side}_force_matrix_ns", int(capture_ns))

    def set_force_matrix_spec(self, prefix: str, spec: str) -> None:
        """登记本段录制的力矩阵规格（录制开始时调一次，须在 start_episode 后）。

        只有 writer 需要知道倍率——×10/×100/×1000 的落盘类型都是 int16，
        元素类型分辨不出倍率，倍率得随 info.json features.scale 落盘，
        训练侧才知道该 ÷N 还是 ÷M。
        """
        if self._writer is None:
            return
        for side in ("left", "right"):
            self._writer.set_force_matrix_spec(
                f"{prefix}gripper_{side}_force_matrix", spec)

    def _pop_gripper_snapshots(self) -> Dict[str, object]:
        """取走并清空本帧窗口内的夹爪快照（写入线程调用）。

        轨迹点在此并入：{prefix}slam_trajectory = 本窗口内全部点的
        扁平 8 值列表（无点则不写键）；其宿主时刻同序并行落
        {prefix}slam_trajectory_ns（int64 纳秒，逐点对齐）。
        """
        with self._gripper_snapshots_lock:
            snapshot = dict(self._gripper_snapshots)
            self._gripper_snapshots.clear()
            for key, points in self._gripper_traj.items():
                if points:
                    snapshot[key] = points
                    stamps = self._gripper_traj_ns.get(key) or []
                    # 长度必须是点数（点=8 值）。全 0 表示本段所有点都
                    # 没戳（旧 native 二进制不打印 Host 字段）——此时
                    # 不建列，免得下游拿到一列「看着有、其实全未知」的
                    # 时刻而误以为可对齐。
                    if len(stamps) == len(points) // 8 and any(stamps):
                        snapshot[f"{key}_ns"] = stamps
            self._gripper_traj.clear()
            self._gripper_traj_ns.clear()
        return snapshot

    def _pop_glove_keypoints(self) -> Dict[str, np.ndarray]:
        """取走并清空本帧窗口内的手套骨架快照（写入线程调用）。"""
        with self._glove_kpts_lock:
            snapshot = dict(self._glove_kpts)
            self._glove_kpts.clear()
        return snapshot

    @property
    def last_recording_frames(self) -> Dict[str, int]:
        """上一轮录制的每相机帧数（快照副本，用于录制完成回调的统计）。"""
        return dict(self._last_recording_frames)

    @property
    def last_drop_stats(self) -> Dict[str, int]:
        """上一轮录制的丢帧统计（快照副本；含 imu_overflow 键）。"""
        return dict(self._last_drop_stats)

    @property
    def last_episode_index(self) -> int:
        """上一轮录制的全局 episode 序号（v1.1.0 池化布局；1 起）。"""
        return self._last_episode_index

    def finish_recording(self, slot_id: str):
        """正常停止——最终化共享会话。"""
        if not self._recording:
            return
        self._recording = False
        self._timer_running = False
        self._duration_timer.stop()

        # v1.3.11（L0-1）：收尾窗口计时。这一段（join + 排空 + 快照）与
        # 后台的 flush/parquet 合起来就是「点停止 → 落盘完成」的全长；
        # 2026-09-18 的断链与位姿发散都发生在这个窗口里，先量再修
        sw = Stopwatch()

        # 等待写入线程退出
        if self._write_thread is not None and self._write_thread.is_alive():
            self._write_thread.join(timeout=3.0)
            self._write_thread = None

        # 写线程已停，残留帧不会再被消费——立即释放（满队列 ~110MB/槽）。
        # 生产线程可能在 _recording=False 与本次排空之间挤进最后一帧，
        # 但下一段 _start_async 还会再排空一次，不会漏进视频
        self._drain_external_queues()

        # 快照每相机帧数：_finish_async 在后台线程 emit 回调，
        # 用户可能抢在回调前开始新一轮录制重置 _per_cam_frame
        self._last_recording_frames = dict(self._per_cam_frame)

        # v1.0.9 丢帧统计快照（含 IMU 溢出次数）：供录制完成回调提示与
        # writer 元数据回写；在 _finish_async（end_episode）之前注入
        # v1.3.10：写线程已 join，此处读 _ext_gap_watches 无竞态
        self._inject_ext_gap_stats()
        self._last_drop_stats = self._drop_stats.snapshot()
        self._last_drop_stats["imu_overflow"] = self._imu_overflow_count
        if self._writer is not None:
            self._writer.set_drop_stats(self._last_drop_stats)

        sw.lap("join")
        path = self._session_path
        threading.Thread(target=self._finish_async, args=(path, sw),
                         daemon=True).start()

    def _finish_async(self, path: str, sw: Optional[Stopwatch] = None):
        writer = self._writer
        if writer:
            writer.end_episode()
            if sw is not None:
                # 一行给出整条收尾路径的分段耗时（flush = 编码器收尾，
                # parquet = 力矩阵列建数组那段，meta = 元数据/统计合并）。
                # getattr：测试里的假 writer 没有这个属性，不能让它把
                # 收尾线程带下去（daemon 线程静默死掉＝数据不落盘）
                for stage, ms in (getattr(writer, "finalize_timing", None)
                                  or {}).items():
                    sw.add(stage, ms)
                self.recording_log.emit(f"[录制] 收尾 {sw.format()}")
            self._writer = None
        self._session_path = None
        self.session_changed.emit(None)
        if path:
            self.recording_finished.emit(self._recording_slot or "", path)

    def abort_recording(self, slot_id: str):
        """异常停止——丢弃共享会话。"""
        if not self._recording:
            return
        self._recording = False
        self._timer_running = False
        self._duration_timer.stop()

        # 等待写入线程退出
        if self._write_thread is not None and self._write_thread.is_alive():
            self._write_thread.join(timeout=3.0)
            self._write_thread = None

        self._drain_external_queues()

        threading.Thread(target=self._abort_async, daemon=True).start()

    def _abort_async(self):
        writer = self._writer
        if writer:
            writer.abort_episode()
            # 中止也要有耗时读数：与「完成」路径的 flush/parquet 对比，
            # 是判断「断链到底由哪一段引起」的那条判据（2026-09-18：中止
            # 同样收尾 ffmpeg 却 0/6 断链）
            timing = getattr(writer, "finalize_timing", None) or {}
            if timing:
                self.recording_log.emit(
                    "[录制] 收尾(中止) " +
                    " ".join(f"{k}={v}ms" for k, v in timing.items()))
            self._writer = None
        self._session_path = None
        self.session_changed.emit(None)
        self.recording_aborted.emit(self._recording_slot or "")

    @property
    def elapsed(self) -> float:
        if not self._recording:
            return 0.0
        return time.monotonic() - self._start_time

    def _tick_duration(self):
        """QTimer 回调 —— 主线程执行，无跨线程问题。"""
        if self._recording:
            self.duration_changed.emit(self._recording_slot or "", self.elapsed)
